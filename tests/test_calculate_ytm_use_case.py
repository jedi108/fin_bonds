"""
Тесты UseCase calculate-ytm (007_ytm_storage).
Проверяют использование канонической YTM, mark-to-market семантику,
фильтрацию --min-ytm и отображение причин для intentional NULL.
"""

import argparse
from datetime import date, timedelta
from decimal import Decimal
import io
import sys
import pytest

from src.data_models import Bond, PortfolioPosition
from src.use_cases.calculate_ytm import CalculateYtmUseCase


def test_calculate_ytm_buy_mode_stored(db):
    """Проверяет режим buy: чтение хранимой YTM и корректный intentional NULL для флоатера."""
    tomorrow = date.today() + timedelta(days=365)
    bond_fix = Bond(
        isin='RU000TESTBY1',
        ticker='BY1',
        name='Fixed Buy 1',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
    )
    bond_flt = Bond(
        isin='RU000TESTBY2',
        ticker='BY2',
        name='Floater Buy 2',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=4,
        coupon_rate_percent=Decimal('15.0'),
        floating_coupon_flag=True,
        market_price=Decimal('1000'),
    )
    db.add_bonds_to_catalog([bond_fix, bond_flt])

    use_case = CalculateYtmUseCase(db=db)
    args = argparse.Namespace(
        mode='buy',
        min_ytm=None,
        max_risk=5,
        min_maturity='2020-01-01',
        max_maturity='2040-01-01',
        no_amortization=False,
        monthly_coupons=False,
        fixed_coupon=False,
        floating_coupon=False,
        limit=10,
    )
    bonds = use_case._get_bonds_for_buying(args)
    by_isin = {b['isin']: b for b in bonds}

    assert 'RU000TESTBY1' in by_isin
    assert by_isin['RU000TESTBY1']['ytm_percent'] is not None
    assert 8.0 < by_isin['RU000TESTBY1']['ytm_percent'] < 12.0
    assert by_isin['RU000TESTBY1']['ytm_reason'] is None

    assert 'RU000TESTBY2' in by_isin
    assert by_isin['RU000TESTBY2']['ytm_percent'] is None
    assert by_isin['RU000TESTBY2']['ytm_reason'] == 'floating_coupon'


def test_calculate_ytm_portfolio_mode_mark_to_market(db):
    """
    Проверяет режим portfolio:
    YTM рассчитывается по market_price каталога (mark-to-market),
    а average_price остаётся справочной.
    """
    tomorrow = date.today() + timedelta(days=365)
    bond = Bond(
        isin='RU000TESTPF1',
        ticker='PF1',
        name='Portfolio Bond 1',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
    )
    db.add_bonds_to_catalog([bond])

    pos = PortfolioPosition(
        isin='RU000TESTPF1',
        ticker='PF1',
        name='Portfolio Bond 1',
        quantity=Decimal('10'),
        average_price=Decimal('800.0'),  # Куплено с дисконтом
        current_price=Decimal('1000.0'),
        current_value=Decimal('10000.0'),
        currency='rub',
        broker_name='tbank',
    )
    db.add_portfolio_positions([pos])

    use_case = CalculateYtmUseCase(db=db)
    args = argparse.Namespace(
        mode='portfolio',
        min_ytm=None,
        max_risk=5,
        min_maturity='2020-01-01',
        max_maturity='2040-01-01',
        no_amortization=False,
        monthly_coupons=False,
        fixed_coupon=False,
        floating_coupon=False,
        limit=10,
    )
    bonds = use_case._get_portfolio_bonds(args)
    assert len(bonds) == 1
    p_bond = bonds[0]
    # YTM по цене 1000 при купоне 10% на 1 год ≈ 10% (а не 100/800 = 12.5%)
    assert p_bond['ytm_percent'] is not None
    assert abs(p_bond['ytm_percent'] - 10.0) < 0.5
    assert float(p_bond['average_price']) == 800.0


def test_calculate_ytm_min_ytm_filter_does_not_coerce_null(db):
    """Проверяет фильтрацию --min-ytm: NULL не превращается в 0 и отсекается при пороге."""
    tomorrow = date.today() + timedelta(days=365)
    # 1. 10% YTM (цена 1000)
    b_10 = Bond(
        isin='RU000TESTFL1',
        ticker='FL1',
        name='Bond 10 pct',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
    )
    # 2. 20% YTM (цена 916 при купоне 10%)
    b_20 = Bond(
        isin='RU000TESTFL2',
        ticker='FL2',
        name='Bond 20 pct',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('916.66'),
    )
    # 3. Floater (intentional NULL)
    b_flt = Bond(
        isin='RU000TESTFL3',
        ticker='FL3',
        name='Floater NULL',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=4,
        coupon_rate_percent=Decimal('15.0'),
        floating_coupon_flag=True,
        market_price=Decimal('1000'),
    )
    db.add_bonds_to_catalog([b_10, b_20, b_flt])

    use_case = CalculateYtmUseCase(db=db)

    # Запрос с min_ytm = 15.0%: должен остаться только b_20
    args_15 = argparse.Namespace(
        mode='buy',
        min_ytm=15.0,
        max_risk=5,
        min_maturity='2020-01-01',
        max_maturity='2040-01-01',
        no_amortization=False,
        monthly_coupons=False,
        fixed_coupon=False,
        floating_coupon=False,
        limit=10,
    )
    bonds_15 = use_case._get_bonds_for_buying(args_15)
    isins_15 = [b['isin'] for b in bonds_15]
    assert 'RU000TESTFL2' in isins_15
    assert 'RU000TESTFL1' not in isins_15
    assert 'RU000TESTFL3' not in isins_15  # intentional NULL отсечён

    # Запрос с min_ytm = 0.0%: b_10 и b_20 проходят, а NULL не проходит
    args_0 = argparse.Namespace(
        mode='buy',
        min_ytm=0.0,
        max_risk=5,
        min_maturity='2020-01-01',
        max_maturity='2040-01-01',
        no_amortization=False,
        monthly_coupons=False,
        fixed_coupon=False,
        floating_coupon=False,
        limit=10,
    )
    bonds_0 = use_case._get_bonds_for_buying(args_0)
    isins_0 = [b['isin'] for b in bonds_0]
    assert 'RU000TESTFL1' in isins_0
    assert 'RU000TESTFL2' in isins_0
    assert 'RU000TESTFL3' not in isins_0


def test_calculate_ytm_unbackfilled_row_fallback(db):
    """Проверяет временную совместимость при ytm_updated_at IS NULL (расчёт на лету)."""
    tomorrow = date.today() + timedelta(days=365)
    bond = Bond(
        isin='RU000TESTUB1',
        ticker='UB1',
        name='Unbackfilled Bond',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('12.0'),
        market_price=Decimal('1000'),
    )
    db.add_bonds_to_catalog([bond])

    # Сбрасываем ytm_updated_at
    with db.conn:
        cursor = db._cursor()
        cursor.execute("""
            UPDATE bonds_catalog
            SET ytm = NULL, ytm_null_reason = NULL, ytm_updated_at = NULL
            WHERE isin = 'RU000TESTUB1'
        """)

    use_case = CalculateYtmUseCase(db=db)
    args = argparse.Namespace(
        mode='buy',
        min_ytm=None,
        max_risk=5,
        min_maturity='2020-01-01',
        max_maturity='2040-01-01',
        no_amortization=False,
        monthly_coupons=False,
        fixed_coupon=False,
        floating_coupon=False,
        limit=10,
    )
    bonds = use_case._get_bonds_for_buying(args)
    ub_bond = [b for b in bonds if b['isin'] == 'RU000TESTUB1'][0]
    assert ub_bond['ytm_percent'] is not None
    assert abs(ub_bond['ytm_percent'] - 12.0) < 0.5


def test_print_bonds_table_formatting(capsys):
    """Проверяет форматирование вывода таблиц buy и portfolio в консоль."""
    use_case = CalculateYtmUseCase(db=None)

    buy_bonds = [
        {
            'isin': 'RU000TEST111',
            'ticker': 'T1',
            'name': 'Test Bond Fixed',
            'nominal': Decimal('1000'),
            'coupon_rate_percent': Decimal('10.5'),
            'coupon_quantity_per_year': 2,
            'current_price': Decimal('980.0'),
            'market_price': Decimal('980.0'),
            'ytm_percent': 11.25,
            'ytm_reason': None,
            'total_monthly_coupon_payment': Decimal('8.75'),
            'coupon_type': 'Фиксированный',
            'maturity_date': date(2027, 1, 1),
            'risk_level': 1,
            'list_level': 1,
            'amortization_flag': False,
        },
        {
            'isin': 'RU000TEST222',
            'ticker': 'T2',
            'name': 'Test Bond Floater',
            'nominal': Decimal('1000'),
            'coupon_rate_percent': Decimal('15.0'),
            'coupon_quantity_per_year': 4,
            'current_price': Decimal('1000.0'),
            'market_price': Decimal('1000.0'),
            'ytm_percent': None,
            'ytm_reason': 'floating_coupon',
            'total_monthly_coupon_payment': Decimal('12.5'),
            'coupon_type': 'Плавающий',
            'maturity_date': date(2028, 1, 1),
            'risk_level': 2,
            'list_level': 1,
            'amortization_flag': False,
        },
    ]

    use_case._print_bonds_table(buy_bonds, "Тест покупки")
    out_buy = capsys.readouterr().out
    assert "RU000TEST111" in out_buy
    assert "11.25%" in out_buy
    assert "— (floating_coupon)" in out_buy
    assert "каноническая доходность" in out_buy

    portfolio_bonds = [
        {
            'isin': 'RU000TEST111',
            'ticker': 'T1',
            'name': 'Test Bond Portfolio',
            'nominal': Decimal('1000'),
            'coupon_rate_percent': Decimal('10.5'),
            'coupon_quantity_per_year': 2,
            'average_price': Decimal('950.0'),
            'current_price': Decimal('980.0'),
            'market_price': Decimal('980.0'),
            'quantity': Decimal('10'),
            'price_change_percent': Decimal('3.16'),
            'ytm_percent': 11.25,
            'ytm_reason': None,
            'total_monthly_coupon_payment': Decimal('87.5'),
            'coupon_type': 'Фиксированный',
            'maturity_date': date(2027, 1, 1),
            'risk_level': 1,
            'list_level': 1,
            'amortization_flag': False,
        },
    ]

    use_case._print_bonds_table(portfolio_bonds, "Тест портфеля")
    out_pf = capsys.readouterr().out
    assert "RU000TEST111" in out_pf
    assert "11.25%" in out_pf
    assert "950.00" in out_pf
    assert "Ср.цена" in out_pf

