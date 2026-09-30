"""Тесты происхождения оценки позиции (задача 008).

Ветка valuation_source в view обязана точно зеркалить COALESCE-ветки
position_value_rub: и значение, и источник проверяются на каждый fallback.
"""
from datetime import date
from decimal import Decimal

import pytest
from psycopg2.extras import RealDictCursor

from src.data_models import Bond

# Порядок колонок view: 20 колонок из 009, 010 дописала position_status,
# 014 дописывает valuation_source (CREATE OR REPLACE VIEW дописывает только в конец).
EXPECTED_VIEW_COLUMNS = (
    "isin",
    "broker_name",
    "account_id",
    "ticker",
    "name",
    "quantity",
    "average_price",
    "current_price",
    "nominal",
    "market_price",
    "position_value_rub",
    "liquidity_loss_ratio",
    "duration_modified",
    "ytm",
    "risk_level",
    "list_level",
    "coupon_rate_percent",
    "coupon_quantity_per_year",
    "company_id",
    "company_name",
    "position_status",
    "valuation_source",
)


def add_bond(db, isin, *, nominal=Decimal(1000), maturity_date=date(2030, 1, 1)):
    db.add_bonds_to_catalog(
        [
            Bond(
                isin=isin,
                name=f"Бонд {isin}",
                nominal=nominal,
                maturity_date=maturity_date,
                currency="RUB",
            )
        ]
    )


def add_position(db, isin, *, broker="tbank", quantity=10, **fields):
    """Позиция у брокера; fields — переопределения денежных колонок."""
    columns = {
        "isin": isin,
        "broker_name": broker,
        "name": f"Бонд {isin}",
        "quantity": quantity,
        "status": "active",
    }
    columns.update(fields)
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with db.conn.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO portfolio_positions ({names}) VALUES ({placeholders})",
            tuple(columns.values()),
        )
    db.conn.commit()


def set_market_price(db, isin, price):
    with db.conn.cursor() as cursor:
        cursor.execute(
            "UPDATE bonds_catalog SET market_price = %s, market_price_updated_at = NOW() "
            "WHERE isin = %s",
            (price, isin),
        )
    db.conn.commit()


def fetch_view_rows(db, isin):
    with db.conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            "SELECT position_value_rub, position_status, valuation_source "
            "FROM v_portfolio_positions_valuation WHERE isin = %s",
            (isin,),
        )
        return cursor.fetchall()


def assert_branch(db, isin, expected_value, expected_source):
    """Ветка источника и (неизменная) арифметика оценки для единственной позиции."""
    rows = fetch_view_rows(db, isin)
    assert len(rows) == 1
    assert rows[0]["valuation_source"] == expected_source
    assert rows[0]["position_value_rub"] == Decimal(expected_value)


def test_view_valuation_source_is_last_column(db):
    """Миграция 014: valuation_source в конце, прежние колонки в прежнем порядке."""
    with db.conn.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'v_portfolio_positions_valuation' "
            "ORDER BY ordinal_position"
        )
        columns = tuple(row[0] for row in cursor.fetchall())

    assert columns == EXPECTED_VIEW_COLUMNS


def test_valuation_source_broker_value(db):
    """current_value от брокера: price_rub может быть 0 — это не ошибка (008)."""
    add_bond(db, "RU000A1")
    add_position(
        db, "RU000A1", quantity=10, current_value=12345.67, current_price=0
    )
    set_market_price(db, "RU000A1", Decimal("95.5"))

    assert_branch(db, "RU000A1", "12345.67", "broker_value")


def test_valuation_source_broker_price(db):
    """current_value пуст, но quantity * current_price доступен."""
    add_bond(db, "RU000A2")
    add_position(db, "RU000A2", quantity=10, current_price=Decimal("99.5"))

    assert_branch(db, "RU000A2", "995.00", "broker_price")


def test_valuation_source_broker_price_zero_current_value(db):
    """current_value = 0 (NULLIF) не переключает ветку: побеждает broker_price."""
    add_bond(db, "RU000A3")
    add_position(db, "RU000A3", quantity=10, current_value=0, current_price=90)
    set_market_price(db, "RU000A3", Decimal("95"))

    assert_branch(db, "RU000A3", "900.00", "broker_price")


def test_valuation_source_market_price(db):
    """Цены брокера обнулены — оценка из market_price каталога."""
    add_bond(db, "RU000A4")
    add_position(db, "RU000A4", quantity=10, current_value=0, current_price=0)
    set_market_price(db, "RU000A4", Decimal("95.5"))

    assert_branch(db, "RU000A4", "955.00", "market_price")


def test_valuation_source_nominal_fallback(db):
    """Оценок нет, бумага непогашена — quantity * nominal."""
    add_bond(db, "RU000A5")
    add_position(db, "RU000A5", quantity=35, current_value=0, current_price=0)

    assert_branch(db, "RU000A5", "35000.00", "nominal_fallback")


def test_valuation_source_zero_when_quantity_or_nominal_missing(db):
    """quantity NULL: quantity * nominal = NULL — это 'zero', не nominal_fallback."""
    add_bond(db, "RU000A6")
    add_position(db, "RU000A6", quantity=None, current_value=0, current_price=0)

    assert_branch(db, "RU000A6", "0.00", "zero")


def test_valuation_source_zero_without_catalog_row(db):
    """Позиция вне каталога: оценке взяться неоткуда."""
    add_position(db, "RU000A7", quantity=5, current_value=0, current_price=0)

    assert_branch(db, "RU000A7", "0.00", "zero")


def test_valuation_source_matured_bond_gets_zero_not_nominal(db):
    """Погашенная бумага: nominal-фолбэк закрыт guard'ом 010 — 'zero'."""
    add_bond(db, "RU000A8", maturity_date=date(2025, 12, 4))
    add_position(db, "RU000A8", quantity=35, current_value=0, current_price=0)

    rows = fetch_view_rows(db, "RU000A8")
    assert len(rows) == 1
    assert rows[0]["valuation_source"] == "zero"
    assert rows[0]["position_value_rub"] == Decimal("0.00")


def test_rows_export_valuation_source_of_largest_position(db):
    """Агрегация по ISIN: источник позиции с наибольшим position_value_rub."""
    add_bond(db, "RU000B1")
    set_market_price(db, "RU000B1", Decimal("100"))
    add_position(
        db, "RU000B1", broker="tbank", quantity=200, current_price=100
    )  # 20000 broker_price
    add_position(
        db, "RU000B1", broker="bcs", quantity=1, current_value=5000
    )  # 5000 broker_value

    rows = db.get_chatgpt_portfolio_rows()

    assert len(rows) == 1
    # value_rub — прежняя сумма позиций, источник — от наибольшей (tbank).
    assert rows[0]["value_rub"] == Decimal("25000.00")
    assert rows[0]["quantity"] == 201
    assert rows[0]["valuation_source"] == "broker_price"


def test_rows_export_valuation_source_single_position(db):
    """Одиночная позиция: источник проходит в строку экспорта без изменений."""
    add_bond(db, "RU000B2")
    add_position(db, "RU000B2", quantity=10, current_value=12345.67, current_price=0)

    rows = db.get_chatgpt_portfolio_rows()

    assert len(rows) == 1
    assert rows[0]["value_rub"] == Decimal("12345.67")
    assert rows[0]["price"] == 0
    assert rows[0]["valuation_source"] == "broker_value"
