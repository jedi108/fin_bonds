"""
Use case import-company-debt: загрузка снимков долга компаний из CSV/JSON (issue #4).

Внешнего API долговых данных нет, поэтому первая версия ingestion — файл с
обязательными source, report_date, source_ref. Компания ищется по ИНН, затем по
точному имени (без учёта регистра); не найденные и неоднозначные строки НЕ
создают компаний и попадают в отчёт. Upsert идемпотентен по
(company_id, report_date, source); пустая метрика остаётся NULL, не 0.
Суммы — в рублях (без масштабирования тыс./млн.).
"""
import argparse
import csv
import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from src.storage import PortfolioStorage
from src.use_cases.base import UseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

# Неотрицательные суммы; net_debt и ebitda могут быть отрицательными.
_NON_NEGATIVE = (
    'total_debt_rub', 'bond_debt_rub', 'bank_debt_rub', 'lease_debt_rub', 'cash_rub',
    'revenue_rub', 'issued_bond_volume_rub', 'outstanding_bond_debt_rub',
)
_SIGNED = ('net_debt_rub', 'ebitda_rub')
_REQUIRED = ('source', 'report_date', 'source_ref')
COVERAGE_VALUES = ('full', 'partial', 'unknown')
QUALITY_VALUES = ('verified', 'unverified', 'estimated')


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in ('', 'null', 'NULL', 'None'))


def parse_snapshot_row(raw: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Валидирует строку файла. Возвращает (снимок или None, список ошибок)."""
    errors: List[str] = []
    snap: Dict[str, Any] = {}
    for key in _REQUIRED:
        if _blank(raw.get(key)):
            errors.append(f"missing_{key}")
        else:
            snap[key] = str(raw[key]).strip()
    if 'report_date' in snap:
        try:
            snap['report_date'] = date.fromisoformat(snap['report_date'])
        except ValueError:
            errors.append('bad_report_date')
    for key in _NON_NEGATIVE + _SIGNED:
        value = raw.get(key)
        if _blank(value):
            snap[key] = None  # NULL остаётся NULL
            continue
        try:
            number = Decimal(str(value).strip().replace(' ', '').replace(',', '.'))
        except InvalidOperation:
            errors.append(f"bad_{key}")
            continue
        if not number.is_finite() or (key in _NON_NEGATIVE and number < 0):
            errors.append(f"bad_{key}")
            continue
        snap[key] = number
    for key, allowed, default in (('coverage_status', COVERAGE_VALUES, 'unknown'),
                                  ('quality_status', QUALITY_VALUES, 'unverified')):
        value = default if _blank(raw.get(key)) else str(raw[key]).strip().lower()
        if value not in allowed:
            errors.append(f"bad_{key}")
        snap[key] = value
    snap['notes'] = None if _blank(raw.get('notes')) else str(raw['notes']).strip()
    return (None, errors) if errors else (snap, [])


def load_rows(path: Path) -> List[Dict[str, Any]]:
    """CSV (UTF-8, заголовок) или JSON (список объектов либо {"snapshots": [...]})."""
    if path.suffix.lower() == '.json':
        data = json.loads(path.read_text(encoding='utf-8'))
        rows = data.get('snapshots') if isinstance(data, dict) else data
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError("JSON: ожидается список объектов или {\"snapshots\": [...]}")
        return rows
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def import_company_debt(db: PortfolioStorage, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Импортирует строки; возвращает сводку {inserted, updated, unmatched, invalid}."""
    summary: Dict[str, Any] = {
        'rows': len(rows), 'inserted': 0, 'updated': 0, 'unmatched': [], 'invalid': [],
    }
    for index, raw in enumerate(rows, 1):
        inn = None if _blank(raw.get('company_inn')) else str(raw['company_inn']).strip()
        name = None if _blank(raw.get('company_name')) else str(raw['company_name']).strip()
        ident = {'row': index, 'company_inn': inn, 'company_name': name}
        snap, errors = parse_snapshot_row(raw)
        if errors:
            summary['invalid'].append({**ident, 'errors': errors})
            continue
        found = db.find_companies_for_debt(inn, name)
        if inn and any(c['inn'] and c['inn'] != inn for c in found):
            found = []  # ИНН не найден, имя совпало с компанией с ДРУГИМ ИНН — не та же компания
        if len(found) != 1:
            summary['unmatched'].append(
                {**ident, 'reason': 'ambiguous' if len(found) > 1 else 'not_found'}
            )
            continue
        created = db.upsert_company_debt_snapshot(found[0]['id'], snap)
        summary['inserted' if created else 'updated'] += 1
    return summary


class ImportCompanyDebtUseCase(UseCase):
    """Импорт снимков долга компаний из CSV/JSON (source, report_date, source_ref обязательны)."""

    def __init__(self, factory: 'UseCaseFactory'):
        super().__init__(factory)
        self.db: PortfolioStorage = self.factory.get_db_connection()

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--file', required=True,
            help='CSV (UTF-8, заголовок) или JSON со снимками долга. Колонки: company_inn, '
                 'company_name, source, report_date (YYYY-MM-DD), source_ref, total_debt_rub, '
                 'bond_debt_rub, bank_debt_rub, lease_debt_rub, cash_rub, net_debt_rub, '
                 'revenue_rub, ebitda_rub, issued_bond_volume_rub, outstanding_bond_debt_rub, '
                 'coverage_status, quality_status, notes. Суммы в рублях; пусто = NULL.'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'ImportCompanyDebtUseCase':
        return cls(factory)

    def execute(self, args: argparse.Namespace):
        rows = load_rows(Path(args.file))
        summary = import_company_debt(self.db, rows)
        logger.info(
            "Импорт долга: строк %s, создано %s, обновлено %s, не сопоставлено %s, невалидных %s.",
            summary['rows'], summary['inserted'], summary['updated'],
            len(summary['unmatched']), len(summary['invalid']),
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
