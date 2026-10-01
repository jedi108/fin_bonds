"""
Use case для расчета и отображения YTM (доходности к погашению) облигаций.

005.3: команда calculate-ytm получает машинный формат и фильтры выдачи.
- `--format json` — канонический JSON для LLM-агента (schema_version=1,
  согласован с rebalance-report): stdout — чистый JSON, логи — только в
  stderr (P-F); в дефолтном режиме (без --verbose) корневой уровень
  логирования поднимается до WARNING+, INFO возвращается флагом -v.
- `--coupon-freq-min N` / `--coupon-freq-max N` / `--coupon-freq-in 2,4` —
  фильтр частоты купонов, min+max задают диапазон (напр. 4..12; P-C;
  конвенция общая с rebalance-report, см. src/use_cases/bond_filters.py).
- `--exclude-sovereign` — исключить ОФЗ и евро-РФ (P-G, entity_type=sovereign
  с фолбэком на имя «ОФЗ…»).
- Воронка отсечений (R9): применённые фильтры и число отсечённых бумаг по
  причинам логируются (INFO) и входят в JSON-блок `excluded`.

Единицы YTM (005.4, миграция 013): bonds_catalog.ytm и весь вывод команды —
проценты годовых (ytm_pct); расчётный движок cashflow-решателя внутри работает
с долей единицы — конвертация ×100 только на границах записи/расчёта на лету.
`--mode buy/portfolio`, live-YTM, бейджи «⚠️ ВДО/Дистресс» и прежние
фильтры сохранены.
"""
import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import logging
from typing import TYPE_CHECKING, List, Dict, Any, Optional, Tuple

from src.services.cashflow import calculate_bond_cashflow_metrics
from src.storage import PortfolioStorage
from src.use_cases.base import UseCase
from src.use_cases.bond_filters import freq_ok, is_sovereign, parse_freq_list

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

# Версия контракта JSON-ответа — согласована с rebalance-report (005.1).
SCHEMA_VERSION = 1

# 005.3: размер пула выборки, когда активны питон-фильтры выдачи (частота
# P-C, суверенные P-G). SQL-пороги риска/листинга/YTM остаются прежними,
# а --limit применяется к ИТОГОВОЙ выдаче — иначе «топ-50 квартальных»
# считался бы по до-фильтровому топу и воронка отсечений была бы неполной.
# Значение то же, что у скринера rebalance-report (_SCREENER_POOL_LIMIT).
_FILTER_POOL_LIMIT = 10000

# Ключи воронки отсечений (R9); sovereign/freq — общие с rebalance-report.
_EXCLUDED_KEYS = ('sovereign', 'freq', 'min_ytm', 'max_ytm', 'limit_truncated')

# Ключи строки бумаги в JSON-ответе (общие с BOND_ROW_KEYS rebalance-report).
_BOND_JSON_KEYS = (
    'isin', 'name', 'ticker', 'price', 'ytm_pct', 'ytm_reason',
    'coupon_pct', 'freq', 'coupon_kind', 'risk_level', 'list_level',
    'maturity', 'amort', 'ku', 'issuer', 'coupon_month_per_bond', 'held_badge',
)
# Дополнение для режима portfolio (позиция портфеля).
_BOND_JSON_KEYS_PORTFOLIO = _BOND_JSON_KEYS + ('qty', 'avg_price', 'price_change_pct')


def _parse_coupon_freq_in(raw: str) -> List[int]:
    """Разбирает '--coupon-freq-in 2,4' (общая логика bond_filters, 005.3)."""
    return parse_freq_list(raw, flag='--coupon-freq-in')


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


class CalculateYtmUseCase(UseCase):
    """
    Рассчитывает и отображает YTM (доходность к погашению) для облигаций.
    Поддерживает два режима:
    1. buy: кандидаты на покупку по рыночной цене каталога (mark-to-market)
    2. portfolio: позиции портфеля по рыночной цене каталога (mark-to-market),
       при этом средняя цена покупки остаётся справочной колонкой.
    """

    def __init__(self, db: PortfolioStorage):
        self.db = db

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды расчета YTM."""
        parser.add_argument('--mode', type=str, choices=['buy', 'portfolio'], required=True,
                           help='Режим анализа: buy - для покупки, portfolio - уже купленные')
        parser.add_argument(
            '--format', choices=['text', 'json'], default='text',
            help='Формат вывода: text — человекочитаемая ASCII-таблица '
                 '(по умолчанию), json — машинный формат для агента: stdout '
                 'чистый JSON (schema_version=1), логи только в stderr, '
                 'WARNING+ без --verbose (005.3/P-F).'
        )
        parser.add_argument(
            '--coupon-freq-max', type=int, default=None,
            help='Максимальная частота купонов в год: 4 = квартальные и реже '
                 '(005.3/P-C; конвенция общая с rebalance-report --freq-max). '
                 'С --coupon-freq-min задаёт диапазон; взаимоисключимо с '
                 '--coupon-freq-in.'
        )
        parser.add_argument(
            '--coupon-freq-min', type=int, default=None,
            help='Минимальная частота купонов в год (023): «от квартальных до '
                 'ежемесячных» = --coupon-freq-min 4 --coupon-freq-max 12. '
                 'NULL/0-частота строгий диапазон не проходит; взаимоисключимо '
                 'с --coupon-freq-in.'
        )
        parser.add_argument(
            '--coupon-freq-in', type=_parse_coupon_freq_in, default=None,
            help='Только указанные частоты купонов через запятую, напр. 2,4 '
                 '(005.3/P-C). Взаимоисключимо с --coupon-freq-max.'
        )
        parser.add_argument(
            '--exclude-sovereign', action='store_true',
            help='Исключить суверенных эмитентов: ОФЗ и евро-РФ '
                 '(companies.entity_type=sovereign; 005.3/P-G).'
        )
        parser.add_argument('--min-ytm', type=float, default=None,
                           help='Минимальная YTM для фильтрации (в процентах)')
        parser.add_argument('--max-risk', type=int, default=3,
                           help='Максимальный уровень риска (1-5)')
        parser.add_argument(
            '--max-listlevel',
            type=int,
            choices=[1, 2, 3],
            default=2,
            help='Максимальный уровень листинга MOEX для buy-режима (1=высший, 3=третий). Default: 2'
        )
        parser.add_argument(
            '--max-ytm',
            type=float,
            default=35.0,
            help='Порог отсечения аномальной доходности (YTM ≥ значения = ВДО/дистресс). Default: 35.0'
        )
        # 002.3/P9.5: устаревшие дефолты дат ('2025-01-01' в прошлом и
        # '2030-12-31' — ловушка P3, отсекавшая топ-фиксы 2030+) заменены на
        # открытое окно: без явных флагов нижняя граница = сегодня (непогашенные),
        # верхней нет — сужать под срок пользователя осознанно вторым прогоном.
        parser.add_argument('--min-maturity', type=str, default=None,
                           help='Минимальная дата погашения (YYYY-MM-DD). '
                                'По умолчанию — только непогашенные бумаги '
                                '(maturity_date >= сегодня)')
        parser.add_argument('--max-maturity', type=str, default=None,
                           help='Максимальная дата погашения (YYYY-MM-DD). '
                                'По умолчанию верхней границы нет — широкое окно '
                                '(002.3/P9.5); скрининг «максимум дохода» начинать '
                                'с явного широкого окна, напр. --max-maturity 2036-12-31')
        parser.add_argument('--no-amortization', action='store_true',
                           help='Исключить облигации с амортизацией')
        parser.add_argument('--monthly-coupons', action='store_true',
                           help='Только облигации с ежемесячными купонами (12 раз в год)')
        parser.add_argument('--fixed-coupon', action='store_true',
                           help='Только облигации с фиксированным купоном')
        parser.add_argument('--floating-coupon', action='store_true',
                           help='Только облигации с плавающим купоном')
        parser.add_argument('--include-held', dest='include_held', action='store_true',
                           help='Режим buy: показывать и бумаги, уже лежащие в портфеле '
                                '(докупки), с бейджем held (доля N%%). По умолчанию '
                                'позиции портфеля исключены (002.2)')
        parser.add_argument('--limit', type=int, default=50,
                           help='Максимальное количество результатов')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'CalculateYtmUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(db=factory.get_db_connection())

    def execute(self, args: argparse.Namespace):
        # 005.3/R6 (P-F): в дефолтном режиме (без --verbose) логи — только
        # WARNING+ в stderr; INFO-поток (прогресс и «команда успешно
        # выполнена» из main.py) вклинивался в машинный вывод при
        # объединении потоков. Поднимаем корневой уровень ДО любых записей;
        # --verbose (main.py ставит DEBUG) не трогаем — там виден и INFO.
        if not getattr(args, 'verbose', False):
            logging.getLogger().setLevel(logging.WARNING)

        # Валидация до try: конфликт флагов — явная ошибка процесса (код 1,
        # понятное сообщение в stderr), а не проглоченная запись в лог
        # (конвенция rebalance-report 005.1).
        self._validate_args(args)

        fmt = getattr(args, 'format', 'text')
        logger.info(f"🚀 Запуск расчета YTM в режиме: {args.mode}")

        try:
            if fmt == 'json':
                self._execute_json(args)
            elif args.mode == 'buy':
                self._analyze_bonds_for_buying(args)
            elif args.mode == 'portfolio':
                self._analyze_portfolio_bonds(args)

        except Exception as e:
            if fmt == 'json':
                # Машинный контракт: stdout остаётся чистым (JSON печатается
                # одним куском только при успехе), ошибка уходит в stderr,
                # процесс — с ненулевым кодом.
                raise
            logger.exception(f"Ошибка при расчете YTM: {e}")

    def _validate_args(self, args: argparse.Namespace):
        """Валидация новых фильтров 005.3 (конвенции rebalance-report)."""
        freq_max = getattr(args, 'coupon_freq_max', None)
        freq_in = getattr(args, 'coupon_freq_in', None)
        freq_min = getattr(args, 'coupon_freq_min', None)
        if freq_max is not None and freq_in is not None:
            raise ValueError("--coupon-freq-max и --coupon-freq-in "
                             "взаимоисключающие: выберите один способ задания частот")
        if freq_min is not None and freq_in is not None:
            raise ValueError("--coupon-freq-min и --coupon-freq-in "
                             "взаимоисключающие: выберите один способ задания частот")
        if freq_max is not None and freq_max < 1:
            raise ValueError("--coupon-freq-max должен быть целым >= 1")
        if freq_min is not None and freq_min < 1:
            raise ValueError("--coupon-freq-min должен быть целым >= 1")
        if freq_min is not None and freq_max is not None and freq_min > freq_max:
            raise ValueError("--coupon-freq-min не должен превышать --coupon-freq-max")

    def _execute_json(self, args: argparse.Namespace):
        """Машинный режим --format json (005.3): сборка и печать канонического JSON.

        Ответ: schema_version, meta, filters (эхо применённых фильтров),
        excluded (воронка отсечений R9), candidates_total, count, bonds.
        """
        if args.mode == 'buy':
            bonds, funnel, candidates_total = self._collect_bonds(args, mode='buy')
        else:
            bonds, funnel, candidates_total = self._collect_bonds(args, mode='portfolio')

        logger.info("📈 Найдено %d облигаций", len(bonds))
        payload = self._build_json_payload(args, bonds, funnel, candidates_total)
        # JSON — в stdout; логи (logging) идут в stderr и ответ не портят.
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        logger.info("calculate-ytm JSON сформирован.")

    def _build_json_payload(
        self,
        args: argparse.Namespace,
        bonds: List[Dict[str, Any]],
        funnel: Dict[str, int],
        candidates_total: int,
    ) -> Dict[str, Any]:
        """Канонический JSON-ответ calculate-ytm (контракт согласован с
        rebalance-report: schema_version, единицы процентов, структура
        filters/excluded). Ключи строк — _BOND_JSON_KEYS (+_PORTFOLIO)."""
        mode = args.mode
        return {
            'schema_version': SCHEMA_VERSION,
            'meta': {
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'mode': mode,
            },
            'filters': {
                'mode': mode,
                'min_ytm_pct': args.min_ytm,
                'max_risk': args.max_risk,
                'max_listlevel': args.max_listlevel,
                'max_ytm_pct': args.max_ytm,
                'min_maturity': args.min_maturity,
                'max_maturity': args.max_maturity,
                'no_amortization': bool(args.no_amortization),
                'monthly_coupons': bool(args.monthly_coupons),
                'fixed_coupon': bool(args.fixed_coupon),
                'floating_coupon': bool(args.floating_coupon),
                'coupon_freq_max': getattr(args, 'coupon_freq_max', None),
                'coupon_freq_min': getattr(args, 'coupon_freq_min', None),
                'coupon_freq_in': getattr(args, 'coupon_freq_in', None),
                'exclude_sovereign': bool(getattr(args, 'exclude_sovereign', False)),
                'include_held': bool(getattr(args, 'include_held', False)),
                'limit': args.limit,
            },
            'excluded': funnel,
            'candidates_total': candidates_total,
            'count': len(bonds),
            'bonds': [self._bond_to_json(bond, mode) for bond in bonds],
        }

    @staticmethod
    def _bond_to_json(bond: Dict[str, Any], mode: str) -> Dict[str, Any]:
        """Строка бумаги в каноническом JSON (ключи общие с rebalance-report).

        Единицы: ytm_pct/coupon_pct — проценты годовых, price/avg_price —
        % от номинала, freq — раз/год, coupon_month_per_bond — ₽/мес на одну
        бумагу (у позиции портфеля SQL-колонка суммарная — нормируется на qty).
        """
        price_raw = bond.get('market_price') or bond.get('current_price')
        ytm = bond.get('ytm_percent')
        coupon_month = _to_float(bond.get('total_monthly_coupon_payment'))
        if mode == 'portfolio':
            qty = _to_float(bond.get('quantity')) or 0.0
            coupon_month = round(coupon_month / qty, 2) if (coupon_month is not None and qty > 0) else None

        row = {
            'isin': bond.get('isin'),
            'name': bond.get('name') or bond.get('isin'),
            'ticker': bond.get('ticker'),
            'price': round(_to_float(price_raw), 4) if price_raw is not None else None,
            'ytm_pct': round(float(ytm), 2) if ytm is not None else None,
            'ytm_reason': bond.get('ytm_reason'),
            'coupon_pct': round(_to_float(bond.get('coupon_rate_percent')), 2)
            if bond.get('coupon_rate_percent') is not None else None,
            'freq': _to_int(bond.get('coupon_quantity_per_year')),
            'coupon_kind': 'float' if bond.get('floating_coupon_flag') else 'fix',
            'risk_level': _to_int(bond.get('risk_level')),
            'list_level': _to_int(bond.get('list_level')),
            'maturity': _to_iso_day(bond.get('maturity_date')),
            'amort': bool(bond.get('amortization_flag')),
            'ku': bool(bond.get('ku')),
            'issuer': bond.get('issuer') or bond.get('name') or bond.get('isin'),
            'coupon_month_per_bond': coupon_month,
            'held_badge': bond.get('held_badge'),
        }
        if mode == 'portfolio':
            row['qty'] = round(_to_float(bond.get('quantity')) or 0.0, 4)
            avg_price = _to_float(bond.get('average_price'))
            row['avg_price'] = round(avg_price, 4) if avg_price is not None else None
            price_change = _to_float(bond.get('price_change_percent'))
            row['price_change_pct'] = round(price_change, 2) if price_change is not None else None
        return row

    def _analyze_bonds_for_buying(self, args: argparse.Namespace):
        """Анализ облигаций для покупки по текущей рыночной цене."""
        logger.info("📊 Анализ облигаций для покупки...")

        bonds = self._get_bonds_for_buying(args)
        if not bonds:
            logger.info("ℹ️ Не найдено облигаций, соответствующих критериям.")
            return

        logger.info(f"📈 Найдено {len(bonds)} облигаций для анализа")
        self._print_bonds_table(bonds, "Облигации для покупки (по текущей цене)")

    def _analyze_portfolio_bonds(self, args: argparse.Namespace):
        """Анализ уже купленных облигаций по средней цене покупки."""
        logger.info("💼 Анализ облигаций в портфеле...")

        bonds = self._get_portfolio_bonds(args)
        if not bonds:
            logger.info("ℹ️ В портфеле нет облигаций, соответствующих критериям.")
            return

        logger.info(f"📈 Найдено {len(bonds)} облигаций в портфеле")
        self._print_bonds_table(bonds, "Облигации в портфеле (mark-to-market YTM)")

    def _enrich_bonds_with_ytm(
        self,
        bonds: List[Dict[str, Any]],
        args: argparse.Namespace,
        valuation_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        """
        Обогащает строки канонической YTM:
        - Если ytm_updated_at задан: берёт сохранённый bc.ytm или bc.ytm_null_reason.
        - Если ytm_updated_at IS NULL (до backfill): временный расчёт через общий cashflow-решатель.
        - Единицы (005.4, миграция 013): bc.ytm хранится в ПРОЦЕНТАХ годовых и
          берётся как есть; движок cashflow-решателя возвращает ДОЛЮ —
          расчёт на лету переводится в проценты (100 * y).
        - Фильтрует по args.min_ytm без приведения NULL к 0.

        valuation_date (030.3): дата расчёта YTM «на лету» (valuation_date
        cashflow-решателя). None — прежнее поведение (date.today(); legacy
        report). PlanningContext передаёт свой frozen as_of_date — скрытый
        второй clock на plan/compare path устранён: replay не пересчитывает
        YTM по настенным часам.
        """
        enriched = []
        fallback_valuation_date = (
            valuation_date if valuation_date is not None else date.today()
        )
        for bond in bonds:
            if bond.get('ytm_updated_at') is not None:
                if bond.get('ytm') is not None:
                    # bc.ytm уже в процентах годовых (005.4, миграция 013)
                    bond['ytm_percent'] = float(bond['ytm'])
                    bond['ytm_reason'] = None
                else:
                    bond['ytm_percent'] = None
                    bond['ytm_reason'] = bond.get('ytm_null_reason') or 'not_applicable'
            else:
                # Временная совместимость до выполнения общего backfill
                price = bond.get('market_price') or bond.get('current_price')
                metrics = calculate_bond_cashflow_metrics(
                    valuation_date=fallback_valuation_date,
                    nominal=bond.get('nominal'),
                    maturity_date=bond.get('maturity_date'),
                    coupon_quantity_per_year=bond.get('coupon_quantity_per_year'),
                    coupon_rate_percent=bond.get('coupon_rate_percent'),
                    market_price=price,
                    perpetual_flag=bond.get('perpetual_flag'),
                    floating_coupon_flag=bond.get('floating_coupon_flag'),
                    amortization_flag=bond.get('amortization_flag'),
                )
                if metrics.ytm is not None:
                    # движок возвращает долю единицы — в выводе проценты
                    bond['ytm_percent'] = float(metrics.ytm) * 100.0
                    bond['ytm_reason'] = None
                else:
                    bond['ytm_percent'] = None
                    bond['ytm_reason'] = metrics.null_reason

            if args.min_ytm is not None:
                if bond['ytm_percent'] is None or bond['ytm_percent'] < args.min_ytm:
                    continue

            enriched.append(bond)
        return enriched

    @staticmethod
    def _pool_limit(args: argparse.Namespace) -> int:
        """Размер SQL-пула выборки (005.3).

        Текстовый режим без питон-фильтров выдачи — args.limit (прежнее
        поведение и производительность). Когда активны фильтры
        частоты/суверенных или запрошен json-режим, пул расширяется до
        _FILTER_POOL_LIMIT: --limit применяется к итоговой выдаче (кейс
        «квартальные максимум» без пост-фильтрации), а воронка отсечений
        честна — в том числе причина «обрезано лимитом» (кейс ППК РЭО 01,
        P-I): без расширения строки за SQL-LIMIT невидимы.
        """
        filters_active = (
            getattr(args, 'coupon_freq_max', None) is not None
            or getattr(args, 'coupon_freq_min', None) is not None
            or getattr(args, 'coupon_freq_in', None) is not None
            or bool(getattr(args, 'exclude_sovereign', False))
        )
        json_mode = getattr(args, 'format', 'text') == 'json'
        if filters_active or json_mode:
            return max(args.limit, _FILTER_POOL_LIMIT)
        return args.limit

    def _collect_bonds(
        self, args: argparse.Namespace, mode: str
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int], int]:
        """Единая выборка обоих режимов с воронкой отсечений (005.3/R9).

        Конвейер: SQL-выборка движка (прежние SQL-пороги риска/листинга/YTM
        и окно погашения) -> фильтры выдачи в Python (суверенные P-G, частота
        P-C — до обогащения, они не зависят от YTM) -> каноническая YTM +
        --min-ytm (движок) -> повтор порога --max-ytm на рассчитанном значении
        (только buy, как раньше; в portfolio дистресс-позиции показываются
        с бейджем «⚠️ ВДО/Дистресс») -> срез до --limit.

        Возвращает (строки, воронка excluded, candidates_total). SQL-стадии
        (риск/листинг/окно/хранимый потолок YTM) отсекают до выборки и в
        воронку не попадают — в JSON их значения видны в блоке filters.
        """
        funnel: Dict[str, int] = {key: 0 for key in _EXCLUDED_KEYS}
        rows = self.db.get_bonds_yield_table(
            mode=mode,
            min_maturity=args.min_maturity,
            max_maturity=args.max_maturity,
            max_risk=args.max_risk,
            no_amortization=args.no_amortization,
            monthly_coupons=args.monthly_coupons,
            fixed_coupon=args.fixed_coupon,
            floating_coupon=args.floating_coupon,
            limit=self._pool_limit(args),
            # --max-listlevel исторически действует только в buy-режиме
            # (в portfolio прежде уходил SQL-дефолт 2) — сохраняем семантику.
            max_listlevel=args.max_listlevel if mode == 'buy' else 2,
            max_ytm=args.max_ytm,
            include_held=getattr(args, 'include_held', False),
        )
        candidates_total = len(rows)

        # P-G: суверенные (ОФЗ + евро-РФ) — общее правило тулкита.
        if getattr(args, 'exclude_sovereign', False):
            kept = [row for row in rows if not is_sovereign(row)]
            funnel['sovereign'] = len(rows) - len(kept)
            rows = kept

        # P-C: частота купонов — общее правило тулкита (NULL не проходит);
        # 023: min+max задают диапазон.
        freq_max = getattr(args, 'coupon_freq_max', None)
        freq_in = getattr(args, 'coupon_freq_in', None)
        freq_min = getattr(args, 'coupon_freq_min', None)
        if freq_max is not None or freq_in is not None or freq_min is not None:
            kept = [
                row for row in rows
                if freq_ok(_to_int(row.get('coupon_quantity_per_year')),
                           freq_max=freq_max, freq_in=freq_in, freq_min=freq_min)
            ]
            funnel['freq'] = len(rows) - len(kept)
            rows = kept

        before_enrich = len(rows)
        rows = self._enrich_bonds_with_ytm(rows, args)
        if args.min_ytm is not None:
            funnel['min_ytm'] = before_enrich - len(rows)

        if mode == 'buy':
            # SQL-фильтр --max-ytm действует на хранимую bc.ytm и пропускает
            # строки до backfill (ytm_updated_at IS NULL), чья YTM считается
            # на лету выше; повторяем порог на рассчитанном значении, чтобы
            # аномальные доходности не попадали в buy-вывод.
            kept = [
                row for row in rows
                if row['ytm_percent'] is None or row['ytm_percent'] <= args.max_ytm
            ]
            funnel['max_ytm'] = len(rows) - len(kept)
            rows = kept

        # --limit — к итоговой выдаче: играет роль только при расширенном
        # пуле (иначе SQL LIMIT уже отрезал топ).
        if len(rows) > args.limit:
            funnel['limit_truncated'] = len(rows) - args.limit
            rows = rows[:args.limit]

        # R9: применённые фильтры и воронка — в лог (INFO виден под -v) и
        # в JSON-блок excluded.
        excluded_total = sum(funnel.values())
        logger.info(
            "Применённые фильтры: mode=%s, coupon-freq-min=%s, coupon-freq-max=%s, "
            "coupon-freq-in=%s, exclude-sovereign=%s, min-ytm=%s, max-risk=%s, "
            "max-listlevel=%s, max-ytm=%s, limit=%s",
            mode,
            freq_min,
            getattr(args, 'coupon_freq_max', None),
            getattr(args, 'coupon_freq_in', None),
            bool(getattr(args, 'exclude_sovereign', False)),
            args.min_ytm, args.max_risk, args.max_listlevel,
            args.max_ytm, args.limit,
        )
        if excluded_total:
            logger.info(
                "Исключено %d бумаг по причинам: суверенные — %d, частота — %d, "
                "min-ytm — %d, max-ytm — %d; обрезано лимитом выдачи — %d",
                excluded_total,
                funnel['sovereign'], funnel['freq'], funnel['min_ytm'],
                funnel['max_ytm'], funnel['limit_truncated'],
            )
        return rows, funnel, candidates_total

    def _get_bonds_for_buying(self, args: argparse.Namespace) -> List[Dict[str, Any]]:
        """Получает облигации для покупки с канонической YTM по рыночной цене."""
        bonds, _funnel, _total = self._collect_bonds(args, mode='buy')
        return bonds

    def _get_portfolio_bonds(self, args: argparse.Namespace) -> List[Dict[str, Any]]:
        """Получает облигации из портфеля с канонической mark-to-market YTM."""
        bonds, _funnel, _total = self._collect_bonds(args, mode='portfolio')
        return bonds

    def _print_bonds_table(self, bonds: List[Dict[str, Any]], title: str):
        """Выводит таблицу с облигациями в консоль."""
        if not bonds:
            return

        print(f"\n{'='*80}")
        print(f"📊 {title}")
        print(f"{'='*80}")

        if 'average_price' in bonds[0]:
            # Режим портфеля
            print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<26} | {'Номинал':<7} | {'Купон %':<6} | "
                  f"{'Частота':<7} | {'Погашение':<10} | {'Риск':<4} | {'Лист':<4} | {'Амортиз.':<8} | "
                  f"{'Ср.цена':<8} | {'Тек.цена':<8} | {'Кол-во':<6} | {'YTM %':<24} | {'Измен.цены %':<11} | {'Купон/мес':<10} | {'Тип купона':<12}")
            print("-" * 180)

            for i, bond in enumerate(bonds):
                try:
                    nominal = float(bond['nominal']) if bond.get('nominal') else 0
                    coupon_rate = float(bond['coupon_rate_percent']) if bond.get('coupon_rate_percent') else 0
                    coupon_freq = int(float(bond['coupon_quantity_per_year'])) if bond.get('coupon_quantity_per_year') else 0
                    avg_price = float(bond['average_price']) if bond.get('average_price') else 0
                    curr_price = float(bond.get('market_price') or bond.get('current_price') or 0)
                    quantity = float(bond['quantity']) if bond.get('quantity') else 0
                    price_change = float(bond['price_change_percent']) if bond.get('price_change_percent') else 0
                    total_monthly_coupon = float(bond['total_monthly_coupon_payment']) if bond.get('total_monthly_coupon_payment') else 0

                    ytm = bond.get('ytm_percent')
                    if ytm is not None:
                        ytm_str = f"{ytm:.2f}%"
                        if ytm > 30:
                            ytm_str += " ⚠️ ВДО/Дистресс"
                    else:
                        reason = bond.get('ytm_reason') or 'Н/Д'
                        ytm_str = f"— ({reason})"

                    print(f"{bond['isin']:<12} | "
                          f"{bond['ticker'] or 'N/A':<8} | "
                          f"{(bond['name'] or 'N/A')[:26]:<26} | "
                          f"{nominal:<7.0f} | "
                          f"{coupon_rate:<6.1f} | "
                          f"{coupon_freq:<7} | "
                          f"{str(bond['maturity_date']) if bond.get('maturity_date') else 'N/A':<10} | "
                          f"{bond['risk_level'] if bond.get('risk_level') is not None else 'N/A':<4} | "
                          f"{bond['list_level'] if bond.get('list_level') is not None else 'N/A':<4} | "
                          f"{'Да' if bond.get('amortization_flag') else 'Нет':<8} | "
                          f"{avg_price:<8.2f} | "
                          f"{curr_price:<8.2f} | "
                          f"{quantity:<6.0f} | "
                          f"{ytm_str:<24} | "
                          f"{price_change:<11.2f} | "
                          f"{total_monthly_coupon:<10.0f} | "
                          f"{bond.get('coupon_type', 'N/A'):<12}")
                except Exception as e:
                    print(f"Ошибка при форматировании строки {i}: {e}")
                    print(f"Данные: {bond}")
                    continue
        else:
            # Режим покупки
            print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<26} | {'Номинал':<7} | {'Купон %':<6} | "
                  f"{'Частота':<7} | {'Погашение':<10} | {'Риск':<4} | {'Лист':<4} | {'Амортиз.':<8} | "
                  f"{'Тек.цена':<8} | {'YTM %':<24} | {'Купон/мес':<10} | {'Тип купона':<12} | {'Портфель':<15}")
            print("-" * 173)

            for bond in bonds:
                try:
                    nominal = float(bond['nominal']) if bond.get('nominal') else 0
                    coupon_rate = float(bond['coupon_rate_percent']) if bond.get('coupon_rate_percent') else 0
                    coupon_freq = int(float(bond['coupon_quantity_per_year'])) if bond.get('coupon_quantity_per_year') else 0
                    curr_price = float(bond.get('market_price') or bond.get('current_price') or 0)
                    total_monthly_coupon = float(bond['total_monthly_coupon_payment']) if bond.get('total_monthly_coupon_payment') else 0

                    ytm = bond.get('ytm_percent')
                    if ytm is not None:
                        ytm_str = f"{ytm:.2f}%"
                        if ytm > 30:
                            ytm_str += " ⚠️ ВДО/Дистресс"
                    else:
                        reason = bond.get('ytm_reason') or 'Н/Д'
                        ytm_str = f"— ({reason})"

                    print(f"{bond['isin']:<12} | "
                          f"{bond['ticker'] or 'N/A':<8} | "
                          f"{(bond['name'] or 'N/A')[:26]:<26} | "
                          f"{nominal:<7.0f} | "
                          f"{coupon_rate:<6.1f} | "
                          f"{coupon_freq:<7} | "
                          f"{str(bond['maturity_date']) if bond.get('maturity_date') else 'N/A':<10} | "
                          f"{bond['risk_level'] if bond.get('risk_level') is not None else 'N/A':<4} | "
                          f"{bond['list_level'] if bond.get('list_level') is not None else 'N/A':<4} | "
                          f"{'Да' if bond.get('amortization_flag') else 'Нет':<8} | "
                          f"{curr_price:<8.2f} | "
                          f"{ytm_str:<24} | "
                          f"{total_monthly_coupon:<10.0f} | "
                          f"{bond.get('coupon_type', 'N/A'):<12} | "
                          f"{bond.get('held_badge') or '':<15}")
                except Exception as e:
                    print(f"Ошибка при форматировании строки: {e}")
                    print(f"Данные: {bond}")
                    continue

        print(f"{'='*80}")
        print(f"📊 Всего облигаций: {len(bonds)}")
        print(f"💡 YTM % — каноническая доходность к погашению (mark-to-market по рыночной чистой цене).")
        print(f"💡 Для флоатеров, бессрочных и амортизируемых бумаг отображается «— (причина)» без упрощенных формул.")
        if 'average_price' in bonds[0]:
            print(f"💡 Ср.цена — средняя цена покупки портфеля (справочная колонка, не подменяет YTM).")
        print(f"{'='*80}\n")
