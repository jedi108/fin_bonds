"""
Качество данных каталога (задача 002.5): coupon_type и точность coupon_rate_percent.

1. round_coupon_rate нормализует точность до 4 знаков на обоих воспроизведённых
   кейсах мусорной точности (Decimal-формула MOEX, repr float-пересчёта).
2. add_bonds_to_catalog выводит coupon_type из floating_coupon_flag (P4).
3. update_bond_moex_data / update_bond_coupon_rate пишут не длиннее 4 знаков (P9.4).
4. Агрегация coupon_type='FLOAT' согласована с floating_coupon_flag —
   воспроизведение кейса «доля флоатеров 0.0%».
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from psycopg2.extras import RealDictCursor

from src.data_models import Bond
from src.utils import round_coupon_rate


def _dict_cursor(db):
    """Курсор с доступом к колонкам по имени (storage внутри ходит RealDictCursor)."""
    return db.conn.cursor(cursor_factory=RealDictCursor)


class TestRoundCouponRate:
    """Юнит-тесты нормализации точности (без БД)."""

    def test_decimal_formula_case_gtlk(self):
        # Точная реплика кейса P9.4: купон 20.55 / номинал 1000, период 30 дней.
        raw = (Decimal('20.55') / 1000) * (Decimal('365') / Decimal('30')) * 100
        assert str(raw) == '25.00250000000000000000000001'
        assert round_coupon_rate(raw) == Decimal('25.0025')

    def test_float_repr_case(self):
        # Второй кейс P9.4: длинный repr float-пересчёта actual/365
        # (купон 0.25 / номинал 1000, период 91 день).
        raw = repr((0.25 / 1000) * (365 / 91) * 100)
        assert raw == '0.10027472527472528'
        assert round_coupon_rate(raw) == Decimal('0.1003')

    def test_none_passthrough(self):
        assert round_coupon_rate(None) is None

    def test_normal_value_unchanged(self):
        assert round_coupon_rate(Decimal('12.5')) == Decimal('12.5')
        assert round_coupon_rate('7.75') == Decimal('7.75')

    def test_garbage_yields_none(self):
        assert round_coupon_rate('не число') is None


def _make_bond(isin, *, floater=False, coupon=None, coupon_type=None):
    return Bond(
        isin=isin,
        ticker=isin[:6],
        name=f'Bond {isin}',
        currency='RUB',
        nominal=Decimal('1000'),
        maturity_date=date.today() + timedelta(days=365),
        coupon_quantity_per_year=2,
        coupon_rate_percent=coupon,
        coupon_type=coupon_type,
        floating_coupon_flag=floater,
    )


def test_add_bonds_derives_coupon_type(db):
    """P4: coupon_type выводится из флага флоатера при записи каталога."""
    db.add_bonds_to_catalog([
        _make_bond('RU000QAFIX01', floater=False, coupon=Decimal('10.0')),
        _make_bond('RU000QAFLT01', floater=True, coupon=Decimal('12.3456789')),
        # Явно заданный тип не перезатирается.
        _make_bond('RU000QAEXPL01', floater=False, coupon=Decimal('8.0'),
                   coupon_type='FLOAT'),
    ])

    with _dict_cursor(db) as cursor:
        cursor.execute("""
            SELECT isin, coupon_type, floating_coupon_flag
            FROM bonds_catalog
            WHERE isin IN ('RU000QAFIX01', 'RU000QAFLT01', 'RU000QAEXPL01')
            ORDER BY isin
        """)
        rows = {r['isin']: r for r in cursor.fetchall()}

    assert rows['RU000QAFIX01']['coupon_type'] == 'FIX'
    assert rows['RU000QAFIX01']['floating_coupon_flag'] is False
    assert rows['RU000QAFLT01']['coupon_type'] == 'FLOAT'
    assert rows['RU000QAFLT01']['floating_coupon_flag'] is True
    assert rows['RU000QAEXPL01']['coupon_type'] == 'FLOAT'


def test_add_bonds_upsert_keeps_coupon_type_in_sync(db):
    """Повторная запись обновляет coupon_type вместе с флагом (upsert)."""
    db.add_bonds_to_catalog([_make_bond('RU000QASWAP01', floater=False)])
    db.add_bonds_to_catalog([_make_bond('RU000QASWAP01', floater=True)])

    with _dict_cursor(db) as cursor:
        cursor.execute(
            "SELECT coupon_type FROM bonds_catalog WHERE isin = 'RU000QASWAP01'"
        )
        assert cursor.fetchone()['coupon_type'] == 'FLOAT'


def test_update_bond_moex_data_rounds_coupon_rate(db):
    """P9.4, канал (а): запись MOEX-расчёта не длиннее 4 знаков."""
    db.add_bonds_to_catalog([_make_bond('RU000QAMOEX01', coupon=Decimal('10.0'))])
    garbage = (Decimal('20.55') / 1000) * (Decimal('365') / Decimal('30')) * 100

    db.update_bond_moex_data(
        isin='RU000QAMOEX01',
        list_level=1,
        coupon_rate=garbage,
        offer_date=None,
    )

    with _dict_cursor(db) as cursor:
        cursor.execute(
            "SELECT coupon_rate_percent FROM bonds_catalog WHERE isin = 'RU000QAMOEX01'"
        )
        stored = cursor.fetchone()['coupon_rate_percent']

    assert stored == Decimal('25.0025')
    assert -stored.as_tuple().exponent <= 4


def test_update_bond_coupon_rate_rounds_new_rate(db):
    """P9.4: канал update-floaters (RUONIA + spread) тоже нормализуется."""
    db.add_bonds_to_catalog([_make_bond('RU000QAFLT02', floater=True, coupon=Decimal('12.0'))])
    long_rate = Decimal('15') + Decimal('0.1234567890123456789012345678')

    db.update_bond_coupon_rate('RU000QAFLT02', long_rate)

    with _dict_cursor(db) as cursor:
        cursor.execute(
            "SELECT coupon_rate_percent FROM bonds_catalog WHERE isin = 'RU000QAFLT02'"
        )
        stored = cursor.fetchone()['coupon_rate_percent']

    assert stored == Decimal('15.1235')
    assert -stored.as_tuple().exponent <= 4


def test_floater_aggregation_not_zero(db):
    """Кейс P4: агрегация coupon_type='FLOAT' согласована с флагом (не 0.0%)."""
    bonds = [
        _make_bond('RU000QAAGG01', floater=False, coupon=Decimal('10.0')),
        _make_bond('RU000QAAGG02', floater=False, coupon=Decimal('11.0')),
        _make_bond('RU000QAAGG03', floater=True, coupon=Decimal('12.0')),
        _make_bond('RU000QAAGG04', floater=True, coupon=Decimal('13.0')),
    ]
    db.add_bonds_to_catalog(bonds)

    with _dict_cursor(db) as cursor:
        cursor.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(coupon_type) AS typed,
                COUNT(*) FILTER (WHERE coupon_type = 'FLOAT') AS float_by_type,
                COUNT(*) FILTER (WHERE floating_coupon_flag IS TRUE) AS float_by_flag,
                ROUND(100.0 * COUNT(*) FILTER (WHERE coupon_type = 'FLOAT')
                      / COUNT(*), 1) AS float_share_pct,
                COUNT(*) FILTER (WHERE coupon_rate_percent <> ROUND(coupon_rate_percent, 4))
                    AS imprecise_rates
            FROM bonds_catalog
        """)
        stats = cursor.fetchone()

    assert stats['typed'] == stats['total']          # coupon_type IS NULL -> 0
    assert stats['float_by_flag'] == 2
    assert stats['float_by_type'] == stats['float_by_flag']
    assert stats['float_share_pct'] == Decimal('50.0')  # не 0.0%
    assert stats['imprecise_rates'] == 0
