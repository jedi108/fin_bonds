"""
Тесты безопасного сравнения внешних подборок с портфелем (issue #5).

Чистая логика (src/services/external_comparison.py) проверяется без БД на
синтетическом PlanningContext; хранилище, агрегат двух счетов и legacy
`analyze-buy-candidates compare` — на тестовой PostgreSQL (фикстура db).
Все ISIN синтетические (RU000TEST...), реальные составы портфеля не используются.
"""
import argparse
import csv
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.services.external_comparison import (
    CONTRACT_VERSION,
    aggregate_held,
    build_comparison_report,
    classify_rating,
    load_external_rows,
)
from src.services.planning_context import CashSnapshot, DataGateState, PlanningContext
from src.storage import PortfolioStorage
from src.use_cases.analyze_buy_candidates import AnalyzeBuyCandidatesUseCase
from src.use_cases.compare_external import CompareExternalUseCase, overlay_context_values
from src.use_cases.factory import UseCaseFactory

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
SCALE = {'AAA': 20, 'AA+': 19, 'AA': 18, 'AA-': 17, 'A+': 16, 'A': 15, 'A-': 14, 'BBB+': 13}


def make_context(held_rows=(), fresh=True):
    gate = DataGateState(
        status='OK' if fresh else 'CRITICAL',
        gate_status='OK' if fresh else 'CRITICAL_NO_PRICES',
        is_fresh=fresh,
        criticals=() if fresh else ('нет цен',),
        warnings=(),
    )
    return PlanningContext(
        context_schema_version=1, calculation_policy_version='1',
        as_of=NOW, as_of_date=TODAY, generated_at=NOW,
        positions_updated_at=None, cash_updated_at=None, data_gate=gate,
        cash=CashSnapshot(None, False), held_rows=tuple(held_rows),
        universe_rows=(), effective_prices={}, universe_policy={},
    )


def cat(**kw):
    """Строка каталога после enrich_with_ytm (синтетическая)."""
    row = {
        'isin': 'RU000TEST001', 'ticker': 'TST1', 'name': 'Тест 01', 'nominal': 1000.0, 'currency': 'rub',
        'market_price': 980.0, 'market_price_updated_at': NOW - timedelta(hours=1),
        'maturity_date': TODAY + timedelta(days=700), 'offer_date': None,
        'risk_level': 1, 'list_level': 1, 'floating_coupon_flag': False,
        'ytm_percent': 18.0, 'ytm_reason': None, 'ytm_updated_at': NOW,
        'company_id': 1, 'issuer': 'Эмитент А',
        'credit_rating': None, 'rating_score': None, 'rating_date': None,
        'issuer_rating': None, 'floater_rate': None, 'floater_rate_date': None,
        'coupon_spread': None,
    }
    row.update(kw)
    return row


def ext(**kw):
    row = {'isin': 'RU000TEST001', 'source': 'ext-list', 'source_as_of': TODAY.isoformat(),
           'yield': 19.0, 'ytm_to': 'maturity', 'price': 98.0}
    row.update(kw)
    return row


def run(raw, catalog, context=None, **kw):
    context = context or make_context()
    return build_comparison_report(
        raw, {c['isin']: c for c in catalog}, context, rating_scale=SCALE, **kw)


def codes(items):
    return [i['code'] for i in items]


def test_comparable_fix_row_has_full_contract_and_fingerprint():
    report = run([ext()], [cat()])
    row = report['rows'][0]
    assert row['contract_version'] == CONTRACT_VERSION
    assert set(row) >= {'identity', 'external', 'market', 'yield', 'credit', 'portfolio', 'decision'}
    assert row['decision']['eligibility'] == 'comparable'
    assert row['decision']['blockers'] == [] and isinstance(row['decision']['warnings'], list)
    assert row['yield']['ytm_diff_pp'] == 1.0
    assert report['context_fingerprint'] == make_context().context_fingerprint
    assert report['advisory_only'] is True


def test_external_a_plus_with_local_null_rating_is_never_buy():
    """Тест 1: внешний A+ + локальный credit_rating=NULL → no BUY."""
    raw = [ext(rating='A+')]
    report = run(raw, [cat()])
    row = report['rows'][0]
    assert row['credit']['rating_status'] != 'confirmed'
    assert row['credit']['credit_rating'] is None and row['credit']['external_rating'] == 'A+'
    assert 'RATING_MISSING' in codes(row['decision']['warnings'])
    # строгий порог A+: внешний рейтинг его не подтверждает
    strict = run(raw, [cat()], min_credit_rating='A+')['rows'][0]
    assert strict['decision']['eligibility'] == 'blocked'
    assert 'RATING_MISSING' in codes(strict['decision']['blockers'])
    assert 'BUY' not in json.dumps(report, ensure_ascii=False)


def test_rating_below_threshold_differs_from_missing():
    low = cat(credit_rating='A-', rating_score=14, rating_date=TODAY - timedelta(days=30))
    row = run([ext()], [low], min_credit_rating='A+')['rows'][0]
    assert codes(row['decision']['blockers']) == ['CREDIT_RATING_BELOW_MIN']
    ok = cat(credit_rating='A+(RU)', rating_score=16, rating_date=TODAY - timedelta(days=30))
    row = run([ext()], [ok], min_credit_rating='A+')['rows'][0]
    assert row['credit']['credit_rating'] == 'A+' and row['credit']['rating_status'] == 'confirmed'
    assert row['decision']['blockers'] == []


def test_risk_level_without_rating_is_proxy_only():
    """Тест 2: risk_level=1 без рейтинга → proxy_only, а не A+."""
    credit = classify_rating(cat(risk_level=1, list_level=None), None, TODAY, SCALE)
    assert credit['rating_status'] == 'proxy_only' and credit['credit_rating'] is None
    assert credit['risk_level'] == 1


@pytest.mark.parametrize('kw, expected', [
    ({}, 'proxy_only'),
    ({'risk_level': None, 'list_level': None}, 'missing'),
    ({'issuer_rating': 'AA'}, 'issuer_only'),
    ({'credit_rating': 'ZZZ', 'rating_date': TODAY}, 'invalid'),
    ({'credit_rating': 'A+', 'rating_score': 16, 'rating_date': TODAY - timedelta(days=500)}, 'stale'),
    ({'credit_rating': 'A+', 'rating_score': 16, 'rating_date': TODAY}, 'confirmed'),
])
def test_rating_status_matrix(kw, expected):
    assert classify_rating(cat(**kw), None, TODAY, SCALE)['rating_status'] == expected


def test_a_plus_preserved_and_external_conflict_detected():
    local = cat(credit_rating='A+', rating_score=16, rating_date=TODAY)
    assert classify_rating(local, None, TODAY, SCALE)['credit_rating'] == 'A+'
    assert classify_rating(local, 'AA', TODAY, SCALE)['rating_status'] == 'conflicting'


def test_different_ytm_to_and_dates_are_not_comparable():
    """Тест 3: разные ytm_to / даты → YTM_NOT_COMPARABLE, не покупка."""
    horizon = run([ext(ytm_to='offer')], [cat()])['rows'][0]
    assert 'YTM_NOT_COMPARABLE' in codes(horizon['decision']['warnings'])
    assert horizon['decision']['eligibility'] == 'not_comparable'
    assert horizon['yield']['ytm_diff_pp'] is None

    missing_horizon = run([ext(ytm_to=None)], [cat()])['rows'][0]
    assert 'YTM_NOT_COMPARABLE' in codes(missing_horizon['decision']['warnings'])

    old = (TODAY - timedelta(days=10)).isoformat()
    skew = run([ext(source_as_of=old)], [cat()])['rows'][0]
    assert skew['identity']['match_status'] == 'STALE_CROSS_SOURCE'
    assert 'YTM_NOT_COMPARABLE' in codes(skew['decision']['warnings'])

    other_price = run([ext(price=90.0)], [cat()])['rows'][0]
    assert 'YTM_NOT_COMPARABLE' in codes(other_price['decision']['warnings'])


def test_fix_and_float_are_not_ranked_together():
    """Тест 4 + контракт FLOAT: ytm_pct=null, reason floating_coupon, отдельный список."""
    fix = cat(isin='RU000TEST001')
    flt = cat(
        isin='RU000TEST002', ticker='TST2', name='Тест 02', floating_coupon_flag=True,
        ytm_percent=25.0,  # даже если в каталоге оказалась «YTM» — ytm_pct остаётся null
        floater_rate=21.5, floater_rate_date=TODAY - timedelta(days=1), coupon_spread=1.5,
    )
    report = run([ext(), ext(isin='RU000TEST002', **{'yield': 30.0})], [fix, flt])
    assert [r['isin'] for r in report['rankings']['fix_ytm']] == ['RU000TEST001']
    assert [r['isin'] for r in report['rankings']['float']] == ['RU000TEST002']
    y = report['rows'][1]['yield']
    assert y['ytm_pct'] is None and y['ytm_reason'] == 'floating_coupon'
    assert y['current_coupon_rate'] == 21.5 and y['coupon_spread_bps'] == 150.0
    assert y['reference_rate'] == 'RUONIA' and y['rate_as_of'] == (TODAY - timedelta(days=1)).isoformat()
    assert {s['delta_bps'] for s in y['rate_scenarios']} >= {-100, 0, 100}
    assert y['ytm_diff_pp'] is None
    assert report['rows'][1]['decision']['eligibility'] == 'not_comparable'


def test_stale_floater_rate_is_blocker():
    """Тест 5: устаревшая (или неизвестная) ставка флоатера блокирует строку."""
    base = dict(floating_coupon_flag=True, floater_rate=21.5, coupon_spread=1.5)
    stale = run([ext()], [cat(**base, floater_rate_date=TODAY - timedelta(days=30))])['rows'][0]
    assert 'FLOATER_RATE_STALE' in codes(stale['decision']['blockers'])
    assert stale['decision']['eligibility'] == 'blocked'
    unknown = run([ext()], [cat(**base)])['rows'][0]
    assert 'FLOATER_RATE_STALE' in codes(unknown['decision']['blockers'])
    assert unknown['yield']['rate_as_of'] is None


def _held(isin, company_id, value, issuer='Эмитент А'):
    return {'isin': isin, 'company_id': company_id, 'issuer': issuer, 'value_rub': Decimal(str(value))}


def test_same_issuer_other_series_warns_with_exposure_before_after():
    """Тест 6: разные серии одного эмитента → HELD_SAME_ISSUER + exposure до/после."""
    held = [_held('RU000TEST900', 1, 100000), _held('RU000TEST901', 2, 400000, 'Другой')]
    report = run(
        [ext(qty=50)], [cat()], make_context(held))
    row = report['rows'][0]
    assert 'HELD_SAME_ISSUER' in codes(row['decision']['warnings'])
    p = row['portfolio']
    assert p['same_issuer_existing_exposure'] is True
    assert p['same_issuer_held_isins'] == ['RU000TEST900']
    assert p['issuer_share_before_pct'] == 20.0
    assert p['planned_value_rub'] == 49000.0  # 50 шт * 980
    assert p['issuer_share_after_pct'] == round((100000 + 49000) / (500000 + 49000) * 100, 2)
    # лимит эмитента 15% проверяется до любой оценки
    assert 'ISSUER_LIMIT' in codes(row['decision']['blockers'])
    matrix = report['issuer_matrix'][0]
    assert matrix['held_isins'] == ['RU000TEST900'] and matrix['limit_exceeded'] is True


def test_no_existing_exposure_and_missing_issuer_link():
    clean = run([ext()], [cat()], make_context([_held('RU000TEST901', 2, 400000, 'Другой')]))
    assert clean['rows'][0]['portfolio']['same_issuer_existing_exposure'] is False
    assert clean['rows'][0]['portfolio']['issuer_share_before_pct'] == 0.0
    no_link = run([ext()], [cat(company_id=None, issuer=None)])['rows'][0]
    assert 'ISSUER_LINK_MISSING' in codes(no_link['decision']['blockers'])
    assert no_link['portfolio']['issuer_share_before_pct'] is None  # неизвестное, не 0


def test_aggregate_held_sums_same_isin_across_accounts():
    rows = [_held('RU000TEST900', 1, 3000), _held('RU000TEST900', 1, 2000), _held('RU000TEST901', 2, 5000)]
    by_isin, issuer_totals, total = aggregate_held(rows)
    assert by_isin['RU000TEST900']['value'] == 5000.0
    assert issuer_totals == {1: 5000.0, 2: 5000.0} and total == 10000.0


def test_unmatched_missing_isin_and_identity_conflict_not_usable():
    raw = [
        ext(isin='RU000UNKNOWN'),
        ext(isin=None),
        ext(name='Совсем Другой Эмитент 77'),
        ext(maturity='2030-01-01'),
        ext(series='ZZZ-99'),
    ]
    rows = run(raw, [cat()])['rows']
    assert rows[0]['identity']['match_status'] == 'UNMATCHED_EXTERNAL'
    assert rows[1]['identity']['match_status'] == 'UNMATCHED_EXTERNAL'
    assert 'ISIN_MISSING' in codes(rows[1]['decision']['blockers'])
    for conflict in rows[2:]:
        assert conflict['identity']['match_status'] == 'IDENTITY_CONFLICT'
        assert 'IDENTITY_CONFLICT' in codes(conflict['decision']['blockers'])
    assert all(r['decision']['eligibility'] == 'blocked' and not r['decision']['plan_input_allowed']
               for r in rows)
    # fuzzy: похожее имя без совпавшего ISIN matched не становится
    assert run([ext(isin='RU000TEST999', name='Тест 01')], [cat()])['rows'][0][
        'identity']['match_status'] == 'UNMATCHED_EXTERNAL'


def test_matching_name_series_and_maturity_pass_identity():
    row = run([ext(name='Тест 01', series='TST1', maturity=(TODAY + timedelta(days=700)).isoformat())],
              [cat()])['rows'][0]
    assert row['identity']['match_status'] == 'MATCHED'


def test_missing_source_and_date_block_without_inventing_values():
    row = run([ext(source=None, source_as_of=None)], [cat()])['rows'][0]
    assert {'EXTERNAL_SOURCE_MISSING', 'EXTERNAL_DATE_UNKNOWN'} <= set(codes(row['decision']['blockers']))
    bad = run([ext(**{'yield': 'abc'})], [cat()])['rows'][0]
    assert bad['yield']['external_yield_pct'] is None
    assert 'EXTERNAL_VALUE_INVALID' in codes(bad['decision']['warnings'])


def test_stale_price_and_missing_local_ytm_block():
    stale = run([ext()], [cat(market_price_updated_at=NOW - timedelta(hours=48))])['rows'][0]
    assert 'PRICE_STALE' in codes(stale['decision']['blockers'])
    no_ytm = run([ext()], [cat(ytm_percent=None, ytm_reason='market_price_missing_or_nonpositive')])['rows'][0]
    assert 'YTM_MISSING' in codes(no_ytm['decision']['blockers'])
    assert no_ytm['yield']['ytm_pct'] is None


def test_stale_data_gate_blocks_every_row():
    report = run([ext(), ext(isin='RU000UNKNOWN')], [cat()], make_context(fresh=False))
    assert report['summary']['blocked'] == 2
    assert all('DATA_GATE_BLOCKED' in codes(r['decision']['blockers']) for r in report['rows'])
    assert report['data_gate']['is_fresh'] is False


def test_table_format_prints_fingerprint_and_codes(capsys):
    report = run([ext(rating='A+'), ext(isin='RU000UNKNOWN')], [cat()])
    CompareExternalUseCase._print_table(report)
    out = capsys.readouterr().out
    assert report['context_fingerprint'] in out and 'UNMATCHED_EXTERNAL' in out and 'BUY' not in out


def test_load_external_rows_json_and_csv(tmp_path):
    js = tmp_path / 'l.json'
    js.write_text(json.dumps({'rows': [ext()]}), encoding='utf-8')
    assert load_external_rows(js)[0]['isin'] == 'RU000TEST001'
    cs = tmp_path / 'l.csv'
    with cs.open('w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=['isin', 'source', 'source_as_of', 'yield'])
        w.writeheader()
        w.writerow({'isin': 'RU000TEST001', 'source': 's', 'source_as_of': '2026-11-03', 'yield': '19,5'})
    row = run(load_external_rows(cs), [cat()])['rows'][0]
    assert row['yield']['external_yield_pct'] == 19.5
    bad = tmp_path / 'b.json'
    bad.write_text('{"x": 1}', encoding='utf-8')
    with pytest.raises(ValueError):
        load_external_rows(bad)


def test_issuer_limit_is_combined_across_series_and_duplicate_isins():
    """Две серии эмитента по отдельности ниже лимита, вместе выше -> обе blocked."""
    held = [_held('RU000TEST901', 2, 900000, 'Другой')]
    cats = [cat(), cat(isin='RU000TEST002', ticker='TST2', name='Тест 02')]
    one = run([ext(qty=100)], cats, make_context(held))['rows'][0]  # 98k из 998k ~ 9.8%
    assert 'ISSUER_LIMIT' not in codes(one['decision']['blockers'])
    both = run([ext(qty=100), ext(isin='RU000TEST002', qty=100)], cats, make_context(held))
    assert all('ISSUER_LIMIT' in codes(r['decision']['blockers']) for r in both['rows'])
    assert both['issuer_matrix'][0]['limit_exceeded'] is True
    dup = run([ext(qty=100), ext(qty=100)], cats, make_context(held))  # тот же ISIN дважды
    assert dup['issuer_matrix'][0]['exposure_after_pct'] == round(196000 / 1096000 * 100, 2)
    assert all(r['decision']['eligibility'] == 'blocked' for r in dup['rows'])


def test_external_rating_ru_prefix_is_not_conflict():
    local = cat(credit_rating='A+', rating_score=16, rating_date=TODAY)
    assert classify_rating(local, 'RUA+', TODAY, SCALE)['rating_status'] == 'confirmed'
    assert classify_rating(local, 'ruA+', TODAY, SCALE)['rating_status'] == 'confirmed'
    assert classify_rating(local, 'RUAA', TODAY, SCALE)['rating_status'] == 'conflicting'


def test_local_ytm_date_skew_makes_not_comparable():
    old = NOW - timedelta(days=10)
    row = run([ext()], [cat(ytm_updated_at=old)])['rows'][0]
    assert 'YTM_NOT_COMPARABLE' in codes(row['decision']['warnings'])
    assert row['decision']['eligibility'] == 'not_comparable' and row['yield']['ytm_diff_pp'] is None


def test_non_rub_currency_has_no_planned_value():
    row = run([ext(qty=10)], [cat(currency='usd')])['rows'][0]
    assert row['portfolio']['planned_value_rub'] is None
    assert 'UNSUPPORTED_CURRENCY' in codes(row['decision']['warnings'])


def test_unknown_held_value_is_not_zero_and_bad_qty_is_rejected():
    held = [_held('RU000TEST900', 1, 100000), {**_held('RU000TEST900', 1, 0), 'value_rub': None}]
    _, issuer_totals, _ = aggregate_held(held)
    assert issuer_totals == {1: None}
    row = run([ext()], [cat()], make_context(held))['rows'][0]
    assert row['portfolio']['issuer_value_rub'] is None and row['portfolio']['issuer_share_before_pct'] is None
    assert 'HELD_VALUE_UNKNOWN' in codes(row['decision']['warnings'])
    for qty in ('0', '-5'):
        bad = run([ext(qty=qty)], [cat()])['rows'][0]
        assert 'EXTERNAL_VALUE_INVALID' in codes(bad['decision']['blockers'])
        assert bad['decision']['eligibility'] == 'blocked'


def test_identity_tolerates_name_variants_but_not_real_mismatch():
    local = cat(name='ОФЗ-ПД 26238', ticker='SU26238RMFS4')
    ok = run([ext(name='ОФЗ 26238')], [local])['rows'][0]
    assert ok['identity']['match_status'] == 'MATCHED'
    bad = run([ext(name='ОФЗ 26239')], [local])['rows'][0]
    assert bad['identity']['match_status'] == 'IDENTITY_CONFLICT'


def test_overlay_prefers_context_values_over_catalog():
    ctx_row = {'isin': 'RU000TEST001', 'market_price': 970.0, 'ytm_percent': 17.0,
               'market_price_updated_at': NOW, 'ytm_updated_at': NOW}
    context = make_context()
    context = PlanningContext(**{**context.__dict__, 'universe_rows': (ctx_row,)})
    rows = overlay_context_values([cat(), cat(isin='RU000TEST002')], context)
    assert rows[0]['market_price'] == 970.0 and rows[0]['ytm_percent'] == 17.0
    assert rows[1]['market_price'] == 980.0  # ISIN вне среза — каталог


def test_compare_external_registered_in_cli():
    assert UseCaseFactory.get_use_case_map(object())['compare-external'] is CompareExternalUseCase
    parser = argparse.ArgumentParser()
    CompareExternalUseCase.setup_parser(parser)
    args = parser.parse_args(['--file', 'x.json'])
    assert args.format == 'json' and args.min_credit_rating is None
    with pytest.raises(SystemExit):
        parser.parse_args([])


# ---------------------------------------------------------------------------
# Хранилище / legacy compare (тестовая PostgreSQL)
# ---------------------------------------------------------------------------


def _company(db, name):
    cur = db._cursor()
    cur.execute("INSERT INTO companies (name, entity_type) VALUES (%s, 'company') RETURNING id", (name,))
    return cur.fetchone()['id']


def _bond(db, **kw):
    row = {
        'isin': 'RU000TEST001', 'ticker': 'TST1', 'name': 'Тест 01', 'currency': 'rub',
        'nominal': Decimal('1000'), 'maturity_date': date.today() + timedelta(days=365),
        'coupon_quantity_per_year': 12, 'coupon_rate_percent': Decimal('20.0'), 'risk_level': 1,
        'is_trade_available': True, 'is_for_qualified_investors': False, 'issue_size': 10 ** 9,
        'market_price': Decimal('950.0'), 'market_price_updated_at': datetime.now(timezone.utc),
        'floating_coupon_flag': False, 'perpetual_flag': False, 'amortization_flag': False,
        'company_id': None, 'ytm': Decimal('18.0'), 'ytm_updated_at': datetime.now(timezone.utc),
    }
    row.update(kw)
    cur = db._cursor()
    cur.execute(
        f"INSERT INTO bonds_catalog ({', '.join(row)}) VALUES ({', '.join(['%s'] * len(row))})",
        list(row.values()))


def _position(db, isin, qty, account):
    db._cursor().execute(
        "INSERT INTO portfolio_positions (isin, ticker, name, quantity, average_price, current_price, "
        "broker_name, account_id) VALUES (%s, 'T', 'N', %s, 1000, 1000, 'tbank', %s)",
        (isin, qty, account))


def test_two_accounts_same_isin_aggregate_before_issuer_exposure(db: PortfolioStorage, tmp_path):
    """Тест 7: один ISIN на двух счетах суммируется до расчёта экспозиции эмитента."""
    issuer = _company(db, 'Эмитент А')
    other = _company(db, 'Эмитент Б')
    _bond(db, isin='RU000TEST900', ticker='H1', name='Held 1', company_id=issuer)
    _bond(db, isin='RU000TEST901', ticker='H2', name='Held 2', company_id=other)
    _bond(db, isin='RU000TEST001', company_id=issuer, ytm_updated_at=datetime.now(timezone.utc))
    _position(db, 'RU000TEST900', 3, 'A1')
    _position(db, 'RU000TEST900', 2, 'A2')   # тот же ISIN на втором счёте: всего 5 шт
    _position(db, 'RU000TEST901', 5, 'A1')
    db.conn.commit()  # snapshot_transaction контекста откатывает незакоммиченное
    path = tmp_path / 'ext.json'
    path.write_text(json.dumps([ext()]), encoding='utf-8')
    args = argparse.Namespace(file=str(path), format='json', min_credit_rating=None, max_source_skew_days=3)

    report = CompareExternalUseCase(db, rating_scale=SCALE).build_report(args)
    p = report['rows'][0]['portfolio']
    assert p['issuer_value_rub'] == 5000.0           # 3 + 2 шт по 1000, не 3000 и не 2000
    assert p['issuer_share_before_pct'] == 50.0      # 5000 из 10000
    assert p['same_issuer_held_isins'] == ['RU000TEST900']
    assert 'HELD_SAME_ISSUER' in codes(report['rows'][0]['decision']['warnings'])
    assert len(report['context_fingerprint']) == 64


def test_get_comparison_catalog_rows_rating_and_floater_rate(db: PortfolioStorage):
    issuer = _company(db, 'Эмитент А')
    _bond(db, isin='RU000TEST001', company_id=issuer)
    _bond(db, isin='RU000TEST002', ticker='F', name='Flt', company_id=issuer, floating_coupon_flag=True,
          coupon_spread=Decimal('1.5'))
    cur = db._cursor()
    cur.execute("INSERT INTO rating_history (isin, rating_date, rating_code, rating_score) "
                "VALUES ('RU000TEST001', CURRENT_DATE, 'A+', 16), ('RU000TEST001', CURRENT_DATE - 400, 'BBB+', 13)")
    cur.execute("INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value) VALUES "
                "('RU000TEST002', CURRENT_DATE - 2, 'floater_coupon_calculator', '21.5'), "
                "('RU000TEST002', CURRENT_DATE - 9, 'floater_coupon_calculator', '99')")
    rows = {r['isin']: r for r in db.get_comparison_catalog_rows(['RU000TEST001', 'RU000TEST002', 'RU000NOPE0000'])}
    assert set(rows) == {'RU000TEST001', 'RU000TEST002'}
    assert rows['RU000TEST001']['credit_rating'] == 'A+'          # A+ сохраняется как A+
    assert rows['RU000TEST001']['issuer_rating'] is None          # свой рейтинг не «рейтинг эмитента»
    assert rows['RU000TEST002']['credit_rating'] is None
    assert rows['RU000TEST002']['issuer_rating'] == 'A+'          # рейтинг другой серии → issuer_only
    assert rows['RU000TEST002']['floater_rate'] == Decimal('21.5')
    assert rows['RU000TEST002']['floater_rate_date'] == date.today() - timedelta(days=2)
    assert db.get_comparison_catalog_rows([]) == []


def test_legacy_compare_is_reference_diff_without_verdict(db: PortfolioStorage, capsys):
    """Legacy compare: сравнение по ytm, без BUY/ПОКУПАТЬ; купон и флоатеры не влияют."""
    issuer = _company(db, 'Эмитент А')
    _bond(db, isin='RU000HELD001', ticker='H', name='Held', company_id=issuer, ytm=Decimal('15.0'))
    _position(db, 'RU000HELD001', 5, 'A1')
    # огромный купон, но низкая YTM — раньше выигрывал по coupon/price
    _bond(db, isin='RU000TEST001', coupon_rate_percent=Decimal('60.0'), ytm=Decimal('10.0'))
    _bond(db, isin='RU000TEST003', ticker='T3', name='Hi YTM', coupon_rate_percent=Decimal('5.0'), ytm=Decimal('20.0'))
    _bond(db, isin='RU000TEST002', ticker='F', name='Flt', floating_coupon_flag=True,
          coupon_rate_percent=Decimal('99.0'), ytm=None)
    _bond(db, isin='RU000TEST004', ticker='T4', name='No YTM', ytm=None)

    rows = db.get_portfolio_comparison_candidates(max_risk=1)
    assert [r['isin'] for r in rows] == ['RU000TEST003', 'RU000TEST001']
    assert rows[0]['ytm_vs_portfolio'] == Decimal('5.00') and rows[1]['ytm_vs_portfolio'] == Decimal('-5.00')
    assert not {'recommendation', 'potential_yield_percent', 'yield_vs_portfolio'} & set(rows[0])

    uc = AnalyzeBuyCandidatesUseCase(db)
    uc._print_comparison_table(rows)
    out = capsys.readouterr().out
    assert 'ПОКУПАТЬ' not in out and 'BUY' not in out and 'РЕКОМЕНДУЕТСЯ' not in out
    assert 'maturity' in out  # ytm_to показан
