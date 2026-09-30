"""Экспорт данных для ChatGPT в Google Sheets.

P0: текущий портфель (лист PORTFOLIO). 019: candidate projection —
транспортные строки будущего листа CANDIDATES строятся только из
канонического отчёта rebalance-report (build_report + default_args,
второго скринера нет); публикация листа — задача 020.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from src.services.google_sheets import GoogleSheetsPortfolioPublisher
from src.storage import PortfolioStorage
from src.use_cases.base import UseCase
from src.use_cases.calculate_ytm import CalculateYtmUseCase
from src.use_cases.check_db import classify_freshness
from src.use_cases.rebalance_report import RebalanceReportUseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

# v2 (007, 008): ytm_reason/duration_reason — причины пустых ytm_pct/duration;
# valuation_source/valuation_warning — происхождение canonical valuation (008).
SCHEMA_VERSION = 2
PORTFOLIO_HEADERS = (
    "isin",
    "name",
    "ticker",
    "issuer",
    "quantity",
    "price_rub",
    "value_rub",
    "valuation_source",
    "valuation_warning",
    "share_pct",
    "issuer_pct",
    "ytm_pct",
    "ytm_reason",
    "coupon_pct",
    "coupon_frequency",
    "coupon_type",
    "maturity_date",
    "offer_date",
    "amortization_flag",
    "credit_rating",
    "risk_level",
    "list_level",
    "duration",
    "modified_duration",
    "duration_reason",
    "liquidity_loss_pct",
)

# 008: предупреждение только для веток, где value_rub не равен quantity*price_rub
# или оценка отсутствует. Тексты зафиксированы в doc/chatgpt_portfolio_sheet_contract.md.
VALUATION_WARNINGS = {
    "nominal_fallback": (
        "value_rub = quantity * nominal; "
        "price_rub may be empty/0 and differ from value_rub/quantity"
    ),
    "zero": "no valuation available; value_rub = 0",
}


def _float(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# 019: транспортные колонки будущего листа CANDIDATES — контракт из задачи.
# Значения берутся только из строк канонического скринера rebalance-report
# (screener.fixed[] / screener.floater[]), второго screener для ChatGPT нет.
CANDIDATE_HEADERS = (
    "candidate_type",
    "rank",
    "isin",
    "name",
    "ticker",
    "issuer",
    "price_rub",
    "ytm_pct",
    "ytm_reason",
    "coupon_pct",
    "coupon_frequency",
    "maturity_date",
    "offer_date",
    "amortization_flag",
    "risk_level",
    "list_level",
    "ku",
    "issuer_pct_current",
    "held_badge",
)


def candidate_report_args() -> argparse.Namespace:
    """Аргументы rebalance-report для кандидатного экспорта (019).

    Defaults — канонические RebalanceReportUseCase.default_args(), экспортёр
    их не дублирует; единственное изменение — include_held=True: уже держимые
    бумаги должны попасть в выборку с бейджем held (доля N%) для ревью.
    """
    args = RebalanceReportUseCase.default_args()
    args.include_held = True
    return args


def build_candidates_payload(
    report: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[List[Any]]]:
    """CANDIDATES payload (019): (metadata, транспортные строки) из канонического отчёта.

    Источник — только report['screener'] (fixed[]/floater[]): FIX и FLOAT идут
    в один набор, rank — с 1 отдельно внутри типа, порядок сохраняет ranking
    канонического скринера (экспортёр ничего не пересортировывает). qty/value
    не экспортируются — у кандидата они 0 и ничего не добавляют; rating и
    duration/liquidity не добавляются (покрытие вне портфеля неполное, их нет
    в каноническом скринере). metadata (filters, candidates_total,
    fixed_matched, floater_matched, excluded) переносится из отчёта без
    пересчёта.
    """
    screener = report["screener"]
    rows: List[List[Any]] = []
    for candidate_type, source in (
        ("FIX", screener["fixed"]),
        ("FLOAT", screener["floater"]),
    ):
        for rank, row in enumerate(source, start=1):
            values = {
                "candidate_type": candidate_type,
                "rank": rank,
                "isin": row["isin"],
                "name": row["name"],
                "ticker": row["ticker"],
                "issuer": row["issuer"],
                "price_rub": row["price"],
                "ytm_pct": row["ytm_pct"],
                "ytm_reason": row["ytm_reason"],
                "coupon_pct": row["coupon_pct"],
                "coupon_frequency": row["freq"],
                "maturity_date": row["maturity"],
                "offer_date": row["offer"],
                "amortization_flag": row["amort"],
                "risk_level": row["risk_level"],
                "list_level": row["list_level"],
                "ku": row["ku"],
                "issuer_pct_current": row["issuer_pct"],
                "held_badge": row["held_badge"],
            }
            rows.append([values[column] for column in CANDIDATE_HEADERS])

    metadata = {
        "filters": screener["filters"],
        "candidates_total": screener["candidates_total"],
        "fixed_matched": screener["fixed_matched"],
        "floater_matched": screener["floater_matched"],
        "excluded": screener["excluded"],
    }
    return metadata, rows


class SyncChatgptPortfolioUseCase(UseCase):
    """Выгружает текущий портфель в настроенную Google Sheet для ChatGPT."""

    def __init__(
        self,
        db: PortfolioStorage,
        *,
        publisher: Optional[GoogleSheetsPortfolioPublisher] = None,
        ytm_engine: Optional[CalculateYtmUseCase] = None,
    ):
        self.db = db
        self.publisher = publisher
        self._ytm_engine = ytm_engine or CalculateYtmUseCase(db)

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        # P0 намеренно без опций: destination фиксирован в env.
        return None

    @classmethod
    def create(cls, factory: "UseCaseFactory") -> "SyncChatgptPortfolioUseCase":
        return cls(db=factory.get_db_connection())

    def _get_publisher(self) -> GoogleSheetsPortfolioPublisher:
        if self.publisher is not None:
            return self.publisher
        return GoogleSheetsPortfolioPublisher(
            spreadsheet_id=os.getenv("CHATGPT_SHEETS_SPREADSHEET_ID", ""),
            service_account_json_b64=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", ""),
        )

    def _build_payload(
        self, now: Optional[datetime] = None
    ) -> Tuple[Dict[str, Any], List[List[Any]]]:
        now = now or datetime.now(timezone.utc)
        freshness = self.db.get_db_freshness()
        criticals, warnings, gate_status, is_fresh = classify_freshness(freshness, now)

        if not is_fresh:
            reasons = criticals or warnings
            raise RuntimeError(
                "Экспорт отменён: данные не прошли freshness gate "
                f"({gate_status}): {'; '.join(reasons)}"
            )

        rows = self.db.get_chatgpt_portfolio_rows()
        if not rows:
            raise RuntimeError(
                "Экспорт отменён: активный портфель пуст. Google Sheet не изменена."
            )

        rows = self._ytm_engine._enrich_bonds_with_ytm(
            rows, argparse.Namespace(min_ytm=None)
        )

        total_value = sum(_float(row.get("value_rub")) or 0.0 for row in rows)
        if total_value <= 0:
            raise RuntimeError(
                "Экспорт отменён: оценка портфеля <= 0. Google Sheet не изменена."
            )

        issuer_values: Dict[str, float] = {}
        for row in rows:
            issuer = row.get("issuer") or row.get("name") or row.get("isin")
            issuer_values[issuer] = issuer_values.get(issuer, 0.0) + (
                _float(row.get("value_rub")) or 0.0
            )

        table_rows: List[List[Any]] = []
        for row in rows:
            value = _float(row.get("value_rub")) or 0.0
            issuer = row.get("issuer") or row.get("name") or row.get("isin")
            share_pct = round(value / total_value * 100.0, 4)
            issuer_pct = round(issuer_values[issuer] / total_value * 100.0, 4)
            coupon_type = "FLOAT" if row.get("floating_coupon_flag") else "FIX"
            duration_macaulay = _float(row.get("duration_macaulay"))
            duration_modified = _float(row.get("duration_modified"))

            values = {
                "isin": row.get("isin"),
                "name": row.get("name"),
                "ticker": row.get("ticker"),
                "issuer": issuer,
                "quantity": _float(row.get("quantity")),
                "price_rub": _float(row.get("price")),
                "value_rub": value,
                # Ветка canonical valuation из view (008) — не восстанавливается
                # из округлённых значений; warning для неоднозначных веток.
                "valuation_source": row.get("valuation_source"),
                "valuation_warning": VALUATION_WARNINGS.get(row.get("valuation_source")),
                "share_pct": share_pct,
                "issuer_pct": issuer_pct,
                "ytm_pct": _float(row.get("ytm_percent")),
                # Причина пустого ytm_pct: готовый ключ enrichment'а (None,
                # если YTM рассчитана); экспортер не пересчитывает её сам.
                "ytm_reason": row.get("ytm_reason"),
                "coupon_pct": _float(row.get("coupon_rate_percent")),
                "coupon_frequency": row.get("coupon_quantity_per_year"),
                "coupon_type": coupon_type,
                "maturity_date": _iso(row.get("maturity_date")),
                "offer_date": _iso(row.get("offer_date")),
                "amortization_flag": bool(row.get("amortization_flag")),
                "credit_rating": row.get("credit_rating"),
                "risk_level": row.get("risk_level"),
                "list_level": row.get("list_level"),
                "duration": duration_macaulay,
                "modified_duration": duration_modified,
                # Дюрация рассчитана — причина не нужна; иначе каноническая
                # причина из БД (None, если расчёт ещё не запускался).
                "duration_reason": (
                    None
                    if duration_macaulay is not None or duration_modified is not None
                    else row.get("duration_null_reason")
                ),
                "liquidity_loss_pct": _float(row.get("liquidity_loss_ratio")),
            }
            table_rows.append([values[column] for column in PORTFOLIO_HEADERS])

        snapshot_id = now.strftime("%Y%m%dT%H%M%SZ")
        control = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "generated_at": now.isoformat(),
            "gate_status": gate_status,
            "is_fresh": True,
            "positions_count": len(table_rows),
            "portfolio_value_rub": round(total_value, 2),
            "portfolio_updated_at": _iso(freshness.get("portfolio_max_updated_at")),
            "market_updated_at": _iso(freshness.get("market_price_max_updated_at")),
            "warnings": json.dumps(warnings, ensure_ascii=False) if warnings else "",
        }
        return control, table_rows

    def execute(self, args: argparse.Namespace):
        control, rows = self._build_payload()
        self._get_publisher().publish(
            control=control,
            headers=PORTFOLIO_HEADERS,
            rows=rows,
        )
        print(
            "✅ Портфель для ChatGPT обновлён\n"
            f"Snapshot: {control['snapshot_id']}\n"
            f"Позиций: {control['positions_count']}\n"
            f"Freshness: {control['gate_status']}"
        )
