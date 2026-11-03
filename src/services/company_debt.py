"""
Долговая нагрузка компаний-эмитентов (issue #4): чистая логика отчёта и overlay.

Два разных понятия не смешиваются:
- issued_bond_volume_rub — SUM(issue_size * nominal) по выпускам каталога. nominal —
  ТЕКУЩИЙ номинал от брокера (после амортизации), поэтому это оценка объёма
  размещения, а НЕ остаток облигационного долга и не леверидж;
- снимки company_debt_snapshots — долг по отчётности (дата, источник, покрытие).

Принципы: отсутствующая метрика остаётся None (не 0); леверидж не выводится из
купона, YTM или числа выпусков; net_debt_to_ebitda считается только при
известных net_debt и ebitda. Модуль без обращений к БД (читает через
PortfolioStorage только в build_*), советующий (advisory) — ордеров не создаёт.
"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_STALE_DAYS = 180  # отчётность квартальная: ~2 периода + лаг публикации

FRESHNESS_FRESH = 'fresh'
FRESHNESS_STALE = 'stale'
FRESHNESS_MISSING = 'missing'

ISSUED_VOLUME_NOTE = (
    "issued_bond_volume_rub = SUM(issue_size * nominal) по RUB-выпускам каталога "
    "(не-RUB и погашенные выпуски не учитываются), nominal — текущий номинал от брокера: "
    "оценка объёма размещения, НЕ остаток долга и НЕ леверидж."
)

_SNAPSHOT_MONEY_FIELDS = (
    'total_debt_rub', 'bond_debt_rub', 'bank_debt_rub', 'lease_debt_rub', 'cash_rub',
    'net_debt_rub', 'revenue_rub', 'ebitda_rub', 'issued_bond_volume_rub',
    'outstanding_bond_debt_rub',
)


def _num(value: Any) -> Optional[float]:
    """Decimal/int/float -> float; None остаётся None."""
    return None if value is None else float(value)


def _day(value: Any) -> Optional[date]:
    return value.date() if isinstance(value, datetime) else value


def classify_debt_freshness(
    report_date: Optional[date], today: date, stale_days: int
) -> Tuple[str, Optional[int]]:
    """(статус, возраст в днях): missing — снимка нет, stale — старше stale_days."""
    if report_date is None:
        return FRESHNESS_MISSING, None
    age = (today - report_date).days
    return (FRESHNESS_STALE if age > stale_days else FRESHNESS_FRESH), age


def derive_net_debt(snapshot: Optional[Dict[str, Any]]) -> Tuple[Optional[float], Optional[str]]:
    """net_debt из снимка: заявленный, иначе total_debt - cash (только если оба известны)."""
    if not snapshot:
        return None, None
    if snapshot.get('net_debt_rub') is not None:
        return _num(snapshot['net_debt_rub']), 'reported'
    if snapshot.get('total_debt_rub') is not None and snapshot.get('cash_rub') is not None:
        return _num(snapshot['total_debt_rub']) - _num(snapshot['cash_rub']), 'derived'
    return None, None


def net_debt_to_ebitda(net_debt: Optional[float], ebitda: Optional[float]) -> Optional[float]:
    """Только при известных net_debt и ebitda > 0 (при ebitda <= 0 мультипликатор бессмыслен)."""
    if net_debt is None or ebitda is None or ebitda <= 0:
        return None
    return round(net_debt / ebitda, 2)


def snapshot_to_json(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Строка снимка -> JSON-совместимый dict (деньги float/None, даты ISO)."""
    out: Dict[str, Any] = {
        'report_date': _day(snapshot['report_date']).isoformat(),
        'source': snapshot['source'],
        'source_ref': snapshot.get('source_ref'),
        'retrieved_at': snapshot['retrieved_at'].isoformat() if snapshot.get('retrieved_at') else None,
        'coverage_status': snapshot.get('coverage_status'),
        'quality_status': snapshot.get('quality_status'),
        'notes': snapshot.get('notes'),
    }
    for field in _SNAPSHOT_MONEY_FIELDS:
        out[field] = _num(snapshot.get(field))
    return out


def portfolio_value_by_company(rows: List[Dict[str, Any]]) -> Tuple[Dict[int, float], float]:
    """({company_id: стоимость}, общая стоимость) из строк get_rebalance_portfolio_rows.

    Позиции без company_id входят в общую стоимость (доли считаются от всего портфеля).
    """
    by_company: Dict[int, float] = {}
    total = 0.0
    for row in rows:
        value = _num(row.get('value_rub')) or 0.0
        total += value
        if row.get('company_id') is not None:
            by_company[row['company_id']] = by_company.get(row['company_id'], 0.0) + value
    return by_company, total


def _unknowns(agg: Dict[str, Any], snap: Optional[Dict[str, Any]], freshness: str,
              net_debt: Optional[float], ebitda: Optional[float]) -> List[str]:
    unknowns: List[str] = []
    if not agg['issues_with_volume_count']:
        unknowns.append('issued_volume_unknown')
    elif agg['issues_with_volume_count'] < agg['issues_count']:
        unknowns.append('issued_volume_partial')
    if snap is None:
        unknowns.append('no_debt_snapshot')
        return unknowns
    if freshness == FRESHNESS_STALE:
        unknowns.append('debt_snapshot_stale')
    if net_debt is None:
        unknowns.append('net_debt_unknown')
    if ebitda is None:
        unknowns.append('ebitda_unknown')
    elif ebitda <= 0:
        unknowns.append('ebitda_non_positive')
    if snap.get('outstanding_bond_debt_rub') is None and snap.get('bond_debt_rub') is None:
        unknowns.append('outstanding_bond_debt_unknown')
    if snap.get('coverage_status') != 'full':
        unknowns.append('coverage_not_full')
    if snap.get('quality_status') != 'verified':
        unknowns.append('quality_not_verified')
    return unknowns


def build_company_rows(
    aggregates: List[Dict[str, Any]],
    snapshots: Dict[int, Dict[str, Any]],
    portfolio_by_company: Dict[int, float],
    portfolio_total: float,
    today: date,
    stale_days: int,
) -> List[Dict[str, Any]]:
    """Строки компаний: объём выпусков, доли, последний снимок, свежесть, unknowns.

    Сортировка/ранг — по issued_bond_volume_rub (убывание; без объёма — в конце, rank=None).
    """
    volumes = [_num(a['issued_bond_volume_rub']) for a in aggregates]
    volume_total = sum(v for v in volumes if v is not None)
    rows: List[Dict[str, Any]] = []
    for agg, volume in zip(aggregates, volumes):
        snap = snapshots.get(agg['company_id'])
        freshness, age = classify_debt_freshness(
            _day(snap['report_date']) if snap else None, today, stale_days
        )
        net_debt, net_debt_source = derive_net_debt(snap)
        ebitda = _num(snap.get('ebitda_rub')) if snap else None
        held = portfolio_by_company.get(agg['company_id'], 0.0)
        rows.append({
            'rank': None,
            'company_id': agg['company_id'],
            'name': agg['name'],
            'inn': agg['inn'],
            'issues_count': agg['issues_count'],
            'issues_with_volume_count': agg['issues_with_volume_count'],
            'issued_bond_volume_rub': round(volume, 2) if volume is not None else None,
            'issued_volume_share_pct': (
                round(volume / volume_total * 100, 2)
                if volume is not None and volume_total > 0 else None
            ),
            'portfolio_value_rub': round(held, 2),
            'portfolio_share_pct': round(held / portfolio_total * 100, 2) if portfolio_total > 0 else None,
            'freshness': {'status': freshness, 'age_days': age, 'stale_days': stale_days},
            'coverage_status': snap.get('coverage_status') if snap else None,
            'quality_status': snap.get('quality_status') if snap else None,
            'net_debt_rub': round(net_debt, 2) if net_debt is not None else None,
            'net_debt_source': net_debt_source,
            'net_debt_to_ebitda': net_debt_to_ebitda(net_debt, ebitda),
            'latest_snapshot': snapshot_to_json(snap) if snap else None,
            'unknowns': _unknowns(agg, snap, freshness, net_debt, ebitda),
        })
    rows.sort(key=lambda r: (r['issued_bond_volume_rub'] is None,
                             -(r['issued_bond_volume_rub'] or 0.0), r['name']))
    for rank, row in enumerate((r for r in rows if r['issued_bond_volume_rub'] is not None), 1):
        row['rank'] = rank
    return rows


def _concentration(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Концентрация по доле issued_bond_volume_rub: top-1/3/5 и HHI (0..10000)."""
    shares = sorted((r['issued_volume_share_pct'] for r in rows
                     if r['issued_volume_share_pct'] is not None), reverse=True)
    return {
        'basis': 'issued_bond_volume_rub',
        'companies_with_volume': len(shares),
        'top1_pct': round(sum(shares[:1]), 2),
        'top3_pct': round(sum(shares[:3]), 2),
        'top5_pct': round(sum(shares[:5]), 2),
        'hhi': round(sum(s * s for s in shares), 1),
    }


def _freshness_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {FRESHNESS_FRESH: 0, FRESHNESS_STALE: 0, FRESHNESS_MISSING: 0}
    for row in rows:
        counts[row['freshness']['status']] += 1
    return counts


def _load_rows(db: Any, today: date, stale_days: int) -> Tuple[List[Dict[str, Any]], float]:
    by_company, total = portfolio_value_by_company(db.get_rebalance_portfolio_rows())
    rows = build_company_rows(
        db.get_company_bond_aggregates(), db.get_latest_company_debt_snapshots(),
        by_company, total, today, stale_days,
    )
    return rows, total


def build_debt_report(db: Any, today: date, stale_days: int = DEFAULT_STALE_DAYS) -> Dict[str, Any]:
    """Полный read-only отчёт company-debt-report (контракт — doc/company_debt_contract.md)."""
    rows, _ = _load_rows(db, today, stale_days)
    return {
        'schema_version': 1,
        'meta': {
            'as_of': today.isoformat(),
            'stale_days': stale_days,
            'currency': 'RUB',
            'issued_volume_note': ISSUED_VOLUME_NOTE,
            'advisory': True,
        },
        'summary': {
            'companies_total': len(rows),
            'freshness': _freshness_counts(rows),
            'issued_bond_volume_total_rub': round(
                sum(r['issued_bond_volume_rub'] or 0.0 for r in rows), 2),
        },
        'concentration': _concentration(rows),
        'companies': rows,
    }


def build_debt_overlay(db: Any, today: date, stale_days: int = DEFAULT_STALE_DAYS) -> Dict[str, Any]:
    """Блок debt_overlay для rebalance-report: только эмитенты текущего портфеля.

    Advisory: на валидность плана, лимиты эмитента, кредитные ограничения,
    гейт свежести и бюджет не влияет и их не меняет.
    """
    rows, total = _load_rows(db, today, stale_days)
    held = sorted((r for r in rows if r['portfolio_value_rub'] > 0),
                  key=lambda r: (-r['portfolio_value_rub'], r['name']))
    held_value = sum(r['portfolio_value_rub'] for r in held)
    fresh_value = sum(r['portfolio_value_rub'] for r in held
                      if r['freshness']['status'] == FRESHNESS_FRESH)

    def pct(value: float) -> Optional[float]:
        return round(value / total * 100, 2) if total > 0 else None

    return {
        'advisory': True,
        'affects_plan_validity': False,
        'as_of': today.isoformat(),
        'stale_days': stale_days,
        'issued_volume_note': ISSUED_VOLUME_NOTE,
        'coverage': {
            'portfolio_issuers': len(held),
            'freshness': _freshness_counts(held),
            'portfolio_share_with_fresh_debt_data_pct': pct(fresh_value),
            'portfolio_share_unlinked_or_non_company_pct': pct(total - held_value),
        },
        'issuers': [
            {k: r[k] for k in (
                'company_id', 'name', 'portfolio_share_pct', 'issues_count',
                'issued_bond_volume_rub', 'issued_volume_share_pct', 'freshness',
                'coverage_status', 'quality_status', 'net_debt_rub',
                'net_debt_to_ebitda', 'unknowns')}
            for r in held
        ],
    }
