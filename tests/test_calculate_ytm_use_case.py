"""
Тесты UseCase calculate-ytm (007_ytm_storage).
Проверяют использование канонической YTM, mark-to-market семантику,
фильтрацию --min-ytm и отображение причин для intentional NULL.
"""

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
import sys
import pytest

from src.data_models import Bond, PortfolioPosition
from src.use_cases.calculate_ytm import CalculateYtmUseCase

# Свежая отметка цены для buy-фикстур: с 018 buy-выборка требует
# market_price_updated_at не старше 24 часов.
_NOW = datetime.now(timezone.utc)


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
        market_price_updated_at=_NOW,
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
        market_price_updated_at=_NOW,
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
        max_listlevel=2,
        max_ytm=35.0,
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
        max_listlevel=2,
        max_ytm=35.0,
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
        market_price_updated_at=_NOW,
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
        market_price_updated_at=_NOW,
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
        market_price_updated_at=_NOW,
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
        max_listlevel=2,
        max_ytm=35.0,
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
        max_listlevel=2,
        max_ytm=35.0,
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
        market_price_updated_at=_NOW,
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
        max_listlevel=2,
        max_ytm=35.0,
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


def test_calculate_ytm_max_listlevel_filters_third_tier(db):
    """012-C: бумага с list_level=3 не попадает в результаты при --max-listlevel 2."""
    tomorrow = date.today() + timedelta(days=365)
    b_top = Bond(
        isin='RU000TESTLL1',
        ticker='LL1',
        name='First Tier Bond',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        list_level=1,
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
        market_price_updated_at=_NOW,
    )
    b_third = Bond(
        isin='RU000TESTLL3',
        ticker='LL3',
        name='Third Tier Bond',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        list_level=3,
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
        market_price_updated_at=_NOW,
    )
    db.add_bonds_to_catalog([b_top, b_third])
    db.update_bonds_derived_metrics(['RU000TESTLL1', 'RU000TESTLL3'])

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
        max_listlevel=2,
        max_ytm=35.0,
    )
    bonds = use_case._get_bonds_for_buying(args)
    isins = [b['isin'] for b in bonds]
    assert 'RU000TESTLL1' in isins
    assert 'RU000TESTLL3' not in isins


def _add_bond_with_stored_ytm(db, isin: str, ticker: str, ytm_percent: Decimal,
                              maturity: date = None):
    """Создаёт бумагу с канонической YTM, записанной напрямую в каталог.

    Единицы — проценты годовых (005.4, миграция 013): bc.ytm хранится
    в процентах, как его печатает CLI.
    """
    tomorrow = date.today() + timedelta(days=365)
    bond = Bond(
        isin=isin,
        ticker=ticker,
        name=f'Distress Test {ticker}',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        list_level=1,
        nominal=Decimal('1000'),
        maturity_date=maturity or tomorrow,
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
        market_price_updated_at=_NOW,
    )
    db.add_bonds_to_catalog([bond])
    with db.conn:
        cursor = db._cursor()
        cursor.execute("""
            UPDATE bonds_catalog
            SET ytm = %s, ytm_null_reason = NULL, ytm_updated_at = CURRENT_TIMESTAMP
            WHERE isin = %s
        """, (ytm_percent, isin))


def _make_buy_args(max_ytm: float) -> argparse.Namespace:
    return argparse.Namespace(
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
        max_listlevel=2,
        max_ytm=max_ytm,
    )


def test_calculate_ytm_max_ytm_filters_distress(db):
    """012-C: бумага с YTM 45% не попадает в результаты при --max-ytm 35."""
    _add_bond_with_stored_ytm(db, 'RU000TESTVD1', 'VD1', Decimal('45'))

    use_case = CalculateYtmUseCase(db=db)
    bonds = use_case._get_bonds_for_buying(_make_buy_args(max_ytm=35.0))
    assert 'RU000TESTVD1' not in [b['isin'] for b in bonds]


def test_calculate_ytm_max_ytm_keeps_normal_yield(db):
    """012-C: бумага с YTM 28% попадает в результаты при --max-ytm 35."""
    _add_bond_with_stored_ytm(db, 'RU000TESTVD2', 'VD2', Decimal('28'))

    use_case = CalculateYtmUseCase(db=db)
    bonds = use_case._get_bonds_for_buying(_make_buy_args(max_ytm=35.0))
    vd2 = [b for b in bonds if b['isin'] == 'RU000TESTVD2']
    assert len(vd2) == 1
    assert vd2[0]['ytm_percent'] is not None
    assert abs(vd2[0]['ytm_percent'] - 28.0) < 0.01


def test_calculate_ytm_max_ytm_filters_unbackfilled_fallback(db):
    """012-C: до backfill (bc.ytm IS NULL) порог --max-ytm применяется к YTM, рассчитанной на лету."""
    tomorrow = date.today() + timedelta(days=365)
    bond = Bond(
        isin='RU000TESTVD3',
        ticker='VD3',
        name='Unbackfilled Distress',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        list_level=1,
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=1,
        coupon_rate_percent=Decimal('40.0'),
        market_price=Decimal('1000'),
        market_price_updated_at=_NOW,
    )
    db.add_bonds_to_catalog([bond])

    use_case = CalculateYtmUseCase(db=db)
    bonds = use_case._get_bonds_for_buying(_make_buy_args(max_ytm=35.0))
    assert 'RU000TESTVD3' not in [b['isin'] for b in bonds]


def test_calculate_ytm_buy_mode_include_held(db):
    """002.2: --include-held в режиме buy показывает бумаги портфеля с бейджем held (N%);
    по умолчанию (include_held отсутствует в args) они исключены — совместимость."""
    tomorrow = date.today() + timedelta(days=365)
    bond = Bond(
        isin='RU000TESTIH1',
        ticker='IH1',
        name='Include Held 1',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('20.0'),
        market_price=Decimal('1000'),
        market_price_updated_at=_NOW,
    )
    db.add_bonds_to_catalog([bond])
    pos = PortfolioPosition(
        isin='RU000TESTIH1',
        ticker='IH1',
        name='Include Held 1',
        quantity=Decimal('10'),
        average_price=Decimal('900.0'),
        current_price=Decimal('1000.0'),
        current_value=Decimal('10000.0'),
        currency='rub',
        broker_name='tbank',
    )
    db.add_portfolio_positions([pos])

    use_case = CalculateYtmUseCase(db=db)
    base_args = dict(
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
        max_listlevel=2,
        max_ytm=35.0,
    )

    # Старые Namespace без include_held: прежнее поведение — исключение портфеля
    args = argparse.Namespace(**base_args)
    assert 'RU000TESTIH1' not in {b['isin'] for b in use_case._get_bonds_for_buying(args)}

    # --include-held: бумага из портфеля видна с бейджем held (доля N%)
    args_held = argparse.Namespace(include_held=True, **base_args)
    rows = use_case._get_bonds_for_buying(args_held)
    held = next(b for b in rows if b['isin'] == 'RU000TESTIH1')
    assert held['held_share_pct'] == Decimal('100.00')
    assert held['held_badge'] == 'held (100.00%)'


def test_calculate_ytm_parser_defaults_wide_window():
    """002.3/P9.5: без явных флагов окно погашения открыто — дефолты дат убраны."""
    parser = argparse.ArgumentParser()
    CalculateYtmUseCase.setup_parser(parser)
    args = parser.parse_args(['--mode', 'buy'])
    assert args.min_maturity is None
    assert args.max_maturity is None


def _make_default_window_args(mode: str = 'buy') -> argparse.Namespace:
    """args с дефолтами CLI 002.3 (min/max-maturity не заданы)."""
    return argparse.Namespace(
        mode=mode,
        min_ytm=None,
        max_risk=5,
        min_maturity=None,
        max_maturity=None,
        no_amortization=False,
        monthly_coupons=False,
        fixed_coupon=False,
        floating_coupon=False,
        limit=10,
        max_listlevel=2,
        max_ytm=35.0,
    )


def test_calculate_ytm_default_window_keeps_long_bonds(db):
    """002.3: кейс РЕСО-2034 — бумага с погашением за горизонтом старого дефолта
    2030-12-31 попадает в выдачу с открытым окном по умолчанию."""
    in_2034 = date.today() + timedelta(days=8 * 365)
    _add_bond_with_stored_ytm(db, 'RU000TESTWM1', 'WM1', Decimal('23.9'), maturity=in_2034)

    use_case = CalculateYtmUseCase(db=db)
    bonds = use_case._get_bonds_for_buying(_make_default_window_args())
    assert 'RU000TESTWM1' in [b['isin'] for b in bonds]


def test_calculate_ytm_narrow_window_still_cuts_long_bonds(db):
    """002.3: кейс Сэтл-2030 — явное узкое окно 2029-12-31 отсекает бумагу 2030 г.,
    широкое окно 2036-12-31 (первый прогон доход-запроса) — нет."""
    seattle_maturity = date.today() + timedelta(days=1255)  # ~ 2030-03
    _add_bond_with_stored_ytm(db, 'RU000TESTWM2', 'WM2', Decimal('24.0'), maturity=seattle_maturity)

    use_case = CalculateYtmUseCase(db=db)

    args_narrow = _make_default_window_args()
    args_narrow.max_maturity = '2029-12-31'
    assert 'RU000TESTWM2' not in [b['isin'] for b in use_case._get_bonds_for_buying(args_narrow)]

    args_wide = _make_default_window_args()
    args_wide.max_maturity = '2036-12-31'
    assert 'RU000TESTWM2' in [b['isin'] for b in use_case._get_bonds_for_buying(args_wide)]


def test_calculate_ytm_default_min_hides_matured(db):
    """002.3/P9.5: без --min-maturity уже погашенные бумаги не показываются
    (нижняя граница = сегодня); старый дефолт 2025-01-01 их пропускал."""
    yesterday = date.today() - timedelta(days=1)
    _add_bond_with_stored_ytm(db, 'RU000TESTZB1', 'ZB1', Decimal('20'), maturity=yesterday)

    use_case = CalculateYtmUseCase(db=db)
    bonds = use_case._get_bonds_for_buying(_make_default_window_args())
    assert 'RU000TESTZB1' not in [b['isin'] for b in bonds]

    # Явная дата в прошлом — осознанный выбор пользователя, фильтр ей подчиняется
    args_explicit = _make_default_window_args()
    args_explicit.min_maturity = '2020-01-01'
    assert 'RU000TESTZB1' in [b['isin'] for b in use_case._get_bonds_for_buying(args_explicit)]


def test_calculate_ytm_default_window_portfolio_mode(db):
    """002.3/P9.5: в режиме portfolio открытое окно по умолчанию показывает
    длинную позицию и скрывает погашенную (зомби)."""
    in_2034 = date.today() + timedelta(days=8 * 365)
    yesterday = date.today() - timedelta(days=1)
    _add_bond_with_stored_ytm(db, 'RU000TESTPL1', 'PL1', Decimal('23.9'), maturity=in_2034)
    _add_bond_with_stored_ytm(db, 'RU000TESTPL2', 'PL2', Decimal('20'), maturity=yesterday)

    pos_long = PortfolioPosition(
        isin='RU000TESTPL1', ticker='PL1', name='Distress Test PL1',
        quantity=Decimal('10'), average_price=Decimal('1000.0'),
        current_price=Decimal('1000.0'), current_value=Decimal('10000.0'),
        currency='rub', broker_name='tbank',
    )
    pos_zombie = PortfolioPosition(
        isin='RU000TESTPL2', ticker='PL2', name='Distress Test PL2',
        quantity=Decimal('10'), average_price=Decimal('1000.0'),
        current_price=Decimal('1000.0'), current_value=Decimal('10000.0'),
        currency='rub', broker_name='tbank',
    )
    db.add_portfolio_positions([pos_long, pos_zombie])

    use_case = CalculateYtmUseCase(db=db)
    bonds = use_case._get_portfolio_bonds(_make_default_window_args(mode='portfolio'))
    isins = [b['isin'] for b in bonds]
    assert 'RU000TESTPL1' in isins
    assert 'RU000TESTPL2' not in isins
