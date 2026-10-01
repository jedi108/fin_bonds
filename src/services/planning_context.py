"""
PlanningContext — один согласованный срез данных для plan/compare (030.3).

Реальный сбой, от которого защищает контекст: report price = 951.30 → LLM
посчитала qty → следующий scenario price = 1103.06 → POSITION_VALUE_LIMIT /
budget failure. Plan обязан считаться целиком по одному срезу, поэтому
PlanningContextBuilder собирает НЕИЗМЕНЯЕМЫЙ контекст ОДИН раз на
plan/compare-run:

- в одной read-only транзакции REPEATABLE READ (гарантия snapshot'а БД,
  READ COMMITTED её не даёт) — `PortfolioStorage.snapshot_transaction`;
- один frozen `as_of` — транзакционный timestamp (CURRENT_TIMESTAMP в
  PostgreSQL зафиксирован на старте транзакции, как и CURRENT_DATE), а не
  wall-clock Python'а; все SQL с CURRENT_DATE/CURRENT_TIMESTAMP внутри той
  же транзакции (maturity-границы, окно 24h market-price freshness buy-path)
  считаются на этом же as_of;
- скрытый второй clock устранён: временный YTM fallback получает
  PlanningContext.as_of_date, окно свежести `get_db_freshness` тоже
  параметризовано as_of;
- canonical data-freshness gate — `classify_freshness` (тот же, что у
  check-db и rebalance-report; второго набора thresholds нет);
- canonical raw universe ДО variant-specific screening: та же широкая
  buy-выборка, что у legacy screener (_SCREENER_* константы — единственная
  копия определения), поэтому relaxed/strict варианты 030.9 скринируют один
  набор исходных строк, и strict не удаляет заранее строки, нужные relaxed;
- effective prices — цена расчёта и basis на каждый ISIN среза, тем же
  калькулятором, что и вселенная сценария (RebalanceEngine.planning_price →
  _make_scenario_state; второго расчёта цен нет). Это и есть eligibility,
  зафиксированная в контексте: replay не пересчитывает ни цены, ни свежесть
  по wall-clock.

Три сущности свежести НЕ смешиваются (контракт 030.3):
1. data_gate          — свежи ли исходные portfolio/catalog/cash;
2. instrument eligibility — свежа ли конкретная цена на context.as_of
   (зафиксирована в effective_prices/universe_rows);
3. context TTL        — не слишком ли стар сам frozen context для replay
   (забота 030.4, здесь только детерминизм содержимого).
Новый контекст из устаревшей БД остаётся stale по data_gate; `feasible`
(финансовая ось движка) и data gate — разные поля разных объектов и в
контексте не конвертируются друг в друга.

`context_fingerprint` — детерминированный sha256 канонической сериализации
ИСХОДНЫХ calculation data + as_of (+ schema/policy версии и data_gate):
Decimal → детерминированная decimal string (не float), datetime/date →
canonical ISO/UTC, коллекции строк сортируются по ISIN (presentation order
исключён), generated_at и сам fingerprint исключены. Intents/constraints
варианта в контекст не входят — сценарии с разными thresholds на одном
рыночном срезе сравнимы (030.9).

Не делается (границы 030.3): persistent snapshot table/database, фиксация
цен между разными пользовательскими запросами, context_id/TTL/store (030.4),
eligibility-гейт покупок (030.6), intents (030.5). Программный API: immutable
PlanningContext передаётся напрямую в одном Python-процессе, без serialization.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Tuple

from src.services.rebalance_engine import RebalanceEngine, _to_float
from src.storage import PortfolioStorage
from src.use_cases.check_db import classify_freshness
from src.use_cases.rebalance_report import (
    _SCREENER_MAX_LISTLEVEL,
    _SCREENER_MAX_RISK,
    _SCREENER_POOL_LIMIT,
)

# Версия контракта PlanningContext (структура полей контекста).
CONTEXT_SCHEMA_VERSION = 1

# Версия расчётной политики среза: семантика effective prices/basis —
# canonical engine 030.2 (held_valuation/market_price/nominal_fallback/
# matured_zero), YTM — расчётный движок calculate-ytm (005.1).
CALCULATION_POLICY_VERSION = '1'

# Политика канонического raw universe ДО variant-specific screening: та же
# широкая buy-выборка, что у legacy screener'а (широкие технические пороги
# риска/листинга, потолок YTM открыт — продуктовые фильтры применяются
# вариантами screening позже, в Python, по одним и тем же строкам).
RAW_UNIVERSE_POLICY: Dict[str, Any] = {
    'mode': 'buy',
    'min_maturity': None,
    'max_maturity': None,
    'max_risk': _SCREENER_MAX_RISK,
    'max_listlevel': _SCREENER_MAX_LISTLEVEL,
    'max_ytm': 10 ** 9,
    'include_held': True,
    'limit': _SCREENER_POOL_LIMIT,
}


@dataclass(frozen=True)
class DataGateState:
    """Canonical data-freshness gate среза (1 из 3 осей свежести).

    Тот же контракт, что `meta.data_freshness` rebalance-report:
    classify_freshness → (criticals, warnings, gate_status, is_fresh);
    status — сводная метка CRITICAL/WARNING/OK. Здесь НЕТ ни feasible, ни
    eligibility, ни TTL — это только «можно ли доверять исходным данным».
    """

    status: str
    gate_status: str
    is_fresh: bool
    criticals: Tuple[str, ...]
    warnings: Tuple[str, ...]

    @classmethod
    def from_classify(
        cls,
        criticals: Tuple[str, ...],
        warnings: Tuple[str, ...],
        gate_status: str,
        is_fresh: bool,
    ) -> 'DataGateState':
        """Сводная метка — тем же правилом, что meta.status у report."""
        status = 'CRITICAL' if criticals else ('WARNING' if warnings else 'OK')
        return cls(
            status=status,
            gate_status=gate_status,
            is_fresh=is_fresh,
            criticals=tuple(criticals),
            warnings=tuple(warnings),
        )


@dataclass(frozen=True)
class CashSnapshot:
    """Canonical cash среза (022): unknown cash не превращается в 0."""

    cash_available_rub: Optional[Decimal]
    cash_known: bool


@dataclass(frozen=True)
class PlanningContext:
    """Immutable срез данных одного plan/compare-run (030.3).

    Поля-коллекции — tuple строк БД (dict); контекст не мутируется: builder
    и потребители не переписывают содержимое. Деньги в строках —
    Decimal БД; effective_prices — canonical float-цены движка (030.2) —
    релей, а не пересчёт (float-арифметики/сравнений в контексте нет).
    """

    context_schema_version: int
    calculation_policy_version: str
    # Frozen as_of: транзакционный timestamp среза и его дата.
    as_of: datetime
    as_of_date: date
    # Wall-clock сборки контекста — только для observability, в fingerprint
    # НЕ входит.
    generated_at: datetime
    positions_updated_at: Optional[datetime]
    cash_updated_at: Optional[datetime]
    # Ось «можно ли доверять исходным данным» — отдельна от feasible/TTL.
    data_gate: DataGateState
    cash: CashSnapshot
    # Текущие позиции (агрегат по ISIN, каноническая оценка view).
    held_rows: Tuple[Dict[str, Any], ...]
    # Canonical raw universe до variant-specific screening (включая held —
    # exclude-held это политика варианта, а не property строк).
    universe_rows: Tuple[Dict[str, Any], ...]
    # Цена расчёта + basis на каждый ISIN среза на as_of (eligibility,
    # зафиксированная в контексте; replay не пересчитывает её по wall-clock).
    effective_prices: Dict[str, Dict[str, Any]]
    # Политика raw universe (metadata фильтров среза).
    universe_policy: Dict[str, Any]

    @property
    def context_fingerprint(self) -> str:
        """Детерминированный hash исходных calculation data + as_of."""
        return compute_context_fingerprint(self)


# ---------------------------------------------------------------------------
# Детерминированная каноническая сериализация и fingerprint
# ---------------------------------------------------------------------------


def canonical_decimal_str(value: Decimal) -> str:
    """Decimal → детерминированная decimal string (не float).

    Хвостовые нули/масштаб не влияют ('951.30' == '951.3'), экспоненциальная
    форма разворачивается ('1E+2' → '100'), '-0' канонизируется в '0'.
    """
    normalized = value.normalize()
    if normalized == 0:
        normalized = Decimal(0)
    return format(normalized, 'f')


def canonical_datetime_str(value: datetime) -> str:
    """datetime → canonical ISO с таймзоной; naive трактуется как UTC
    (конвенция проекта в _fmt_dt/_format_dt), прочие зоны приводятся к UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _canonical(value: Any) -> Any:
    """Рекурсивная канонизация значения в JSON-примитивы.

    Однозначное представление None/bool/int/string (bool проверяется раньше
    int); Decimal → decimal string; datetime/date → canonical ISO; float →
    repr (детерминированный shortest round-trip; новые planning-данные
    должны приходить Decimal'ами); dict — сортировка ключей; list/tuple —
    порядок сохраняется (сортировка строк-коллекций по ISIN — выше, на
    уровне payload).
    """
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, Decimal):
        return canonical_decimal_str(value)
    if isinstance(value, datetime):
        return canonical_datetime_str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return str(value)


def _sorted_by_isin(
    rows: Tuple[Dict[str, Any], ...]
) -> Tuple[Dict[str, Any], ...]:
    """Стабильная сортировка строк-коллекций по ISIN (presentation order
    исключён из fingerprint)."""
    return tuple(sorted(rows, key=lambda row: str(row.get('isin') or '')))


def canonical_calculation_payload(context: PlanningContext) -> Dict[str, Any]:
    """Canonical payload fingerprint'а: исходные calculation data + as_of.

    Включены: все данные среза, которые могут изменить расчёт, + версии
    schema/policy + as_of + data_gate + provenance-метки (positions/cash
    updated_at). Исключены: generated_at (wall-clock, на расчёт не влияет) и
    сам fingerprint (вычисляется из payload — цикла нет по построению).
    Intents/constraints варианта в контексте отсутствуют by design.
    """
    return {
        'context_schema_version': context.context_schema_version,
        'calculation_policy_version': context.calculation_policy_version,
        'as_of': canonical_datetime_str(context.as_of),
        'as_of_date': context.as_of_date.isoformat(),
        'data_gate': {
            'status': context.data_gate.status,
            'gate_status': context.data_gate.gate_status,
            'is_fresh': context.data_gate.is_fresh,
            'criticals': list(context.data_gate.criticals),
            'warnings': list(context.data_gate.warnings),
        },
        'cash': {
            'cash_available_rub': _canonical(context.cash.cash_available_rub),
            'cash_known': context.cash.cash_known,
        },
        'positions_updated_at': (
            canonical_datetime_str(context.positions_updated_at)
            if context.positions_updated_at is not None else None
        ),
        'cash_updated_at': (
            canonical_datetime_str(context.cash_updated_at)
            if context.cash_updated_at is not None else None
        ),
        'universe_policy': _canonical(context.universe_policy),
        'held_rows': _canonical(_sorted_by_isin(context.held_rows)),
        'universe_rows': _canonical(_sorted_by_isin(context.universe_rows)),
        'effective_prices': _canonical(context.effective_prices),
    }


def compute_context_fingerprint(context: PlanningContext) -> str:
    """sha256 канонической сериализации payload (детерминированный)."""
    payload = json.dumps(
        canonical_calculation_payload(context),
        sort_keys=True,
        ensure_ascii=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class PlanningContextBuilder:
    """Собирает PlanningContext — один раз на plan/compare-run (030.3).

    Чтение — в одной транзакции `PortfolioStorage.snapshot_transaction`
    (REPEATABLE READ READ ONLY): позиции, cash, raw universe, freshness и
    транзакционный timestamp видят один snapshot БД. Effective prices и YTM
    считаются каноническими движками от строк этого же среза (копий расчёта
    нет), maturity-guard и YTM fallback — на frozen as_of_date.
    """

    def __init__(self, db: PortfolioStorage, rating_scale: Optional[Dict[str, int]] = None):
        self.db = db
        # Единый расчётный движок (правило 030: один calculator): YTM-enrichment
        # и planning prices контекста — его методы, собственного пересчёта нет.
        self._engine = RebalanceEngine(db, rating_scale=rating_scale)

    def build(self) -> PlanningContext:
        with self.db.snapshot_transaction():
            tx = self.db.get_transaction_timestamp()
            as_of: datetime = tx['ts']
            as_of_date: date = tx['day']

            freshness = self.db.get_db_freshness(now=as_of)
            criticals, warnings, gate_status, is_fresh = classify_freshness(
                freshness, now=as_of
            )
            cash = self.db.get_available_cash_rub()
            held_rows = self.db.get_rebalance_portfolio_rows()
            universe_rows = self.db.get_bonds_yield_table(**RAW_UNIVERSE_POLICY)

            # YTM (включая временный on-the-fly fallback) — на frozen as_of:
            # replay не пересчитывает YTM по wall-clock.
            held_rows = self._engine.enrich_with_ytm(held_rows, as_of_date=as_of_date)
            universe_rows = self._engine.enrich_with_ytm(
                universe_rows, as_of_date=as_of_date
            )

        effective_prices: Dict[str, Dict[str, Any]] = {}
        for row in held_rows:
            effective_prices[row['isin']] = self._engine.planning_price(
                row,
                qty_before=_to_float(row.get('quantity')) or 0.0,
                value_now=_to_float(row.get('value_rub')),
                as_of_date=as_of_date,
                is_held=True,
            )
        for row in universe_rows:
            # Held ISIN попадает в raw universe тоже (include_held=True) —
            # цена held-позиции каноничнее (view-оценка), дублей нет.
            if row['isin'] in effective_prices:
                continue
            effective_prices[row['isin']] = self._engine.planning_price(
                row,
                qty_before=0.0,
                value_now=None,
                as_of_date=as_of_date,
                is_held=False,
            )

        return PlanningContext(
            context_schema_version=CONTEXT_SCHEMA_VERSION,
            calculation_policy_version=CALCULATION_POLICY_VERSION,
            as_of=as_of,
            as_of_date=as_of_date,
            generated_at=datetime.now(timezone.utc),
            positions_updated_at=freshness.get('portfolio_max_updated_at'),
            cash_updated_at=cash.get('cash_updated_at'),
            data_gate=DataGateState.from_classify(
                criticals, warnings, gate_status, is_fresh
            ),
            cash=CashSnapshot(
                cash_available_rub=cash.get('cash_available_rub'),
                cash_known=bool(cash.get('cash_known')),
            ),
            held_rows=tuple(held_rows),
            universe_rows=tuple(universe_rows),
            effective_prices=effective_prices,
            universe_policy=dict(RAW_UNIVERSE_POLICY),
        )
