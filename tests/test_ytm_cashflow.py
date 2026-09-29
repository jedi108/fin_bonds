"""
Чистые модульные тесты для cashflow-решателя YTM (007_ytm_storage).
Проверяют математическую сходимость расчёта доходности к погашению
на зафиксированных синтетических данных без сети, БД и реальных ISIN.
"""

from datetime import date
from decimal import Decimal
import pytest

from src.services.cashflow import calculate_bond_cashflow_metrics


def test_par_bond_ytm_equals_coupon_rate():
    """Облигация, торгующаяся по номиналу (1000 руб.) с купоном 10% годовых, имеет YTM = 10%."""
    v_date = date(2025, 1, 1)
    m_date = date(2026, 1, 1)

    metrics = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=Decimal('1000'),
        maturity_date=m_date,
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
    )
    assert metrics.null_reason is None
    assert metrics.ytm is not None
    # YTM долей единицы = 0.100000
    assert abs(float(metrics.ytm) - 0.10) < 1e-4


def test_discount_bond_ytm_exceeds_coupon():
    """Облигация с дисконтом (цена 900 руб.) при номинале 1000 руб. и купоне 10% имеет YTM > 10%."""
    v_date = date(2025, 1, 1)
    m_date = date(2026, 1, 1)

    metrics = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=Decimal('1000'),
        maturity_date=m_date,
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('900'),
    )
    assert metrics.null_reason is None
    assert metrics.ytm is not None
    # При цене 900 купон 100 + прирост капитала 100 => YTM ~ 22.2%
    assert float(metrics.ytm) > 0.10
    assert 0.20 < float(metrics.ytm) < 0.25


def test_deep_premium_negative_ytm():
    """Премиальная бумага с отрицательной YTM (глубокая премия, dirty_price > суммарных выплат)."""
    v_date = date(2025, 1, 1)
    m_date = date(2026, 1, 1)

    # Купон 5% (50 руб), номинал 1000 руб, суммарный поток = 1050 руб.
    # Цена 1200 руб. Инвестор гарантированно теряет деньги при удержании до погашения => YTM < 0.
    metrics = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=Decimal('1000'),
        maturity_date=m_date,
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('5.0'),
        market_price=Decimal('1200'),
    )
    assert metrics.null_reason is None
    assert metrics.ytm is not None
    assert float(metrics.ytm) < 0.0
    # Должна сойтись к отрицательному значению ~ -12.5%
    assert -0.20 < float(metrics.ytm) < -0.10


def test_zero_coupon_ytm():
    """Бескупонная облигация на 2 года: 800 руб. при номинале 1000 руб."""
    v_date = date(2025, 1, 1)
    m_date = date(2027, 1, 1)

    metrics = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=Decimal('1000'),
        maturity_date=m_date,
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('0.0'),
        market_price=Decimal('800'),
    )
    assert metrics.null_reason is None
    assert metrics.ytm is not None
    # 800 * (1 + y)^2 = 1000 => 1 + y = sqrt(1.25) ≈ 1.11803 => y ≈ 11.8%
    assert abs(float(metrics.ytm) - 0.1180) < 0.005


def test_semi_annual_and_monthly_coupon_frequency():
    """Проверка работы решателя с разными частотами выплат (2 раза в год, 12 раз в год)."""
    v_date = date(2025, 1, 1)
    m_date = date(2027, 1, 1)

    # Полугодовые купоны (2 раза в год)
    m_semi = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=Decimal('1000'),
        maturity_date=m_date,
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('12.0'),
        market_price=Decimal('1000'),
    )
    assert m_semi.null_reason is None
    assert abs(float(m_semi.ytm) - 0.12) < 1e-4

    # Ежемесячные купоны (12 раз в год)
    m_monthly = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=Decimal('1000'),
        maturity_date=m_date,
        coupon_quantity_per_year=12,
        coupon_rate_percent=Decimal('12.0'),
        market_price=Decimal('1000'),
    )
    assert m_monthly.null_reason is None
    assert abs(float(m_monthly.ytm) - 0.12) < 1e-4


def test_intentional_null_reasons_contract():
    """Проверка закрытого списка причин отказа от расчёта (DoD 004/007)."""
    v_date = date(2025, 1, 1)
    m_date = date(2026, 1, 1)

    # 1. Флоатер
    m_flt = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        floating_coupon_flag=True,
    )
    assert m_flt.ytm is None
    assert m_flt.null_reason == 'floating_coupon'

    # 2. Бессрочная
    m_perp = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        perpetual_flag=True,
    )
    assert m_perp.ytm is None
    assert m_perp.null_reason == 'perpetual'

    # 3. Амортизация
    m_amr = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        amortization_flag=True,
    )
    assert m_amr.ytm is None
    assert m_amr.null_reason == 'amortization_schedule_missing'

    # 4. Отсутствие или невалидная частота купона
    m_freq = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        coupon_quantity_per_year=0,
    )
    assert m_freq.ytm is None
    assert m_freq.null_reason == 'coupon_frequency_missing'

    # 5. Прошедшая дата погашения
    m_past = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        coupon_quantity_per_year=2,
        maturity_date=date(2024, 1, 1),
    )
    assert m_past.ytm is None
    assert m_past.null_reason == 'maturity_missing_or_past'

    # 6. Отсутствие или отрицательная цена
    m_price = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        coupon_quantity_per_year=2,
        maturity_date=m_date,
        market_price=0,
    )
    assert m_price.ytm is None
    assert m_price.null_reason == 'market_price_missing_or_nonpositive'

    # 7. Отсутствие или отрицательный номинал
    m_nom = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        coupon_quantity_per_year=2,
        maturity_date=m_date,
        market_price=1000,
        nominal=-100,
    )
    assert m_nom.ytm is None
    assert m_nom.null_reason == 'nominal_missing_or_nonpositive'

    # 8. Отсутствие купонной ставки
    m_rate = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        coupon_quantity_per_year=2,
        maturity_date=m_date,
        market_price=1000,
        nominal=1000,
        coupon_rate_percent=None,
    )
    assert m_rate.ytm is None
    assert m_rate.null_reason == 'coupon_rate_missing_or_negative'
