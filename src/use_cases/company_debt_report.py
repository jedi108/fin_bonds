"""
Use case company-debt-report: read-only отчёт по долговой нагрузке эмитентов (issue #4).

Объём выпусков (issued_bond_volume_rub) — оценка размещения, не остаток долга;
снимки долга — по отчётности с гейтом свежести. Ничего не пишет в БД, ордеров
не создаёт. Контракт JSON — doc/company_debt_contract.md.
"""
import argparse
import json
import logging
from datetime import date
from typing import Any, Dict, TYPE_CHECKING

from src.services.company_debt import DEFAULT_STALE_DAYS, build_debt_report
from src.storage import PortfolioStorage
from src.use_cases.base import UseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


def format_table(report: Dict[str, Any]) -> str:
    """Человекочитаемая таблица; «—» = неизвестно (NULL), не ноль."""
    def fmt(value: Any, spec: str = ',.0f') -> str:
        return '—' if value is None else format(value, spec)

    lines = [
        f"Долг эмитентов на {report['meta']['as_of']} (stale > {report['meta']['stale_days']} дн.)",
        report['meta']['issued_volume_note'],
        f"{'#':>3} {'Компания':<32} {'Выпусков':>8} {'Объём разм., ₽':>16} {'Доля,%':>7} "
        f"{'В портф.,%':>10} {'ND/EBITDA':>9}  Свежесть  Неизвестно",
    ]
    for r in report['companies']:
        lines.append(
            f"{fmt(r['rank'], 'd'):>3} {r['name'][:32]:<32} {r['issues_count']:>8} "
            f"{fmt(r['issued_bond_volume_rub']):>16} {fmt(r['issued_volume_share_pct'], '.2f'):>7} "
            f"{fmt(r['portfolio_share_pct'], '.2f'):>10} {fmt(r['net_debt_to_ebitda'], '.2f'):>9}  "
            f"{r['freshness']['status']:<8}  {','.join(r['unknowns'])}"
        )
    c = report['concentration']
    lines.append(f"Концентрация объёма: top1 {c['top1_pct']}%, top3 {c['top3_pct']}%, "
                 f"top5 {c['top5_pct']}%, HHI {c['hhi']}")
    return '\n'.join(lines)


class CompanyDebtReportUseCase(UseCase):
    """Read-only отчёт по долговой нагрузке эмитентов: объём выпусков, снимки долга, свежесть."""

    def __init__(self, factory: 'UseCaseFactory'):
        super().__init__(factory)
        self.db: PortfolioStorage = self.factory.get_db_connection()

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument('--format', choices=['json', 'table'], default='json',
                            help='Формат вывода (default: json).')
        parser.add_argument('--stale-days', type=int, default=DEFAULT_STALE_DAYS,
                            help=f'Снимок старше N дней считается stale (default: {DEFAULT_STALE_DAYS}).')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'CompanyDebtReportUseCase':
        return cls(factory)

    def execute(self, args: argparse.Namespace):
        if args.stale_days < 1:
            raise ValueError("--stale-days должен быть >= 1")
        report = build_debt_report(self.db, date.today(), args.stale_days)
        if args.format == 'table':
            print(format_table(report))
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
