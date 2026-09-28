"""
Тесты UseCase calculate-duration (004_duration_metric).
Проверяют расчёт средневзвешенной дюрации, покрытие, причины исключений
и сходимость с ручным расчётом на фиксированных синтетических данных.
"""

import argparse
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from src.data_models import Bond
from src.services.cashflow import calculate_bond_cashflow_metrics
from src.use_cases.calculate_duration import CalculateDurationUseCase


def test_dod_fixed_examples_convergence():
    """
    DoD: результат сходится с независимым ручным расчётом на 2–3 зафиксированных примерах
    по той же синтетической сетке и clean/dirty-конвенции (допуск ±0.05 года).
    """
    v_date = date(2025, 1, 1)

    # Пример 1: Облигация на 1 год (365 дней), годовой купон 10%, торгуется по номиналу (1000 руб.)
    # t = 365 / 365.25 ≈ 0.9993155
    # При dirty_price = 1000, YTM = 10%
    # Потоки: 1100 руб через t
    # D_mac = t ≈ 0.9993
    # D_mod = D_mac / (1 + 0.10) = 0.9993155 / 1.10 ≈ 0.9085 года
    m1 = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=Decimal('1000'),
        maturity_date=date(2026, 1, 1),
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),  # clean price (AI = 0 при t = t_mat = 1/f)
    )
    assert m1.null_reason is None
    assert abs(float(m1.duration_macaulay) - 0.9993) < 0.05
    assert abs(float(m1.duration_modified) - 0.9085) < 0.05

    # Пример 2: Бескупонная облигация на 2 года (730 дней), номинал 1000 руб., цена 800 руб.
    # t = 730 / 365.25 ≈ 1.99863
    # D_mac бескупонной облигации в точности равна t
    # YTM: 800 = 1000 / (1 + y)^t => y = (1000/800)^(1/t) - 1 ≈ 0.1180 (11.8%)
    # D_mod = t / (1 + y) ≈ 1.99863 / 1.1180 ≈ 1.7877 года
    m2 = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=Decimal('1000'),
        maturity_date=date(2027, 1, 1),
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('0.0'),
        market_price=Decimal('800'),
    )
    assert m2.null_reason is None
    assert abs(float(m2.duration_macaulay) - 1.9986) < 0.05
    assert abs(float(m2.duration_modified) - 1.7877) < 0.05

    # Пример 3: Облигация на 3 года (1095 дней), полугодовой купон 12% (60 руб каждые полгода),
    # торгуется с дисконтом по 950 руб.
    # t = 1095 / 365.25 ≈ 2.99795 года
    # Модифицированная дюрация должна быть в районе 2.3 - 2.6 года
    m3 = calculate_bond_cashflow_metrics(
        valuation_date=v_date,
        nominal=Decimal('1000'),
        maturity_date=date(2028, 1, 1),
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('12.0'),
        market_price=Decimal('950'),
    )
    assert m3.null_reason is None
    assert 2.0 < float(m3.duration_modified) < 3.0
    assert float(m3.duration_macaulay) > float(m3.duration_modified)


def test_portfolio_duration_aggregation(capsys):
    """
    Проверяет расчёт средневзвешенной дюрации, покрытия по стоимости и вывод причин исключений.
    """
    factory_mock = MagicMock()
    storage_mock = MagicMock()
    factory_mock.get_db_connection.return_value = storage_mock

    # Тестовый набор позиций:
    # 1. Обычная бумага 1: вес 200,000 руб, mod_dur = 1.0, mac_dur = 1.1
    # 2. Обычная бумага 2: вес 300,000 руб, mod_dur = 2.0, mac_dur = 2.2
    #    Итого покрыто: 500,000 руб.
    #    Взвешенная мод. дюрация: (200k*1.0 + 300k*2.0) / 500k = (200k + 600k)/500k = 1.60 года
    # 3. Флоатер: вес 300,000 руб, duration_null_reason = 'floating_coupon'
    # 4. Амортизация: вес 150,000 руб, duration_null_reason = 'amortization_schedule_missing'
    # 5. Вечная: вес 50,000 руб, duration_null_reason = 'perpetual'
    # Всего портфель: 1,000,000 руб. Покрытие: 500k / 1000k = 50.0%
    storage_mock.get_portfolio_durations.return_value = [
        {
            'isin': 'RU000A000001',
            'ticker': 'BOND1',
            'name': 'Bond Fixed 1',
            'current_value': Decimal('200000'),
            'market_price': Decimal('1000'),
            'duration_macaulay': Decimal('1.1000'),
            'duration_modified': Decimal('1.0000'),
            'duration_null_reason': None,
        },
        {
            'isin': 'RU000A000002',
            'ticker': 'BOND2',
            'name': 'Bond Fixed 2',
            'current_value': Decimal('300000'),
            'market_price': Decimal('980'),
            'duration_macaulay': Decimal('2.2000'),
            'duration_modified': Decimal('2.0000'),
            'duration_null_reason': None,
        },
        {
            'isin': 'RU000A000003',
            'ticker': 'FLOAT1',
            'name': 'Floater Bond',
            'current_value': Decimal('300000'),
            'market_price': Decimal('1000'),
            'duration_macaulay': None,
            'duration_modified': None,
            'duration_null_reason': 'floating_coupon',
        },
        {
            'isin': 'RU000A000004',
            'ticker': 'AMORT1',
            'name': 'Amortizing Bond',
            'current_value': Decimal('150000'),
            'market_price': Decimal('1000'),
            'duration_macaulay': None,
            'duration_modified': None,
            'duration_null_reason': 'amortization_schedule_missing',
        },
        {
            'isin': 'RU000A000005',
            'ticker': 'PERP1',
            'name': 'Perpetual Bond',
            'current_value': Decimal('50000'),
            'market_price': Decimal('1000'),
            'duration_macaulay': None,
            'duration_modified': None,
            'duration_null_reason': 'perpetual',
        },
    ]

    use_case = CalculateDurationUseCase(factory_mock)
    args = argparse.Namespace(mode='portfolio', isin=None, recalculate=False)
    use_case.execute(args)

    out = capsys.readouterr().out

    # Проверяем покрытие 50.0%
    assert "50.0% от стоимости портфеля" in out
    # Проверяем средневзвешенную дюрацию 1.60 года
    assert "1.60 года" in out
    # Проверяем наличие всех причин исключений
    assert "floating_coupon" in out
    assert "amortization_schedule_missing" in out
    assert "perpetual" in out
    assert "500,000 ₽" in out


def test_bond_mode(capsys):
    """
    Проверяет вывод для режима одной бумаги.
    """
    factory_mock = MagicMock()
    storage_mock = MagicMock()
    factory_mock.get_db_connection.return_value = storage_mock

    bond = Bond(
        isin='RU000A123456',
        ticker='TEST1',
        name='Test Company Bond',
        nominal=Decimal('1000'),
        currency='RUB',
        market_price=Decimal('995.50'),
        coupon_rate_percent=Decimal('14.5'),
        coupon_quantity_per_year=4,
        maturity_date=date(2027, 6, 1),
        duration_macaulay=Decimal('1.8500'),
        duration_modified=Decimal('1.7200'),
        duration_null_reason=None,
    )
    storage_mock.get_bond_by_isin.return_value = bond

    use_case = CalculateDurationUseCase(factory_mock)
    args = argparse.Namespace(mode='bond', isin='RU000A123456', recalculate=False)
    use_case.execute(args)

    out = capsys.readouterr().out
    assert "RU000A123456" in out
    assert "1.8500 года" in out
    assert "1.7200 года" in out
    assert "Рассчитана" in out
