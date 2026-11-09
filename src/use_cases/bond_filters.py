"""
Переиспользуемые фильтры бумаг Python-тулкита (задачи 005.1/005.3, 023).

Одна реализация для rebalance-report (--freq-min/--freq-max/--freq-in/
--exclude-sovereign) и calculate-ytm (--coupon-freq-min/--coupon-freq-max/
--coupon-freq-in/--exclude-sovereign), чтобы конвенции фильтров команд
совпадали:
- P-C: частота купонов — «от N/год» и/или «максимум N/год» (диапазон)
  или «только из списка»;
- P-G: суверенные эмитенты — ОФЗ и евро-РФ.

Фильтры применяются к строкам выборки движка calculate-ytm (dict) в Python —
это даёт честную воронку «сколько отсечено и почему» (R9) и одинаковые
правила в обеих командах.

030.6 — модуль дополнен каноническими правилами structural BUY gate и
построчной policy-политикой скринера; потребитель правил — три:
хранилище (SQL-фрагменты buy-выборки), скринер отчёта (построчные policy-
правила) и eligibility-гейт планирования (src/services/buy_eligibility.py).
Модуль намеренно не импортирует ничего из проекта (только stdlib): его
импортирует src/storage.py, поэтому никакой обратной зависимости к
движку/хранилищу здесь быть не может.
"""
import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, List, Mapping, NamedTuple, Optional, Tuple

# ---------------------------------------------------------------------------
# 030.6: канонические константы structural BUY gate.
#
# Единственная копия определения: из них собран и SQL buy-выборки
# хранилища (get_bonds_yield_table mode='buy'), и построчная структурная
# оценка (STRUCTURAL_BUY_GATES ниже); ручных копий значений в коде нет.
# ---------------------------------------------------------------------------

# RUB buy-universe (bc.currency = 'rub' в canonical buy-path).
BUY_UNIVERSE_CURRENCY = 'rub'

# Окно свежести market_price кандидата (018): время записи цены в БД,
# известное ограничение задачи 009.
MARKET_PRICE_FRESHNESS_HOURS = 24

# Широкие технические пределы пула кандидатов (риск 1-5, листинг 1-3):
# SQL-пороги buy-выборки открываются на максимум шкал, продуктовые фильтры
# (max_risk/max_listlevel конкретного запроса) применяются позже в Python —
# у скринера и у planner'а по одним и тем же строкам. Прежние имена в
# rebalance_report (_SCREENER_MAX_RISK/_SCREENER_MAX_LISTLEVEL) — реэкспорт.
POOL_MAX_RISK = 5
POOL_MAX_LISTLEVEL = 3


# ---------------------------------------------------------------------------
# Внутренние приведения (локальные: модуль не зависит от движка/хранилища)
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def _as_float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _as_date(value: Any) -> Optional[date]:
    """date/datetime/'YYYY-MM-DD' -> date; None проходит."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _as_utc(value: Any) -> Optional[datetime]:
    """datetime/date -> aware UTC (naive трактуется как UTC — конвенция
    проекта; date трактуется как полночь UTC — frozen as_of_date контекста)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


def _iso_day(value: Any) -> Optional[str]:
    day = _as_date(value)
    return day.isoformat() if day is not None else None


# ---------------------------------------------------------------------------
# 030.6: structural BUY gates — одна таблица для SQL и Python.
#
# Фактические gates канонического buy-path (030.1): currency RUB;
# is_trade_available false не проходит (true OR null проходит — не ужесточать
# молча); perpetual не проходит; maturity не в прошлом; coupon data по
# canonical rule (COALESCE фиксированной ставки с последней ставкой
# флоатера — canonical raw-строки несут уже COALESCE-значение); market_price
# существует и > 0 (номинал подменой не служит, 018); market_price_updated_at
# в freshness window; широкие технические пределы пула.
#
# Правило 030 №1 (extract, не копия): sql — WHERE-фрагмент buy-выборки
# хранилища, violation(row, as_of_date) — ТОТ ЖЕ гейт на canonical raw-строке
# (для structural-оценки ISIN вне пула). Второго набора «почти одинаковых
# условий» нет: изменился гейт — изменилась одна строка таблицы.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructuralBuyGate:
    """Один structural gate канонического buy-path (030.6).

    sql — условие WHERE buy-выборки (alias bc); violation — тот же гейт в
    Python на canonical raw-строке: возвращает (actual, limit) при нарушении,
    None — гейт пройден. as_of_date — frozen as_of PlanningContext (в SQL
    ту же роль играет CURRENT_DATE той же snapshot-транзакции, 030.3).
    """

    code: str
    sql: str
    violation: Callable[[Mapping, Any], Optional[Tuple[Any, Any]]]


# Коды нарушений structural gate (машиночитаемые, контракт 030.6).
REASON_UNSUPPORTED_CURRENCY = 'UNSUPPORTED_CURRENCY'
REASON_TRADE_UNAVAILABLE = 'TRADE_UNAVAILABLE'
REASON_MATURED = 'MATURED'
REASON_PERPETUAL_NOT_BUYABLE = 'PERPETUAL_NOT_BUYABLE'
REASON_COUPON_DATA_MISSING = 'COUPON_DATA_MISSING'
REASON_MARKET_PRICE_MISSING = 'MARKET_PRICE_MISSING'
REASON_MARKET_PRICE_STALE = 'MARKET_PRICE_STALE'
REASON_RISK_OUT_OF_POOL = 'RISK_OUT_OF_POOL'
REASON_LIST_LEVEL_OUT_OF_POOL = 'LIST_LEVEL_OUT_OF_POOL'


def _currency_violation(row: Mapping, as_of_date: Any) -> Optional[Tuple[Any, Any]]:
    # SQL: bc.currency = 'rub'
    currency = row.get('currency')
    if currency != BUY_UNIVERSE_CURRENCY:
        return (currency, BUY_UNIVERSE_CURRENCY)
    return None


def _trade_available_violation(row: Mapping, as_of_date: Any) -> Optional[Tuple[Any, Any]]:
    # SQL: (bc.is_trade_available IS TRUE OR bc.is_trade_available IS NULL)
    # — не проходит только явно FALSE (NULL не ужесточается молча).
    if row.get('is_trade_available') is False:
        return (False, 'IS TRUE OR NULL')
    return None


def _perpetual_violation(row: Mapping, as_of_date: Any) -> Optional[Tuple[Any, Any]]:
    # SQL: bc.perpetual_flag IS NOT TRUE
    if row.get('perpetual_flag') is True:
        return (True, False)
    return None


def _matured_violation(row: Mapping, as_of_date: Any) -> Optional[Tuple[Any, Any]]:
    # SQL: bc.maturity_date >= CURRENT_DATE (NULL-срок гейту не проходит,
    # как и в SQL). as_of_date — frozen as_of, не wall-clock.
    maturity = _as_date(row.get('maturity_date'))
    floor = _as_date(as_of_date)
    if maturity is None or maturity < floor:
        return (_iso_day(maturity), _iso_day(floor))
    return None


def _pool_risk_violation(row: Mapping, as_of_date: Any) -> Optional[Tuple[Any, Any]]:
    # SQL: bc.risk_level <= POOL_MAX_RISK (NULL не проходит, как и в SQL).
    risk = _as_int(row.get('risk_level'))
    if risk is None or risk > POOL_MAX_RISK:
        return (risk, POOL_MAX_RISK)
    return None


def _coupon_violation(row: Mapping, as_of_date: Any) -> Optional[Tuple[Any, Any]]:
    # SQL: COALESCE(bc.coupon_rate_percent, mc_floater...) IS NOT NULL.
    # Canonical raw-строки несут уже COALESCE-значение в coupon_rate_percent.
    if row.get('coupon_rate_percent') is None:
        return (None, None)
    return None


def _market_price_violation(row: Mapping, as_of_date: Any) -> Optional[Tuple[Any, Any]]:
    # SQL: bc.market_price IS NOT NULL AND bc.market_price > 0.
    # Номинал в gate не участвует (018: nominal fallback молча запрещён).
    price = row.get('market_price')
    if price is None or price <= 0:
        return (None if price is None else float(price), 0)
    return None


def _price_freshness_violation(row: Mapping, as_of_date: Any) -> Optional[Tuple[Any, Any]]:
    # SQL: bc.market_price_updated_at >= CURRENT_TIMESTAMP - INTERVAL
    # '<MARKET_PRICE_FRESHNESS_HOURS> hours' (NULL не проходит, как и в SQL).
    updated = _as_utc(row.get('market_price_updated_at'))
    as_of = _as_utc(as_of_date)
    floor = as_of - timedelta(hours=MARKET_PRICE_FRESHNESS_HOURS)
    if updated is None or updated < floor:
        return (
            updated.isoformat() if updated is not None else None,
            floor.isoformat(),
        )
    return None


def _pool_listlevel_violation(row: Mapping, as_of_date: Any) -> Optional[Tuple[Any, Any]]:
    # SQL: (bc.list_level IS NULL OR bc.list_level <= POOL_MAX_LISTLEVEL).
    level = _as_int(row.get('list_level'))
    if level is not None and level > POOL_MAX_LISTLEVEL:
        return (level, POOL_MAX_LISTLEVEL)
    return None


# Порядок таблицы = порядок условий в WHERE buy-выборки (и порядок reasons).
STRUCTURAL_BUY_GATES = (
    StructuralBuyGate(
        REASON_UNSUPPORTED_CURRENCY, f"bc.currency = '{BUY_UNIVERSE_CURRENCY}'",
        _currency_violation,
    ),
    StructuralBuyGate(
        REASON_TRADE_UNAVAILABLE,
        '(bc.is_trade_available IS TRUE OR bc.is_trade_available IS NULL)',
        _trade_available_violation,
    ),
    StructuralBuyGate(
        REASON_PERPETUAL_NOT_BUYABLE, 'bc.perpetual_flag IS NOT TRUE',
        _perpetual_violation,
    ),
    StructuralBuyGate(
        REASON_MATURED, 'bc.maturity_date >= CURRENT_DATE', _matured_violation,
    ),
    StructuralBuyGate(
        REASON_RISK_OUT_OF_POOL, 'bc.risk_level <= %s', _pool_risk_violation,
    ),
    StructuralBuyGate(
        REASON_COUPON_DATA_MISSING,
        'COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) '
        'IS NOT NULL',
        _coupon_violation,
    ),
    StructuralBuyGate(
        REASON_MARKET_PRICE_MISSING,
        '(bc.market_price IS NOT NULL AND bc.market_price > 0)',
        _market_price_violation,
    ),
    StructuralBuyGate(
        REASON_MARKET_PRICE_STALE,
        "bc.market_price_updated_at >= CURRENT_TIMESTAMP "
        f"- INTERVAL '{MARKET_PRICE_FRESHNESS_HOURS} hours'",
        _price_freshness_violation,
    ),
    StructuralBuyGate(
        REASON_LIST_LEVEL_OUT_OF_POOL,
        '(bc.list_level IS NULL OR bc.list_level <= %s)',
        _pool_listlevel_violation,
    ),
)

# Нижняя граница погашения — единственный gate, который policy-окно запроса
# (min_maturity) может заменить параметром (семантика 002.3/P9.5); в Python
# structural-оценке он применяется всегда (относительно as_of).
BUY_MATURITY_FLOOR_SQL = 'bc.maturity_date >= CURRENT_DATE'

# Фиксированный structural-блок WHERE buy-выборки: все gates, кроме
# заменяемой нижней границы погашения (её хранилище добавляет через
# maturity_filters — floor или policy-параметр, прежнее поведение).
STRUCTURAL_BUY_WHERE_SQL = ('\n                AND ').join(
    gate.sql for gate in STRUCTURAL_BUY_GATES
    if gate.sql != BUY_MATURITY_FLOOR_SQL
)


def structural_buy_violations(
    row: Mapping, as_of_date: Any
) -> Tuple[Tuple[str, Any, Any], ...]:
    """Структурные нарушения одной canonical raw-строки (030.6).

    Оценивает ВСЕ gates таблицы в каноническом порядке; as_of_date — frozen
    as_of PlanningContext (MATURED/MARKET_PRICE_STALE считаются от него,
    не от wall-clock). Пустой кортеж — бумага структурно покупаема.
    """
    violations: List[Tuple[str, Any, Any]] = []
    for gate in STRUCTURAL_BUY_GATES:
        found = gate.violation(row, as_of_date)
        if found is not None:
            violations.append((gate.code, found[0], found[1]))
    return tuple(violations)


# ---------------------------------------------------------------------------
# 030.6: построчная policy-политика скринера — единственная реализация для
# отчёта и eligibility-гейта планирования (report не получает второй набор
# почти одинаковых Python-условий). include_held/screener_limit/сортировка —
# presentation/discovery controls, ЗДЕСЬ не входят (не eligibility).
# ---------------------------------------------------------------------------


class ScreenerRejection(NamedTuple):
    """Построчный отказ policy-фильтра скринера.

    key — ключ excluded-счётчика отчёта ('ku'/'risk'/...); actual/limit —
    машиночитаемые значения для eligibility-reasons (где применимо).
    """

    key: str
    actual: Any = None
    limit: Any = None


def screener_policy_rejection(
    row: Mapping,
    *,
    include_ku: bool,
    max_risk: int,
    max_listlevel: int,
    freq_min: Optional[int] = None,
    freq_max: Optional[int] = None,
    freq_in: Optional[List[int]] = None,
    exclude_sovereign: bool = False,
    min_credit_rating: Optional[str] = None,
    min_rating_score: Optional[int] = None,
) -> Optional[ScreenerRejection]:
    """Первый сработавший policy-фильтр скринера для одной строки или None.

    Порядок правил — прежний порядок построчного цикла _build_screener
    (023/029): ku → sovereign → freq → risk → listlevel → rating; первая
    причина выигрывает, счётчики отчёта и eligibility получают одну и ту же.
    freq/sovereign/rating — одновременно final-portfolio constraints: у
    planner'а они применяются canonical validator'ом движка к целевому
    портфелю (030.7), candidate-only вызов передаёт их незаданными.
    """
    if row.get('ku') and not include_ku:
        return ScreenerRejection('ku', bool(row.get('ku')), bool(include_ku))
    if exclude_sovereign and is_sovereign(row):
        return ScreenerRejection(
            'sovereign', row.get('issuer') or row.get('name'), 'exclude_sovereign'
        )
    freq = _as_int(row.get('coupon_quantity_per_year'))
    if not freq_ok(freq, freq_max=freq_max, freq_in=freq_in, freq_min=freq_min):
        limit = list(freq_in) if freq_in is not None else (freq_min, freq_max)
        return ScreenerRejection('freq', freq, limit)
    risk = _as_int(row.get('risk_level'))
    if risk is None or risk > max_risk:
        return ScreenerRejection('risk', risk, max_risk)
    list_level = _as_int(row.get('list_level'))
    if list_level is not None and list_level > max_listlevel:
        return ScreenerRejection('listlevel', list_level, max_listlevel)
    if min_rating_score is not None:
        # 023: строгий фильтр рейтинга — отдельная ветка от risk_level:
        # нет строки в rating_history (нет и балла) — rating_missing;
        # балл ниже порога — rating_below_min. risk_level подменой не служит.
        score = _as_int(row.get('rating_score'))
        if score is None:
            return ScreenerRejection(
                'rating_missing', row.get('credit_rating'), min_credit_rating
            )
        if score < min_rating_score:
            return ScreenerRejection(
                'rating_below_min', row.get('credit_rating'), min_credit_rating
            )
    return None


def screener_ytm_rejection(
    row: Mapping, *, max_ytm: float
) -> Optional[ScreenerRejection]:
    """YTM-правило скринера для одной строки (порог в % годовых).

    Фикс: обогащённая ytm_percent обязательна и не выше потолка (как в
    движке calculate-ytm; до backfill SQL-фильтр bc.ytm строки с расчётной
    YTM на лету не отсекает — порог повторяется здесь); флоатер: YTM по
    конвенции проекта не определена — потолок применяется к рассчитанной
    ставке купона. max_ytm — canonical float-релей (деньги/ставки отчёта
    остаются в домене движка).
    """
    if row.get('floating_coupon_flag'):
        coupon_pct = _as_float(row.get('coupon_rate_percent'))
        if coupon_pct is None:
            return ScreenerRejection('floater_coupon_missing', None, max_ytm)
        if coupon_pct > max_ytm:
            return ScreenerRejection('floater_max_ytm', coupon_pct, max_ytm)
        return None
    ytm_pct = _as_float(row.get('ytm_percent'))
    if ytm_pct is None:
        return ScreenerRejection('fixed_ytm_missing', None, max_ytm)
    if ytm_pct > max_ytm:
        return ScreenerRejection('fixed_max_ytm', ytm_pct, max_ytm)
    return None


def parse_freq_list(raw: str, flag: str = '--freq-in') -> List[int]:
    """Разбирает '2,4' в список целых частот (для argparse type=).

    flag — имя CLI-флага для сообщений об ошибке (у команд разный:
    --freq-in у rebalance-report, --coupon-freq-in у calculate-ytm).
    """
    values: List[int] = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(int(part))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{flag}: нецелое значение частоты {part!r}"
            )
    if not values:
        raise argparse.ArgumentTypeError(f"{flag}: пустой список частот")
    return values


def is_sovereign(row: dict) -> bool:
    """Суверенный эмитент: связка с companies (entity_type='sovereign') либо
    каталог-имя ОФЗ (фолбэк на случай незаполненной связки company_id)."""
    if row.get('entity_type') == 'sovereign':
        return True
    name = (row.get('name') or '').strip().upper()
    return name.startswith('ОФЗ')


def freq_ok(
    freq: Optional[int],
    freq_max: Optional[int] = None,
    freq_in: Optional[List[int]] = None,
    freq_min: Optional[int] = None,
) -> bool:
    """Фильтр частоты купонов (P-C): без флагов проходит всё;
    NULL-частота фильтру не проходит (конвенция rebalance-report 005.1).

    023: freq_min/freq_max вместе задают диапазон (напр. 4..12 — «от
    квартальных до ежемесячных»); freq_in — список точных значений,
    взаимоисключим с диапазоном на уровне валидации CLI. Нулевая частота
    строгий диапазон не проходит (0 < freq_min при freq_min >= 1), при
    старом вызове «только --freq-max» прежняя семантика сохранена
    (0 <= freq_max проходил и проходит).
    """
    if freq_max is None and freq_in is None and freq_min is None:
        return True
    if freq is None:
        return False
    if freq_in is not None:
        return freq in freq_in
    if freq_min is not None and freq < freq_min:
        return False
    if freq_max is not None and freq > freq_max:
        return False
    return True
