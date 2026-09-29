"""
Интеграционные тесты хранения и пересчёта YTM в PostgreSQL (007_ytm_storage).
"""

import argparse
from datetime import date, timedelta
from decimal import Decimal
import pytest

from src.data_models import Bond
from src.use_cases.calculate_ytm import CalculateYtmUseCase


def test_migration_008_columns_and_dto(db):
    """Проверяет наличие колонок ytm, ytm_null_reason, ytm_updated_at в bonds_catalog и в Bond DTO."""
    cursor = db._cursor()
    cursor.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'bonds_catalog'
          AND column_name IN ('ytm', 'ytm_null_reason', 'ytm_updated_at')
    """)
    cols = {row['column_name']: row['data_type'] for row in cursor.fetchall()}
    assert 'ytm' in cols, "ytm column missing in bonds_catalog"
    assert 'ytm_null_reason' in cols, "ytm_null_reason column missing in bonds_catalog"
    assert 'ytm_updated_at' in cols, "ytm_updated_at column missing in bonds_catalog"

    bond = Bond(
        isin='RU000TESTYTM0',
        ticker='TEST0',
        name='Test YTM DTO',
        currency='RUB',
        ytm=Decimal('0.152500'),
        ytm_null_reason=None,
    )
    assert bond.ytm == Decimal('0.152500')
    assert bond.ytm_null_reason is None


def test_derived_metrics_atomic_update_and_reasons(db):
    """
    Проверяет:
    1. add_bonds_to_catalog рассчитывает и duration, и ytm атомарно.
    2. Intentional NULL корректно заполняет ytm_null_reason и ytm_updated_at.
    3. save_market_price и update_market_prices пересчитывают и duration, и ytm.
    4. Обнуление цены сбрасывает YTM с причиной market_price_missing_or_nonpositive.
    """
    tomorrow = date.today() + timedelta(days=365)
    bond_fix = Bond(
        isin='RU000TESTYTM1',
        ticker='YTM1',
        name='Fixed Bond YTM 1',
        currency='RUB',
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
        market_price_source='tbank',
    )
    bond_flt = Bond(
        isin='RU000TESTYTM2',
        ticker='YTM2',
        name='Floater Bond YTM 2',
        currency='RUB',
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=4,
        coupon_rate_percent=Decimal('15.0'),
        floating_coupon_flag=True,
        market_price=Decimal('1000'),
    )
    bond_amr = Bond(
        isin='RU000TESTYTM3',
        ticker='YTM3',
        name='Amort Bond YTM 3',
        currency='RUB',
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=4,
        coupon_rate_percent=Decimal('12.0'),
        amortization_flag=True,
        market_price=Decimal('1000'),
    )

    db.add_bonds_to_catalog([bond_fix, bond_flt, bond_amr])

    # 1. Проверяем расчет после add_bonds_to_catalog
    b1 = db.get_bond_by_isin('RU000TESTYTM1')
    assert b1.ytm is not None
    # 005.4 (миграция 013): единицы хранения — проценты годовых
    assert 8.0 < float(b1.ytm) < 12.0
    assert b1.ytm_null_reason is None
    assert b1.ytm_updated_at is not None
    assert b1.duration_modified is not None

    b2 = db.get_bond_by_isin('RU000TESTYTM2')
    assert b2.ytm is None
    assert b2.ytm_null_reason == 'floating_coupon'
    assert b2.ytm_updated_at is not None

    b3 = db.get_bond_by_isin('RU000TESTYTM3')
    assert b3.ytm is None
    assert b3.ytm_null_reason == 'amortization_schedule_missing'
    assert b3.ytm_updated_at is not None

    # 2. Проверяем пересчет через save_market_price
    db.save_market_price('RU000TESTYTM1', Decimal('900.0'), source='tbank')
    b1_updated = db.get_bond_by_isin('RU000TESTYTM1')
    assert b1_updated.market_price == Decimal('900.0')
    assert b1_updated.ytm is not None
    assert float(b1_updated.ytm) > float(b1.ytm)

    # 3. Проверяем пересчет через update_market_prices (пакетно)
    db.update_market_prices([('RU000TESTYTM1', Decimal('950.0'), 'tbank')])
    b1_batch = db.get_bond_by_isin('RU000TESTYTM1')
    assert b1_batch.market_price == Decimal('950.0')
    assert b1_batch.ytm is not None
    assert float(b1_batch.ytm) < float(b1_updated.ytm)

    # 4. Проверяем сброс YTM при некорректной цене
    db.save_market_price('RU000TESTYTM1', Decimal('0.0'), source='tbank')
    b1_zero = db.get_bond_by_isin('RU000TESTYTM1')
    assert b1_zero.ytm is None
    assert b1_zero.ytm_null_reason == 'market_price_missing_or_nonpositive'
    assert b1_zero.ytm_updated_at is not None


def test_recalculate_derived_metrics_full_backfill(db):
    """Проверяет полный backfill каталога через update_bonds_derived_metrics() без параметров."""
    tomorrow = date.today() + timedelta(days=365)
    bond = Bond(
        isin='RU000TESTBF1',
        ticker='BF1',
        name='Backfill Bond 1',
        currency='RUB',
        nominal=Decimal('1000'),
        maturity_date=tomorrow,
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
    )
    db.add_bonds_to_catalog([bond])

    # Сбрасываем YTM и duration вручную, имитируя состояние до бэкфилла
    with db.conn:
        cursor = db._cursor()
        cursor.execute("""
            UPDATE bonds_catalog
            SET ytm = NULL, ytm_null_reason = NULL, ytm_updated_at = NULL,
                duration_macaulay = NULL, duration_modified = NULL,
                duration_null_reason = NULL, duration_updated_at = NULL
            WHERE isin = 'RU000TESTBF1'
        """)

    b_reset = db.get_bond_by_isin('RU000TESTBF1')
    assert b_reset.ytm is None
    assert b_reset.ytm_updated_at is None
    assert b_reset.duration_modified is None

    # Запускаем общий backfill
    count = db.update_bonds_derived_metrics()
    assert count >= 1

    b_filled = db.get_bond_by_isin('RU000TESTBF1')
    assert b_filled.ytm is not None
    assert b_filled.ytm_updated_at is not None
    assert b_filled.duration_modified is not None




# ---------------------------------------------------------------------------
# 005.4 / P-E: регрессия тихой ловушки 100x «доля vs проценты»
# ---------------------------------------------------------------------------


def _add_par_bond(db, isin: str, ticker: str):
    """Паевая бумага (цена = номинал): YTM численно равна купонной ставке (10%)."""
    bond = Bond(
        isin=isin,
        ticker=ticker,
        name=f'Par Bond {ticker}',
        currency='rub',
        risk_level=1,
        is_trade_available=True,
        list_level=1,
        nominal=Decimal('1000'),
        maturity_date=date.today() + timedelta(days=365),
        coupon_quantity_per_year=2,
        coupon_rate_percent=Decimal('10.0'),
        market_price=Decimal('1000'),
    )
    db.add_bonds_to_catalog([bond])


def test_ytm_stored_units_are_percent(db):
    """005.4 (миграция 013): bonds_catalog.ytm хранится в ПРОЦЕНТАХ годовых.

    Паевая бумага с купоном 10% имеет YTM 10.0. Если писатель вернётся к долям,
    в колонке окажется 0.1 — и все потребители, смешавшие каталог с CLI-выводом,
    молча ошибутся в 100 раз (ловушка P-E из 005.4).
    """
    _add_par_bond(db, 'RU000TESTUN1', 'UN1')

    stored = db.get_bond_by_isin('RU000TESTUN1')
    assert stored.ytm is not None
    value = float(stored.ytm)
    assert 9.0 < value < 11.0, (
        f"bonds_catalog.ytm должен хранить проценты годовых (~10.0), получено {value} — "
        "похоже на долю единицы (ловушка 100x, задача 005.4)"
    )


def test_ytm_catalog_units_match_engine_output(db):
    """005.4 регрессия: SQL-колонка и вывод движка calculate-ytm — одни единицы.

    Значение bc.ytm, отданное движком как ytm_percent, должно совпадать с
    хранимым 1:1 (без ×100/÷100): потребитель не может молча смешать
    SQL-колонку с CLI-выводом.
    """
    _add_par_bond(db, 'RU000TESTUN2', 'UN2')

    stored = db.get_bond_by_isin('RU000TESTUN2')
    assert stored.ytm is not None

    rows = db.get_bonds_yield_table(
        mode='buy', min_maturity=None, max_maturity=None, max_risk=5,
        no_amortization=False, monthly_coupons=False, fixed_coupon=False,
        floating_coupon=False, limit=10, max_listlevel=2, max_ytm=35.0,
    )
    assert any(r['isin'] == 'RU000TESTUN2' for r in rows)

    use_case = CalculateYtmUseCase(db=db)
    enriched = use_case._enrich_bonds_with_ytm(
        rows, argparse.Namespace(min_ytm=None)
    )
    engine_pct = next(
        b['ytm_percent'] for b in enriched if b['isin'] == 'RU000TESTUN2'
    )
    assert engine_pct is not None
    assert abs(engine_pct - float(stored.ytm)) < 0.05, (
        f"Единицы bc.ytm ({stored.ytm}) и движка ytm_percent ({engine_pct}) "
        "расходятся — тихая ловушка 100x (задача 005.4)"
    )


def test_migration_013_column_comment_declares_percent(db):
    """005.4: конвенция единиц закреплена комментарием на колонке в схеме."""
    cursor = db._cursor()
    cursor.execute("""
        SELECT col_description('bonds_catalog'::regclass,
                              (SELECT attnum FROM pg_attribute
                               WHERE attrelid = 'bonds_catalog'::regclass
                                 AND attname = 'ytm')) AS comment
    """)
    row = cursor.fetchone()
    comment = row['comment'] or ''
    assert 'процент' in comment.lower(), (
        f"Ожидается COMMENT ON COLUMN bonds_catalog.ytm с указанием процентов, получено: {comment!r}"
    )
