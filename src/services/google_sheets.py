"""Минимальный publisher снапшота ChatGPT в одну заранее разрешённую Google Sheet."""
from __future__ import annotations

import base64
import json
from typing import Any, Iterable, Mapping, Optional, Sequence

import gspread
from google.oauth2.service_account import Credentials

# Сабмодуль не импортируется сам через `google.auth.transport` — без явного
# импорта любое обращение к google.auth.transport.requests падает с
# AttributeError (в т.ч. в ad-hoc скриптах проверки ключа, запускаемых через stdin).
import google.auth.transport.requests  # noqa: F401

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def _sheet_value(value: Any) -> Any:
    """Приводит Python/PostgreSQL значения к безопасным значениям Google Sheets."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class GoogleSheetsPortfolioPublisher:
    """Публикует CONTROL + SCHEMA + PORTFOLIO + CANDIDATES в фиксированную Google Sheet.

    Atomic protocol (v3): CONTROL.sync_status выставляется в WRITING до изменения
    данных; затем записывается SCHEMA, PORTFOLIO и CANDIDATES (обе таблицы данных
    очищаются полностью, чтобы shrink не оставлял хвостов), после чего проверяется
    число строк обеих таблиц. READY выставляется только после обеих проверок — это
    commit-marker: потребитель не должен читать данные, пока sync_status != READY.
    """

    def __init__(
        self,
        spreadsheet_id: str,
        service_account_json_b64: str,
        *,
        control_sheet: str = "CONTROL",
        portfolio_sheet: str = "PORTFOLIO",
        schema_sheet: str = "SCHEMA",
        candidates_sheet: str = "CANDIDATES",
        client: Any = None,
    ):
        if not spreadsheet_id.strip():
            raise ValueError("CHATGPT_SHEETS_SPREADSHEET_ID не задан")
        if not service_account_json_b64.strip() and client is None:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON_B64 не задан")

        self.spreadsheet_id = spreadsheet_id.strip()
        self.control_sheet = control_sheet
        self.portfolio_sheet = portfolio_sheet
        self.schema_sheet = schema_sheet
        self.candidates_sheet = candidates_sheet
        self._client = client or self._build_client(service_account_json_b64)

    @staticmethod
    def _build_client(service_account_json_b64: str):
        try:
            raw = base64.b64decode(service_account_json_b64, validate=True)
            info = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_JSON_B64 должен содержать base64 "
                "от валидного service-account JSON"
            ) from exc

        credentials = Credentials.from_service_account_info(
            info,
            scopes=[SHEETS_SCOPE],
        )
        return gspread.authorize(credentials)

    @staticmethod
    def _worksheet(spreadsheet: Any, title: str, *, rows: int, cols: int):
        try:
            return spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            return spreadsheet.add_worksheet(
                title=title,
                rows=max(rows, 20),
                cols=max(cols, 2),
            )

    def publish(
        self,
        *,
        control: Mapping[str, Any],
        headers: Sequence[str],
        rows: Iterable[Sequence[Any]],
        schema_headers: Optional[Sequence[str]] = None,
        schema_rows: Optional[Iterable[Sequence[Any]]] = None,
        candidate_headers: Optional[Sequence[str]] = None,
        candidate_rows: Optional[Iterable[Sequence[Any]]] = None,
    ) -> None:
        data_rows = [[_sheet_value(v) for v in row] for row in rows]
        if not headers:
            raise ValueError("PORTFOLIO headers пуст")
        if not data_rows:
            raise ValueError("PORTFOLIO пуст: публикация отменена")
        # v3: SCHEMA и CANDIDATES передаются парой (headers, rows); None
        # сохраняет P0-поведение (только CONTROL + PORTFOLIO).
        normalized_schema_rows = (
            [[_sheet_value(v) for v in row] for row in schema_rows or []]
            if schema_headers is not None
            else None
        )
        normalized_candidate_rows = (
            [[_sheet_value(v) for v in row] for row in candidate_rows or []]
            if candidate_headers is not None
            else None
        )

        spreadsheet = self._client.open_by_key(self.spreadsheet_id)
        control_ws = self._worksheet(
            spreadsheet, self.control_sheet, rows=len(control) + 2, cols=2
        )
        portfolio_ws = self._worksheet(
            spreadsheet,
            self.portfolio_sheet,
            rows=len(data_rows) + 5,
            cols=len(headers),
        )
        schema_ws = (
            self._worksheet(
                spreadsheet,
                self.schema_sheet,
                rows=len(normalized_schema_rows) + 5,
                cols=len(schema_headers),
            )
            if normalized_schema_rows is not None
            else None
        )
        candidates_ws = (
            self._worksheet(
                spreadsheet,
                self.candidates_sheet,
                rows=len(normalized_candidate_rows) + 5,
                cols=len(candidate_headers),
            )
            if normalized_candidate_rows is not None
            else None
        )

        # Commit protocol: до изменения данных помечаем snapshot как неполный.
        control_ws.update(
            range_name="A1:B2",
            values=[
                ["key", "value"],
                ["sync_status", "WRITING"],
            ],
            value_input_option="RAW",
        )

        # SCHEMA статична в рамках версии — пишется поверх (без clear).
        if schema_ws is not None and schema_headers is not None:
            schema_ws.update(
                range_name="A1",
                values=[list(schema_headers), *normalized_schema_rows],
                value_input_option="RAW",
            )

        portfolio_ws.clear()
        portfolio_ws.update(
            range_name="A1",
            values=[list(headers), *data_rows],
            value_input_option="RAW",
        )

        # CANDIDATES очищается полностью, чтобы shrink не оставлял хвостов.
        if candidates_ws is not None and candidate_headers is not None:
            candidates_ws.clear()
            candidates_ws.update(
                range_name="A1",
                values=[list(candidate_headers), *normalized_candidate_rows],
                value_input_option="RAW",
            )

        written_rows = portfolio_ws.get_all_values()
        actual_count = max(len(written_rows) - 1, 0)
        if actual_count != len(data_rows):
            raise RuntimeError(
                f"Google Sheets verification failed: expected "
                f"{len(data_rows)} portfolio rows, got {actual_count}"
            )

        if candidates_ws is not None:
            written_candidates = candidates_ws.get_all_values()
            actual_candidates = max(len(written_candidates) - 1, 0)
            if actual_candidates != len(normalized_candidate_rows):
                raise RuntimeError(
                    f"Google Sheets verification failed: expected "
                    f"{len(normalized_candidate_rows)} candidates rows, "
                    f"got {actual_candidates}"
                )

        # READY — последняя операция, только после обеих проверок.
        final_control = {"sync_status": "READY", **dict(control)}
        control_ws.clear()
        control_ws.update(
            range_name="A1",
            values=[
                ["key", "value"],
                *[[_sheet_value(k), _sheet_value(v)] for k, v in final_control.items()],
            ],
            value_input_option="RAW",
        )
