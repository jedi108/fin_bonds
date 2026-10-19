"""
Сервис расчёта денежных потоков облигаций, доходности (YTM) и дюрации.

Реализует чистую функцию cashflow-решателя для задач 004 (дюрация) и 007 (YTM).
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import logging
from functools import lru_cache
from typing import Any, Dict, Mapping, Optional, Union, Tuple, List

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CashflowMetrics:
    """Результаты расчёта денежных потоков облигации."""
    ytm: Optional[Decimal] = None
    duration_macaulay: Optional[Decimal] = None
    duration_modified: Optional[Decimal] = None
    null_reason: Optional[str] = None


def _parse_date(val: Union[date, datetime, str, None]) -> Optional[date]:
    """Приводит входное значение к datetime.date."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        try:
            return date.fromisoformat(val[:10])
        except (ValueError, TypeError):
            return None
    return None


# issue #2: порог сверки нашей YTM с MOEX YIELD по умолчанию (п.п.), config.yaml ytm_check_threshold_pp
_YTM_CHECK_THRESHOLD_PP = 2.0


def ytm_horizon_date(
    maturity_date: Union[date, datetime, str, None],
    offer_date: Union[date, datetime, str, None],
    valuation_date: Union[date, datetime, str, None] = None,
) -> Tuple[Optional[date], Optional[str]]:
    """Горизонт YTM (issue #1): (дата, 'offer' | 'maturity'); оферта — если она между оценкой и погашением."""
    v_date = _parse_date(valuation_date) or date.today()
    m_date = _parse_date(maturity_date)
    o_date = _parse_date(offer_date)
    if o_date is not None and v_date < o_date and (m_date is None or o_date < m_date):
        return o_date, 'offer'
    return (m_date, 'maturity') if m_date is not None else (None, None)


@lru_cache(maxsize=1)
def _ytm_check_threshold_pp() -> float:
    from src.utils import load_config  # ленивый импорт: сервис остаётся чистым без конфига
    try:
        return float(load_config().get('ytm_check_threshold_pp', _YTM_CHECK_THRESHOLD_PP))
    except Exception:  # нет config.yaml (тесты/скрипты) — порог по умолчанию
        return _YTM_CHECK_THRESHOLD_PP


def ytm_check_fields(row: Mapping[str, Any], threshold_pp: Optional[float] = None) -> Dict[str, Any]:
    """Горизонт и сверка YTM с MOEX (issues #2, #3) для JSON-строки бумаги.

    row — строка выборки: ytm_percent, maturity_date, offer_date, moex_yield, moex_yield_date.
    ytm_check: None (нет нашей YTM) | 'no_moex_data' | 'ok' | 'mismatch' (разница > порога
    или другой горизонт). Наш ytm_pct под MOEX не подгоняется — только пометка.
    """
    ytm = row.get('ytm_percent')
    horizon, ytm_to = ytm_horizon_date(row.get('maturity_date'), row.get('offer_date'))
    moex = row.get('moex_yield')
    moex_pct = round(float(moex), 2) if moex is not None else None
    if ytm is None:
        return {'ytm_to': None, 'moex_yield_pct': moex_pct, 'ytm_check': None}
    if moex_pct is None:
        check = 'no_moex_data'
    else:
        limit = _ytm_check_threshold_pp() if threshold_pp is None else threshold_pp
        moex_date = _parse_date(row.get('moex_yield_date'))
        off_horizon = moex_date is not None and horizon is not None and moex_date != horizon
        check = 'mismatch' if off_horizon or abs(float(ytm) - moex_pct) > limit else 'ok'
    return {'ytm_to': ytm_to, 'moex_yield_pct': moex_pct, 'ytm_check': check}


def calculate_bond_cashflow_metrics(
    valuation_date: Optional[Union[date, datetime, str]] = None,
    nominal: Optional[Union[Decimal, float, int]] = None,
    maturity_date: Optional[Union[date, datetime, str]] = None,
    coupon_quantity_per_year: Optional[int] = None,
    coupon_rate_percent: Optional[Union[Decimal, float, int]] = None,
    market_price: Optional[Union[Decimal, float, int]] = None,
    perpetual_flag: Optional[bool] = None,
    floating_coupon_flag: Optional[bool] = None,
    amortization_flag: Optional[bool] = None,
    offer_date: Optional[Union[date, datetime, str]] = None,
) -> CashflowMetrics:
    """
    Чистая функция cashflow-решателя.

    Возвращает ytm (долей единицы), duration_macaulay и duration_modified (в годах),
    либо null_reason с одной из контрактных причин.

    Горизонт (issue #1): при будущей put-оферте раньше погашения расчёт идёт
    к оферте с выкупом по номиналу — купоны после оферты неизвестны, так
    считают MOEX и брокер. Иначе — к погашению.

    Единицы ytm — внутренний контракт движка (005.4): хранение (bonds_catalog.ytm)
    и вывод CLI — в процентах годовых; конвертация ×100 выполняется на границах
    записи (update_bonds_derived_metrics) и расчёта на лету (_enrich_bonds_with_ytm).
    """
    # 1. Валидация флагов до расчёта горизонта по дате
    if perpetual_flag:
        return CashflowMetrics(null_reason='perpetual')

    if floating_coupon_flag:
        return CashflowMetrics(null_reason='floating_coupon')

    # Амортизация: до появления локального графика возвращаем amortization_schedule_missing
    if amortization_flag:
        return CashflowMetrics(null_reason='amortization_schedule_missing')

    # 2. Частота купонов
    if coupon_quantity_per_year is None or coupon_quantity_per_year <= 0:
        return CashflowMetrics(null_reason='coupon_frequency_missing')

    # 3. Дата оценки и дата погашения
    v_date = _parse_date(valuation_date) or date.today()
    m_date = _parse_date(maturity_date)
    if m_date is None or m_date <= v_date:
        return CashflowMetrics(null_reason='maturity_missing_or_past')
    # shortcut: выкуп по 100% номинала; если появятся выпуски с BUYBACKPRICE≠100 — хранить цену выкупа
    m_date, _ = ytm_horizon_date(m_date, offer_date, v_date)

    # 4. Цена (clean-цена в рублях за бумагу)
    if market_price is None or float(market_price) <= 0:
        return CashflowMetrics(null_reason='market_price_missing_or_nonpositive')

    # 5. Номинал
    if nominal is None or float(nominal) <= 0:
        return CashflowMetrics(null_reason='nominal_missing_or_nonpositive')

    # 6. Купонная ставка
    if coupon_rate_percent is None or float(coupon_rate_percent) < 0:
        return CashflowMetrics(null_reason='coupon_rate_missing_or_negative')

    f = float(coupon_quantity_per_year)
    nom = float(nominal)
    c_rate = float(coupon_rate_percent)
    clean_price = float(market_price)

    # Построение синтетической сетки денежных потоков
    days_to_maturity = (m_date - v_date).days
    t_maturity = days_to_maturity / 365.25
    if t_maturity <= 0:
        return CashflowMetrics(null_reason='maturity_missing_or_past')

    t_list: List[float] = []
    k = 0
    while True:
        t_i = t_maturity - k / f
        if t_i <= 1e-9:
            break
        t_list.append(t_i)
        k += 1

    if not t_list:
        return CashflowMetrics(null_reason='maturity_missing_or_past')

    t_list.sort()
    t_next_coupon = t_list[0]

    coupon_per_period = nom * c_rate / 100.0 / f
    ai = coupon_per_period * (1.0 - f * t_next_coupon)
    dirty_price = clean_price + ai

    if dirty_price <= 0:
        return CashflowMetrics(null_reason='market_price_missing_or_nonpositive')

    # Формируем список потоков (t_i, CF_i)
    cashflows: List[Tuple[float, float]] = []
    for t_i in t_list:
        if abs(t_i - t_maturity) < 1e-9:
            cf = coupon_per_period + nom
        else:
            cf = coupon_per_period
        cashflows.append((t_i, cf))

    # Функция PV(y) - dirty_price для y > -f
    def pv_diff(y: float) -> float:
        rate_factor = 1.0 + y / f
        if rate_factor <= 0:
            return float('inf')
        pv = sum(cf / (rate_factor ** (f * t_i)) for t_i, cf in cashflows)
        return pv - dirty_price

    # Поиск интервала изоляции корня [y_low, y_high]
    # На области y > -f: PV строго убывает.
    # При y -> -f+, PV -> +inf.
    # При y -> +inf, PV -> 0.
    diff_zero = pv_diff(0.0)

    if abs(diff_zero) < 1e-12:
        y_root = 0.0
    elif diff_zero < 0:
        # dirty_price > sum(CF_i), y_root < 0 (премиальная бумага с отрицательной YTM)
        y_high = 0.0
        step = 0.5
        y_low = -f * step
        while pv_diff(y_low) < 0 and step < 0.999999:
            step = step + (1.0 - step) / 2.0
            y_low = -f * step

        if pv_diff(y_low) < 0:
            return CashflowMetrics(null_reason='solver_no_convergence')
        y_root = _bisect(pv_diff, y_low, y_high)
    else:
        # y_root > 0
        y_low = 0.0
        y_high = 1.0
        while pv_diff(y_high) > 0 and y_high < 10000.0:
            y_high *= 2.0

        if pv_diff(y_high) > 0:
            return CashflowMetrics(null_reason='solver_no_convergence')
        y_root = _bisect(pv_diff, y_low, y_high)

    if y_root is None:
        return CashflowMetrics(null_reason='solver_no_convergence')

    # Расчёт дюрации Маколея и модифицированной дюрации
    rate_factor = 1.0 + y_root / f
    if rate_factor <= 0:
        return CashflowMetrics(null_reason='solver_no_convergence')

    pv_times_t_sum = sum(
        (t_i * cf) / (rate_factor ** (f * t_i))
        for t_i, cf in cashflows
    )
    d_mac = pv_times_t_sum / dirty_price
    d_mod = d_mac / rate_factor

    return CashflowMetrics(
        ytm=Decimal(str(round(y_root, 6))),
        duration_macaulay=Decimal(str(round(d_mac, 4))),
        duration_modified=Decimal(str(round(d_mod, 4))),
        null_reason=None,
    )


def _bisect(func, low: float, high: float, max_iter: int = 100, tol: float = 1e-10) -> Optional[float]:
    """Бисекция монотонной функции."""
    f_low = func(low)
    f_high = func(high)

    if abs(f_low) < tol:
        return low
    if abs(f_high) < tol:
        return high

    for _ in range(max_iter):
        mid = (low + high) / 2.0
        f_mid = func(mid)

        if abs(f_mid) < tol or (high - low) / 2.0 < tol:
            return mid

        if f_mid > 0:
            low = mid
        else:
            high = mid

    return (low + high) / 2.0
