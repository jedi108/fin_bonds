"""Минимальный publisher портфеля в одну заранее разрешённую Google Sheet."""
from __future__ import annotations

import base64
import json
from typing import Any, Iterable, Mapping, Sequence

import gspread
from google.oauth2.service_account import Credentials

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
    """Публикует CONTROL + PORTFOLIO в фиксированную Google Sheet.

    CONTROL.sync_status выставляется в WRITING до изменения PORTFOLIO и в READY
    только после успешной записи и проверки количества строк. Это commit-marker:
    потребитель не должен читать PORTFOLIO, пока sync_status != READY.
    """

    def __init__(
        self,
        spreadsheet_id: str,
        service_account_json_b64: str,
        *,
        control_sheet: str = "CONTROL",
        portfolio_sheet: str = "PORTFOLIO",
        client: Any = None,
    ):
        if not spreadsheet_id.strip():
            raise ValueError("CHATGPT_SHEETS_SPREADSHEET_ID не задан")
        if not service_account_json_b64.strip() and client is None:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON_B64 не задан")

        self.spreadsheet_id = spreadsheet_id.strip()
        self.control_sheet = control_sheet
        self.portfolio_sheet = portfolio_sheet
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
    ) -> None:
        data_rows = [[_sheet_value(v) for v in row] for row in rows]
        if not headers:
            raise ValueError("PORTFOLIO headers пуст")
        if not data_rows:
            raise ValueError("PORTFOLIO пуст: публикация отменена")

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

        # Commit protocol: до изменения данных помечаем snapshot как неполный.
        control_ws.update(
            range_name="A1:B2",
            values=[
                ["key", "value"],
                ["sync_status", "WRITING"],
            ],
            value_input_option="RAW",
        )

        portfolio_ws.clear()
        portfolio_ws.update(
            range_name="A1",
            values=[list(headers), *data_rows],
            value_input_option="RAW",
        )

        written_rows = portfolio_ws.get_all_values()
        actual_count = max(len(written_rows) - 1, 0)
        if actual_count != len(data_rows):
            raise RuntimeError(
                f"Google Sheets verification failed: expected "
                f"{len(data_rows)} portfolio rows, got {actual_count}"
            )

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
