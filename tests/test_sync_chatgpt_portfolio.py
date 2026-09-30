from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.data_models import Bond
from src.services.google_sheets import GoogleSheetsPortfolioPublisher
from src.use_cases.sync_chatgpt_portfolio import (
    PORTFOLIO_HEADERS,
    SCHEMA_VERSION,
    VALUATION_WARNINGS,
    SyncChatgptPortfolioUseCase,
)


class FakeDb:
    def __init__(self, rows, freshness=None):
        self.rows = rows
        self.freshness = freshness or {
            "bonds_with_market_price": 100,
            "bonds_tradeable_count": 100,
            "bonds_tradeable_stale_or_missing": 0,
            "portfolio_positions_rows": len(rows),
            "portfolio_zero_value_positions": 0,
            "portfolio_zero_rows_suspicious": 0,
            "portfolio_zero_value_isins": "",
            "last_sync_time": datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
            "portfolio_max_updated_at": datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
            "market_price_max_updated_at": datetime(2026, 9, 30, 8, 55, tzinfo=timezone.utc),
        }

    def get_db_freshness(self):
        return self.freshness

    def get_chatgpt_portfolio_rows(self):
        return [dict(row) for row in self.rows]


class FakeYtmEngine:
    """Повторяет контракт enrichment'а реального движка: в строке всегда есть
    ytm_percent и ytm_reason (None для рассчитанной, иначе причина из строки)."""

    def _enrich_bonds_with_ytm(self, rows, _args):
        result = []
        for row in rows:
            item = dict(row)
            ytm_percent = item.setdefault("ytm_percent", item.get("ytm"))
            if ytm_percent is None:
                item.setdefault(
                    "ytm_reason", item.get("ytm_null_reason") or "not_applicable"
                )
            else:
                item.setdefault("ytm_reason", None)
            result.append(item)
        return result


class FakePublisher:
    def __init__(self):
        self.call = None

    def publish(self, **kwargs):
        self.call = kwargs


def sample_rows():
    base = {
        "ticker": "TEST",
        "coupon_rate_percent": 15,
        "coupon_quantity_per_year": 4,
        "floating_coupon_flag": False,
        "amortization_flag": False,
        "credit_rating": "AA",
        "risk_level": 1,
        "list_level": 1,
        "duration_macaulay": 1.5,
        "duration_modified": 1.4,
        "liquidity_loss_ratio": 0.5,
        "ytm": 18.0,
        "ytm_null_reason": None,
        "duration_null_reason": None,
        "maturity_date": None,
        "offer_date": None,
    }
    return [
        {
            **base,
            "isin": "RU000A",
            "name": "Bond A",
            "issuer": "Issuer",
            "quantity": 10,
            "price": 1000,
            "value_rub": 10000,
        },
        {
            **base,
            "isin": "RU000B",
            "name": "Bond B",
            "issuer": "Issuer",
            "quantity": 15,
            "price": 1000,
            "value_rub": 15000,
        },
    ]


def test_build_payload_has_position_and_issuer_weights():
    now = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    use_case = SyncChatgptPortfolioUseCase(
        FakeDb(sample_rows()),
        publisher=FakePublisher(),
        ytm_engine=FakeYtmEngine(),
    )

    control, rows = use_case._build_payload(now)

    assert control["snapshot_id"] == "20260930T100000Z"
    assert control["positions_count"] == 2
    assert control["portfolio_value_rub"] == 25000
    assert control["gate_status"] == "OK"

    share_idx = PORTFOLIO_HEADERS.index("share_pct")
    issuer_idx = PORTFOLIO_HEADERS.index("issuer_pct")
    assert rows[0][share_idx] == 40.0
    assert rows[1][share_idx] == 60.0
    assert rows[0][issuer_idx] == 100.0
    assert rows[1][issuer_idx] == 100.0


def make_rows(**overrides):
    """Одна строка портфеля с переопределениями полей под кейс причин NULL."""
    row = {
        "isin": "RU000A",
        "name": "Bond A",
        "issuer": "Issuer",
        "quantity": 10,
        "price": 1000,
        "value_rub": 10000,
        "ticker": "TEST",
        "coupon_rate_percent": 15,
        "coupon_quantity_per_year": 4,
        "floating_coupon_flag": False,
        "amortization_flag": False,
        "credit_rating": "AA",
        "risk_level": 1,
        "list_level": 1,
        "duration_macaulay": 1.5,
        "duration_modified": 1.4,
        "liquidity_loss_ratio": 5.0,
        "ytm": 18.0,
        "ytm_null_reason": None,
        "duration_null_reason": None,
        "maturity_date": None,
        "offer_date": None,
    }
    row.update(overrides)
    return [row]


def build_payload(rows):
    now = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    use_case = SyncChatgptPortfolioUseCase(
        FakeDb(rows),
        publisher=FakePublisher(),
        ytm_engine=FakeYtmEngine(),
    )
    return use_case._build_payload(now)


def cell(row_values, column):
    """Ячейка строки по имени колонки — защита от рассинхронизации порядка."""
    return row_values[PORTFOLIO_HEADERS.index(column)]


def test_reason_columns_follow_value_columns():
    """ytm_reason идёт сразу после ytm_pct, duration_reason — после modified_duration."""
    assert PORTFOLIO_HEADERS.index("ytm_reason") == PORTFOLIO_HEADERS.index("ytm_pct") + 1
    assert (
        PORTFOLIO_HEADERS.index("duration_reason")
        == PORTFOLIO_HEADERS.index("modified_duration") + 1
    )
    assert SCHEMA_VERSION == 2


def test_build_payload_row_values_match_headers_order():
    """Каждая ячейка строки лежит на позиции своего заголовка."""
    control, rows = build_payload(make_rows())

    assert control["schema_version"] == 2
    assert len(rows[0]) == len(PORTFOLIO_HEADERS)
    expected = {
        "isin": "RU000A",
        "share_pct": 100.0,
        "ytm_pct": 18.0,
        "ytm_reason": None,
        "duration": 1.5,
        "modified_duration": 1.4,
        "duration_reason": None,
    }
    for column, value in expected.items():
        assert cell(rows[0], column) == value


def test_fixed_bond_computed_metrics_have_empty_reasons():
    """Рассчитанные метрики экспортируются с пустыми причинами."""
    _, rows = build_payload(make_rows())

    assert cell(rows[0], "ytm_pct") == 18.0
    assert cell(rows[0], "ytm_reason") is None
    assert cell(rows[0], "duration") == 1.5
    assert cell(rows[0], "duration_reason") is None


def test_floater_bond_exports_floating_coupon_reason():
    _, rows = build_payload(
        make_rows(
            floating_coupon_flag=True,
            ytm=None,
            ytm_null_reason="floating_coupon",
            duration_macaulay=None,
            duration_modified=None,
            duration_null_reason="floating_coupon",
        )
    )

    assert cell(rows[0], "ytm_pct") is None
    assert cell(rows[0], "ytm_reason") == "floating_coupon"
    assert cell(rows[0], "duration_reason") == "floating_coupon"


def test_amortizing_bond_exports_amortization_schedule_missing_reason():
    _, rows = build_payload(
        make_rows(
            amortization_flag=True,
            ytm=None,
            ytm_null_reason="amortization_schedule_missing",
            duration_macaulay=None,
            duration_modified=None,
            duration_null_reason="amortization_schedule_missing",
        )
    )

    assert cell(rows[0], "ytm_reason") == "amortization_schedule_missing"
    assert cell(rows[0], "duration_reason") == "amortization_schedule_missing"


def test_nonpositive_price_exports_reason_from_storage():
    """Неположительная цена: причина берётся из storage, не пересчитывается."""
    _, rows = build_payload(
        make_rows(
            price=0,
            ytm=None,
            ytm_null_reason="market_price_missing_or_nonpositive",
            duration_macaulay=None,
            duration_modified=None,
            duration_null_reason="market_price_missing_or_nonpositive",
        )
    )

    assert cell(rows[0], "ytm_reason") == "market_price_missing_or_nonpositive"
    assert cell(rows[0], "duration_reason") == "market_price_missing_or_nonpositive"


def test_maturity_in_past_exports_maturity_reason():
    _, rows = build_payload(
        make_rows(
            maturity_date=date(2020, 1, 1),
            ytm=None,
            ytm_null_reason="maturity_missing_or_past",
            duration_macaulay=None,
            duration_modified=None,
            duration_null_reason="maturity_missing_or_past",
        )
    )

    assert cell(rows[0], "ytm_reason") == "maturity_missing_or_past"
    assert cell(rows[0], "duration_reason") == "maturity_missing_or_past"


def test_duration_reason_empty_when_duration_calculated():
    """Если дюрация рассчитана, причина пуста, даже если в БД остался хвост."""
    _, rows = build_payload(make_rows(duration_null_reason="floating_coupon"))

    assert cell(rows[0], "duration") == 1.5
    assert cell(rows[0], "duration_reason") is None


def test_valuation_columns_follow_value_column():
    """valuation_source идёт сразу после value_rub, warning — сразу после него."""
    assert (
        PORTFOLIO_HEADERS.index("valuation_source")
        == PORTFOLIO_HEADERS.index("value_rub") + 1
    )
    assert (
        PORTFOLIO_HEADERS.index("valuation_warning")
        == PORTFOLIO_HEADERS.index("valuation_source") + 1
    )
    assert SCHEMA_VERSION == 2


def test_valuation_warning_by_source():
    """Warning только для неоднозначных веток; текст берётся из словаря контракта."""
    cases = {
        "broker_value": None,
        "broker_price": None,
        "market_price": None,
        "nominal_fallback": VALUATION_WARNINGS["nominal_fallback"],
        "zero": VALUATION_WARNINGS["zero"],
    }
    for source, expected_warning in cases.items():
        _, rows = build_payload(make_rows(valuation_source=source))
        assert cell(rows[0], "valuation_source") == source
        assert cell(rows[0], "valuation_warning") == expected_warning, source


def test_nominal_fallback_warning_explains_price_mismatch():
    """Кейс 008: нулевой price при ненулевом value — warning объясняет расхождение."""
    _, rows = build_payload(
        make_rows(valuation_source="nominal_fallback", price=0, value_rub=5000)
    )

    warning = cell(rows[0], "valuation_warning")
    assert warning == (
        "value_rub = quantity * nominal; "
        "price_rub may be empty/0 and differ from value_rub/quantity"
    )
    # Арифметика оценки не меняется: value и доли считаются как раньше.
    assert cell(rows[0], "value_rub") == 5000
    assert cell(rows[0], "share_pct") == 100.0


def test_missing_valuation_source_exports_empty_provenance():
    """Строка без valuation_source (старые данные) не ломает экспорт."""
    control, rows = build_payload(make_rows())

    assert cell(rows[0], "valuation_source") is None
    assert cell(rows[0], "valuation_warning") is None
    assert control["portfolio_value_rub"] == 10000


def test_stale_data_blocks_export_before_sheet_write():
    freshness = {
        "bonds_with_market_price": 100,
        "bonds_tradeable_count": 100,
        "bonds_tradeable_stale_or_missing": 0,
        "portfolio_positions_rows": 2,
        "portfolio_zero_value_positions": 0,
        "portfolio_zero_rows_suspicious": 0,
        "last_sync_time": datetime(2026, 9, 20, tzinfo=timezone.utc),
    }
    publisher = FakePublisher()
    use_case = SyncChatgptPortfolioUseCase(
        FakeDb(sample_rows(), freshness),
        publisher=publisher,
        ytm_engine=FakeYtmEngine(),
    )

    with pytest.raises(RuntimeError, match="freshness gate"):
        use_case.execute(None)

    assert publisher.call is None


class FakeWorksheet:
    def __init__(self):
        self.updates = []
        self.clears = 0
        self.values = []

    def update(self, **kwargs):
        self.updates.append(kwargs)
        values = kwargs["values"]
        if kwargs["range_name"] == "A1":
            self.values = values

    def clear(self):
        self.clears += 1
        self.values = []

    def get_all_values(self):
        return self.values


class FakeSpreadsheet:
    def __init__(self):
        self.sheets = {
            "CONTROL": FakeWorksheet(),
            "PORTFOLIO": FakeWorksheet(),
        }

    def worksheet(self, title):
        return self.sheets[title]


class FakeClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, _key):
        return self.spreadsheet


def test_publisher_marks_writing_then_ready():
    spreadsheet = FakeSpreadsheet()
    publisher = GoogleSheetsPortfolioPublisher(
        "sheet-id",
        "",
        client=FakeClient(spreadsheet),
    )

    publisher.publish(
        control={"snapshot_id": "snap-1", "positions_count": 1},
        headers=["isin"],
        rows=[["RU000A"]],
    )

    control = spreadsheet.sheets["CONTROL"]
    assert control.updates[0]["values"][1] == ["sync_status", "WRITING"]
    final_values = control.updates[-1]["values"]
    assert ["sync_status", "READY"] in final_values
    assert ["snapshot_id", "snap-1"] in final_values


def test_chatgpt_rows_return_null_reasons_from_db(db):
    """Storage отдаёт канонические причины NULL — экспортер берёт их оттуда."""
    db.add_bonds_to_catalog(
        [
            Bond(
                isin="RU000A9",
                name="Флоатер Синтетический",
                nominal=Decimal(1000),
                maturity_date=date(2030, 1, 1),
                currency="RUB",
                floating_coupon_flag=True,
            )
        ]
    )
    with db.conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE bonds_catalog
            SET ytm_null_reason = 'floating_coupon',
                ytm_updated_at = NOW(),
                duration_null_reason = 'floating_coupon',
                duration_updated_at = NOW()
            WHERE isin = 'RU000A9'
            """
        )
        cursor.execute(
            """
            INSERT INTO portfolio_positions
                (isin, broker_name, name, quantity, current_price, status, updated_at)
            VALUES ('RU000A9', 'tbank', 'Флоатер Синтетический', 10, 990, 'active', NOW())
            """
        )
    db.conn.commit()

    rows = db.get_chatgpt_portfolio_rows()

    assert len(rows) == 1
    assert rows[0]["ytm_null_reason"] == "floating_coupon"
    assert rows[0]["duration_null_reason"] == "floating_coupon"
