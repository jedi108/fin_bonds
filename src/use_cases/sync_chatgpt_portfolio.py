"""Экспорт данных для ChatGPT в Google Sheets.

P0: текущий портфель (лист PORTFOLIO). 019: candidate projection —
транспортные строки CANDIDATES строятся только из канонического отчёта
rebalance-report (build_report + default_args, второго скринера нет).
020: schema v3 — публикуются SCHEMA (самодокументация контракта) и
CANDIDATES, CONTROL расширен кандидатными ключами и consumer_rule.
025: schema v4 — CONTROL несёт cash контекст канонического снапшота
(cash_available_rub / investable_total_rub / cash_updated_at; unknown —
пустая ячейка, не 0), CANDIDATES — canonical credit_rating кандидата.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

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
# v3 (020): SCHEMA (самодокументация) + CANDIDATES (кандидаты buy-выборки).
# v4 (025): CONTROL + cash контекст (cash_available_rub/investable_total_rub/
# cash_updated_at; unknown = пустая ячейка), CANDIDATES + credit_rating.
SCHEMA_VERSION = 4
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

# 020: versioned definition колонок PORTFOLIO для самодокументируемого листа
# SCHEMA — живёт в коде рядом с headers; тест ловит header, отсутствующий в
# SCHEMA (runtime source-of-truth — сам лист SCHEMA в Google Sheet).
PORTFOLIO_SCHEMA = (
    ("isin", "string", "Bond issue identifier; one row per ISIN (positions across accounts/brokers aggregated)"),
    ("name", "string", "Bond name"),
    ("ticker", "string", "Ticker"),
    ("issuer", "string", "Issuer company from the reference book; fallback to bond name"),
    ("quantity", "count", "Number of papers in the position"),
    ("price_rub", "RUB", "Price per paper: volume-weighted trade price or market price"),
    ("value_rub", "RUB", "Position valuation"),
    ("valuation_source", "string", "Which canonical valuation branch produced value_rub: broker_value / broker_price / market_price / nominal_fallback / zero"),
    ("valuation_warning", "string or empty", "Fixed warning only for ambiguous valuation branches (nominal_fallback, zero); empty otherwise. Read it as a flag that price_rub does not follow from value_rub by ordinary division"),
    ("share_pct", "percent", "Position share of the portfolio (share x 100)"),
    ("issuer_pct", "percent", "Issuer share of the portfolio"),
    ("ytm_pct", "percent_per_year or empty", "Yield to maturity; an empty value is a deliberate NULL — see ytm_reason"),
    ("ytm_reason", "string or empty", "Reason for empty ytm_pct (perpetual, floating_coupon, amortization_schedule_missing, coupon_frequency_missing, maturity_missing_or_past, market_price_missing_or_nonpositive, nominal_missing_or_nonpositive, coupon_rate_missing_or_negative, solver_no_convergence; not_applicable = YTM calc never ran). Empty when ytm_pct is present"),
    ("coupon_pct", "percent_per_year", "Coupon rate"),
    ("coupon_frequency", "count", "Coupons per year"),
    ("coupon_type", "FIX or FLOAT", "Coupon type"),
    ("maturity_date", "ISO date", "Maturity date"),
    ("offer_date", "ISO date or empty", "Offer (put/call) date"),
    ("amortization_flag", "boolean", "TRUE when nominal amortization exists"),
    ("credit_rating", "string or empty", "Latest credit rating"),
    ("risk_level", "count or empty", "Risk level (Tinkoff)"),
    ("list_level", "count or empty", "MOEX listing level"),
    ("duration", "years or empty", "Macaulay duration; an empty value is a deliberate NULL — see duration_reason"),
    ("modified_duration", "years or empty", "Modified duration"),
    ("duration_reason", "string or empty", "Reason for empty duration/modified_duration; empty when calculated"),
    ("liquidity_loss_pct", "percent", "Estimated loss when exiting the position at market, weighted by position (legacy name; the DB column is liquidity_loss_ratio but stores percent)"),
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
# 025: + credit_rating — последний canonical рейтинг из строки скринера.
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
    "credit_rating",
    "risk_level",
    "list_level",
    "ku",
    "issuer_pct_current",
    "held_badge",
)

# 020: versioned definition колонок CANDIDATES для листа SCHEMA.
CANDIDATE_SCHEMA = (
    ("candidate_type", "FIX or FLOAT", "Candidate group; FIX and FLOAT share one sheet and are ranked separately"),
    ("rank", "count", "Rank within candidate_type starting from 1; order preserves the canonical screener ranking"),
    ("isin", "string", "Bond issue identifier"),
    ("name", "string", "Bond name"),
    ("ticker", "string", "Ticker"),
    ("issuer", "string", "Issuer company"),
    ("price_rub", "RUB", "Fresh market price required by the canonical screener buy selection (no nominal fallback)"),
    ("ytm_pct", "percent_per_year or empty", "Yield to maturity; an empty value is a deliberate NULL — see ytm_reason"),
    ("ytm_reason", "string or empty", "Reason for empty ytm_pct (e.g. floating_coupon); empty when ytm_pct is present"),
    ("coupon_pct", "percent_per_year or empty", "Coupon rate"),
    ("coupon_frequency", "count", "Coupons per year"),
    ("maturity_date", "ISO date", "Maturity date"),
    ("offer_date", "ISO date or empty", "Offer (put/call) date"),
    ("amortization_flag", "boolean", "TRUE when nominal amortization exists"),
    ("credit_rating", "string or empty", "Latest canonical credit rating of the candidate (last rating_history row by ISIN, copied from the canonical screener row); empty when the bond has no rating row"),
    ("risk_level", "count", "Risk level (Tinkoff); a separate Tinkoff scale — NOT a substitute for credit_rating"),
    ("list_level", "count", "MOEX listing level"),
    ("ku", "boolean", "TRUE when the bond is available only to qualified investors"),
    ("issuer_pct_current", "percent", "Current issuer share in the portfolio BEFORE the hypothetical purchase (the candidate itself is not counted in it)"),
    ("held_badge", "string or empty", "Non-empty when the candidate is already held in the portfolio (include_held export context); format: held (N%)"),
)

# 020: самодокументация контракта. SCHEMA описывает все поля CONTROL,
# PORTFOLIO и CANDIDATES и записывается в таблицу при каждом sync; runtime
# source-of-truth для ChatGPT — сам лист SCHEMA, doc/ — только зеркало.
SCHEMA_HEADERS = ("sheet", "field", "type_or_unit", "description")

CONTROL_SCHEMA = (
    ("sync_status", "string", "WRITING while data sheets are being written; READY only after PORTFOLIO and CANDIDATES are written and both row counts verified (commit-marker)"),
    ("schema_version", "count", "Version of the sheet contract (4 = cash context in CONTROL and credit_rating in CANDIDATES)"),
    ("snapshot_id", "string", "UTC snapshot id (YYYYMMDDThhmmssZ) identifying one sync run"),
    ("generated_at", "ISO datetime", "UTC time when the snapshot was generated"),
    ("gate_status", "string", "Freshness gate result (OK/WARN/BLOCKED); exports with not-fresh data are not published"),
    ("is_fresh", "boolean", "Always TRUE: exports that fail the freshness gate are not published"),
    ("positions_count", "count", "Number of data rows in PORTFOLIO; verify before use"),
    ("portfolio_value_rub", "RUB", "Bonds-only valuation: the sum of PORTFOLIO value_rub. NOT including cash"),
    ("cash_available_rub", "RUB or empty", "Free available RUB cash across the configured broker accounts, from the canonical portfolio snapshot (only sync-portfolio writes it). An EMPTY cell means cash is unknown (never synced, or the last sync failed) — treat it as unknown, NOT as zero"),
    ("investable_total_rub", "RUB or empty", "portfolio_value_rub + cash_available_rub (bonds + free cash = total investable capital); empty when cash is unknown"),
    ("cash_updated_at", "ISO datetime or empty", "Timestamp of the last successful cash sync; empty when cash is unknown. A stale value means cash may be outdated (a failed sync does not clear the snapshot)"),
    ("portfolio_updated_at", "ISO datetime", "When portfolio positions were last updated in the source DB"),
    ("market_updated_at", "ISO datetime", "When market prices were last updated in the source DB. Limitation: this is a DB write timestamp — the source trade timestamp is not stored, so freshness means recently written to DB, not recently traded"),
    ("warnings", "JSON or empty", "JSON list of freshness gate warnings; empty when none"),
    ("candidates_count", "count", "Number of data rows in CANDIDATES; verify before use"),
    ("candidate_meta", "JSON", "CANDIDATES metadata copied from the canonical screener report (filters, candidates_total, fixed_matched, floater_matched, excluded) without recalculation"),
    ("contract_sheet", "string", "Name of the sheet that documents all fields: SCHEMA"),
    ("consumer_rule", "string", "Short machine contract for the consumer"),
)

CONSUMER_RULE = (
    "Read CONTROL first. Use data only when sync_status=READY.\n"
    "Read SCHEMA before interpreting PORTFOLIO or CANDIDATES.\n"
    "Verify positions_count and candidates_count.\n"
    "Empty cash_available_rub / investable_total_rub / cash_updated_at mean "
    "unknown cash, not zero."
)


def build_schema_rows() -> List[List[Any]]:
    """Строки листа SCHEMA (020) из versioned definitions рядом с headers."""
    rows: List[List[Any]] = []
    for sheet, definitions in (
        ("CONTROL", CONTROL_SCHEMA),
        ("PORTFOLIO", PORTFOLIO_SCHEMA),
        ("CANDIDATES", CANDIDATE_SCHEMA),
    ):
        for field, type_or_unit, description in definitions:
            rows.append([sheet, field, type_or_unit, description])
    return rows


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
    не экспортируются — у кандидата они 0 и ничего не добавляют; duration и
    liquidity не добавляются (покрытие вне портфеля неполное, их нет в
    каноническом скринере); credit_rating добавлен в 025 — он есть в
    canonical screener row (023). metadata (filters, candidates_total,
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
                "credit_rating": row["credit_rating"],
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
    """Выгружает снапшот для ChatGPT (CONTROL + SCHEMA + PORTFOLIO + CANDIDATES)
    в настроенную Google Sheet."""

    def __init__(
        self,
        db: PortfolioStorage,
        *,
        publisher: Optional[GoogleSheetsPortfolioPublisher] = None,
        ytm_engine: Optional[CalculateYtmUseCase] = None,
        report_builder: Optional[Callable[[argparse.Namespace], Dict[str, Any]]] = None,
    ):
        self.db = db
        self.publisher = publisher
        self._ytm_engine = ytm_engine or CalculateYtmUseCase(db)
        # 020: источник кандидатов — программный канонический отчёт
        # rebalance-report (второго скринера нет); подменяется только в тестах.
        self._report_builder = report_builder or self._default_report_builder

    def _default_report_builder(self, args: argparse.Namespace) -> Dict[str, Any]:
        return RebalanceReportUseCase(db=self.db).build_report(args)

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
    ) -> Tuple[Dict[str, Any], List[List[Any]], List[List[Any]]]:
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

        # 020: кандидаты — только из канонического отчёта rebalance-report
        # (build_report + candidate_report_args, пересчётов нет); metadata
        # переносится в CONTROL как есть.
        report = self._report_builder(candidate_report_args())
        candidate_meta, candidate_rows = build_candidates_payload(report)

        # 025: cash контекст канонического снапшота — тот же canonical reader,
        # что у rebalance-report (022): PortfolioStorage.get_available_cash_rub.
        # Никакого broker API; unknown cash публикуется пустой ячейкой, не 0.
        cash = self.db.get_available_cash_rub()
        cash_known = bool(cash.get("cash_known"))
        cash_available_rub = (
            round(_float(cash.get("cash_available_rub")), 2) if cash_known else None
        )

        snapshot_id = now.strftime("%Y%m%dT%H%M%SZ")
        control = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "generated_at": now.isoformat(),
            "gate_status": gate_status,
            "is_fresh": True,
            "positions_count": len(table_rows),
            "portfolio_value_rub": round(total_value, 2),
            "cash_available_rub": cash_available_rub,
            "investable_total_rub": (
                round(total_value + cash_available_rub, 2)
                if cash_available_rub is not None
                else None
            ),
            "cash_updated_at": _iso(cash.get("cash_updated_at")) if cash_known else None,
            "portfolio_updated_at": _iso(freshness.get("portfolio_max_updated_at")),
            "market_updated_at": _iso(freshness.get("market_price_max_updated_at")),
            "warnings": json.dumps(warnings, ensure_ascii=False) if warnings else "",
            # 020: кандидатный контекст и контракт потребителя.
            "candidates_count": len(candidate_rows),
            "candidate_meta": json.dumps(candidate_meta, ensure_ascii=False),
            "contract_sheet": "SCHEMA",
            "consumer_rule": CONSUMER_RULE,
        }
        return control, table_rows, candidate_rows

    def execute(self, args: argparse.Namespace):
        control, rows, candidate_rows = self._build_payload()
        self._get_publisher().publish(
            control=control,
            headers=PORTFOLIO_HEADERS,
            rows=rows,
            schema_headers=SCHEMA_HEADERS,
            schema_rows=build_schema_rows(),
            candidate_headers=CANDIDATE_HEADERS,
            candidate_rows=candidate_rows,
        )
        print(
            "✅ Портфель для ChatGPT обновлён\n"
            f"Snapshot: {control['snapshot_id']}\n"
            f"Позиций: {control['positions_count']}\n"
            f"Кандидатов: {control['candidates_count']}\n"
            f"Freshness: {control['gate_status']}"
        )
