"""
029-T09 — equivalence-тесты: rebalance-report vs sync-chatgpt-portfolio
на одном неизменном snapshot.

Канонический сценарий задачи 029 (одна тестовая БД, один valuation date,
никакого data refresh между вызовами):

    rebalance-report --freq-min 4 --freq-max 12 --min-credit-rating AA- \
        --exclude-sovereign --include-held --screener-limit 30
    sync-chatgpt-portfolio --freq-min 4 --freq-max 12 --min-credit-rating AA- \
        --exclude-sovereign --screener-limit 30

(include_held=true во втором вызове — product-default exporter.) Оба вызова
идут по программному canonical path: exporter строит кандидатов тем же
RebalanceReportUseCase.build_report (020/029-T04..T08) — второго скринера и
post-filtering для Google Sheets нет.

Snapshot / valuation-time caveat (обязательный контекст задачи 029): тест
изолирован от DB-time границ — ytm + ytm_updated_at сеются явно (fallback с
valuation_date=date.today() не участвует), market_price_updated_at сеется
заведомо свежим с запасом до 24h cutoff, maturity_date заведомо в будущем
(CURRENT_DATE bound не участвует). Patch Python date.today() не замораживает
PostgreSQL CURRENT_TIMESTAMP/CURRENT_DATE — см.
test_ytm_null_fallback_is_date_dependent_ranking_input.

НЕ заявляется транзакционная DB-snapshot consistency: exporter не читает
PORTFOLIO / rebalance-report / cash в одной PostgreSQL REPEATABLE READ
transaction (CONTROL.snapshot_id — один export run, не одна транзакция).
"""
import argparse
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from src.use_cases import calculate_ytm
from src.use_cases.rebalance_report import RebalanceReportUseCase
from src.use_cases.sync_chatgpt_portfolio import (
    CANDIDATE_HEADERS,
    SyncChatgptPortfolioUseCase,
)

_NOW = datetime.now(timezone.utc)
# Заведомо в будущем: CURRENT_DATE bound buy-выборки не участвует.
_FUTURE_MATURITY = date.today() + timedelta(days=730)

# Зеркало шкалы config.yaml (как в тестах rebalance-report).
_RATING_SCALE = {
    'AAA.RU': 13, 'AA+.RU': 12, 'AA.RU': 11, 'AA-.RU': 10,
    'A+.RU': 9, 'A.RU': 8, 'A-.RU': 7, 'BBB+.RU': 6, 'BBB.RU': 5, 'BBB-.RU': 4,
}

# Канонический filter set задачи 029: у report-вызова явно --include-held,
# у sync — его нет (include_held=True — product-default exporter).
_REPORT_FILTER_ARGS = [
    '--freq-min', '4', '--freq-max', '12', '--min-credit-rating', 'AA-',
    '--exclude-sovereign', '--include-held', '--screener-limit', '30',
]
_EXPORT_FILTER_ARGS = [
    '--freq-min', '4', '--freq-max', '12', '--min-credit-rating', 'AA-',
    '--exclude-sovereign', '--screener-limit', '30',
]

# Столбец CANDIDATES -> ключ canonical screener-строки (мэппинг
# build_candidates_payload): любое расхождение — post-filter transformation.
_CANDIDATE_SOURCE_FIELDS = {
    'isin': 'isin',
    'name': 'name',
    'ticker': 'ticker',
    'issuer': 'issuer',
    'price_rub': 'price',
    'ytm_pct': 'ytm_pct',
    'ytm_reason': 'ytm_reason',
    'coupon_pct': 'coupon_pct',
    'coupon_frequency': 'freq',
    'maturity_date': 'maturity',
    'offer_date': 'offer',
    'amortization_flag': 'amort',
    'credit_rating': 'credit_rating',
    'risk_level': 'risk_level',
    'list_level': 'list_level',
    'ku': 'ku',
    'issuer_pct_current': 'issuer_pct',
    'held_badge': 'held_badge',
}


def _report_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    RebalanceReportUseCase.setup_parser(parser)
    return parser.parse_args(_REPORT_FILTER_ARGS)


def _export_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    SyncChatgptPortfolioUseCase.setup_parser(parser)
    return parser.parse_args(_EXPORT_FILTER_ARGS)


def _cand(row_values, column):
    return row_values[CANDIDATE_HEADERS.index(column)]


class _FakeFactory:
    """Дублёр UseCaseFactory: кешированный db connection + config."""

    def __init__(self, db, config):
        self._db = db
        self.config = config

    def get_db_connection(self):
        return self._db


class _FakePublisher:
    def __init__(self):
        self.call = None

    def publish(self, **kwargs):
        self.call = kwargs


def _insert_bond(db, isin, ticker, name, *, company_id, freq, coupon,
                 ytm='15.0', floating=False, ku=False):
    """Каталогная строка, видимая buy-выборкой: свежая цена (запас до 24h
    cutoff), заведомо будущая maturity (запас до CURRENT_DATE bound)."""
    cur = db._cursor()
    cur.execute(
        "INSERT INTO bonds_catalog (isin, ticker, name, currency, nominal, "
        "maturity_date, coupon_quantity_per_year, coupon_rate_percent, "
        "coupon_spread, risk_level, list_level, is_trade_available, "
        "is_for_qualified_investors, issue_size, market_price, "
        "market_price_updated_at, floating_coupon_flag, perpetual_flag, "
        "amortization_flag, company_id, ytm, ytm_updated_at) "
        "VALUES (%s, %s, %s, 'rub', %s, %s, %s, %s, %s, 1, 1, TRUE, %s, "
        "1000000, %s, %s, %s, FALSE, FALSE, %s, %s, %s)",
        (isin, ticker, name, Decimal('1000'), _FUTURE_MATURITY, freq,
         None if floating else Decimal(coupon),
         Decimal('1.5') if floating else None, ku,
         Decimal('950.0'), _NOW, floating, company_id,
         None if ytm is None else Decimal(ytm),
         None if ytm is None else _NOW),
    )


def _seed_equivalence_snapshot(db):
    """Неизменный snapshot: 2 позиции (held fix + held floater) и 11 каталогных
    бумаг, из которых в канонический filter set задачи 029 проходят 3 фикса и
    1 флоатер; ytm + ytm_updated_at сеются явно — fallback не участвует."""
    cur = db._cursor()
    cur.execute(
        "INSERT INTO companies (name, entity_type) "
        "VALUES ('Эквиваленс Пром', 'company') RETURNING id"
    )
    corp = cur.fetchone()['id']
    cur.execute(
        "INSERT INTO companies (name, entity_type) "
        "VALUES ('Минфин России', 'sovereign') RETURNING id"
    )
    minfin = cur.fetchone()['id']

    # FIX-кандидаты: EQV1 (held), EQV2, EQV3 проходят; EQVL ниже порога AA-,
    # EQVF freq 2 < 4, EQVK — КУ, EQVN без рейтинга, OFZ — суверенный.
    _insert_bond(db, 'RU000A0EQV1', 'EQV1', 'Эквиваленс 001P-01', company_id=corp,
                 freq=12, coupon='20.0', ytm='16.0')
    _insert_bond(db, 'RU000A0EQV2', 'EQV2', 'Эквиваленс 002P-02', company_id=corp,
                 freq=4, coupon='14.0', ytm='15.0')
    _insert_bond(db, 'RU000A0EQV3', 'EQV3', 'Эквиваленс 003P-03', company_id=corp,
                 freq=6, coupon='12.0', ytm='14.0')
    _insert_bond(db, 'RU000A0EQVL', 'EQVL', 'Эквиваленс Низкий Рейтинг',
                 company_id=corp, freq=4, coupon='15.0', ytm='18.0')
    _insert_bond(db, 'RU000A0EQVF', 'EQVF', 'Эквиваленс Редкий Купон',
                 company_id=corp, freq=2, coupon='16.0', ytm='19.0')
    _insert_bond(db, 'RU000A0EQVK', 'EQVK', 'Эквиваленс КУ', company_id=corp,
                 freq=4, coupon='15.0', ytm='17.0', ku=True)
    _insert_bond(db, 'RU000A0EQVN', 'EQVN', 'Эквиваленс Без Рейтинга',
                 company_id=corp, freq=4, coupon='15.0', ytm='17.5')
    _insert_bond(db, 'RU000A0OFZE', 'ОФЗЭ', 'ОФЗ 26299', company_id=minfin,
                 freq=4, coupon='10.0', ytm='20.0')
    # FLOAT-кандидаты (ставка — из monitoring_checks): EQVA (held) проходит,
    # EQVB freq 2 < 4, EQVC купон 40 > max-ytm 35.
    _insert_bond(db, 'RU000A0EQVA', 'EQVA', 'Эквиваленс Флоатер 001',
                 company_id=corp, freq=4, coupon=None, ytm=None, floating=True)
    _insert_bond(db, 'RU000A0EQVB', 'EQVB', 'Эквиваленс Флоатер 002',
                 company_id=corp, freq=2, coupon=None, ytm=None, floating=True)
    _insert_bond(db, 'RU000A0EQVC', 'EQVC', 'Эквиваленс Флоатер Аномалия',
                 company_id=corp, freq=4, coupon=None, ytm=None, floating=True)

    for isin, metric in (('RU000A0EQVA', '18.0'), ('RU000A0EQVB', '12.0'),
                         ('RU000A0EQVC', '40.0')):
        cur.execute(
            "INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value) "
            "VALUES (%s, CURRENT_DATE, 'floater_coupon_calculator', %s)",
            (isin, metric),
        )

    # Позиции: held fix + held floater — include_held=true в обоих вызовах.
    for isin, quantity, price in (('RU000A0EQV1', 10, 1000.0), ('RU000A0EQVA', 20, 950.0)):
        cur.execute(
            "INSERT INTO portfolio_positions (isin, ticker, name, quantity, "
            "average_price, current_price, current_value, broker_name, "
            "account_id, status, updated_at) "
            "VALUES (%s, 'TICK', 'Позиция', %s, %s, %s, %s, 'TBank', 't1', "
            "'active', %s)",
            (isin, quantity, price, price, quantity * price, _NOW),
        )

    # Настоящие рейтинги (latest rating_history): AA- и выше проходят, A — нет.
    ratings = {
        'RU000A0EQV1': ('AAA', 13), 'RU000A0EQV2': ('AA-', 10),
        'RU000A0EQV3': ('AA-', 10), 'RU000A0EQVL': ('A', 8),
        'RU000A0EQVF': ('AA-', 10), 'RU000A0EQVK': ('AA-', 10),
        'RU000A0OFZE': ('AAA', 13), 'RU000A0EQVA': ('AA-', 10),
        'RU000A0EQVB': ('AA-', 10), 'RU000A0EQVC': ('AA-', 10),
    }
    for isin, (code, score) in ratings.items():
        db.add_rating(isin, date.today(), code, score)
    db.conn.commit()


def test_report_and_sync_candidates_equivalent(db):
    """Один snapshot, один valuation date: rebalance-report и
    sync-chatgpt-portfolio дают один canonical screener result —
    одинаковые CANDIDATES (ISIN, ranks, filters), exporter ничего
    не дофильтровывает и не преобразует (029-T09)."""
    _seed_equivalence_snapshot(db)

    # Шаг 1: rebalance-report с filter set задачи 029.
    report = RebalanceReportUseCase(db=db, rating_scale=_RATING_SCALE).build_report(
        _report_args()
    )

    # Шаг 2: sync-chatgpt-portfolio с тем же filter set (factory path,
    # publisher подменён — тот же db, никакого data refresh между вызовами).
    exporter = SyncChatgptPortfolioUseCase.create(
        _FakeFactory(db, {'rating_scale': _RATING_SCALE})
    )
    publisher = _FakePublisher()
    exporter.publisher = publisher
    exporter.execute(_export_args())

    # Шаг 3: canonical screener rows vs exported CANDIDATES.
    screener = report['screener']
    control = publisher.call['control']
    candidates = publisher.call['candidate_rows']
    meta = json.loads(control['candidate_meta'])

    # Фильтр set действительно канонический (задача 029).
    filters = screener['filters']
    assert (filters['freq_min'], filters['freq_max']) == (4, 12)
    assert filters['min_credit_rating'] == 'AA-'
    assert filters['exclude_sovereign'] is True
    assert filters['screener_limit'] == 30
    # candidate_meta.filters совпадает — exporter переносит блок как есть.
    assert meta['filters'] == filters
    assert meta['filters']['include_held'] is True  # product-default exporter

    fix_report = [row['isin'] for row in screener['fixed']]
    float_report = [row['isin'] for row in screener['floater']]
    fix_export = [
        _cand(r, 'isin') for r in candidates if _cand(r, 'candidate_type') == 'FIX'
    ]
    float_export = [
        _cand(r, 'isin') for r in candidates if _cand(r, 'candidate_type') == 'FLOAT'
    ]

    # Содержательный canon сеяного snapshot: фильтры честно отработали.
    assert fix_report == ['RU000A0EQV1', 'RU000A0EQV2', 'RU000A0EQV3']
    assert float_report == ['RU000A0EQVA']

    # Одинаковый набор ISIN и одинаковый порядок/rank FIX.
    assert set(fix_export) == set(fix_report)
    assert fix_export == fix_report
    assert [_cand(r, 'rank') for r in candidates
            if _cand(r, 'candidate_type') == 'FIX'] == [1, 2, 3]
    # Одинаковый набор ISIN и одинаковый порядок/rank FLOAT.
    assert set(float_export) == set(float_report)
    assert float_export == float_report
    assert [_cand(r, 'rank') for r in candidates
            if _cand(r, 'candidate_type') == 'FLOAT'] == [1]

    # Остальная candidate metadata переносится как есть (без пересчёта).
    assert meta['candidates_total'] == screener['candidates_total']
    assert meta['fixed_matched'] == screener['fixed_matched'] == 3
    assert meta['floater_matched'] == screener['floater_matched'] == 1
    assert meta['excluded'] == screener['excluded']
    assert meta['excluded'] == {
        'held': 0, 'ku': 1, 'sovereign': 1, 'freq': 2, 'risk': 0, 'listlevel': 0,
        'rating_missing': 1, 'rating_below_min': 1,
        'fixed_ytm_missing': 0, 'fixed_max_ytm': 0,
        'floater_coupon_missing': 0, 'floater_max_ytm': 1,
    }

    # Exporter не делает post-filter transformations: каждый столбец CANDIDATES
    # равен полю canonical screener-строки, count совпадает 1:1.
    source_by_isin = {row['isin']: row for row in screener['fixed'] + screener['floater']}
    assert len(candidates) == control['candidates_count'] == len(source_by_isin)
    for row in candidates:
        source = source_by_isin[_cand(row, 'isin')]
        for column, key in _CANDIDATE_SOURCE_FIELDS.items():
            assert _cand(row, column) == source[key], (row, column)
    # Held-бумаги дошли до CANDIDATES с бейджем (include_held=true в обоих).
    held = {_cand(r, 'isin') for r in candidates
            if (_cand(r, 'held_badge') or '').startswith('held (')}
    assert held == {'RU000A0EQV1', 'RU000A0EQVA'}

    # Изоляция от границ: всё опубликованное имеет заведомо будущую maturity;
    # свежесть цен с запасом до 24h cutoff задана seed'ом (_NOW).
    future = _FUTURE_MATURITY.isoformat()
    for row in screener['fixed'] + screener['floater']:
        assert row['maturity'] == future


def test_ytm_null_fallback_is_date_dependent_ranking_input(db, monkeypatch):
    """Regression к snapshot caveat (029): ytm_updated_at IS NULL — canonical
    YTM считается на лету cashflow-решателем с valuation_date=date.today(),
    т.е. ranking-вход зависит от календарного дня вызова, а не только от строк
    БД. «Same DB rows» без same valuation date — не строгая гарантия
    идентичного ranking (Python-патч не замораживает PostgreSQL
    CURRENT_TIMESTAMP/CURRENT_DATE). Именно поэтому equivalence-тест выше
    сеет ytm + ytm_updated_at и исключает fallback из сравнения."""
    cur = db._cursor()
    cur.execute(
        "INSERT INTO companies (name, entity_type) "
        "VALUES ('Фолбэк Пром', 'company') RETURNING id"
    )
    corp = cur.fetchone()['id']
    # Цена свежая, maturity в будущем — из DB-time границ участвует только
    # отсутствие ytm_updated_at (fallback-ветка движка).
    _insert_bond(db, 'RU000A0FBLK', 'FBLK', 'Фолбэк 001P-01', company_id=corp,
                 freq=4, coupon='14.0', ytm=None)
    db.add_rating('RU000A0FBLK', date.today(), 'AA-', 10)
    db.conn.commit()

    class _ValuationDay(date):
        fixed = date(2026, 1, 1)

        @classmethod
        def today(cls):
            return cls.fixed

    monkeypatch.setattr(calculate_ytm, 'date', _ValuationDay)
    use_case = RebalanceReportUseCase(db=db, rating_scale=_RATING_SCALE)

    early = use_case.build_report(_report_args())
    row_early = next(
        r for r in early['screener']['fixed'] if r['isin'] == 'RU000A0FBLK'
    )
    # YTM появилась в отчёте, хотя в БД её нет — она рассчитана на лету.
    assert row_early['ytm_pct'] is not None
    assert row_early['ytm_reason'] is None
    cur.execute(
        "SELECT ytm, ytm_updated_at FROM bonds_catalog WHERE isin = 'RU000A0FBLK'"
    )
    stored = cur.fetchone()
    assert stored['ytm'] is None and stored['ytm_updated_at'] is None

    # Та же строка БД, другой valuation date — другая YTM: ranking при
    # нескольких таких бумагах может измениться через границу календарного дня.
    _ValuationDay.fixed = date(2027, 6, 1)
    late = use_case.build_report(_report_args())
    row_late = next(
        r for r in late['screener']['fixed'] if r['isin'] == 'RU000A0FBLK'
    )
    assert row_late['ytm_pct'] != row_early['ytm_pct']
