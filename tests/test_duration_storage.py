"""
Интеграционные тесты хранения и пересчёта дюрации в PostgreSQL (004_duration_metric).
"""

from datetime import date, timedelta
from decimal import Decimal
import pytest

from src.data_models import Bond


def test_storage_duration_lifecycle(db):
    """
    Проверяет:
    1. add_bonds_to_catalog рассчитывает дюрацию.
    2. save_market_price сохраняет цену в рублях, source, timestamp и пересчитывает дюрацию.
    3. update_market_prices обновляет цены и пересчитывает дюрации.
    4. Пограничные случаи дают корректный duration_null_reason.
    """
    tomorrow = date.today() + timedelta(days=365)

    bond_fixed = Bond(
        isin='RU000TESTFIX1',
        ticker='TEST1',
        name='Fixed Bond 1',
        currency='RUB',
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
        market_price_source='tbank',
    )

    bond_floater = Bond(
        isin='RU000TESTFLT1',
        ticker='FLT1',
        name='Floater Bond 1',
        currency='RUB',
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=4,
        coupon_rate_percent=Decimal('15.0'),
        floating_coupon_flag=True,
        market_price=Decimal('1000'),
    )

    bond_amort = Bond(
        isin='RU000TESTAMR1',
        ticker='AMR1',
        name='Amort Bond 1',
        currency='RUB',
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=4,
        coupon_rate_percent=Decimal('12.0'),
        amortization_flag=True,
        market_price=Decimal('1000'),
    )

    # Добавляем в каталог
    db.add_bonds_to_catalog([bond_fixed, bond_floater, bond_amort])

    # 1. Проверяем расчет после add_bonds_to_catalog
    b_fix = db.get_bond_by_isin('RU000TESTFIX1')
    assert b_fix is not None
    assert b_fix.duration_modified is not None
    assert b_fix.duration_macaulay is not None
    assert b_fix.duration_null_reason is None
    assert b_fix.duration_updated_at is not None

    b_flt = db.get_bond_by_isin('RU000TESTFLT1')
    assert b_flt is not None
    assert b_flt.duration_modified is None
    assert b_flt.duration_null_reason == 'floating_coupon'
    assert b_flt.duration_updated_at is not None

    b_amr = db.get_bond_by_isin('RU000TESTAMR1')
    assert b_amr is not None
    assert b_amr.duration_modified is None
    assert b_amr.duration_null_reason == 'amortization_schedule_missing'

    # 2. Проверяем save_market_price
    db.save_market_price('RU000TESTFIX1', Decimal('950.0'), source='tbank')
    b_fix_updated = db.get_bond_by_isin('RU000TESTFIX1')
    assert b_fix_updated.market_price == Decimal('950.0')
    assert b_fix_updated.market_price_source == 'tbank'
    assert b_fix_updated.market_price_updated_at is not None
    # Дюрация изменилась после изменения цены
    assert b_fix_updated.duration_modified != b_fix.duration_modified

    # 3. Проверяем update_market_prices (пакетно)
    db.update_market_prices([
        ('RU000TESTFIX1', Decimal('980.0'), 'tbank'),
    ])
    b_fix_batch = db.get_bond_by_isin('RU000TESTFIX1')
    assert b_fix_batch.market_price == Decimal('980.0')
    assert b_fix_batch.duration_modified is not None

    # 4. Проверяем сброс дюрации при обнулении цены
    db.save_market_price('RU000TESTFIX1', Decimal('0.0'), source='tbank')
    b_fix_zero = db.get_bond_by_isin('RU000TESTFIX1')
    assert b_fix_zero.duration_modified is None
    assert b_fix_zero.duration_null_reason == 'market_price_missing_or_nonpositive'
