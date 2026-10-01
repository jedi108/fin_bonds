"""
Use case: rebalance-report — ядро Python-тулкита ребалансировки (задача 005.1).

Одна команда, отдающая агенту канонический JSON (schema_version=2: v2/022
добавила cash канонического снапшота в блок portfolio) с блоками
meta / portfolio / concentrations / screener / redemptions_6m / artifacts —
без env-переменных, psql и ad-hoc SQL в командной строке (анти-P-A).
DSN читается слоем хранения из .env, SQL живёт только в src/storage.py.

Источник аналитики — существующий движок: YTM считается вызовом метода
CalculateYtmUseCase._enrich_bond_with_ytm через import (не subprocess и не
повторная реализация); гейт свежести — classify_freshness из check_db
(тот же контракт WARN vs CRITICAL по P1/002).

Анти-ловушки:
- P-D: параметры команды принимают ТОЛЬКО ISIN; человекочитаемые имена,
  тикеры и эмитенты тулкит проставляет сам из каталога в каждой строке.
- P-E: YTM покидает код только в одном формате — проценты (ytm_pct),
  из единственного расчётного движка; bonds_catalog.ytm с миграции 013
  (005.4) хранится в тех же процентах — смешение источников безопасно.
- P-C/P-G/анти-P2 (002): фильтры частоты, исключение суверенных,
  бейдж held (доля N%) для уже держимых бумаг.
- 023: частота купонов — диапазоном --freq-min/--freq-max («от квартальных
  до ежемесячных» = 4..12); кредитный рейтинг кандидата — настоящий
  (latest rating_history по ISIN, normalize_rating_code/rating_code_to_score),
  строгий порог --min-credit-rating: без рейтинга и ниже порога — мимо.
  TBank risk_level рейтингом не подменяется (остаётся отдельным фильтром).

Сценарный режим --scenario (005.2) — перенос scenario_summary.sql
(артефакт A2 из 002.4) и simulate_swap.sql (001) в Python: trades
(isin, qty_delta) -> целевой портфель, дельты купонного потока ₽/мес и
₽/год, лимиты эмитента 15% по каждой ноге и по сумме всех ног (анти-P6:
агент не суммирует доли вручную), частотный микс «до/после», явные ошибки
сценария (unknown ISIN, отрицательный остаток) вместо падения.

024 — budget и финальный валидатор формальных ограничений:
- budget: cash канонического снапшота (022) + продажи/покупки ног дают
  cash_after_estimate; unknown cash остаётся null (не 0); оценка —
  qty * eff_price, это НЕ settlement с НКД и комиссиями (потому estimate);
- structural validity (scenario.valid: UNKNOWN_ISIN / NEGATIVE_QTY_AFTER /
  NO_COMPANY_LINK) отделена от investment feasibility (scenario.feasible):
  нарушение формального ограничения не удаляет summary/positions_after;
- constraint_checks проверяет ВЕСЬ целевой портфель по canonical args
  (freq_min/freq_max/freq_in, exclude_sovereign, min_credit_rating,
  max_position_value, лимит эмитента 15% — переиспользуется
  scenario.issuer_limits), а не только покупки.

025 — canonical bond row дополняется share_pct (аддитивно, версия контракта
не меняется — та же policy, что в 023 для credit_rating): CSV различает
«Доля выпуска, %» (share_pct) и «Доля эмитента, %» (issuer_pct) — раньше
под «Доля, %» уходила агрегированная доля эмитента (подтверждённый баг).

030.2 — сценарная математика/валидация (--scenario) вынесена в canonical
engine `src/services/rebalance_engine.py::RebalanceEngine` (правило 030:
один расчётчик — никакого второго калькулятора рядом): use case читает
scenario.json, собирает ScenarioConstraints из canonical args и зовёт
`evaluate_scenario()` одним вызовом; JSON-контракт блока scenario не меняется
(ключ `file` добавляет use case, engine-ключ planning_prices в JSON не
публикуется). Общая арифметика (issuer_shares, value_mix, bond_to_json,
YTM-enrichment) живёт тоже в engine — отсюда она импортируется, копий нет;
контрактные константы scenario/constraints реэкспортируются для совместимости
импортов (тесты, exporter).
"""
import argparse
import calendar
import csv
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from src.services.rebalance_engine import (
    BOND_ROW_KEYS,
    CONSTRAINT_COUPON_FREQUENCY_ABOVE_MAX,
    CONSTRAINT_COUPON_FREQUENCY_BELOW_MIN,
    CONSTRAINT_COUPON_FREQUENCY_MISSING,
    CONSTRAINT_COUPON_FREQUENCY_NOT_IN_LIST,
    CONSTRAINT_CREDIT_RATING_BELOW_MIN,
    CONSTRAINT_CREDIT_RATING_MISSING,
    CONSTRAINT_INSUFFICIENT_CASH_ESTIMATE,
    CONSTRAINT_ISSUER_LIMIT,
    CONSTRAINT_POSITION_VALUE_LIMIT,
    CONSTRAINT_SOVEREIGN_NOT_ALLOWED,
    ISSUER_CONCENTRATION_LIMIT_PCT,
    RebalanceEngine,
    SCENARIO_ERROR_NEGATIVE_QTY_AFTER,
    SCENARIO_ERROR_NO_COMPANY_LINK,
    SCENARIO_ERROR_UNKNOWN_ISIN,
    SCENARIO_LEG_KEYS,
    SCENARIO_LIMIT_STATUS_PASS,
    SCENARIO_LIMIT_STATUS_VIOLATION,
    SCENARIO_POSITION_KEYS,
    SCENARIO_SUMMARY_KEYS,
    SCENARIO_TRADE_KEYS,
    ScenarioConstraints,
    _to_float,
    bond_to_json,
    issuer_shares,
    value_mix,
)
from src.storage import PortfolioStorage
from src.use_cases.base import UseCase
from src.use_cases.bond_filters import (
    POOL_MAX_LISTLEVEL,
    POOL_MAX_RISK,
    parse_freq_list,
    screener_policy_rejection,
    screener_ytm_rejection,
)
from src.use_cases.check_db import classify_freshness
from src.utils import rating_code_to_score

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

# Версия контракта JSON-ответа: скилл проверяет совместимость формата.
# 2 (задача 022): portfolio дополняется cash-полями канонического снапшота
# (securities_value_rub, cash_available_rub, investable_total_rub,
# cash_updated_at); total_value и арифметика долей не меняются.
# 025 оставляет версию 2: share_pct в canonical bond row — аддитивное
# изменение (та же policy, что в 023 для credit_rating).
SCHEMA_VERSION = 2

# Сколько эмитентов показывать в concentrations.issuers.top.
ISSUER_TOP_SIZE = 5

# Широкая выборка кандидатов скринера: фильтры риска/листинга/YTM применяются
# в use-case'е с подсчётом «сколько отсечено и почему», поэтому SQL-пороги
# открываются на максимум шкал (риск 1-5, листинг 1-3).
# 030.6: canonical значения живут в единственной таблице structural buy gates
# (src/use_cases/bond_filters.py — тот же источник, что SQL buy-выборки и
# eligibility-гейта планирования); прежние имена сохранены реэкспортом для
# существующих импортов (planning_context).
_SCREENER_MAX_RISK = POOL_MAX_RISK
_SCREENER_MAX_LISTLEVEL = POOL_MAX_LISTLEVEL
_SCREENER_POOL_LIMIT = 10000

# Заголовки CSV целевого портфеля (UTF-8 BOM, русские заголовки, строка ИТОГО).
# 025: доля выпуска и доля эмитента — разные колонки (Доля выпуска = share_pct,
# Доля эмитента = issuer_pct; раньше под «Доля, %» шёл issuer_pct).
# Ключи canonical bond row (BOND_ROW_KEYS) и контрактные константы блока
# scenario (SCENARIO_*/CONSTRAINT_*) с 030.2 живут в rebalance_engine и
# реэкспортируются выше — единственная копия определения.
CSV_HEADERS = [
    'ISIN', 'Название', 'Тикер', 'Эмитент', 'Кол-во', 'Цена',
    'Стоимость, ₽', 'Доля выпуска, %', 'Доля эмитента, %', 'YTM, %',
    'Купон, %', 'Частота купонов', 'Тип купона', 'Риск', 'Листинг',
    'Погашение', 'Оферта', 'Бейдж',
]


def _parse_freq_in(raw: str) -> List[int]:
    """Разбирает '--freq-in 2,4' в список целых частот (для argparse type=).

    Общая реализация с calculate-ytm — см. src/use_cases/bond_filters.py
    (задача 005.3: конвенции фильтров команд совпадают).
    """
    return parse_freq_list(raw, flag='--freq-in')


def _parse_iso_date(raw: str) -> str:
    """Валидирует дату 'YYYY-MM-DD' (для argparse type=), возвращает как есть."""
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Ожидалась дата в формате YYYY-MM-DD, получено {raw!r}"
        )
    return raw


# ---------------------------------------------------------------------------
# 029-T06: общий CLI-контракт candidate/screener фильтров.
#
# _SCREENER_FILTER_SPECS — единственный источник parsing semantics и canonical
# default values этих 12 фильтров: его использует и RebalanceReportUseCase
# (setup_parser/default_args), и sync-chatgpt-portfolio (без materialization
# defaults, см. add_screener_filter_arguments). Ручных копий default
# max-risk/max-listlevel/max-ytm/screener-limit в коде быть не должно.
# ---------------------------------------------------------------------------
_SCREENER_FILTER_SPECS = (
    ('--freq-min', dict(
        type=int, default=None,
        help='Минимальная частота купонов в год (023): «от квартальных '
             'до ежемесячных» = --freq-min 4 --freq-max 12. NULL/0-частота '
             'строгий диапазон не проходит; взаимоисключимо с --freq-in.'
    )),
    ('--freq-max', dict(
        type=int, default=None,
        help='Максимальная частота купонов в год (напр. 4 = не чаще '
             'квартальных; P-C). С --freq-min задаёт диапазон; '
             'взаимоисключимо с --freq-in.'
    )),
    ('--freq-in', dict(
        type=_parse_freq_in, default=None,
        help='Только указанные частоты купонов через запятую (напр. 2,4; P-C). '
             'Взаимоисключимо с --freq-min/--freq-max.'
    )),
    ('--min-credit-rating', dict(
        type=str, default=None,
        help='Минимальный кредитный рейтинг кандидата (023), напр. AA-: '
             'строгий порог по latest rating_history — без рейтинга или '
             'ниже порога кандидат исключается. Код нормализуется '
             'существующим rating_code_to_score (AAA(RU)/ruAAA = AAA); '
             'risk_level рейтингом не подменяется.'
    )),
    ('--exclude-sovereign', dict(
        action='store_true',
        help='Исключить суверенных эмитентов: ОФЗ и евро-РФ (P-G).'
    )),
    ('--include-ku', dict(
        action='store_true',
        help='Включить бумаги для квалифицированных инвесторов '
             '(по умолчанию исключены).'
    )),
    ('--max-risk', dict(
        type=int, default=1,
        help='Максимальный уровень риска Т-Банка кандидатов (1-5). Default: 1.'
    )),
    ('--max-listlevel', dict(
        type=int, choices=[1, 2, 3], default=2,
        help='Максимальный уровень листинга MOEX кандидатов. Default: 2.'
    )),
    ('--max-ytm', dict(
        type=float, default=35.0,
        help='Потолок YTM кандидатов, %% годовых (аномалия = ВДО/дистресс). Default: 35.0.'
    )),
    ('--min-maturity', dict(
        type=_parse_iso_date, default=None,
        help='Минимальная дата погашения кандидатов, YYYY-MM-DD.'
    )),
    ('--max-maturity', dict(
        type=_parse_iso_date, default=None,
        help='Максимальная дата погашения кандидатов, YYYY-MM-DD.'
    )),
    ('--screener-limit', dict(
        type=int, default=10,
        help='Размер топов fixed[]/floater[] в скринере. Default: 10.'
    )),
)

# dest-имена тех же 12 фильтров — выводятся из таблицы определений (второго
# списка имён не появляется; используется exporter'ом для переноса explicit
# overrides в канонические args отчёта).
SCREENER_FILTER_DESTS = tuple(
    flag.lstrip('-').replace('-', '_') for flag, _kwargs in _SCREENER_FILTER_SPECS
)


def add_screener_filter_arguments(
    parser: argparse.ArgumentParser, *, defaults: bool = True
) -> None:
    """Регистрирует 12 candidate/screener фильтров (029-T06) — публичный
    переиспользуемый helper, единственный источник parsing semantics.

    defaults=True (rebalance-report): canonical default values materialize
    в Namespace — тот же parser contract питает и default_args().
    defaults=False (sync-chatgpt-portfolio): default=argparse.SUPPRESS —
    в sync Namespace попадают только явно переданные флаги, второй набор
    defaults не materialize; explicit override определяется по факту
    присутствия атрибута (hasattr), а не сравнением со скопированным default.
    """
    for flag, kwargs in _SCREENER_FILTER_SPECS:
        if defaults:
            parser.add_argument(flag, **kwargs)
        else:
            parser.add_argument(flag, **{**kwargs, 'default': argparse.SUPPRESS})


def _add_months(day: date, months: int) -> date:
    """Дата + months месяцев (календарно, как make_interval в PostgreSQL)."""
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


class RebalanceReportUseCase(UseCase):
    """
    Сценарий: канонический JSON-отчёт для ребалансировки одной командой.

    Блоки ответа: meta (свежесть/гейт), portfolio (позиции + оценка),
    concentrations (эмитенты/риск/частота), screener (fixed/floater топы
    с фильтрами и подсчётом отсечённых), redemptions_6m (пул реинвеста),
    artifacts (CSV целевого портфеля); в сценарном режиме --scenario —
    ещё блок scenario (trades -> целевой портфель, дельты купонного
    потока, лимиты эмитента по сумме ног, 005.2).
    """

    def __init__(self, db: PortfolioStorage, rating_scale: Optional[Dict[str, int]] = None):
        self.db = db
        # 030.2: расчёт целевого портфеля/валидация — в canonical engine
        # (правило 030: один расчётчик); use case остаётся точкой сборки
        # отчёта и CLI-контракта. Единый расчётный движок YTM внутри engine
        # (требование 005.1: единицы YTM — только из одного источника).
        self._engine = RebalanceEngine(db, rating_scale=rating_scale)
        # Шкала рейтингов из конфига (023): порог --min-credit-rating и
        # сравнение идут через существующий rating_code_to_score — второй
        # нормализации рейтингов нет. Пустая шкала валидна, пока порог
        # не задан (проверяется в _validate_args). Движку передаётся та же
        # шкала (валидатор constraint_checks живёт там).
        self.rating_scale = rating_scale or {}

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        # 029-T06: 12 candidate/screener фильтров — общий helper с canonical
        # defaults (единственный источник argument definitions + default values
        # и для CLI, и для default_args(), и для exporter'а).
        add_screener_filter_arguments(parser, defaults=True)
        parser.add_argument(
            '--format', choices=['json'], default='json',
            help='Формат ответа: json — канонический машинный формат для агента.'
        )
        parser.add_argument(
            '--include-held', action='store_true',
            help='Показывать в скринере бумаги, уже лежащие в портфеле, '
                 'с бейджем held (доля N%%); по умолчанию они исключены (анти-P2 из 002).'
        )
        parser.add_argument(
            '--max-position-value', type=float, default=None,
            help='Формальное ограничение (024): максимальная стоимость одного '
                 'выпуска в целевом портфеле, ₽ (напр. 150000). Проверяется '
                 'каждая позиция positions_after, включая уже держимые без '
                 'сделок; нарушение — constraint_checks POSITION_VALUE_LIMIT.'
        )
        parser.add_argument(
            '--redemptions-months', type=int, default=6,
            help='Горизонт календаря погашений/оферт в месяцах. Default: 6.'
        )
        parser.add_argument(
            '--csv-out', type=str, default=None,
            help='Путь к CSV целевого портфеля (UTF-8 BOM, русские заголовки, '
                 'строка ИТОГО). Без флага artifacts.target_portfolio_csv = null.'
        )
        parser.add_argument(
            '--scenario', type=str, default=None,
            help='Путь к JSON-файлу сценария вида '
                 '{"trades": [{"isin": "...", "qty_delta": -530}, ...]}: '
                 'продажа — отрицательный qty_delta, покупка — положительный '
                 '(штуки). Добавляет блок scenario в ответ; при --csv-out CSV '
                 'содержит целевой портфель ПОСЛЕ сделок (005.2).'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'RebalanceReportUseCase':
        """Создает use case с хранилищем из фабрики (DSN — из .env, не из CLI).

        Шкала рейтингов берётся из конфига (023): порог --min-credit-rating
        переводится в балл существующим rating_code_to_score.
        """
        return cls(
            db=factory.get_db_connection(),
            rating_scale=factory.config.get('rating_scale', {}),
        )

    @classmethod
    def default_args(cls) -> argparse.Namespace:
        """Канонические аргументы по умолчанию для программного вызова (019).

        Единственный источник значений — setup_parser: пустой argv парсится тем
        же парсером, что и CLI, поэтому программные потребители (экспортёр
        ChatGPT) не дублируют defaults. Канонический сценарий (019):

            args = RebalanceReportUseCase.default_args()
            args.include_held = True
            report = RebalanceReportUseCase(db).build_report(args)
        """
        parser = argparse.ArgumentParser(add_help=False)
        cls.setup_parser(parser)
        return parser.parse_args([])

    # ------------------------------------------------------------------
    # Точка входа
    # ------------------------------------------------------------------

    def execute(self, args: argparse.Namespace):
        logger.info("🚀 Формирование rebalance-report (schema_version=%s)", SCHEMA_VERSION)
        # 029-T05: canonical validation — внутри build_report (единственная
        # точка для CLI и программных потребителей, двойной validation нет).
        report = self.build_report(args)

        # JSON — в stdout; логи (logging) идут в stderr и ответ не портят.
        print(json.dumps(report, ensure_ascii=False, indent=2))
        logger.info("Rebalance-report сформирован.")

    def _validate_args(self, args: argparse.Namespace):
        if args.freq_max is not None and args.freq_in is not None:
            raise ValueError("--freq-max и --freq-in взаимоисключающие: "
                             "выберите один способ задания частот")
        if args.freq_min is not None and args.freq_in is not None:
            raise ValueError("--freq-min и --freq-in взаимоисключающие: "
                             "выберите один способ задания частот")
        if args.freq_max is not None and args.freq_max < 1:
            raise ValueError("--freq-max должен быть целым >= 1")
        if args.freq_min is not None and args.freq_min < 1:
            raise ValueError("--freq-min должен быть целым >= 1")
        if (args.freq_min is not None and args.freq_max is not None
                and args.freq_min > args.freq_max):
            raise ValueError("--freq-min не должен превышать --freq-max")
        if args.min_credit_rating is not None:
            # 023: граница Python/LLM — Python получает конкретный порог и
            # переводит его в балл существующей шкалой; «высокий рейтинг»
            # сюда не доходит. Неизвестный код — явная ошибка CLI.
            threshold = rating_code_to_score(args.min_credit_rating, self.rating_scale)
            if threshold is None:
                raise ValueError(
                    f"--min-credit-rating: код {args.min_credit_rating!r} не входит "
                    "в шкалу rating_scale (config.yaml)"
                )
        if args.redemptions_months < 1:
            raise ValueError("--redemptions-months должен быть >= 1")
        if args.max_position_value is not None and args.max_position_value <= 0:
            raise ValueError("--max-position-value должен быть > 0")
        if args.screener_limit < 1:
            raise ValueError("--screener-limit должен быть >= 1")
        if args.max_ytm <= 0:
            raise ValueError("--max-ytm должен быть > 0")

    # ------------------------------------------------------------------
    # Сборка отчёта
    # ------------------------------------------------------------------

    def build_report(self, args: argparse.Namespace) -> Dict[str, Any]:
        """Собирает канонический отчёт — программный API (019).

        029-T05: canonical validation (_validate_args) выполняется здесь —
        programmatic consumers (экспортёр ChatGPT) не могут обойти её и
        построить отчёт на невалидной комбинации фильтров. CLI идёт через
        тот же метод из execute(), повторной validation нет (проверка
        чистая — повторный вызов идемпотентен). Возвращает тот же dict,
        что execute() печатает как JSON: второго аналитического контура
        не появляется.
        """
        self._validate_args(args)
        now = datetime.now(timezone.utc)
        positions, total_value = self._build_portfolio()
        concentrations = self._build_concentrations(positions, total_value)
        screener = self._build_screener(args, positions, total_value)
        redemptions = self._build_redemptions(args.redemptions_months)

        # Сценарный режим (005.2): блок scenario аддитивный — контракт ядра
        # (ответ без --scenario) не меняется. CSV в сценарии — целевой
        # портфель ПОСЛЕ сделок; при ошибках сценария он не пишется
        # (блоки «после» валидны только при пустом блоке ошибок, как в A2).
        scenario: Optional[Dict[str, Any]] = None
        csv_positions, csv_total = positions, total_value
        scenario_path = getattr(args, 'scenario', None)
        if scenario_path:
            scenario = self._build_scenario(args)
            if scenario['valid']:
                csv_positions = scenario['positions_after']
                csv_total = scenario['summary']['portfolio_after']
            else:
                logger.warning(
                    "Сценарий содержит ошибки (%s) — CSV целевого портфеля "
                    "не формируется.", ", ".join(e['type'] for e in scenario['errors'])
                )

        artifacts: Dict[str, Any] = {'target_portfolio_csv': None}
        if args.csv_out and (scenario is None or scenario['valid']):
            artifacts['target_portfolio_csv'] = self._write_target_csv(
                args.csv_out, csv_positions, csv_total
            )

        report: Dict[str, Any] = {
            'schema_version': SCHEMA_VERSION,
            'meta': self._build_meta(now),
            'portfolio': self._build_portfolio_block(total_value, positions),
            'concentrations': concentrations,
            'screener': screener,
            'redemptions_6m': redemptions,
        }
        if scenario is not None:
            report['scenario'] = scenario
        report['artifacts'] = artifacts
        return report

    def _build_meta(self, now: datetime) -> Dict[str, Any]:
        """meta: свежесть данных по контракту гейта 001/002 (WARN vs CRITICAL по P1/002)."""
        freshness = self.db.get_db_freshness()
        criticals, warnings, gate_status, is_fresh = classify_freshness(freshness, now)
        return {
            'generated_at': now.isoformat(),
            'catalog_updated_at': self._fmt_dt(freshness.get('bonds_catalog_max_updated_at')),
            'positions_updated_at': self._fmt_dt(freshness.get('portfolio_max_updated_at')),
            'market_prices_updated_at': self._fmt_dt(freshness.get('market_price_max_updated_at')),
            'status': "CRITICAL" if criticals else ("WARNING" if warnings else "OK"),
            'gate_status': gate_status,
            'is_fresh': is_fresh,
            'criticals': criticals,
            'warnings': warnings,
        }

    @staticmethod
    def _fmt_dt(value: Any) -> Optional[str]:
        """datetime -> ISO-строка с таймзоной (строки каталога обновлений)."""
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        return str(value)

    # ------------------------------------------------------------------
    # portfolio
    # ------------------------------------------------------------------

    def _build_portfolio_block(
        self, total_value: float, positions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Блок portfolio: оценка облигаций + cash канонического снапшота (022).

        - total_value / securities_value_rub — стоимость облигаций (семантика
          total_value не меняется, доли и концентрации считаются от неё же);
        - cash_available_rub — свободный RUB из PostgreSQL (портфель cash-
          балансов, обновляемый sync-portfolio; broker API из отчёта не
          вызывается);
        - investable_total_rub = securities_value_rub + cash_available_rub;
        - unknown cash (sync ещё не выполнялся): cash_available_rub и
          investable_total_rub = null — NULL не трактуется как 0;
        - cash_updated_at — метка последнего успешного cash sync (по ней
          потребитель видит устаревший cash после API failure).
        """
        cash = self.db.get_available_cash_rub()
        cash_available_rub = (
            round(_to_float(cash['cash_available_rub']), 2) if cash['cash_known'] else None
        )
        investable_total_rub = (
            round(total_value + cash_available_rub, 2)
            if cash_available_rub is not None
            else None
        )
        return {
            'total_value': total_value,
            'securities_value_rub': total_value,
            'cash_available_rub': cash_available_rub,
            'investable_total_rub': investable_total_rub,
            'cash_updated_at': self._fmt_dt(cash['cash_updated_at']),
            'positions': positions,
        }

    def _build_portfolio(self) -> Tuple[List[Dict[str, Any]], float]:
        """Позиции портфеля по ISIN с YTM от расчётного движка + оценка портфеля."""
        rows = self.db.get_rebalance_portfolio_rows()
        rows = self._enrich_with_ytm(rows)

        total_value = round(sum(_to_float(r.get('value_rub')) or 0.0 for r in rows), 2)
        issuer_shares_map = issuer_shares(rows, total_value)

        positions = [
            bond_to_json(row, total_value, issuer_shares_map, held_default=True)
            for row in rows
        ]
        return positions, total_value

    def _enrich_with_ytm(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Каноническая YTM в процентах — единый путь через canonical engine (030.2)."""
        return self._engine.enrich_with_ytm(rows)

    # ------------------------------------------------------------------
    # concentrations
    # ------------------------------------------------------------------

    def _build_concentrations(
        self, positions: List[Dict[str, Any]], total_value: float
    ) -> Dict[str, Any]:
        """Концентрации: эмитенты (top + нарушения >15%), риск-микс, частотный микс."""
        issuer_totals: Dict[str, float] = {}
        for pos in positions:
            issuer_totals[pos['issuer']] = issuer_totals.get(pos['issuer'], 0.0) + pos['value']

        def pct(value: float) -> float:
            return round(value / total_value * 100, 2) if total_value > 0 else 0.0

        issuer_items = [
            {'issuer': issuer, 'value': round(value, 2), 'pct': pct(value)}
            for issuer, value in issuer_totals.items()
        ]
        issuer_items.sort(key=lambda item: (-item['value'], item['issuer']))

        return {
            'issuers': {
                'limit_pct': ISSUER_CONCENTRATION_LIMIT_PCT,
                'top': issuer_items[:ISSUER_TOP_SIZE],
                'violations': [
                    item for item in issuer_items
                    if item['pct'] > ISSUER_CONCENTRATION_LIMIT_PCT
                ],
            },
            'risk_mix': value_mix(
                [(pos['risk_level'], pos['value']) for pos in positions],
                total_value, 'risk_level',
            ),
            'freq_mix': value_mix(
                [(pos['freq'], pos['value']) for pos in positions],
                total_value, 'freq',
            ),
        }

    # ------------------------------------------------------------------
    # screener
    # ------------------------------------------------------------------

    def _build_screener(
        self,
        args: argparse.Namespace,
        positions: List[Dict[str, Any]],
        total_value: float,
    ) -> Dict[str, Any]:
        """Топы fixed/floater с фильтрами и подсчётом «сколько отсечено и почему».

        Базовая выборка берётся у движка calculate-ytm (buy-режим) с открытыми
        порогами риска/листинга/YTM и include_held=True; все продуктовые
        фильтры применяются здесь по этапам — каждый отсчёт честный и видимый
        в excluded. Имена/эмитенты/флаги приходят из каталога (анти-P-D).
        Свежесть цены кандидата гарантирована buy-выборкой хранилища (018):
        без реальной market_price не старше 24 часов строка не доходит сюда.

        030.6: построчная policy-политика — общая функция
        screener_policy_rejection (bond_filters): её же вызывает
        eligibility-гейт планирования; у отчёта и planner'а одна реализация
        правил, у этого цикла — только счётчики excluded. include_held —
        presentation control и в policy-функцию не входит (не eligibility).
        """
        candidates = self.db.get_bonds_yield_table(
            mode='buy',
            min_maturity=args.min_maturity,
            max_maturity=args.max_maturity,
            max_risk=_SCREENER_MAX_RISK,
            max_listlevel=_SCREENER_MAX_LISTLEVEL,
            max_ytm=10 ** 9,
            include_held=True,
            limit=_SCREENER_POOL_LIMIT,
        )
        candidates = self._enrich_with_ytm(candidates)

        issuer_shares_map = {pos['issuer']: pos['issuer_pct'] for pos in positions}

        excluded: Dict[str, int] = {
            'held': 0, 'ku': 0, 'sovereign': 0, 'freq': 0, 'risk': 0, 'listlevel': 0,
            'rating_missing': 0, 'rating_below_min': 0,
            'fixed_ytm_missing': 0, 'fixed_max_ytm': 0,
            'floater_coupon_missing': 0, 'floater_max_ytm': 0,
        }
        # 023: порог рейтинга один раз переводится в балл существующей шкалой
        # (валидация кода — в _validate_args); сравнение строгое, >= порога.
        min_rating_score = (
            rating_code_to_score(args.min_credit_rating, self.rating_scale)
            if args.min_credit_rating is not None else None
        )
        survivors: List[Dict[str, Any]] = []
        for row in candidates:
            if row.get('held_share_pct') is not None and not args.include_held:
                excluded['held'] += 1
                continue
            rejection = screener_policy_rejection(
                row,
                include_ku=args.include_ku,
                max_risk=args.max_risk,
                max_listlevel=args.max_listlevel,
                freq_min=args.freq_min,
                freq_max=args.freq_max,
                freq_in=args.freq_in,
                exclude_sovereign=args.exclude_sovereign,
                min_credit_rating=args.min_credit_rating,
                min_rating_score=min_rating_score,
            )
            if rejection is not None:
                excluded[rejection.key] += 1
                continue
            survivors.append(row)

        fixes, floaters = [], []
        for row in survivors:
            (floaters if row.get('floating_coupon_flag') else fixes).append(row)

        kept_fixes = self._finish_fixes(fixes, args, excluded)
        kept_floaters = self._finish_floaters(floaters, args, excluded)

        kept_fixes.sort(key=lambda r: (-r['ytm_percent'], r['isin']))
        kept_floaters.sort(
            key=lambda r: (-_to_float(r.get('coupon_rate_percent')) or 0.0, r['isin'])
        )

        published = [
            bond_to_json(row, total_value, issuer_shares_map)
            for row in kept_fixes[:args.screener_limit]
        ]
        published += [
            bond_to_json(row, total_value, issuer_shares_map)
            for row in kept_floaters[:args.screener_limit]
        ]

        return {
            'filters': {
                'freq_min': args.freq_min,
                'freq_max': args.freq_max,
                'freq_in': args.freq_in,
                'exclude_sovereign': bool(args.exclude_sovereign),
                'include_held': bool(args.include_held),
                'include_ku': bool(args.include_ku),
                'max_risk': args.max_risk,
                'min_credit_rating': args.min_credit_rating,
                'max_listlevel': args.max_listlevel,
                'max_ytm_pct': args.max_ytm,
                'min_maturity': args.min_maturity,
                'max_maturity': args.max_maturity,
                'screener_limit': args.screener_limit,
            },
            'candidates_total': len(candidates),
            'excluded': excluded,
            'fixed_matched': len(kept_fixes),
            'floater_matched': len(kept_floaters),
            'fixed': [
                row for row in published if row['coupon_kind'] == 'fix'
            ],
            'floater': [
                row for row in published if row['coupon_kind'] == 'float'
            ],
            'issuers': self._screener_issuer_aggregate(published),
        }

    # (Построчные policy-правила скринера с 030.6 живут в bond_filters
    # (screener_policy_rejection / screener_ytm_rejection) — единственная
    # реализация для отчёта и eligibility-гейта планирования; собственных
    # обёрток у use case'а больше нет.)

    def _finish_fixes(
        self, fixes: List[Dict[str, Any]], args: argparse.Namespace, excluded: Dict[str, int]
    ) -> List[Dict[str, Any]]:
        """Финал фиксов: YTM обязательна и не выше потолка (как в движке calculate-ytm).

        030.6: правило — общая screener_ytm_rejection (bond_filters), та же
        для eligibility-гейта планирования; здесь только счётчик excluded.
        """
        kept: List[Dict[str, Any]] = []
        for row in fixes:
            rejection = screener_ytm_rejection(row, max_ytm=args.max_ytm)
            if rejection is not None:
                excluded[rejection.key] += 1
                continue
            kept.append(row)
        return kept

    def _finish_floaters(
        self, floaters: List[Dict[str, Any]], args: argparse.Namespace, excluded: Dict[str, int]
    ) -> List[Dict[str, Any]]:
        """Финал флоатеров: YTM по конвенции проекта не определена (float), топ
        строится по рассчитанной ставке купона с тем же потолком аномалий."""
        kept: List[Dict[str, Any]] = []
        for row in floaters:
            rejection = screener_ytm_rejection(row, max_ytm=args.max_ytm)
            if rejection is not None:
                excluded[rejection.key] += 1
                continue
            kept.append(row)
        return kept

    @staticmethod
    def _screener_issuer_aggregate(published: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Агрегат «эмитент → все его серии» по опубликованным топам (анти-P-D):
        выбор серии между одноимёнными выпусками делается явно, по ISIN из вывода."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in published:
            grouped.setdefault(row['issuer'], []).append(row)

        items = []
        for issuer, rows in grouped.items():
            ytm_values = [r['ytm_pct'] for r in rows if r['ytm_pct'] is not None]
            items.append({
                'issuer': issuer,
                'best_ytm_pct': max(ytm_values) if ytm_values else None,
                'series': [
                    {
                        'isin': r['isin'],
                        'name': r['name'],
                        'ticker': r['ticker'],
                        'coupon_kind': r['coupon_kind'],
                        'coupon_pct': r['coupon_pct'],
                        'ytm_pct': r['ytm_pct'],
                        'freq': r['freq'],
                        'maturity': r['maturity'],
                        'offer': r['offer'],
                        'risk_level': r['risk_level'],
                        'list_level': r['list_level'],
                    }
                    for r in rows
                ],
            })
        items.sort(key=lambda item: (
            -(item['best_ytm_pct'] if item['best_ytm_pct'] is not None else -1.0),
            item['issuer'],
        ))
        return items

    # ------------------------------------------------------------------
    # redemptions_6m
    # ------------------------------------------------------------------

    def _build_redemptions(self, months: int) -> Dict[str, Any]:
        """Календарь погашений/оферт портфеля; суммы — пул реинвеста.

        Одна позиция даёт одно событие по ближайшей дате в окне (при наличии
        и погашения, и оферты задвоения суммы нет): деньги освобождаются один раз.
        """
        rows = self.db.get_portfolio_redemption_events(months)
        today = date.today()
        window_end = _add_months(today, months)

        events: List[Dict[str, Any]] = []
        for row in rows:
            candidates: List[Tuple[date, str]] = []
            if row.get('maturity_date') and today <= row['maturity_date'] < window_end:
                candidates.append((row['maturity_date'], 'maturity'))
            if row.get('offer_date') and today <= row['offer_date'] < window_end:
                candidates.append((row['offer_date'], 'offer'))
            if not candidates:
                continue
            event_date, event_type = min(candidates)
            events.append({
                'isin': row['isin'],
                'name': row.get('name') or row['isin'],
                'ticker': row.get('ticker'),
                'issuer': row.get('issuer') or row.get('name') or row['isin'],
                'type': event_type,
                'date': event_date.isoformat(),
                'qty': round(_to_float(row.get('quantity')) or 0.0, 4),
                'value': round(_to_float(row.get('position_value_rub')) or 0.0, 2),
            })

        events.sort(key=lambda e: (e['date'], e['isin']))
        total = round(sum(e['value'] for e in events), 2)
        return {
            'period_months': months,
            'from': today.isoformat(),
            'to': window_end.isoformat(),
            'total': total,
            'events': events,
        }

    # ------------------------------------------------------------------
    # scenario (--scenario, 005.2): расчёт — в canonical engine (030.2),
    # здесь только CLI-контракт: чтение scenario.json и сборка блока.
    # ------------------------------------------------------------------

    def _load_scenario_trades(self, path_str: str) -> List[Dict[str, Any]]:
        """Читает и валидирует scenario.json: {'trades': [{'isin', 'qty_delta'}]}.

        Структурные проблемы (нет файла, невалидный JSON, неверные поля) —
        ValueError с понятным сообщением (CLI завершается с кодом 1).
        Семантические ошибки (unknown ISIN, отрицательный остаток, разрыв
        связи с эмитентом) НЕ исключения — они попадают в scenario.errors
        ответа (блок 0 A2), чтобы агент получил явный машиночитаемый отказ.
        """
        path = Path(path_str)
        if not path.is_file():
            raise ValueError(f"--scenario: файл не найден: {path_str}")
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"--scenario: файл {path_str} не содержит валидный JSON: {exc}")
        if not isinstance(data, dict) or 'trades' not in data:
            raise ValueError("--scenario: ожидался JSON-объект с ключом 'trades'")
        raw_trades = data['trades']
        if not isinstance(raw_trades, list) or not raw_trades:
            raise ValueError("--scenario: 'trades' должен быть непустым списком сделок")

        trades: List[Dict[str, Any]] = []
        for index, raw in enumerate(raw_trades, start=1):
            if not isinstance(raw, dict):
                raise ValueError(
                    f"--scenario: сделка #{index} должна быть объектом вида "
                    "{'isin': ..., 'qty_delta': ...}"
                )
            isin = raw.get('isin')
            if not isinstance(isin, str) or not isin.strip():
                raise ValueError(
                    f"--scenario: сделка #{index}: 'isin' должен быть непустой строкой"
                )
            qty_delta = raw.get('qty_delta')
            if isinstance(qty_delta, bool) or not isinstance(qty_delta, (int, float)):
                raise ValueError(
                    f"--scenario: сделка #{index} ({isin.strip()}): 'qty_delta' должен "
                    "быть числом в штуках (продажа — отрицательный, покупка — "
                    "положительный)"
                )
            trades.append({'isin': isin.strip(), 'qty_delta': float(qty_delta)})
        return trades

    def _build_scenario(self, args: argparse.Namespace) -> Dict[str, Any]:
        """Блок scenario: расчёт целиком в canonical engine (030.2).

        Use case остаётся точкой CLI-контракта: читает и структурно
        валидирует scenario.json (_load_scenario_trades), собирает
        ScenarioConstraints из canonical args (без протягивания argparse
        в engine) и одним вызовом RebalanceEngine.evaluate_scenario()
        получает весь расчёт (summary/budget/лимиты эмитента/positions_after/
        constraint_checks/feasible, 005.2/024). JSON-контракт блока не
        меняется: ни одно поле не удалено и не переименовано; engine-ключ
        planning_prices (030.2, цена+basis на ISIN) в JSON не публикуется —
        он часть Python API движка (030.7+).
        """
        trades = self._load_scenario_trades(args.scenario)
        constraints = ScenarioConstraints(
            freq_min=args.freq_min,
            freq_max=args.freq_max,
            freq_in=args.freq_in,
            exclude_sovereign=bool(args.exclude_sovereign),
            min_credit_rating=args.min_credit_rating,
            max_position_value=args.max_position_value,
        )
        result = self._engine.evaluate_scenario(trades, constraints)
        result.pop('planning_prices')
        # 'file' — единственное CLI-поле блока; порядок ключей прежний.
        return {'file': args.scenario, **result}

    # ------------------------------------------------------------------
    # artifacts (CSV)
    # ------------------------------------------------------------------

    @staticmethod
    def _write_target_csv(
        path_str: str, positions: List[Dict[str, Any]], total_value: float
    ) -> str:
        """CSV целевого портфеля: UTF-8 BOM, русские заголовки, строка ИТОГО.

        В ядре (005.1) целевой портфель = текущий портфель (базис до сделок);
        сценарный режим --scenario дописывает trades поверх (005.2).
        """
        path = Path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)

        def fmt_num(value: Any, ndigits: int = 2) -> str:
            if value is None:
                return ''
            return f"{float(value):.{ndigits}f}"

        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)
            for pos in positions:
                writer.writerow([
                    pos['isin'], pos['name'] or '', pos['ticker'] or '', pos['issuer'],
                    fmt_num(pos['qty'], 0), fmt_num(pos['price']),
                    # 025: доля выпуска (share_pct) и доля эмитента (issuer_pct)
                    # — разные колонки; раньше под «Доля, %» шёл issuer_pct.
                    fmt_num(pos['value']), fmt_num(pos['share_pct']),
                    fmt_num(pos['issuer_pct']),
                    fmt_num(pos['ytm_pct']), fmt_num(pos['coupon_pct']),
                    pos['freq'] if pos['freq'] is not None else '',
                    'Плавающий' if pos['coupon_kind'] == 'float' else 'Фиксированный',
                    pos['risk_level'] if pos['risk_level'] is not None else '',
                    pos['list_level'] if pos['list_level'] is not None else '',
                    pos['maturity'] or '', pos['offer'] or '', pos['held_badge'] or '',
                ])
            writer.writerow([
                'ИТОГО', f"позиций: {len(positions)}", '', '', '', '',
                # 100% — сумма долей выпусков (share_pct); доли эмитентов
                # агрегированы по колонке «Доля эмитента, %».
                fmt_num(total_value), '100.00', '',
                '', '', '', '', '', '', '', '', '',
            ])
        return str(path)
