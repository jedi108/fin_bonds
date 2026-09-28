"""
Unit-тесты для сервиса расчёта денежных потоков, YTM и дюрации (004_duration_metric).
"""
from datetime import date
from decimal import Decimal
import pytest

from src.services.cashflow import (
    calculate_bond_cashflow_metrics,
    CashflowMetrics,
)


def test_null_reason_perpetual():
    res = calculate_bond_cashflow_metrics(
        valuation_date=date(2025, 1, 1),
        nominal=Decimal('1000'),
        maturity_date=date(2030, 1, 1),
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
        perpetual_flag=True,
    )
    assert res.null_reason == 'perpetual'
    assert res.duration_macaulay is None
    assert res.duration_modified is None
    assert res.ytm is None


def test_null_reason_floating_coupon():
    res = calculate_bond_cashflow_metrics(
        valuation_date=date(2025, 1, 1),
        nominal=Decimal('1000'),
        maturity_date=date(2030, 1, 1),
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
        floating_coupon_flag=True,
    )
    assert res.null_reason == 'floating_coupon'
    assert res.duration_macaulay is None


def test_null_reason_amortization_schedule_missing():
    # Амортизация дает amortization_schedule_missing до появления реального локального графика
    res = calculate_bond_cashflow_metrics(
        valuation_date=date(2025, 1, 1),
        nominal=Decimal('1000'),
        maturity_date=date(2030, 1, 1),
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
        amortization_flag=True,
    )
    assert res.null_reason == 'amortization_schedule_missing'
    assert res.duration_macaulay is None


@pytest.mark.parametrize("freq", [None, 0, -1])
def test_null_reason_coupon_frequency_missing(freq):
    res = calculate_bond_cashflow_metrics(
        valuation_date=date(2025, 1, 1),
        nominal=Decimal('1000'),
        maturity_date=date(2030, 1, 1),
        coupon_quantity_per_year=freq,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
    )
    assert res.null_reason == 'coupon_frequency_missing'


@pytest.mark.parametrize("mat_date", [None, date(2025, 1, 1), date(2024, 1, 1)])
def test_null_reason_maturity_missing_or_past(mat_date):
    res = calculate_bond_cashflow_metrics(
        valuation_date=date(2025, 1, 1),
        nominal=Decimal('1000'),
        maturity_date=mat_date,
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
    )
    assert res.null_reason == 'maturity_missing_or_past'


@pytest.mark.parametrize("price", [None, 0, -50])
def test_null_reason_market_price_missing_or_nonpositive(price):
    res = calculate_bond_cashflow_metrics(
        valuation_date=date(2025, 1, 1),
        nominal=Decimal('1000'),
        maturity_date=date(2026, 1, 1),
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=price,
    )
    assert res.null_reason == 'market_price_missing_or_nonpositive'


@pytest.mark.parametrize("nom", [None, 0, -100])
def test_null_reason_nominal_missing_or_nonpositive(nom):
    res = calculate_bond_cashflow_metrics(
        valuation_date=date(2025, 1, 1),
        nominal=nom,
        maturity_date=date(2026, 1, 1),
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
    )
    assert res.null_reason == 'nominal_missing_or_nonpositive'


@pytest.mark.parametrize("rate", [None, -1.0])
def test_null_reason_coupon_rate_missing_or_negative(rate):
    res = calculate_bond_cashflow_metrics(
        valuation_date=date(2025, 1, 1),
        nominal=Decimal('1000'),
        maturity_date=date(2026, 1, 1),
        coupon_quantity_per_year=2,
        coupon_rate_percent=rate,
        market_price=Decimal('1000'),
    )
    assert res.null_reason == 'coupon_rate_missing_or_negative'


def test_zero_coupon_bond():
    # Бескупонная облигация: D_mac в точности равна времени до погашения t
    v_date = date(2025, 1, 1)
    m_date = date(2026, 1, 1)  # 365 дней
    t = 365 / 365.25

    res = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=Decimal('1000'),
        maturity_date=m_date,
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('0.0'),
        market_price=Decimal('900'),
    )
    assert res.null_reason is None
    assert res.duration_macaulay is not None
    assert abs(float(res.duration_macaulay) - t) < 1e-4

    # YTM: 900 = 1000 / (1 + y)^t => y = (1000/900)^(1/t) - 1
    expected_y = (1000.0 / 900.0) ** (1.0 / t) - 1.0
    assert abs(float(res.ytm) - expected_y) < 1e-4

    # D_mod = D_mac / (1 + y)
    expected_mod = t / (1.0 + expected_y)
    assert abs(float(res.duration_modified) - expected_mod) < 1e-4


def test_par_coupon_bond():
    # Облигация по номиналу: чистая цена скорректирована так, что dirty_price = nominal
    v_date = date(2025, 1, 1)
    m_date = date(2026, 1, 1)
    f = 2
    rate = Decimal('10.0')
    nom = Decimal('1000')

    # t_maturity = 365 / 365.25
    t_mat = 365 / 365.25
    t_next = t_mat - 1 / f
    coupon_per_period = float(nom) * 0.10 / f  # 50.0
    ai = coupon_per_period * (1.0 - f * t_next)
    clean_price = float(nom) - ai

    res = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=nom,
        maturity_date=m_date,
        coupon_quantity_per_year=f,
        coupon_rate_percent=rate,
        market_price=Decimal(str(round(clean_price, 4))),
    )
    assert res.null_reason is None
    # При dirty_price == nom доходность ytm должна равняться ставке купона (10%)
    assert abs(float(res.ytm) - 0.10) < 1e-4

    # Проверяем, что дюрация положительна и меньше t_mat
    assert 0 < float(res.duration_macaulay) < t_mat
    assert float(res.duration_modified) == pytest.approx(
        float(res.duration_macaulay) / (1 + 0.10 / f), rel=1e-3
    )


def test_premium_bond_negative_ytm():
    # Премиальная бумага: dirty_price > сумма недисконтированных потоков
    # Должен найтись отрицательный корень y в интервале (-f, 0)
    v_date = date(2025, 1, 1)
    m_date = date(2025, 7, 1)
    f = 1
    nom = Decimal('1000')
    rate = Decimal('2.0')  # купон 20 руб, всего потоков 1020 руб

    res = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=nom,
        maturity_date=m_date,
        coupon_quantity_per_year=f,
        coupon_rate_percent=rate,
        market_price=Decimal('1050'),  # цена выше суммы всех выплат
    )
    assert res.null_reason is None
    assert res.ytm is not None
    assert float(res.ytm) < 0
    assert float(res.ytm) > -f
    assert res.duration_macaulay is not None
    assert res.duration_modified is not None
    assert float(res.duration_macaulay) > 0
    assert float(res.duration_modified) > 0
