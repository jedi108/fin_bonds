from datetime import datetime, timezone

import pytest

from src.services.google_sheets import GoogleSheetsPortfolioPublisher
from src.use_cases.sync_chatgpt_portfolio import (
    PORTFOLIO_HEADERS,
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
    def _enrich_bonds_with_ytm(self, rows, _args):
        result = []
        for row in rows:
            item = dict(row)
            item.setdefault("ytm_percent", item.get("ytm"))
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
