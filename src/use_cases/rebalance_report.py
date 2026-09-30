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
"""
import argparse
import calendar
import csv
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from src.storage import PortfolioStorage
from src.use_cases.base import UseCase
from src.use_cases.bond_filters import freq_ok as _freq_ok_rule
from src.use_cases.bond_filters import is_sovereign as _is_sovereign_rule
from src.use_cases.bond_filters import parse_freq_list
from src.use_cases.calculate_ytm import CalculateYtmUseCase
from src.use_cases.check_db import classify_freshness
from src.utils import rating_code_to_score

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

# Версия контракта JSON-ответа: скилл проверяет совместимость формата.
# 2 (задача 022): portfolio дополняется cash-полями канонического снапшота
# (securities_value_rub, cash_available_rub, investable_total_rub,
# cash_updated_at); total_value и арифметика долей не меняются.
SCHEMA_VERSION = 2

# Лимит концентрации эмитента, % портфеля (план ребалансировки 001/002).
ISSUER_CONCENTRATION_LIMIT_PCT = 15.0

# Сколько эмитентов показывать в concentrations.issuers.top.
ISSUER_TOP_SIZE = 5

# Широкая выборка кандидатов скринера: фильтры риска/листинга/YTM применяются
# в use-case'е с подсчётом «сколько отсечено и почему», поэтому SQL-пороги
# открываются на максимум шкал (риск 1-5, листинг 1-3).
_SCREENER_MAX_RISK = 5
_SCREENER_MAX_LISTLEVEL = 3
_SCREENER_POOL_LIMIT = 10000

# Ключи строки бумаги в ответе (portfolio.positions и screener.fixed/floater).
# 023: credit_rating — настоящий рейтинг кандидата (latest rating_history),
# risk_level остаётся отдельным полем/фильтром.
BOND_ROW_KEYS = (
    'isin', 'name', 'ticker', 'qty', 'price', 'value',
    'ytm_pct', 'ytm_reason', 'coupon_pct', 'freq',
    'coupon_kind', 'risk_level', 'list_level', 'maturity', 'offer',
    'amort', 'ku', 'issuer', 'issuer_pct', 'held_badge', 'credit_rating',
)

# Заголовки CSV целевого портфеля (UTF-8 BOM, русские заголовки, строка ИТОГО).
CSV_HEADERS = [
    'ISIN', 'Название', 'Тикер', 'Эмитент', 'Кол-во', 'Цена',
    'Стоимость, ₽', 'Доля, %', 'YTM, %', 'Купон, %', 'Частота купонов',
    'Тип купона', 'Риск', 'Листинг', 'Погашение', 'Оферта', 'Бейдж',
]

# ---------------------------------------------------------------------------
# Сценарный режим --scenario (005.2): контракт блока scenario.
# ---------------------------------------------------------------------------

# Явные ошибки сценария (аналог блока 0 scenario_summary.sql / A2 из 002.4).
SCENARIO_ERROR_UNKNOWN_ISIN = 'UNKNOWN_ISIN'
SCENARIO_ERROR_NEGATIVE_QTY_AFTER = 'NEGATIVE_QTY_AFTER'
SCENARIO_ERROR_NO_COMPANY_LINK = 'NO_COMPANY_LINK'

# Статусы лимита эмитента — строки как в блоке 3 A2 (совместимость контракта).
SCENARIO_LIMIT_STATUS_PASS = 'PASS'
SCENARIO_LIMIT_STATUS_VIOLATION = 'LIMIT_VIOLATION (>15%)'

# Ключи строк блока scenario.
SCENARIO_TRADE_KEYS = (
    'isin', 'name', 'ticker', 'issuer', 'qty_delta', 'action',
    'qty_before', 'qty_after', 'price', 'value_delta',
)
SCENARIO_POSITION_KEYS = BOND_ROW_KEYS + ('coupon_month',)
SCENARIO_LEG_KEYS = (
    'leg', 'isin', 'name', 'ticker', 'issuer', 'qty_delta',
    'trade_value', 'issuer_pct_after_leg', 'limit_status',
)
SCENARIO_SUMMARY_KEYS = (
    'portfolio_before', 'portfolio_after',
    'coupon_month_before', 'coupon_month_after',
    'coupon_year_before', 'coupon_year_after',
    'delta_month', 'delta_year',
)


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


def _add_months(day: date, months: int) -> date:
    """Дата + months месяцев (календарно, как make_interval в PostgreSQL)."""
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _to_float(value: Any) -> Optional[float]:
    """Decimal/int/str -> float, None проходит (JSON не умеет Decimal)."""
    if value is None:
        return None
    return float(value)


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _to_iso_day(value: Any) -> Optional[str]:
    """date/datetime -> 'YYYY-MM-DD', None проходит."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _fmt_qty(value: float) -> str:
    """Количество в штуках без хвостового '.0' (для бейджей и сообщений)."""
    qty = float(value)
    if qty.is_integer():
        return str(int(qty))
    return f"{qty:.4f}".rstrip('0').rstrip('.')


def _issuer_limit_status(issuer_pct: float) -> str:
    """Статус лимита эмитента 15% — строки как в блоке 3 A2 (002.4)."""
    if issuer_pct > ISSUER_CONCENTRATION_LIMIT_PCT:
        return SCENARIO_LIMIT_STATUS_VIOLATION
    return SCENARIO_LIMIT_STATUS_PASS


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
        # Единый расчётный движок YTM (требование 005.1: единицы YTM — только
        # из одного источника, расчётный движок CLI-уровня).
        self._ytm_engine = CalculateYtmUseCase(db)
        # Шкала рейтингов из конфига (023): порог --min-credit-rating и
        # сравнение идут через существующий rating_code_to_score — второй
        # нормализации рейтингов нет. Пустая шкала валидна, пока порог
        # не задан (проверяется в _validate_args).
        self.rating_scale = rating_scale or {}

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--format', choices=['json'], default='json',
            help='Формат ответа: json — канонический машинный формат для агента.'
        )
        parser.add_argument(
            '--freq-max', type=int, default=None,
            help='Максимальная частота купонов в год (напр. 4 = не чаще '
                 'квартальных; P-C). С --freq-min задаёт диапазон; '
                 'взаимоисключимо с --freq-in.'
        )
        parser.add_argument(
            '--freq-min', type=int, default=None,
            help='Минимальная частота купонов в год (023): «от квартальных '
                 'до ежемесячных» = --freq-min 4 --freq-max 12. NULL/0-частота '
                 'строгий диапазон не проходит; взаимоисключимо с --freq-in.'
        )
        parser.add_argument(
            '--freq-in', type=_parse_freq_in, default=None,
            help='Только указанные частоты купонов через запятую (напр. 2,4; P-C). '
                 'Взаимоисключимо с --freq-min/--freq-max.'
        )
        parser.add_argument(
            '--exclude-sovereign', action='store_true',
            help='Исключить суверенных эмитентов: ОФЗ и евро-РФ (P-G).'
        )
        parser.add_argument(
            '--include-held', action='store_true',
            help='Показывать в скринере бумаги, уже лежащие в портфеле, '
                 'с бейджем held (доля N%%); по умолчанию они исключены (анти-P2 из 002).'
        )
        parser.add_argument(
            '--include-ku', action='store_true',
            help='Включить бумаги для квалифицированных инвесторов '
                 '(по умолчанию исключены).'
        )
        parser.add_argument(
            '--min-maturity', type=_parse_iso_date, default=None,
            help='Минимальная дата погашения кандидатов, YYYY-MM-DD.'
        )
        parser.add_argument(
            '--max-maturity', type=_parse_iso_date, default=None,
            help='Максимальная дата погашения кандидатов, YYYY-MM-DD.'
        )
        parser.add_argument(
            '--max-risk', type=int, default=1,
            help='Максимальный уровень риска Т-Банка кандидатов (1-5). Default: 1.'
        )
        parser.add_argument(
            '--min-credit-rating', type=str, default=None,
            help='Минимальный кредитный рейтинг кандидата (023), напр. AA-: '
                 'строгий порог по latest rating_history — без рейтинга или '
                 'ниже порога кандидат исключается. Код нормализуется '
                 'существующим rating_code_to_score (AAA(RU)/ruAAA = AAA); '
                 'risk_level рейтингом не подменяется.'
        )
        parser.add_argument(
            '--max-listlevel', type=int, choices=[1, 2, 3], default=2,
            help='Максимальный уровень листинга MOEX кандидатов. Default: 2.'
        )
        parser.add_argument(
            '--max-ytm', type=float, default=35.0,
            help='Потолок YTM кандидатов, %% годовых (аномалия = ВДО/дистресс). Default: 35.0.'
        )
        parser.add_argument(
            '--screener-limit', type=int, default=10,
            help='Размер топов fixed[]/floater[] в скринере. Default: 10.'
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
        self._validate_args(args)

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
        if args.screener_limit < 1:
            raise ValueError("--screener-limit должен быть >= 1")
        if args.max_ytm <= 0:
            raise ValueError("--max-ytm должен быть > 0")

    # ------------------------------------------------------------------
    # Сборка отчёта
    # ------------------------------------------------------------------

    def build_report(self, args: argparse.Namespace) -> Dict[str, Any]:
        """Собирает канонический отчёт — программный API (019).

        Возвращает тот же dict, что execute() печатает как JSON: CLI и
        программные потребители (экспортёр ChatGPT) идут через один метод,
        второго аналитического контура не появляется.
        """
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
        issuer_shares = self._issuer_shares(rows, total_value)

        positions = [
            self._bond_to_json(row, total_value, issuer_shares, held_default=True)
            for row in rows
        ]
        return positions, total_value

    def _enrich_with_ytm(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Каноническая YTM в процентах — только через расчётный движок calculate-ytm."""
        return self._ytm_engine._enrich_bonds_with_ytm(rows, argparse.Namespace(min_ytm=None))

    @staticmethod
    def _issuer_shares(rows: List[Dict[str, Any]], total_value: float) -> Dict[str, float]:
        """Доля эмитента в портфеле, % (для issuer_pct и концентраций)."""
        totals: Dict[str, float] = {}
        for row in rows:
            issuer = row.get('issuer') or row.get('name') or row.get('isin')
            totals[issuer] = totals.get(issuer, 0.0) + (_to_float(row.get('value_rub')) or 0.0)
        if total_value <= 0:
            return {}
        return {issuer: round(value / total_value * 100, 2) for issuer, value in totals.items()}

    def _bond_to_json(
        self,
        row: Dict[str, Any],
        total_value: float,
        issuer_shares: Dict[str, float],
        held_default: bool = False,
    ) -> Dict[str, Any]:
        """Строка бумаги в каноническом контракте (portfolio и screener)."""
        value = round(_to_float(row.get('value_rub')) or 0.0, 2)
        share_pct = round(value / total_value * 100, 2) if total_value > 0 else 0.0

        # Бейдж held: в скринере приходит из SQL (002.2), в портфеле строится
        # по доле ISIN — контракт бейджа единый: 'held (N%)'.
        badge = row.get('held_badge')
        if badge is None and held_default and total_value > 0:
            badge = f"held ({share_pct:.2f}%)"

        issuer = row.get('issuer') or row.get('name') or row.get('isin')
        # price: у позиций портфеля — из выборки; у кандидатов скринера —
        # market_price каталога (018: buy-выборка без fallback на nominal).
        price_raw = row.get('price')
        if price_raw is None:
            price_raw = row.get('current_price')
        return {
            'isin': row.get('isin'),
            'name': row.get('name') or row.get('isin'),
            'ticker': row.get('ticker'),
            'qty': round(_to_float(row.get('quantity')) or 0.0, 4),
            'price': round(_to_float(price_raw), 4) if price_raw is not None else None,
            'value': value,
            'ytm_pct': round(_to_float(row.get('ytm_percent')), 2)
            if row.get('ytm_percent') is not None else None,
            'ytm_reason': row.get('ytm_reason'),
            'coupon_pct': round(_to_float(row.get('coupon_rate_percent')), 2)
            if row.get('coupon_rate_percent') is not None else None,
            'freq': _to_int(row.get('coupon_quantity_per_year')),
            'coupon_kind': 'float' if row.get('floating_coupon_flag') else 'fix',
            'risk_level': _to_int(row.get('risk_level')),
            'credit_rating': row.get('credit_rating') or None,
            'list_level': _to_int(row.get('list_level')),
            'maturity': _to_iso_day(row.get('maturity_date')),
            'offer': _to_iso_day(row.get('offer_date')),
            'amort': bool(row.get('amortization_flag')),
            'ku': bool(row.get('ku')),
            'issuer': issuer,
            'issuer_pct': issuer_shares.get(issuer),
            'held_badge': badge,
        }

    # ------------------------------------------------------------------
    # concentrations
    # ------------------------------------------------------------------

    @staticmethod
    def _value_mix(
        items: List[Tuple[Any, float]], total_value: float, key: str
    ) -> List[Dict[str, Any]]:
        """Стоимостный микс по группировочному признаку (риск/частота).

        Единая арифметика для concentrations ядра и частотного микса
        «до/после» сценария (005.2): доля группы — от стоимости
        соответствующего состояния портфеля; группы без значения (None) —
        в конце списка.
        """
        totals: Dict[Any, float] = {}
        for group, value in items:
            totals[group] = totals.get(group, 0.0) + value

        def pct(value: float) -> float:
            return round(value / total_value * 100, 2) if total_value > 0 else 0.0

        return [
            {key: group, 'value': round(value, 2), 'pct': pct(value)}
            for group, value in sorted(totals.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
        ]

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
            'risk_mix': self._value_mix(
                [(pos['risk_level'], pos['value']) for pos in positions],
                total_value, 'risk_level',
            ),
            'freq_mix': self._value_mix(
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

        issuer_shares = {pos['issuer']: pos['issuer_pct'] for pos in positions}

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
            if row.get('ku') and not args.include_ku:
                excluded['ku'] += 1
                continue
            if args.exclude_sovereign and self._is_sovereign(row):
                excluded['sovereign'] += 1
                continue
            if not self._freq_ok(row, args):
                excluded['freq'] += 1
                continue
            risk_level = _to_int(row.get('risk_level'))
            if risk_level is None or risk_level > args.max_risk:
                excluded['risk'] += 1
                continue
            list_level = _to_int(row.get('list_level'))
            if list_level is not None and list_level > args.max_listlevel:
                excluded['listlevel'] += 1
                continue
            # 023: строгий фильтр рейтинга — отдельная ветка от risk_level:
            # нет строки в rating_history (нет и балла) — rating_missing;
            # балл ниже порога — rating_below_min. risk_level подменой не служит.
            if min_rating_score is not None:
                rating_score = _to_int(row.get('rating_score'))
                if rating_score is None:
                    excluded['rating_missing'] += 1
                    continue
                if rating_score < min_rating_score:
                    excluded['rating_below_min'] += 1
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
            self._bond_to_json(row, total_value, issuer_shares)
            for row in kept_fixes[:args.screener_limit]
        ]
        published += [
            self._bond_to_json(row, total_value, issuer_shares)
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

    @staticmethod
    def _is_sovereign(row: Dict[str, Any]) -> bool:
        """Суверенный эмитент — общее правило тулкита (bond_filters, 005.3):
        связка с companies (entity_type=sovereign) либо каталог-имя ОФЗ."""
        return _is_sovereign_rule(row)

    @staticmethod
    def _freq_ok(row: Dict[str, Any], args: argparse.Namespace) -> bool:
        """Фильтр частоты купонов (P-C) — общее правило тулкита
        (bond_filters, 005.3/023): NULL-частота фильтру не проходит;
        freq_min/freq_max — диапазон, freq_in — точный список."""
        return _freq_ok_rule(
            _to_int(row.get('coupon_quantity_per_year')),
            freq_max=args.freq_max,
            freq_in=args.freq_in,
            freq_min=args.freq_min,
        )

    def _finish_fixes(
        self, fixes: List[Dict[str, Any]], args: argparse.Namespace, excluded: Dict[str, int]
    ) -> List[Dict[str, Any]]:
        """Финал фиксов: YTM обязательна и не выше потолка (как в движке calculate-ytm)."""
        kept: List[Dict[str, Any]] = []
        for row in fixes:
            ytm_pct = _to_float(row.get('ytm_percent'))
            if ytm_pct is None:
                excluded['fixed_ytm_missing'] += 1
                continue
            # Повторяем порог движка на рассчитанном значении (до backfill
            # SQL-фильтр bc.ytm строки с расчётной YTM на лету не отсекает).
            if ytm_pct > args.max_ytm:
                excluded['fixed_max_ytm'] += 1
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
            coupon_pct = _to_float(row.get('coupon_rate_percent'))
            if coupon_pct is None:
                excluded['floater_coupon_missing'] += 1
                continue
            if coupon_pct > args.max_ytm:
                excluded['floater_max_ytm'] += 1
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
    # scenario (--scenario, 005.2): перенос scenario_summary.sql (A2/002.4)
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

    @staticmethod
    def _make_scenario_state(
        row: Dict[str, Any], qty_before: float, value_now: Optional[float],
        today: date, is_held: bool,
    ) -> Dict[str, Any]:
        """Состояние одного ISIN вселенной сценария (аналог base-CTE A2).

        Цена «после» (eff_price): погашенные — 0 (P1-guard: без номинального
        фолбэка и фантомного купона); держимые — каноническая оценка view
        на штуку (value_rub/qty); покупки — market_price с фолбэком на
        номинал. Купон ₽/мес = qty * номинал * ставка% / 1200 (амортизация
        не моделируется — то же ограничение, что у A2/002-P8).
        """
        maturity = row.get('maturity_date')
        is_matured = maturity is not None and maturity < today
        nominal = _to_float(row.get('nominal')) or 0.0
        coupon_rate = 0.0 if is_matured else (_to_float(row.get('coupon_rate_percent')) or 0.0)

        if is_matured:
            eff_price, value_before = 0.0, 0.0
        elif qty_before > 0 and value_now is not None and value_now > 0:
            eff_price = value_now / qty_before
            value_before = value_now
        else:
            market_price = _to_float(row.get('market_price'))
            eff_price = market_price if market_price else nominal
            value_before = round(qty_before * eff_price, 2)

        return {
            'row': row,
            'is_held': is_held,
            'is_matured': is_matured,
            'qty_before': qty_before,
            'value_before': value_before,
            'eff_price': eff_price,
            'nominal': nominal,
            'coupon_rate': coupon_rate,
            'coupon_month_before': round(qty_before * nominal * coupon_rate / 1200.0, 2),
            'freq': _to_int(row.get('coupon_quantity_per_year')),
            'name': row.get('name') or row['isin'],
            'ticker': row.get('ticker'),
            'issuer': row.get('issuer') or row.get('name') or row['isin'],
            'company_id': row.get('company_id'),
        }

    def _build_scenario(self, args: argparse.Namespace) -> Dict[str, Any]:
        """Блок scenario: trades -> целевой портфель без ручной арифметики (анти-P6).

        Валидный сценарий: summary («сейчас → после» по стоимости и купонному
        потоку ₽/мес и ₽/год), частотный микс до/после, лимиты эмитента 15% по
        каждой ноге (кумулятивно) и по сумме всех ног, целевой портфель
        positions_after. При ошибках (блок 0 A2) все блоки «после» = null:
        числа невалидного сценария нельзя интерпретировать.
        """
        today = date.today()
        trades = self._load_scenario_trades(args.scenario)

        held_rows = self.db.get_rebalance_portfolio_rows()
        held_by_isin = {row['isin']: row for row in held_rows}

        # Каталог для ISIN вне портфеля (покупки) — те же колонки, что у позиций.
        missing = sorted({t['isin'] for t in trades} - set(held_by_isin))
        catalog_by_isin = {
            row['isin']: row
            for row in (self.db.get_scenario_universe_rows(missing) if missing else [])
        }

        # Вселенная сценария: текущий портфель + все ISIN из сделок.
        universe: Dict[str, Dict[str, Any]] = {}
        for isin, row in held_by_isin.items():
            universe[isin] = self._make_scenario_state(
                row,
                qty_before=_to_float(row.get('quantity')) or 0.0,
                value_now=_to_float(row.get('value_rub')),
                today=today,
                is_held=True,
            )
        for isin, row in catalog_by_isin.items():
            if isin not in universe:
                universe[isin] = self._make_scenario_state(
                    row, qty_before=0.0, value_now=None, today=today, is_held=False,
                )

        # Суммарная дельта по ISIN (аналог GROUP BY isin в planned из A2).
        deltas: Dict[str, float] = {}
        for trade in trades:
            deltas[trade['isin']] = deltas.get(trade['isin'], 0.0) + trade['qty_delta']
        for isin, state in universe.items():
            delta = deltas.get(isin, 0.0)
            state['qty_delta'] = delta
            state['qty_after'] = max(state['qty_before'] + delta, 0.0)
            state['value_after'] = round(state['qty_after'] * state['eff_price'], 2)
            state['coupon_month_after'] = round(
                state['qty_after'] * state['nominal'] * state['coupon_rate'] / 1200.0, 2
            )

        # Блок 0: явные ошибки сценария (штатно — пустой список).
        errors: List[Dict[str, Any]] = []
        for isin in sorted({t['isin'] for t in trades} - set(universe)):
            errors.append({
                'type': SCENARIO_ERROR_UNKNOWN_ISIN,
                'isin': isin,
                'message': f"{isin}: ISIN не найден ни в портфеле, ни в каталоге — сделка не применена",
            })
        for isin, state in universe.items():
            remainder = state['qty_before'] + state['qty_delta']
            if remainder < 0:
                errors.append({
                    'type': SCENARIO_ERROR_NEGATIVE_QTY_AFTER,
                    'isin': isin,
                    'message': (
                        f"{isin}: остаток после сделок {_fmt_qty(remainder)} шт "
                        f"(позиция {_fmt_qty(state['qty_before'])} шт, "
                        f"сделки {_fmt_qty(state['qty_delta'])} шт)"
                    ),
                })
            elif state['qty_after'] > 0 and state['company_id'] is None:
                errors.append({
                    'type': SCENARIO_ERROR_NO_COMPANY_LINK,
                    'isin': isin,
                    'message': f"{isin}: бумага не связана с эмитентом (company_id IS NULL)",
                })
        valid = not errors

        # Целевое состояние (блок 2 A2): непогашенные позиции после сделок.
        target_states = [
            state for state in universe.values()
            if state['qty_after'] > 0 and not state['is_matured']
        ]
        total_before = round(sum(s['value_before'] for s in universe.values()), 2)
        total_after = round(sum(s['value_after'] for s in target_states), 2)

        # Сделки с именами из каталога (анти-P-D) + лимит по каждой ноге
        # кумулятивно (анти-P6: в 002 суммарную долю РЕСО считали вручную).
        running = {isin: state['qty_before'] for isin, state in universe.items()}
        trades_echo: List[Dict[str, Any]] = []
        legs: List[Dict[str, Any]] = []
        for leg_no, trade in enumerate(trades, start=1):
            isin, delta = trade['isin'], trade['qty_delta']
            state = universe.get(isin)
            if state is None:
                trades_echo.append({
                    'isin': isin, 'name': isin, 'ticker': None, 'issuer': None,
                    'qty_delta': delta,
                    'action': 'buy' if delta > 0 else ('sell' if delta < 0 else 'noop'),
                    'qty_before': None, 'qty_after': None,
                    'price': None, 'value_delta': None,
                })
                continue
            qty_before_leg = running.get(isin, 0.0)
            running[isin] = qty_before_leg + delta
            trades_echo.append({
                'isin': isin,
                'name': state['name'],
                'ticker': state['ticker'],
                'issuer': state['issuer'],
                'qty_delta': delta,
                'action': 'buy' if delta > 0 else ('sell' if delta < 0 else 'noop'),
                'qty_before': round(qty_before_leg, 4),
                'qty_after': round(running[isin], 4),
                'price': round(state['eff_price'], 4),
                'value_delta': round(delta * state['eff_price'], 2),
            })
            issuer_value = sum(
                max(running[other], 0.0) * other_state['eff_price']
                for other, other_state in universe.items()
                if other_state['issuer'] == state['issuer']
            )
            issuer_pct = issuer_value / total_after * 100 if total_after > 0 else 0.0
            legs.append({
                'leg': leg_no,
                'isin': isin,
                'name': state['name'],
                'ticker': state['ticker'],
                'issuer': state['issuer'],
                'qty_delta': delta,
                'trade_value': round(delta * state['eff_price'], 2),
                'issuer_pct_after_leg': round(issuer_pct, 2),
                'limit_status': _issuer_limit_status(issuer_pct),
            })

        # Лимиты эмитента по сумме всех ног (блок 3 A2) — по целевому портфелю.
        issuer_totals: Dict[str, float] = {}
        for state in target_states:
            issuer = state['issuer']
            issuer_totals[issuer] = issuer_totals.get(issuer, 0.0) + state['value_after']
        after_all = [
            {
                'issuer': issuer,
                'value': round(value, 2),
                'pct': round(value / total_after * 100, 2) if total_after > 0 else 0.0,
                'limit_status': _issuer_limit_status(
                    value / total_after * 100 if total_after > 0 else 0.0
                ),
            }
            for issuer, value in issuer_totals.items()
        ]
        after_all.sort(key=lambda item: (-item['value'], item['issuer']))

        coupon_month_before = round(
            sum(s['coupon_month_before'] for s in universe.values()), 2
        )
        coupon_month_after = round(
            sum(s['coupon_month_after'] for s in target_states), 2
        )
        delta_month = round(coupon_month_after - coupon_month_before, 2)

        summary = {
            'portfolio_before': total_before,
            'portfolio_after': total_after,
            'coupon_month_before': coupon_month_before,
            'coupon_month_after': coupon_month_after,
            'coupon_year_before': round(coupon_month_before * 12, 2),
            'coupon_year_after': round(coupon_month_after * 12, 2),
            'delta_month': delta_month,
            'delta_year': round(delta_month * 12, 2),
        }

        # Частотный микс «до/после»: доля частот в стоимости соответствующего
        # состояния (до — текущий портфель, после — целевой портфель).
        freq_mix_before = self._value_mix(
            [(s['freq'], s['value_before']) for s in universe.values() if s['is_held']],
            total_before, 'freq',
        )
        freq_mix_after = self._value_mix(
            [(s['freq'], s['value_after']) for s in target_states],
            total_after, 'freq',
        )

        # Целевый портфель в канонической форме строк ядра (+ coupon_month):
        # YTM — от того же расчётного движка, имена/эмитенты — из каталога.
        after_rows: List[Dict[str, Any]] = []
        for state in target_states:
            raw = dict(state['row'])
            raw['quantity'] = state['qty_after']
            raw['value_rub'] = state['value_after']
            raw['price'] = state['eff_price']
            after_rows.append(raw)
        after_rows = self._enrich_with_ytm(after_rows)
        issuer_shares_after = self._issuer_shares(after_rows, total_after)

        positions_after: List[Dict[str, Any]] = []
        for raw in after_rows:
            pos = self._bond_to_json(raw, total_after, issuer_shares_after, held_default=True)
            pos['coupon_month'] = universe[raw['isin']]['coupon_month_after']
            if raw['isin'] in deltas:
                sign = '+' if deltas[raw['isin']] > 0 else ''
                pos['held_badge'] = f"сделка {sign}{_fmt_qty(deltas[raw['isin']])} шт"
            positions_after.append(pos)
        positions_after.sort(key=lambda pos: (-pos['value'], pos['isin']))

        return {
            'file': args.scenario,
            'valid': valid,
            'errors': errors,
            'trades': trades_echo,
            # Блоки «после» валидны только при пустом блоке ошибок (как в A2).
            'summary': summary if valid else None,
            'freq_mix_before': freq_mix_before if valid else None,
            'freq_mix_after': freq_mix_after if valid else None,
            'issuer_limits': {
                'limit_pct': ISSUER_CONCENTRATION_LIMIT_PCT,
                'legs': legs,
                'after_all': after_all,
                'has_violations': any(
                    item['limit_status'] != SCENARIO_LIMIT_STATUS_PASS
                    for item in after_all
                ),
            } if valid else None,
            'positions_after': positions_after if valid else [],
        }

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
                    fmt_num(pos['value']), fmt_num(pos['issuer_pct']),
                    fmt_num(pos['ytm_pct']), fmt_num(pos['coupon_pct']),
                    pos['freq'] if pos['freq'] is not None else '',
                    'Плавающий' if pos['coupon_kind'] == 'float' else 'Фиксированный',
                    pos['risk_level'] if pos['risk_level'] is not None else '',
                    pos['list_level'] if pos['list_level'] is not None else '',
                    pos['maturity'] or '', pos['offer'] or '', pos['held_badge'] or '',
                ])
            writer.writerow([
                'ИТОГО', f"позиций: {len(positions)}", '', '', '', '',
                fmt_num(total_value), '100.00', '', '', '', '', '', '', '', '', '',
            ])
        return str(path)
