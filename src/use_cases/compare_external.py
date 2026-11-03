"""
Use case: compare-external — безопасное сравнение внешней подборки облигаций с
портфелем (issue #5). Advisory-only: заявок не создаёт, вердикта покупки не
выдаёт.

Вход — JSON/CSV с обязательными на строку isin, source, source_as_of
(опционально name/series, price, yield, ytm_to, rating, maturity,
source_snapshot_id, qty). Ключ сопоставления — только полный ISIN
(UNMATCHED_EXTERNAL / IDENTITY_CONFLICT / STALE_CROSS_SOURCE вместо fuzzy).

Контекст — тот же PlanningContextBuilder, что у rebalance-plan: ответ несёт
context_fingerprint, а stale/critical data gate блокирует каждую строку.
Логика строки — чистые функции src/services/external_comparison.py.
"""
import argparse
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from src.services.external_comparison import (
    DEFAULT_MAX_SOURCE_SKEW_DAYS,
    build_comparison_report,
    load_external_rows,
    normalize_external_row,
)
from src.services.planning_context import PlanningContextBuilder
from src.services.rebalance_engine import RebalanceEngine
from src.storage import PortfolioStorage
from src.use_cases.base import UseCase
from src.utils import rating_code_to_score

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


# Поля цены/YTM, которые для ISIN среза берутся из PlanningContext (а не из повторного запроса).
_CONTEXT_FIELDS = (
    'market_price', 'market_price_updated_at', 'ytm', 'ytm_percent', 'ytm_reason',
    'ytm_null_reason', 'ytm_updated_at',
)


def overlay_context_values(catalog, context):
    """Цена и YTM из context.universe_rows/held_rows для ISIN среза поверх каталога."""
    in_context = {row['isin']: row for row in context.universe_rows}
    for row in context.held_rows:
        in_context.setdefault(row['isin'], row)
    for row in catalog:
        source = in_context.get(row['isin'])
        if source is not None:
            row.update({k: source[k] for k in _CONTEXT_FIELDS if k in source})
    return catalog


class CompareExternalUseCase(UseCase):
    """Безопасное сравнение внешней подборки облигаций с портфелем (advisory-only)."""

    def __init__(self, db: PortfolioStorage, rating_scale: Optional[Dict[str, int]] = None):
        self.db = db
        self.rating_scale = rating_scale or {}
        self._engine = RebalanceEngine(db, rating_scale=self.rating_scale)

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--file', required=True,
            help='JSON/CSV внешней подборки: на строку isin, source, source_as_of '
                 '(+ name/series, price, yield, ytm_to offer|maturity, rating, maturity, qty)')
        parser.add_argument(
            '--format', choices=['json', 'table'], default='json',
            help='json — канонический контракт строки (по умолчанию); table — краткая таблица')
        parser.add_argument(
            '--min-credit-rating', default=None,
            help='Строгий порог рейтинга (напр. A+): проходит только подтверждённый '
                 'credit_rating из rating_history; risk_level рейтинг не подтверждает')
        parser.add_argument(
            '--max-source-skew-days', type=int, default=DEFAULT_MAX_SOURCE_SKEW_DAYS,
            help='Допустимое расхождение source_as_of и даты локальной цены, дн. '
                 f'(по умолчанию {DEFAULT_MAX_SOURCE_SKEW_DAYS}); больше — STALE_CROSS_SOURCE')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'CompareExternalUseCase':
        return cls(
            db=factory.get_db_connection(),
            rating_scale=factory.config.get('rating_scale', {}),
        )

    def execute(self, args: argparse.Namespace):
        report = self.build_report(args)
        if args.format == 'table':
            self._print_table(report)
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))

    def build_report(self, args: argparse.Namespace) -> Dict[str, Any]:
        min_rating = args.min_credit_rating
        if min_rating and rating_code_to_score(min_rating, self.rating_scale) is None:
            raise ValueError(
                f"--min-credit-rating: код {min_rating!r} не входит в шкалу rating_scale (config.yaml)")
        if args.max_source_skew_days < 0:
            raise ValueError("--max-source-skew-days должен быть >= 0")

        raw_rows = load_external_rows(Path(args.file))
        # Тот же срез, что строит rebalance-plan: один context_fingerprint.
        context = PlanningContextBuilder(self.db, rating_scale=self.rating_scale).build()
        isins = sorted({
            ext['isin'] for ext in (normalize_external_row(r, i) for i, r in enumerate(raw_rows, 1))
            if ext['isin']
        })
        # shortcut: каталог читается отдельным запросом после сборки контекста;
        # цена/YTM ISIN из контекста берутся из него (покрыты context_fingerprint),
        # каталожные значения — только для ISIN вне среза; upgrade — snapshot-транзакция.
        catalog = self._engine.enrich_with_ytm(
            self.db.get_comparison_catalog_rows(isins), as_of_date=context.as_of_date)
        catalog = overlay_context_values(catalog, context)
        return build_comparison_report(
            raw_rows, {row['isin']: row for row in catalog}, context,
            rating_scale=self.rating_scale, min_credit_rating=min_rating,
            max_skew_days=args.max_source_skew_days,
        )

    @staticmethod
    def _print_table(report: Dict[str, Any]):
        print(f"context_fingerprint={report['context_fingerprint']} "
              f"data_gate={report['data_gate']['gate_status']} (advisory-only)")
        print(f"{'ISIN':<12} | {'match':<18} | {'eligibility':<14} | {'YTM %':<6} | {'ytm_to':<8} | "
              f"{'ext %':<6} | {'rating_status':<13} | blockers / warnings")
        for row in report['rows']:
            y, d = row['yield'], row['decision']
            codes = [b['code'] for b in d['blockers']] + [f"~{w['code']}" for w in d['warnings']]
            print(f"{(row['identity']['isin'] or '-'):<12} | {row['identity']['match_status']:<18} | "
                  f"{d['eligibility']:<14} | {_fmt(y['ytm_pct']):<6} | {y['ytm_to'] or '-':<8} | "
                  f"{_fmt(y['external_yield_pct']):<6} | {row['credit']['rating_status']:<13} | "
                  f"{', '.join(codes)}")


def _fmt(value: Any) -> str:
    return '-' if value is None else f"{value:.2f}"
