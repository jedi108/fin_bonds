import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.data_models import Bond
from src.services.google_sheets import GoogleSheetsPortfolioPublisher
from src.use_cases.rebalance_report import RebalanceReportUseCase
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
    assert SCHEMA_VERSION == 3


def test_build_payload_row_values_match_headers_order():
    """Каждая ячейка строки лежит на позиции своего заголовка."""
    control, rows = build_payload(make_rows())

    assert control["schema_version"] == 3
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
    assert SCHEMA_VERSION == 3


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
    }
    row.update(overrides)
    return row


def fake_screener_report():
    """Канонический отчёт-фикстура: блок screener с 2 фиксами и 1 флоатером."""
    return {
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
    assert SCHEMA_VERSION == 3
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
# 020: schema v3 — SCHEMA + CANDIDATES в атомарном протоколе публикации
# ---------------------------------------------------------------------------


def make_v3_use_case(db_rows, publisher, report_builder=None):
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
    use_case = make_v3_use_case(sample_rows(), FakePublisher())
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


def test_build_payload_v3_control_fields_and_candidates():
    """schema v3: control несёт кандидатов и контракт потребителя; кандидатные
    строки — третий элемент payload, count совпадает."""
    use_case = make_v3_use_case(sample_rows(), FakePublisher())

    control, rows, candidate_rows = use_case._build_payload(
        datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    )

    assert control["schema_version"] == 3
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


def test_execute_publishes_v3_sheets_to_publisher():
    """execute передаёт в publisher один снапшот: PORTFOLIO + SCHEMA + CANDIDATES."""
    publisher = FakePublisher()
    use_case = make_v3_use_case(sample_rows(), publisher)

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
            "schema_version": 3,
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
