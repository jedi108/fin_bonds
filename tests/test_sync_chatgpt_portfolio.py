import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.data_models import Bond
from src.services.google_sheets import GoogleSheetsPortfolioPublisher
from src.use_cases import sync_chatgpt_portfolio
from src.use_cases.rebalance_report import (
    SCREENER_FILTER_DESTS,
    RebalanceReportUseCase,
    add_screener_filter_arguments,
)
from src.use_cases.sync_chatgpt_portfolio import (
    CANDIDATE_HEADERS,
    CONSUMER_RULE,
    PORTFOLIO_HEADERS,
    SCHEMA_HEADERS,
    SCHEMA_VERSION,
    VALUATION_WARNINGS,
    SyncChatgptPortfolioUseCase,
    build_candidates_payload,
    build_schema_rows,
    candidate_report_args,
)


class FakeDb:
    def __init__(self, rows, freshness=None, cash=None):
        self.rows = rows
        self.cash = cash or {
            "cash_available_rub": None,
            "cash_updated_at": None,
            "cash_known": False,
        }
        # 029-T08: exporter не должен читать cash сам — счётчик для guard-теста.
        self.cash_reads = 0
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

    def get_available_cash_rub(self):
        """Canonical cash reader (022) — тот же контракт, что у storage."""
        self.cash_reads += 1
        return dict(self.cash)


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
        report_builder=lambda _args: fake_screener_report(),
    )

    control, rows, _candidates = use_case._build_payload(now)

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
        report_builder=lambda _args: fake_screener_report(),
    )
    control, rows, _candidates = use_case._build_payload(now)
    return control, rows


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
    assert SCHEMA_VERSION == 4


def test_build_payload_row_values_match_headers_order():
    """Каждая ячейка строки лежит на позиции своего заголовка."""
    control, rows = build_payload(make_rows())

    assert control["schema_version"] == 4
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
    assert SCHEMA_VERSION == 4


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
    """Лист с семантикой, близкой к реальной Google Sheets: ячейки хранятся,
    пока лист не очищен; update пишет блок в сетку с указанного диапазона и НЕ
    затирает хвост листа, если новый блок короче предыдущего. Именно поэтому
    shrink-тест ловит пропажу clear() из протокола публикации."""

    def __init__(self, name="sheet", events=None):
        self.name = name
        self.events = events if events is not None else []
        self.updates = []
        self.clears = 0
        self.grid = []

    @staticmethod
    def _range_start(range_name):
        first_cell = range_name.split(":")[0]
        match = re.fullmatch(r"([A-Z]+)(\d+)", first_cell)
        letters, row = match.group(1), int(match.group(2))
        col = 0
        for ch in letters:
            col = col * 26 + (ord(ch) - ord("A") + 1)
        return row - 1, col - 1

    def update(self, **kwargs):
        self.events.append(f"{self.name}.update")
        self.updates.append(kwargs)
        top, left = self._range_start(kwargs["range_name"])
        for row_offset, row_values in enumerate(kwargs["values"]):
            row_index = top + row_offset
            while len(self.grid) <= row_index:
                self.grid.append([])
            target = self.grid[row_index]
            while len(target) < left + len(row_values):
                target.append("")
            for col_offset, value in enumerate(row_values):
                target[left + col_offset] = value

    def clear(self):
        self.events.append(f"{self.name}.clear")
        self.clears += 1
        self.grid = []

    def get_all_values(self):
        self.events.append(f"{self.name}.get_all_values")
        return [list(row) for row in self.grid]

    def as_dicts(self):
        """CONTROL-лист как dict key/value — так его читает потребитель."""
        return {row[0]: row[1] for row in self.grid if row}


class FailingUpdateWorksheet(FakeWorksheet):
    """Лист, у которого падает запись блока данных с A1 (шаг после WRITING)."""

    def update(self, **kwargs):
        if kwargs["range_name"] == "A1":
            raise RuntimeError("sheet update failed")
        super().update(**kwargs)


class ShortReadWorksheet(FakeWorksheet):
    """Лист, чья проверка чтения возвращает на одну строку меньше записанной."""

    def get_all_values(self):
        return super().get_all_values()[:-1]


class FakeSpreadsheet:
    def __init__(self):
        self.events = []
        self.sheets = {
            "CONTROL": FakeWorksheet("CONTROL", self.events),
            "PORTFOLIO": FakeWorksheet("PORTFOLIO", self.events),
            "SCHEMA": FakeWorksheet("SCHEMA", self.events),
            "CANDIDATES": FakeWorksheet("CANDIDATES", self.events),
        }

    def worksheet(self, title):
        return self.sheets[title]


class FakeClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, _key):
        return self.spreadsheet


def make_publisher(spreadsheet):
    return GoogleSheetsPortfolioPublisher(
        "sheet-id",
        "",
        client=FakeClient(spreadsheet),
    )


def test_publisher_marks_writing_then_ready():
    spreadsheet = FakeSpreadsheet()
    publisher = make_publisher(spreadsheet)

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


def test_publisher_shrink_removes_tail_of_previous_snapshot():
    """Повторная публикация меньшего снапшота не оставляет хвост предыдущего."""
    spreadsheet = FakeSpreadsheet()
    publisher = make_publisher(spreadsheet)
    big_rows = [[f"RU000{i}"] for i in range(5)]
    small_rows = [["RU000A"], ["RU000B"]]

    publisher.publish(
        control={"snapshot_id": "snap-1", "positions_count": 5},
        headers=["isin"],
        rows=big_rows,
    )
    publisher.publish(
        control={"snapshot_id": "snap-2", "positions_count": 2},
        headers=["isin"],
        rows=small_rows,
    )

    portfolio = spreadsheet.sheets["PORTFOLIO"].get_all_values()
    assert len(portfolio) == 1 + len(small_rows)  # заголовок + данные
    isins = [row[0] for row in portfolio[1:]]
    assert isins == ["RU000A", "RU000B"]
    stale_tail = {row[0] for row in big_rows} - {row[0] for row in small_rows}
    assert not stale_tail.intersection(isins)
    control = spreadsheet.sheets["CONTROL"].as_dicts()
    assert control["sync_status"] == "READY"
    assert control["snapshot_id"] == "snap-2"


def test_publisher_failure_after_writing_leaves_control_not_ready():
    """Исключение на записи PORTFOLIO (после WRITING) прокидывается, READY нет."""
    spreadsheet = FakeSpreadsheet()
    spreadsheet.sheets["PORTFOLIO"] = FailingUpdateWorksheet(
        "PORTFOLIO", spreadsheet.events
    )
    publisher = make_publisher(spreadsheet)

    with pytest.raises(RuntimeError, match="sheet update failed"):
        publisher.publish(
            control={"snapshot_id": "snap-1", "positions_count": 1},
            headers=["isin"],
            rows=[["RU000A"]],
        )

    control = spreadsheet.sheets["CONTROL"].as_dicts()
    assert control["sync_status"] == "WRITING"
    assert "READY" not in control.values()


def test_publisher_verify_failure_does_not_publish_ready():
    """READY выставляется только после успешного row-count verify: расхождение
    числа прочитанных строк с записанным роняет publish без READY."""
    spreadsheet = FakeSpreadsheet()
    spreadsheet.sheets["PORTFOLIO"] = ShortReadWorksheet(
        "PORTFOLIO", spreadsheet.events
    )
    publisher = make_publisher(spreadsheet)

    with pytest.raises(RuntimeError, match="verification failed"):
        publisher.publish(
            control={"snapshot_id": "snap-1", "positions_count": 2},
            headers=["isin"],
            rows=[["RU000A"], ["RU000B"]],
        )

    control = spreadsheet.sheets["CONTROL"].as_dicts()
    assert control["sync_status"] == "WRITING"
    assert "READY" not in control.values()


def test_publisher_marks_writing_before_touching_portfolio():
    """Порядок протокола: WRITING-маркер пишется в CONTROL до любых изменений
    PORTFOLIO; READY — последняя операция, после verify."""
    spreadsheet = FakeSpreadsheet()
    publisher = make_publisher(spreadsheet)

    publisher.publish(
        control={"snapshot_id": "snap-1", "positions_count": 1},
        headers=["isin"],
        rows=[["RU000A"]],
    )

    events = spreadsheet.events
    control = spreadsheet.sheets["CONTROL"]
    assert control.updates[0]["values"][1] == ["sync_status", "WRITING"]
    assert events[0] == "CONTROL.update"  # WRITING раньше всего остального
    first_portfolio_change = min(
        events.index("PORTFOLIO.clear"), events.index("PORTFOLIO.update")
    )
    assert first_portfolio_change > events.index("CONTROL.update")
    # verify PORTFOLIO происходит до финального READY-обновления CONTROL...
    assert events.index("PORTFOLIO.get_all_values") < len(events) - 1
    # ...а финальная операция публикации — запись READY в CONTROL.
    assert events[-1] == "CONTROL.update"
    assert control.as_dicts()["sync_status"] == "READY"


def test_build_payload_positions_count_matches_table_rows():
    """positions_count равен фактическому числу строк данных таблицы."""
    db_rows = [make_rows(isin=f"RU000{i}", name=f"Bond {i}")[0] for i in range(5)]

    control, table_rows = build_payload(db_rows)

    assert control["positions_count"] == len(table_rows) == 5


def test_published_control_satisfies_consumer_contract():
    """Контракт потребителя: после успешной публикации CONTROL содержит
    sync_status=READY и тот же snapshot_id, positions_count совпадает с числом
    строк данных PORTFOLIO — такой снапшот можно читать."""
    spreadsheet = FakeSpreadsheet()
    publisher = make_publisher(spreadsheet)
    control_in = {"snapshot_id": "20260930T100000Z", "positions_count": 2}

    publisher.publish(
        control=control_in,
        headers=["isin"],
        rows=[["RU000A"], ["RU000B"]],
    )

    control = spreadsheet.sheets["CONTROL"].as_dicts()
    portfolio = spreadsheet.sheets["PORTFOLIO"].get_all_values()
    assert control["sync_status"] == "READY"
    assert control["snapshot_id"] == control_in["snapshot_id"]
    assert int(control["positions_count"]) == len(portfolio) - 1 == 2


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


# ---------------------------------------------------------------------------
# 019: candidate projection из канонического rebalance-report
# ---------------------------------------------------------------------------

def candidate_cell(row_values, column):
    """Ячейка кандидатной строки по имени колонки (как cell() для PORTFOLIO)."""
    return row_values[CANDIDATE_HEADERS.index(column)]


def screener_row(**overrides):
    """Строка screener в каноническом контракте (BOND_ROW_KEYS) для проекции."""
    row = {
        "isin": "RU000A",
        "name": "Bond A",
        "ticker": "TEST",
        "qty": 0.0,
        "price": 950.0,
        "value": 0.0,
        "share_pct": 0.0,
        "ytm_pct": 15.0,
        "ytm_reason": None,
        "coupon_pct": 12.0,
        "freq": 4,
        "coupon_kind": "fix",
        "risk_level": 1,
        "list_level": 2,
        "maturity": "2030-01-01",
        "offer": None,
        "amort": False,
        "ku": False,
        "issuer": "Issuer",
        "issuer_pct": None,
        "held_badge": None,
        "credit_rating": "AA-",
    }
    row.update(overrides)
    return row


def fake_screener_report(cash=None):
    """Канонический отчёт-фикстура: блок portfolio (cash контекст, 022) и
    блок screener с 2 фиксами и 1 флоатером. cash — dict из canonical reader
    (cash_available_rub/cash_updated_at/cash_known); по умолчанию unknown."""
    cash = cash or {
        "cash_available_rub": None,
        "cash_updated_at": None,
        "cash_known": False,
    }
    cash_known = bool(cash["cash_known"])
    return {
        "portfolio": {
            "cash_available_rub": (
                cash["cash_available_rub"] if cash_known else None
            ),
            "cash_updated_at": (
                cash["cash_updated_at"].isoformat() if cash_known else None
            ),
        },
        "screener": {
            "filters": {"max_risk": 1, "screener_limit": 10},
            "candidates_total": 7,
            "excluded": {"held": 1, "ku": 2},
            "fixed_matched": 3,
            "floater_matched": 1,
            "fixed": [
                screener_row(isin="RU000F1", name="Fix 1"),
                screener_row(isin="RU000F2", name="Fix 2", ytm_pct=14.0),
            ],
            "floater": [
                screener_row(
                    isin="RU000FL1",
                    name="Float 1",
                    coupon_kind="float",
                    ytm_pct=None,
                    ytm_reason="floating_coupon",
                ),
            ],
        }
    }


def test_candidate_report_args_only_flips_include_held():
    """Экспортный контекст: канонические defaults rebalance-report (без дублей
    в exporter), единственное изменение — include_held=True."""
    args = candidate_report_args()
    expected = vars(RebalanceReportUseCase.default_args())
    expected["include_held"] = True
    assert vars(args) == expected
    assert args.include_held is True


def test_candidate_projection_single_set_with_per_type_rank():
    """FIX и FLOAT идут в один набор; rank — с 1 отдельно внутри типа;
    порядок внутри типа сохраняет ranking канонического скринера."""
    _, rows = build_candidates_payload(fake_screener_report())

    assert [candidate_cell(r, "isin") for r in rows] == ["RU000F1", "RU000F2", "RU000FL1"]
    assert [candidate_cell(r, "candidate_type") for r in rows] == ["FIX", "FIX", "FLOAT"]
    assert [candidate_cell(r, "rank") for r in rows] == [1, 2, 1]


def test_candidate_projection_fields_match_source_screener():
    """Каждое поле проекции соответствует значению source screener."""
    source = fake_screener_report()["screener"]["fixed"][0]
    _, rows = build_candidates_payload(fake_screener_report())

    expected = {
        "candidate_type": "FIX",
        "rank": 1,
        "isin": source["isin"],
        "name": source["name"],
        "ticker": source["ticker"],
        "issuer": source["issuer"],
        "price_rub": source["price"],
        "ytm_pct": source["ytm_pct"],
        "ytm_reason": source["ytm_reason"],
        "coupon_pct": source["coupon_pct"],
        "coupon_frequency": source["freq"],
        "maturity_date": source["maturity"],
        "offer_date": source["offer"],
        "amortization_flag": source["amort"],
        "credit_rating": source["credit_rating"],
        "risk_level": source["risk_level"],
        "list_level": source["list_level"],
        "ku": source["ku"],
        "issuer_pct_current": source["issuer_pct"],
        "held_badge": source["held_badge"],
    }
    assert len(expected) == len(CANDIDATE_HEADERS)
    for column, value in expected.items():
        assert candidate_cell(rows[0], column) == value, column


def test_candidate_projection_carries_badge_and_issuer_pct_as_is():
    """held_badge и issuer_pct_current переносятся как есть (контекст
    include_held=True: держимый кандидат с бейджем и долей эмитента)."""
    report = fake_screener_report()
    report["screener"]["fixed"][0]["held_badge"] = "held (12.34%)"
    report["screener"]["fixed"][0]["issuer_pct"] = 12.34

    _, rows = build_candidates_payload(report)

    assert candidate_cell(rows[0], "held_badge") == "held (12.34%)"
    assert candidate_cell(rows[0], "issuer_pct_current") == 12.34


def test_candidate_projection_carries_screener_metadata():
    """metadata переносится из отчёта без ручного пересчёта."""
    report = fake_screener_report()
    metadata, _ = build_candidates_payload(report)

    assert metadata == {
        "filters": report["screener"]["filters"],
        "candidates_total": report["screener"]["candidates_total"],
        "fixed_matched": report["screener"]["fixed_matched"],
        "floater_matched": report["screener"]["floater_matched"],
        "excluded": report["screener"]["excluded"],
    }


def test_p0_portfolio_payload_not_touched_by_candidate_export():
    """P0 PORTFOLIO payload не меняется кандидатным экспортом: колонки те же
    (v3 поднимает только версию контракта), candidate_type/rank не протекают."""
    assert SCHEMA_VERSION == 4
    assert PORTFOLIO_HEADERS[0] == "isin"
    assert "candidate_type" not in PORTFOLIO_HEADERS
    assert "rank" not in PORTFOLIO_HEADERS


def _seed_screener_candidates(db):
    """Два фикса и один флоатер вне портфеля — живые кандидаты buy-выборки."""
    now = datetime.now(timezone.utc)

    def candidate(isin, name, **overrides):
        defaults = dict(
            isin=isin,
            name=name,
            ticker=isin[-5:],
            currency="rub",
            nominal=Decimal(1000),
            maturity_date=date.today() + timedelta(days=730),
            coupon_quantity_per_year=4,
            coupon_rate_percent=Decimal("14.0"),
            risk_level=1,
            list_level=1,
            is_trade_available=True,
            market_price=Decimal("950.0"),
            market_price_updated_at=now,
            ytm=Decimal("15.5"),
            ytm_updated_at=now,
        )
        defaults.update(overrides)
        return Bond(**defaults)

    db.add_bonds_to_catalog(
        [
            candidate("RU000A0CAND1", "Кандидат Фикс 1"),
            candidate("RU000A0CAND2", "Кандидат Фикс 2"),
            candidate(
                "RU000A0CFLTR",
                "Кандидат Флоатер",
                floating_coupon_flag=True,
                coupon_rate_percent=None,
                coupon_spread=Decimal("1.5"),
            ),
        ]
    )
    with db.conn.cursor() as cursor:
        # upsert каталога не пишет ytm; 018-гвард buy-выборки требует
        # у фиксов и свежую цену, и свежую YTM — выставляем напрямую.
        cursor.execute(
            "UPDATE bonds_catalog SET ytm = 15.5, ytm_updated_at = NOW() "
            "WHERE isin = 'RU000A0CAND1'"
        )
        cursor.execute(
            "UPDATE bonds_catalog SET ytm = 14.0, ytm_updated_at = NOW() "
            "WHERE isin = 'RU000A0CAND2'"
        )
        cursor.execute(
            "INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value) "
            "VALUES ('RU000A0CFLTR', CURRENT_DATE, 'floater_coupon_calculator', '18.0')"
        )
    db.conn.commit()


def test_candidates_payload_built_from_canonical_report(db):
    """Сквозной кейс 019: payload строится из программного канонического отчёта
    build_report(candidate_report_args()) — ranking и поля совпадают со
    screener отчёта, metadata переносится как есть."""
    _seed_screener_candidates(db)
    report = RebalanceReportUseCase(db=db).build_report(candidate_report_args())
    metadata, rows = build_candidates_payload(report)

    screener = report["screener"]
    fixed_count = len(screener["fixed"])
    assert fixed_count == 2
    assert [candidate_cell(r, "isin") for r in rows] == (
        [row["isin"] for row in screener["fixed"]]
        + [row["isin"] for row in screener["floater"]]
    )
    assert [candidate_cell(r, "candidate_type") for r in rows] == ["FIX", "FIX", "FLOAT"]
    assert [candidate_cell(r, "rank") for r in rows] == [1, 2, 1]

    first_fix = screener["fixed"][0]
    assert candidate_cell(rows[0], "price_rub") == first_fix["price"]
    assert candidate_cell(rows[0], "ytm_pct") == first_fix["ytm_pct"]
    assert candidate_cell(rows[0], "coupon_pct") == first_fix["coupon_pct"]
    assert candidate_cell(rows[0], "coupon_frequency") == first_fix["freq"]
    assert candidate_cell(rows[0], "maturity_date") == first_fix["maturity"]

    floater_row = screener["floater"][0]
    last = rows[-1]
    assert candidate_cell(last, "candidate_type") == "FLOAT"
    assert candidate_cell(last, "ytm_pct") == floater_row["ytm_pct"]
    assert candidate_cell(last, "ytm_reason") == floater_row["ytm_reason"]
    assert candidate_cell(last, "coupon_pct") == floater_row["coupon_pct"]

    assert metadata["filters"] == screener["filters"]
    assert metadata["candidates_total"] == screener["candidates_total"]
    assert metadata["fixed_matched"] == screener["fixed_matched"]
    assert metadata["floater_matched"] == screener["floater_matched"]
    assert metadata["excluded"] == screener["excluded"]


# ---------------------------------------------------------------------------
# 020/025: schema v4 — SCHEMA + CANDIDATES + cash-контекст в атомарном
# протоколе публикации
# ---------------------------------------------------------------------------


def make_v4_use_case(db_rows, publisher, report_builder=None):
    return SyncChatgptPortfolioUseCase(
        FakeDb(db_rows),
        publisher=publisher,
        ytm_engine=FakeYtmEngine(),
        report_builder=report_builder or (lambda _args: fake_screener_report()),
    )


def test_schema_documents_every_header_in_order():
    """Тест ловит header, отсутствующий в SCHEMA: каждая колонка PORTFOLIO и
    CANDIDATES имеет строку в SCHEMA того же листа и в том же порядке."""
    rows = build_schema_rows()
    assert all(len(row) == len(SCHEMA_HEADERS) for row in rows)
    assert [row[1] for row in rows if row[0] == "PORTFOLIO"] == list(PORTFOLIO_HEADERS)
    assert [row[1] for row in rows if row[0] == "CANDIDATES"] == list(CANDIDATE_HEADERS)


def test_schema_documents_control_keys():
    """Все ключи CONTROL (sync_status + payload) описаны в SCHEMA — без пропусков
    и без устаревших записей."""
    use_case = make_v4_use_case(sample_rows(), FakePublisher())
    control, _rows, _candidates = use_case._build_payload(
        datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    )

    documented = {row[1] for row in build_schema_rows() if row[0] == "CONTROL"}
    assert documented == {"sync_status", *control.keys()}


def test_schema_describes_units_and_semantics():
    """Точечные проверки контракта SCHEMA: единицы, NULL-semantics, valuation,
    issuer_pct_current до гипотетической покупки, limitation свежести."""
    rows = {(row[0], row[1]): row for row in build_schema_rows()}
    units = {row[2] for row in build_schema_rows()}
    for unit in ("RUB", "percent", "percent_per_year", "years", "count", "ISO date"):
        assert any(unit in u for u in units), unit

    assert rows[("PORTFOLIO", "quantity")][2] == "count"
    assert rows[("PORTFOLIO", "price_rub")][2] == "RUB"
    assert rows[("PORTFOLIO", "share_pct")][2] == "percent"
    assert rows[("PORTFOLIO", "ytm_pct")][2] == "percent_per_year or empty"
    assert rows[("PORTFOLIO", "duration")][2] == "years or empty"
    assert rows[("PORTFOLIO", "maturity_date")][2] == "ISO date"
    # NULL semantics: пустое значение — осознанный NULL, причина рядом.
    assert "not_applicable" in rows[("PORTFOLIO", "ytm_reason")][3]
    assert "ytm_reason" in rows[("PORTFOLIO", "ytm_pct")][3]
    # valuation_source / valuation_warning описаны.
    assert "nominal_fallback" in rows[("PORTFOLIO", "valuation_source")][3]
    assert "nominal_fallback" in rows[("PORTFOLIO", "valuation_warning")][3]
    assert "zero" in rows[("PORTFOLIO", "valuation_warning")][3]
    # issuer_pct_current — доля ДО гипотетической покупки.
    assert "BEFORE" in rows[("CANDIDATES", "issuer_pct_current")][3]
    # limitation: freshness market price по DB write timestamp.
    assert "DB write timestamp" in rows[("CONTROL", "market_updated_at")][3]
    assert "trade timestamp" in rows[("CONTROL", "market_updated_at")][3]
    # 025: cash-поля и различие bonds-only / investable описаны в SCHEMA.
    assert rows[("CONTROL", "cash_available_rub")][2] == "RUB or empty"
    assert rows[("CONTROL", "investable_total_rub")][2] == "RUB or empty"
    assert "NOT including cash" in rows[("CONTROL", "portfolio_value_rub")][3]
    assert "unknown, NOT as zero" in rows[("CONTROL", "cash_available_rub")][3]
    assert (
        rows[("CONTROL", "investable_total_rub")][3]
        == "portfolio_value_rub + cash_available_rub (bonds + free cash = total investable capital); empty when cash is unknown"
    )
    assert "last successful cash sync" in rows[("CONTROL", "cash_updated_at")][3]
    # 025: credit_rating кандидата — canonical, risk_level рейтингом не служит.
    assert rows[("CANDIDATES", "credit_rating")][2] == "string or empty"
    assert "rating_history" in rows[("CANDIDATES", "credit_rating")][3]
    assert "NOT a substitute for credit_rating" in rows[("CANDIDATES", "risk_level")][3]
    # 025: consumer rule предупреждает о пустых cash-ячейках.
    assert "unknown cash, not zero" in CONSUMER_RULE


def test_build_payload_v3_control_fields_and_candidates():
    """schema v3: control несёт кандидатов и контракт потребителя; кандидатные
    строки — третий элемент payload, count совпадает."""
    use_case = make_v4_use_case(sample_rows(), FakePublisher())

    control, rows, candidate_rows = use_case._build_payload(
        datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    )

    assert control["schema_version"] == 4
    assert control["positions_count"] == len(rows)
    assert control["candidates_count"] == len(candidate_rows) == 3
    assert json.loads(control["candidate_meta"]) == {
        "filters": {"max_risk": 1, "screener_limit": 10},
        "candidates_total": 7,
        "fixed_matched": 3,
        "floater_matched": 1,
        "excluded": {"held": 1, "ku": 2},
    }
    assert control["contract_sheet"] == "SCHEMA"
    assert control["consumer_rule"] == CONSUMER_RULE


def test_build_payload_v4_control_carries_known_cash():
    """schema v4: известный cash попадает в CONTROL из report["portfolio"]
    того же canonical report run, investable_total_rub = transport
    portfolio_value_rub + cash_available_rub; exporter сам cash не читает
    (второго get_available_cash_rub после report_builder нет, 029-T08)."""
    db = FakeDb(sample_rows())
    use_case = SyncChatgptPortfolioUseCase(
        db,
        publisher=FakePublisher(),
        ytm_engine=FakeYtmEngine(),
        report_builder=lambda _args: fake_screener_report(
            cash={
                "cash_available_rub": 1234.56,
                "cash_updated_at": datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
                "cash_known": True,
            }
        ),
    )
    control, _rows, _candidates = use_case._build_payload(
        datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    )

    assert control["schema_version"] == 4
    assert control["portfolio_value_rub"] == 25000
    assert control["cash_available_rub"] == 1234.56
    assert control["investable_total_rub"] == 26234.56
    assert control["cash_updated_at"] == "2026-09-30T09:00:00+00:00"
    assert db.cash_reads == 0


def test_build_payload_v4_unknown_cash_is_empty_not_zero():
    """unknown cash (sync ещё не выполнялся): CONTROL публикует пустые
    значения (publisher пишет их как пустые ячейки), не нули."""
    use_case = make_v4_use_case(sample_rows(), FakePublisher())
    control, _rows, _candidates = use_case._build_payload(
        datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    )

    assert control["cash_available_rub"] is None
    assert control["investable_total_rub"] is None
    assert control["cash_updated_at"] is None


# ---------------------------------------------------------------------------
# 029-T08: один canonical cash read + candidate metadata + stdout контракт
# ---------------------------------------------------------------------------

# 13 canonical filter полей report["screener"]["filters"] (раздел 13).
CANONICAL_FILTER_FIELDS = (
    "freq_min",
    "freq_max",
    "freq_in",
    "min_credit_rating",
    "exclude_sovereign",
    "include_held",
    "include_ku",
    "max_risk",
    "max_listlevel",
    "max_ytm_pct",
    "min_maturity",
    "max_maturity",
    "screener_limit",
)


def _t08_report():
    """Канонический отчёт с полным filters-блоком (13 полей) и известным cash."""
    report = fake_screener_report(
        cash={
            "cash_available_rub": 4321.5,
            "cash_updated_at": datetime(2026, 9, 30, 9, 30, tzinfo=timezone.utc),
            "cash_known": True,
        }
    )
    report["screener"]["filters"] = {
        "freq_min": None,
        "freq_max": None,
        "freq_in": [2],
        "min_credit_rating": "AA-",
        "exclude_sovereign": True,
        "include_held": True,
        "include_ku": False,
        "max_risk": 2,
        "max_listlevel": 2,
        "max_ytm_pct": 20.0,
        "min_maturity": None,
        "max_maturity": None,
        "screener_limit": 30,
    }
    return report


def test_candidate_meta_filters_are_canonical_thirteen_fields():
    """029-T08: candidate_meta.filters == report["screener"]["filters"] как есть
    (без ручной реконструкции); CONTROL показывает все 13 canonical полей;
    cash CONTROL — из report["portfolio"], exporter сам cash не читает."""
    report = _t08_report()
    db = FakeDb(sample_rows())
    use_case = SyncChatgptPortfolioUseCase(
        db,
        publisher=FakePublisher(),
        ytm_engine=FakeYtmEngine(),
        report_builder=lambda _args: report,
    )
    control, _rows, _candidates = use_case._build_payload(
        datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    )

    meta = json.loads(control["candidate_meta"])
    assert len(CANONICAL_FILTER_FIELDS) == 13
    assert set(meta["filters"]) == set(CANONICAL_FILTER_FIELDS)
    assert meta["filters"] == report["screener"]["filters"]
    assert db.cash_reads == 0


def test_execute_stdout_carries_cash_and_candidate_filters(capsys):
    """029-T08 stdout-контракт (раздел 26): Свободные деньги / Cash snapshot /
    Candidate filters; filters — canonical serialization report["screener"]
    ["filters"] (skill T15 не угадывает значения по переданной команде)."""
    report = _t08_report()
    use_case = SyncChatgptPortfolioUseCase(
        FakeDb(sample_rows()),
        publisher=FakePublisher(),
        ytm_engine=FakeYtmEngine(),
        report_builder=lambda _args: report,
    )

    use_case.execute(None)

    out = capsys.readouterr().out
    assert "Свободные деньги: 4321.5 ₽" in out
    assert "Cash snapshot: 2026-09-30T09:30:00+00:00" in out
    prefix = "Candidate filters: "
    filters_line = next(line for line in out.splitlines() if line.startswith(prefix))
    assert json.loads(filters_line[len(prefix):]) == report["screener"]["filters"]


def test_execute_stdout_unknown_cash_and_filters(capsys):
    """unknown cash в stdout: Свободные деньги «неизвестно», Cash snapshot
    «unknown»; Candidate filters печатается и для canonical defaults."""
    use_case = make_v4_use_case(sample_rows(), FakePublisher())

    use_case.execute(None)

    out = capsys.readouterr().out
    assert "Свободные деньги: неизвестно" in out
    assert "Cash snapshot: unknown" in out
    assert "Candidate filters: " in out


def test_candidates_projection_carries_credit_rating():
    """025: CANDIDATES переносит canonical credit_rating из строки скринера."""
    report = fake_screener_report()
    report["screener"]["fixed"][0]["credit_rating"] = "AA"
    report["screener"]["fixed"][1]["credit_rating"] = None

    _, rows = build_candidates_payload(report)

    assert candidate_cell(rows[0], "credit_rating") == "AA"
    assert candidate_cell(rows[1], "credit_rating") is None
    assert CANDIDATE_HEADERS.index("credit_rating") == CANDIDATE_HEADERS.index(
        "amortization_flag"
    ) + 1


def test_execute_publishes_v3_sheets_to_publisher():
    """execute передаёт в publisher один снапшот: PORTFOLIO + SCHEMA + CANDIDATES."""
    publisher = FakePublisher()
    use_case = make_v4_use_case(sample_rows(), publisher)

    use_case.execute(None)

    call = publisher.call
    assert call["headers"] == PORTFOLIO_HEADERS
    assert call["schema_headers"] == SCHEMA_HEADERS
    assert call["schema_rows"] == build_schema_rows()
    assert call["candidate_headers"] == CANDIDATE_HEADERS
    assert len(call["candidate_rows"]) == call["control"]["candidates_count"] == 3


def test_stale_data_blocks_candidate_report_before_sheet_write():
    """Freshness-гейт срабатывает до построения кандидатного отчёта и записи."""
    calls = []

    def spy_report_builder(args):
        calls.append(args)
        return fake_screener_report()

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
        report_builder=spy_report_builder,
    )

    with pytest.raises(RuntimeError, match="freshness gate"):
        use_case.execute(None)

    assert not calls
    assert publisher.call is None


def test_publisher_writes_schema_and_candidates_between_writing_and_ready():
    """Порядок протокола v3: WRITING → SCHEMA → PORTFOLIO → CANDIDATES →
    verify обеих таблиц → READY последней операцией."""
    spreadsheet = FakeSpreadsheet()
    publisher = make_publisher(spreadsheet)

    publisher.publish(
        control={
            "schema_version": 4,
            "positions_count": 2,
            "candidates_count": 2,
            "contract_sheet": "SCHEMA",
        },
        headers=["isin"],
        rows=[["RU000A"], ["RU000B"]],
        schema_headers=["sheet", "field"],
        schema_rows=[["PORTFOLIO", "isin"], ["CANDIDATES", "isin"]],
        candidate_headers=["candidate_type", "isin"],
        candidate_rows=[["FIX", "RU000F1"], ["FLOAT", "RU000FL1"]],
    )

    events = spreadsheet.events
    control = spreadsheet.sheets["CONTROL"]
    assert events[0] == "CONTROL.update"  # WRITING раньше всего остального
    assert events.index("SCHEMA.update") < events.index("PORTFOLIO.clear")
    assert events.index("CANDIDATES.clear") > events.index("PORTFOLIO.update")
    # обе проверки строк происходят до финального READY-обновления CONTROL
    assert events.index("PORTFOLIO.get_all_values") < events.index(
        "CANDIDATES.get_all_values"
    ) < len(events) - 1
    assert events[-1] == "CONTROL.update"
    assert control.as_dicts()["sync_status"] == "READY"
    assert control.as_dicts()["contract_sheet"] == "SCHEMA"

    candidates = spreadsheet.sheets["CANDIDATES"].get_all_values()
    assert candidates[0] == ["candidate_type", "isin"]
    assert len(candidates) - 1 == 2
    schema = spreadsheet.sheets["SCHEMA"].get_all_values()
    assert schema[0] == ["sheet", "field"]
    assert len(schema) - 1 == 2


def test_publisher_candidates_shrink_removes_stale_tail():
    """Повторная публикация меньшего набора кандидатов удаляет stale tail."""
    spreadsheet = FakeSpreadsheet()
    publisher = make_publisher(spreadsheet)

    publisher.publish(
        control={"candidates_count": 4},
        headers=["isin"],
        rows=[["RU000A"]],
        candidate_headers=["isin"],
        candidate_rows=[["RU000F1"], ["RU000F2"], ["RU000F3"], ["RU000F4"]],
    )
    publisher.publish(
        control={"candidates_count": 2},
        headers=["isin"],
        rows=[["RU000A"]],
        candidate_headers=["isin"],
        candidate_rows=[["RU000F1"], ["RU000F2"]],
    )

    candidates = spreadsheet.sheets["CANDIDATES"].get_all_values()
    assert [row[0] for row in candidates[1:]] == ["RU000F1", "RU000F2"]
    control = spreadsheet.sheets["CONTROL"].as_dicts()
    assert control["sync_status"] == "READY"
    assert int(control["candidates_count"]) == 2


def test_publisher_candidates_verify_failure_does_not_publish_ready():
    """READY только после обеих проверок: расхождение числа строк CANDIDATES
    роняет publish без READY."""
    spreadsheet = FakeSpreadsheet()
    spreadsheet.sheets["CANDIDATES"] = ShortReadWorksheet(
        "CANDIDATES", spreadsheet.events
    )
    publisher = make_publisher(spreadsheet)

    with pytest.raises(RuntimeError, match="verification failed"):
        publisher.publish(
            control={"candidates_count": 2},
            headers=["isin"],
            rows=[["RU000A"]],
            candidate_headers=["isin"],
            candidate_rows=[["RU000F1"], ["RU000F2"]],
        )

    control = spreadsheet.sheets["CONTROL"].as_dicts()
    assert control["sync_status"] == "WRITING"
    assert "READY" not in control.values()


def test_publisher_candidates_failure_after_writing_leaves_control_not_ready():
    """Ошибка записи CANDIDATES (после WRITING) прокидывается, READY нет."""
    spreadsheet = FakeSpreadsheet()
    spreadsheet.sheets["CANDIDATES"] = FailingUpdateWorksheet(
        "CANDIDATES", spreadsheet.events
    )
    publisher = make_publisher(spreadsheet)

    with pytest.raises(RuntimeError, match="sheet update failed"):
        publisher.publish(
            control={"candidates_count": 1},
            headers=["isin"],
            rows=[["RU000A"]],
            candidate_headers=["isin"],
            candidate_rows=[["RU000F1"]],
        )

    control = spreadsheet.sheets["CONTROL"].as_dicts()
    assert control["sync_status"] == "WRITING"
    assert "READY" not in control.values()


# ---------------------------------------------------------------------------
# 029-T04: factory-backed canonical report builder (rating_scale)
# ---------------------------------------------------------------------------

# Зеркало шкалы config.yaml (rating_scale) — как в test_rebalance_report:
# ключи с национальным суффиксом, нормализация внутри rating_code_to_score.
RATING_SCALE = {
    "AAA.RU": 13, "AA+.RU": 12, "AA.RU": 11, "AA-.RU": 10,
    "A+.RU": 9, "A.RU": 8, "A-.RU": 7,
}


class FakeReportDb:
    """Минимальный storage-контракт RebalanceReportUseCase.build_report без БД:
    portfolio и redemptions пустые, screener — строки buy-выборки
    get_bonds_yield_table (rating_score уже в строке, как даёт SQL)."""

    def __init__(self, screener_rows):
        self.screener_rows = screener_rows
        self.freshness = {
            "bonds_with_market_price": 100,
            "bonds_tradeable_count": 100,
            "bonds_tradeable_stale_or_missing": 0,
            "portfolio_positions_rows": 0,
            "portfolio_zero_value_positions": 0,
            "portfolio_zero_rows_suspicious": 0,
            "portfolio_zero_value_isins": "",
            "last_sync_time": datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
            "portfolio_max_updated_at": datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
            "market_price_max_updated_at": datetime(
                2026, 9, 30, 8, 55, tzinfo=timezone.utc
            ),
        }

    def get_db_freshness(self):
        return dict(self.freshness)

    def get_rebalance_portfolio_rows(self):
        return []

    def get_bonds_yield_table(self, **_kwargs):
        return [dict(row) for row in self.screener_rows]

    def get_portfolio_redemption_events(self, _months):
        return []

    def get_available_cash_rub(self):
        return {"cash_available_rub": None, "cash_updated_at": None, "cash_known": False}


class FakeFactory:
    """Дублёр UseCaseFactory: кешированный db connection + config;
    повторно прочитать config.yaml через него невозможно по построению."""

    def __init__(self, db, config):
        self._db = db
        self.config = config

    def get_db_connection(self):
        return self._db


def screener_candidate(isin, rating_score=None, credit_rating=None):
    """Строка buy-выборки storage (SQL get_bonds_yield_table): ytm свежий —
    enrichment берёт его как есть; rating_score — latest rating_history."""
    return {
        "isin": isin,
        "name": f"Bond {isin}",
        "ticker": isin[-5:],
        "issuer": "Issuer",
        "quantity": 0,
        "value_rub": 0,
        "price": 950.0,
        "nominal": 1000.0,
        "ytm": 15.0,
        "ytm_updated_at": datetime(2026, 9, 30, 8, 55, tzinfo=timezone.utc),
        "ytm_null_reason": None,
        "coupon_rate_percent": 12.0,
        "coupon_quantity_per_year": 4,
        "floating_coupon_flag": False,
        "amortization_flag": False,
        "perpetual_flag": False,
        "risk_level": 1,
        "list_level": 1,
        "ku": False,
        "held_share_pct": None,
        "credit_rating": credit_rating,
        "rating_score": rating_score,
        "maturity_date": date(2030, 1, 1),
        "offer_date": None,
    }


def make_factory_exporter(screener_rows):
    """Factory-path exporter: create(FakeFactory) с canonical rating_scale."""
    db = FakeReportDb(screener_rows)
    factory = FakeFactory(db, {"rating_scale": RATING_SCALE})
    return SyncChatgptPortfolioUseCase.create(factory), db, factory


def test_create_injects_factory_canonical_report_builder():
    """create(factory) инжектит factory-created canonical
    RebalanceReportUseCase.build_report: тот же db instance (кешированный
    factory connection) и та же шкала конфига по identity — второй rating
    scale в exporter не создаётся, config.yaml сам exporter не читает."""
    rows = [screener_candidate("RU000CANDA", rating_score=10, credit_rating="AA-")]
    exporter, db, factory = make_factory_exporter(rows)
    builder_use_case = exporter._report_builder.report_use_case

    assert exporter.db is db
    assert isinstance(builder_use_case, RebalanceReportUseCase)
    assert builder_use_case.db is db
    assert builder_use_case.rating_scale is factory.config["rating_scale"]
    assert not hasattr(sync_chatgpt_portfolio, "load_config")


def test_factory_path_equivalent_to_canonical_create():
    """Production create-path эквивалентен RebalanceReportUseCase.create
    (factory): на тех же данных и args screener отчёта совпадает."""
    rows = [screener_candidate("RU000CANDA", rating_score=10, credit_rating="AA-")]
    exporter, _db, factory = make_factory_exporter(rows)

    args = candidate_report_args()
    args.min_credit_rating = "AA-"
    report_via_exporter = exporter._report_builder(args)
    report_direct = RebalanceReportUseCase.create(factory).build_report(args)

    assert report_via_exporter["screener"] == report_direct["screener"]


def test_factory_path_filters_min_credit_rating():
    """При min-credit-rating AA- factory-path exporter реально фильтрует:
    AA- проходит (порог включительно), рейтинг ниже порога и отсутствие
    рейтинга отсекаются с честным подсчётом в excluded."""
    rows = [
        screener_candidate("RU000CANDA", rating_score=10, credit_rating="AA-"),
        screener_candidate("RU000CANDL", rating_score=8, credit_rating="A"),
        screener_candidate("RU000CANDN", rating_score=None, credit_rating=None),
    ]
    exporter, _db, _factory = make_factory_exporter(rows)

    args = candidate_report_args()
    args.min_credit_rating = "AA-"
    report = exporter._report_builder(args)

    excluded = report["screener"]["excluded"]
    assert excluded["rating_below_min"] == 1
    assert excluded["rating_missing"] == 1
    assert [row["isin"] for row in report["screener"]["fixed"]] == ["RU000CANDA"]
    assert report["screener"]["filters"]["min_credit_rating"] == "AA-"


def test_factory_path_unknown_rating_code_canonical_cli_error():
    """Unknown rating code — та же ошибка, что canonical rebalance-report CLI:
    текст совпадает с ошибкой _validate_args factory-created use case."""
    rows = [screener_candidate("RU000CANDA", rating_score=10, credit_rating="AA-")]
    exporter, _db, factory = make_factory_exporter(rows)

    args = candidate_report_args()
    args.min_credit_rating = "XYZ"
    with pytest.raises(ValueError) as exporter_error:
        exporter._report_builder(args)

    canonical = RebalanceReportUseCase.create(factory)
    with pytest.raises(ValueError) as cli_error:
        canonical._validate_args(args)

    assert "не входит в шкалу rating_scale" in str(exporter_error.value)
    assert str(exporter_error.value) == str(cli_error.value)


def test_builder_does_not_duplicate_validation(monkeypatch):
    """029-T05: exporter не дублирует canonical validation — явные вызовы
    _validate_args из 029-T04 убраны, args валидируются ровно один раз
    внутри build_report (второй точки validation нет)."""
    rows = [screener_candidate("RU000CANDA", rating_score=10, credit_rating="AA-")]
    exporter, _db, _factory = make_factory_exporter(rows)

    calls = []
    original = RebalanceReportUseCase._validate_args

    def counting_validate(self, args):
        calls.append(args)
        return original(self, args)

    monkeypatch.setattr(RebalanceReportUseCase, "_validate_args", counting_validate)

    report = exporter._report_builder(candidate_report_args())

    assert len(calls) == 1
    assert report["schema_version"] == 2


def test_injected_report_builder_seam_still_supported():
    """Injection seam не сужается: поданный в constructor builder вызывается
    как есть с candidate args (include_held=True) — без дополнительной
    обёртки поверх; payload строится из его отчёта."""
    seen = []

    def spy_report_builder(args):
        seen.append(args)
        return fake_screener_report()

    use_case = SyncChatgptPortfolioUseCase(
        FakeDb(sample_rows()),
        publisher=FakePublisher(),
        ytm_engine=FakeYtmEngine(),
        report_builder=spy_report_builder,
    )
    control, _rows, candidates = use_case._build_payload(
        datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    )

    assert len(seen) == 1
    assert vars(seen[0]) == vars(candidate_report_args())
    assert seen[0].include_held is True
    assert control["candidates_count"] == len(candidates) == 3


def test_direct_constructor_min_credit_rating_fail_fast():
    """Прямой constructor без factory/report_builder: strict min-credit-rating
    fail-fast по canonical validation (пустая шкала не валидирует код), а не
    silent skip rating filter."""
    use_case = SyncChatgptPortfolioUseCase(
        FakeReportDb([screener_candidate("RU000CANDA", rating_score=10)]),
        publisher=FakePublisher(),
        ytm_engine=FakeYtmEngine(),
    )
    args = candidate_report_args()
    args.min_credit_rating = "AA-"

    with pytest.raises(ValueError, match="не входит в шкалу rating_scale"):
        use_case._report_builder(args)


def test_default_builder_without_rating_threshold_builds_canonical_report():
    """Без min-credit-rating прямой constructor строит тот же канонический
    отчёт (canonical validation канонические defaults пропускает)."""
    rows = [
        screener_candidate("RU000CANDA", rating_score=10, credit_rating="AA-"),
        screener_candidate("RU000CANDL", rating_score=8, credit_rating="A"),
    ]
    use_case = SyncChatgptPortfolioUseCase(
        FakeReportDb(rows),
        publisher=FakePublisher(),
        ytm_engine=FakeYtmEngine(),
    )

    report = use_case._report_builder(candidate_report_args())

    assert report["schema_version"] == 2
    assert {row["isin"] for row in report["screener"]["fixed"]} == {
        "RU000CANDA",
        "RU000CANDL",
    }


# ---------------------------------------------------------------------------
# 029-T06: общий helper screener CLI args — sync-chatgpt-portfolio
# ---------------------------------------------------------------------------

def _sync_parse(argv):
    parser = argparse.ArgumentParser(add_help=False)
    SyncChatgptPortfolioUseCase.setup_parser(parser)
    return parser.parse_args(argv)


def _sync_parser_options(parser):
    return {
        option
        for action in parser._actions
        for option in action.option_strings
    }


class TestScreenerCliArgsForwarding:
    def test_no_args_keeps_default_export(self):
        """Пустой CLI: в sync Namespace нет materialized defaults, кандидатные
        args = canonical defaults rebalance-report + include_held=True —
        текущий default export не меняется."""
        overrides = _sync_parse([])
        for dest in SCREENER_FILTER_DESTS:
            assert not hasattr(overrides, dest)
        assert vars(candidate_report_args(overrides)) == vars(candidate_report_args())

    def test_all_12_candidate_filter_args_accepted(self):
        """sync parser принимает все 12 candidate-filter args из спеки 029-T06."""
        parser = argparse.ArgumentParser(add_help=False)
        SyncChatgptPortfolioUseCase.setup_parser(parser)
        opts = _sync_parser_options(parser)
        for flag in (
            "--freq-min", "--freq-max", "--freq-in", "--min-credit-rating",
            "--exclude-sovereign", "--include-ku", "--max-risk",
            "--max-listlevel", "--max-ytm", "--min-maturity",
            "--max-maturity", "--screener-limit",
        ):
            assert flag in opts
        # и ничего лишнего сверх 12 фильтров не появляется (add_help=False)
        expected = {f"--{dest.replace('_', '-')}" for dest in SCREENER_FILTER_DESTS}
        assert opts == expected

    def test_freq_rating_sovereign_ku_forwarded(self):
        overrides = _sync_parse([
            "--freq-min", "4", "--freq-max", "12",
            "--min-credit-rating", "AA-", "--exclude-sovereign", "--include-ku",
        ])
        args = candidate_report_args(overrides)
        assert args.freq_min == 4
        assert args.freq_max == 12
        assert args.min_credit_rating == "AA-"
        assert args.exclude_sovereign is True
        assert args.include_ku is True
        # непереданные фильтры остаются canonical defaults (не нулями/копией)
        assert args.max_risk == 1
        assert args.max_listlevel == 2
        assert args.max_ytm == 35.0
        assert args.screener_limit == 10

    def test_risk_listlevel_ytm_maturity_limit_forwarded(self):
        overrides = _sync_parse([
            "--max-risk", "3", "--max-listlevel", "3", "--max-ytm", "20",
            "--min-maturity", "2027-01-01", "--max-maturity", "2030-06-30",
            "--screener-limit", "5",
        ])
        args = candidate_report_args(overrides)
        assert args.max_risk == 3
        assert args.max_listlevel == 3
        assert args.max_ytm == 20.0
        assert args.min_maturity == "2027-01-01"
        assert args.max_maturity == "2030-06-30"
        assert args.screener_limit == 5

    def test_freq_in_forwarded(self):
        args = candidate_report_args(_sync_parse(["--freq-in", "2,4"]))
        assert args.freq_in == [2, 4]
        assert args.freq_min is None
        assert args.freq_max is None

    def test_include_held_stays_true_product_default(self):
        """include_held=True — product-default exporter при любых overrides;
        бессмысленного --include-held store_true и --exclude-held нет."""
        args = candidate_report_args(_sync_parse(["--include-ku", "--max-risk", "2"]))
        assert args.include_held is True
        parser = argparse.ArgumentParser(add_help=False)
        SyncChatgptPortfolioUseCase.setup_parser(parser)
        opts = _sync_parser_options(parser)
        assert "--include-held" not in opts
        assert "--exclude-held" not in opts

    def test_rebalance_specific_args_not_in_sync_parser(self):
        """scenario/csv/max-position-value/redemptions/format/include-held
        остаются у rebalance-report — exporter их не принимает."""
        parser = argparse.ArgumentParser(add_help=False)
        SyncChatgptPortfolioUseCase.setup_parser(parser)
        opts = _sync_parser_options(parser)
        for flag in (
            "--format", "--include-held", "--max-position-value",
            "--redemptions-months", "--csv-out", "--scenario",
        ):
            assert flag not in opts


class TestScreenerCliArgsDriftGuard:
    def test_defaults_come_from_shared_definitions(self):
        """Drift guard: defaults=True даёт те же canonical значения, что
        default_args() (общие parser definitions — единственный источник);
        sync Namespace (defaults=False) ничего не materialize — второй
        источник defaults появиться не может."""
        parser = argparse.ArgumentParser(add_help=False)
        add_screener_filter_arguments(parser, defaults=True)
        helper_defaults = vars(parser.parse_args([]))
        assert set(helper_defaults) == set(SCREENER_FILTER_DESTS)
        canonical = vars(RebalanceReportUseCase.default_args())
        for dest, value in helper_defaults.items():
            assert canonical[dest] == value, dest

    def test_overrides_reach_canonical_validation(self):
        """Forwarded фильтры попадают в canonical args: невалидная комбинация
        (freq-max + freq-in) отклоняется той же validation, что и у CLI
        rebalance-report (exporter второй валидации не заводит)."""
        args = candidate_report_args(
            _sync_parse(["--freq-max", "4", "--freq-in", "2,4"])
        )
        use_case = RebalanceReportUseCase.__new__(RebalanceReportUseCase)
        with pytest.raises(ValueError, match="взаимоисключающие"):
            use_case._validate_args(args)


# ---------------------------------------------------------------------------
# 029-T07: data flow args — execute(args) -> candidate_report_args -> build_report
# ---------------------------------------------------------------------------

class FakeExportPortfolioDb(FakeReportDb):
    """FakeReportDb + строки PORTFOLIO exporter'а: одного db хватает на весь
    execute — и get_chatgpt_portfolio_rows, и canonical screener build_report."""

    def __init__(self, screener_rows, portfolio_rows):
        super().__init__(screener_rows)
        self.freshness["portfolio_positions_rows"] = len(portfolio_rows)
        self._portfolio_rows = portfolio_rows

    def get_chatgpt_portfolio_rows(self):
        return [dict(row) for row in self._portfolio_rows]


def _spy_use_case(seen):
    """Exporter с spy report builder: запоминает полученные args, отдаёт
    fake-отчёт (data flow проверяем по args, не по содержимому отчёта)."""

    def spy(args):
        seen.append(args)
        return fake_screener_report()

    return SyncChatgptPortfolioUseCase(
        FakeDb(sample_rows()),
        publisher=FakePublisher(),
        ytm_engine=FakeYtmEngine(),
        report_builder=spy,
    )


def test_execute_forwards_cli_args_to_candidate_report_args():
    """029-T07 data flow: execute(args) передаёт распарсенный CLI Namespace
    до candidate report — builder получает ровно candidate_report_args(overrides);
    CLI args не теряются."""
    seen = []
    use_case = _spy_use_case(seen)
    overrides = _sync_parse(
        ["--max-risk", "3", "--include-ku", "--screener-limit", "5"]
    )

    use_case.execute(overrides)

    assert len(seen) == 1
    assert vars(seen[0]) == vars(candidate_report_args(overrides))
    assert seen[0].include_held is True


def test_execute_no_args_backward_compatible_candidate_args():
    """No-args export не изменился: execute(None) даёт builder прежние
    candidate_report_args() — canonical defaults + include_held=True."""
    seen = []
    use_case = _spy_use_case(seen)

    use_case.execute(None)

    assert len(seen) == 1
    assert vars(seen[0]) == vars(candidate_report_args())


def test_cli_filters_reach_canonical_build_report_through_execute():
    """Candidate filters доходят до RebalanceReportUseCase.build_report через
    полный execute-path (factory builder): --min-credit-rating AA- реально
    фильтрует buy-выборку и публикуется в CONTROL candidate_meta; rows
    exporter сам не фильтрует — CANDIDATES = выжившие строки report["screener"]."""
    exporter = SyncChatgptPortfolioUseCase.create(
        FakeFactory(
            FakeExportPortfolioDb(
                [
                    screener_candidate(
                        "RU000CANDA", rating_score=10, credit_rating="AA-"
                    ),
                    screener_candidate(
                        "RU000CANDL", rating_score=8, credit_rating="A"
                    ),
                ],
                sample_rows(),
            ),
            {"rating_scale": RATING_SCALE},
        )
    )
    exporter._ytm_engine = FakeYtmEngine()
    publisher = FakePublisher()
    exporter.publisher = publisher

    exporter.execute(_sync_parse(["--min-credit-rating", "AA-"]))

    control = publisher.call["control"]
    meta = json.loads(control["candidate_meta"])
    assert meta["filters"]["min_credit_rating"] == "AA-"
    assert meta["excluded"]["rating_below_min"] == 1
    isins = {
        candidate_cell(row, "isin") for row in publisher.call["candidate_rows"]
    }
    assert isins == {"RU000CANDA"}
