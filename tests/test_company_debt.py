"""
Тесты долговой нагрузки компаний (issue #4): миграция 017, import-company-debt,
company-debt-report, override-слой идентичности, rebalance-report --debt-overlay.

Данные синтетические (тестовые ИНН/ISIN вида RU000TEST001), реальные составы
портфеля не используются. Нужна тестовая БД (фикстура db, POSTGRES_DSN_TEST).
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.company_normalization import resolve_company
from src.services.company_debt import (
    build_debt_overlay,
    build_debt_report,
    classify_debt_freshness,
    derive_net_debt,
    net_debt_to_ebitda,
)
from src.use_cases.company_debt_report import CompanyDebtReportUseCase, format_table
from src.use_cases.factory import UseCaseFactory
from src.use_cases.import_company_debt import (
    ImportCompanyDebtUseCase,
    import_company_debt,
    load_rows,
    parse_snapshot_row,
)
from src.use_cases.link_companies import link_bonds_to_companies
from src.use_cases.rebalance_report import RebalanceReportUseCase

from test_rebalance_report import _NOW, _insert_bond, _insert_company, _insert_position, _make_args
from test_rebalance_scenario import GM1, _seed_base, _write_scenario_file

TODAY = date(2026, 11, 3)


def _row(**kw):
    base = {'company_inn': '7700000001', 'source': 'test-src', 'report_date': '2026-06-30',
            'source_ref': 'ref-1'}
    base.update(kw)
    return base


def _company(db, name, inn=None):
    cur = db._cursor()
    cur.execute("INSERT INTO companies (name, inn) VALUES (%s, %s) RETURNING id", (name, inn))
    db.conn.commit()
    return cur.fetchone()['id']


def _snapshot_count(db):
    cur = db._cursor()
    cur.execute("SELECT COUNT(*) AS n FROM company_debt_snapshots")
    return cur.fetchone()['n']


# ---------------------------------------------------------------------------
# Чистая логика
# ---------------------------------------------------------------------------

class TestPureLogic:
    def test_freshness(self):
        assert classify_debt_freshness(None, TODAY, 180) == ('missing', None)
        assert classify_debt_freshness(TODAY - timedelta(days=180), TODAY, 180) == ('fresh', 180)
        assert classify_debt_freshness(TODAY - timedelta(days=181), TODAY, 180) == ('stale', 181)

    def test_net_debt_derived_only_when_both_known(self):
        assert derive_net_debt({'net_debt_rub': Decimal('5')}) == (5.0, 'reported')
        assert derive_net_debt({'total_debt_rub': Decimal('10'), 'cash_rub': Decimal('4')}) == (6.0, 'derived')
        assert derive_net_debt({'total_debt_rub': Decimal('10'), 'cash_rub': None}) == (None, None)
        assert derive_net_debt(None) == (None, None)

    def test_ratio_needs_net_debt_and_positive_ebitda(self):
        assert net_debt_to_ebitda(300.0, 100.0) == 3.0
        assert net_debt_to_ebitda(None, 100.0) is None
        assert net_debt_to_ebitda(300.0, None) is None
        assert net_debt_to_ebitda(300.0, 0.0) is None
        assert net_debt_to_ebitda(300.0, -10.0) is None

    def test_parse_requires_provenance_and_keeps_null(self):
        snap, errors = parse_snapshot_row(_row(total_debt_rub='1 000,5', cash_rub=''))
        assert errors == []
        assert snap['total_debt_rub'] == Decimal('1000.5')
        assert snap['cash_rub'] is None  # NULL остаётся NULL, не 0
        for missing in ('source', 'report_date', 'source_ref'):
            _, errors = parse_snapshot_row(_row(**{missing: ''}))
            assert f'missing_{missing}' in errors

    def test_parse_rejects_bad_values(self):
        assert 'bad_total_debt_rub' in parse_snapshot_row(_row(total_debt_rub='-1'))[1]
        assert 'bad_cash_rub' in parse_snapshot_row(_row(cash_rub='abc'))[1]
        assert 'bad_report_date' in parse_snapshot_row(_row(report_date='30.06.2026'))[1]
        assert 'bad_coverage_status' in parse_snapshot_row(_row(coverage_status='great'))[1]
        # '250,000' неоднозначно (250 тыс. или 250.000) и смесь разделителей — отклоняем
        for bad in ('250,000', '1,234.5', '1,234,567'):
            assert 'bad_total_debt_rub' in parse_snapshot_row(_row(total_debt_rub=bad))[1], bad
        assert parse_snapshot_row(_row(total_debt_rub='250.000'))[0]['total_debt_rub'] == Decimal('250.000')
        assert parse_snapshot_row(_row(total_debt_rub='2,5'))[0]['total_debt_rub'] == Decimal('2.5')
        # будущая report_date не может считаться «свежей»
        future = (date.today() + timedelta(days=1)).isoformat()
        assert 'bad_report_date' in parse_snapshot_row(_row(report_date=future))[1]
        assert parse_snapshot_row(_row(report_date=date.today().isoformat()))[1] == []
        # net_debt и ebitda могут быть отрицательными
        assert parse_snapshot_row(_row(net_debt_rub='-5', ebitda_rub='-1'))[1] == []


# ---------------------------------------------------------------------------
# Идентичность: override вместо таблицы алиасов, без удаления строк
# ---------------------------------------------------------------------------

class TestIdentityOverride:
    def test_samolet_override_maps_both_names_to_one_company_non_destructive(self, db):
        # Исторические дубли одного юрлица: у одной строки ИНН пуст (по имени сливаем
        # только одинаковые/пустые ИНН; разные ИНН по имени не сливаются)
        old_gk = _company(db, 'ГК Самолет', None)
        old_short = _company(db, 'Самолет', '7700000102')
        _insert_bond(db, isin='RU000TEST001', name='ГК Самолет БО-П01', company_id=old_gk)
        _insert_bond(db, isin='RU000TEST002', name='Самолет 001Р-02', company_id=old_short)
        db.conn.commit()

        overrides = {'ГК Самолет': 'Самолет'}
        assert resolve_company('ГК Самолет БО-П01', overrides).name == 'Самолет'
        assert resolve_company('Самолет 001Р-02', overrides).name == 'Самолет'

        link_bonds_to_companies(db, overrides, only_unlinked=False)

        cur = db._cursor()
        cur.execute("SELECT DISTINCT company_id FROM bonds_catalog "
                    "WHERE isin IN ('RU000TEST001', 'RU000TEST002')")
        assert len(cur.fetchall()) == 1
        # старые строки companies не удалены
        cur.execute("SELECT id FROM companies WHERE id IN (%s, %s)", (old_gk, old_short))
        assert {r['id'] for r in cur.fetchall()} == {old_gk, old_short}


# ---------------------------------------------------------------------------
# Миграция и импорт
# ---------------------------------------------------------------------------

class TestImport:
    def test_match_by_inn_then_name_and_unmatched_not_inserted(self, db):
        by_inn = _company(db, 'Альфа Пром', '7700000001')
        by_name = _company(db, 'Бета Финанс')
        rows = [
            _row(total_debt_rub='100'),                                   # по ИНН
            _row(company_inn='', company_name='бета финанс', total_debt_rub='50'),  # по имени
            _row(company_inn='7799999999', company_name='Нет Такой'),     # не найдена
            _row(report_date=''),                                         # невалидна
        ]
        summary = import_company_debt(db, rows)
        assert (summary['inserted'], summary['updated']) == (2, 0)
        assert [u['reason'] for u in summary['unmatched']] == ['not_found']
        assert summary['invalid'][0]['errors'] == ['missing_report_date']
        assert _snapshot_count(db) == 2
        cur = db._cursor()
        cur.execute("SELECT COUNT(*) AS n FROM companies")
        assert cur.fetchone()['n'] == 2  # компании не создаются
        assert {by_inn, by_name} == set(db.get_latest_company_debt_snapshots())

    def test_inn_mismatch_with_name_is_not_matched(self, db):
        _company(db, 'Альфа Пром', '7700000001')
        summary = import_company_debt(db, [_row(company_inn='7700000002', company_name='Альфа Пром')])
        assert summary['inserted'] == 0
        assert summary['unmatched'][0]['reason'] == 'not_found'

    def test_ambiguous_name_not_matched(self, db):
        _company(db, 'Дубль')
        _company(db, 'дубль')
        summary = import_company_debt(db, [_row(company_inn='', company_name='Дубль')])
        assert summary['unmatched'][0]['reason'] == 'ambiguous'

    def test_name_matching_other_inn_and_null_inn_is_not_ambiguous(self, db):
        _company(db, 'Гамма', '7700000009')   # другой ИНН — не та же компания
        null_inn = _company(db, 'гамма')      # ИНН пуст — единственный кандидат
        summary = import_company_debt(db, [_row(company_inn='7700000001', company_name='Гамма')])
        assert (summary['inserted'], summary['unmatched']) == (1, [])
        assert set(db.get_latest_company_debt_snapshots()) == {null_inn}

    def test_idempotent_upsert_and_null_stays_null(self, db):
        cid = _company(db, 'Альфа Пром', '7700000001')
        rows = [_row(total_debt_rub='100', cash_rub='30', ebitda_rub='20')]
        assert import_company_debt(db, rows)['inserted'] == 1
        again = import_company_debt(db, rows)
        assert (again['inserted'], again['updated']) == (0, 1)
        assert _snapshot_count(db) == 1
        # новая версия без cash: поле снова NULL, не 0; другой источник — отдельная строка
        import_company_debt(db, [_row(total_debt_rub='100')])
        assert db.get_latest_company_debt_snapshots()[cid]['cash_rub'] is None
        import_company_debt(db, [_row(source='other-src', total_debt_rub='1')])
        assert _snapshot_count(db) == 2

    def test_load_rows_csv_and_json(self, tmp_path):
        csv_path = tmp_path / 'debt.csv'
        with csv_path.open('w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(_row().keys()) + ['cash_rub'])
            writer.writeheader()
            writer.writerow({**_row(), 'cash_rub': '5'})
        assert load_rows(csv_path)[0]['cash_rub'] == '5'
        json_path = tmp_path / 'debt.json'
        json_path.write_text(json.dumps({'snapshots': [_row()]}), encoding='utf-8')
        assert load_rows(json_path)[0]['source'] == 'test-src'

    def test_use_case_prints_summary(self, db, tmp_path, capsys):
        _company(db, 'Альфа Пром', '7700000001')
        path = tmp_path / 'debt.json'
        path.write_text(json.dumps([_row(total_debt_rub='1')]), encoding='utf-8')

        class _Factory:
            def get_db_connection(self):
                return db
        use_case = ImportCompanyDebtUseCase(_Factory())
        use_case.execute(argparse.Namespace(file=str(path)))
        assert json.loads(capsys.readouterr().out)['inserted'] == 1


def test_commands_registered():
    mapping = UseCaseFactory.get_use_case_map(UseCaseFactory.__new__(UseCaseFactory))
    assert mapping['import-company-debt'] is ImportCompanyDebtUseCase
    assert mapping['company-debt-report'] is CompanyDebtReportUseCase


# ---------------------------------------------------------------------------
# company-debt-report
# ---------------------------------------------------------------------------

def _seed_report(db):
    """Три компании: свежий снимок, устаревший, без снимка; + физлицо и суверен вне отчёта."""
    big = _company(db, 'Большая Компания', '7700000001')
    mid = _company(db, 'Средняя Компания', '7700000002')
    small = _company(db, 'Малая Компания', '7700000003')
    cur = db._cursor()
    cur.execute("INSERT INTO companies (name, entity_type) VALUES ('Минфин', 'sovereign')")
    db.conn.commit()
    # big: 2 выпуска, 1 000 000 * 1000 = 1e9 и 500 000 * 500 = 2.5e8
    _insert_bond(db, isin='RU000TEST001', company_id=big, issue_size=1000000, nominal=Decimal('1000'))
    _insert_bond(db, isin='RU000TEST002', company_id=big, issue_size=500000, nominal=Decimal('500'))
    _insert_bond(db, isin='RU000TEST003', company_id=mid, issue_size=100000, nominal=Decimal('1000'))
    # у small размер выпуска неизвестен
    _insert_bond(db, isin='RU000TEST004', company_id=small, issue_size=None, nominal=Decimal('1000'))
    db.conn.commit()
    db.upsert_company_debt_snapshot(big, {
        'report_date': TODAY - timedelta(days=60), 'source': 's', 'source_ref': 'r',
        'total_debt_rub': Decimal('900'), 'cash_rub': Decimal('300'), 'ebitda_rub': Decimal('200'),
        'coverage_status': 'full', 'quality_status': 'verified'})
    db.upsert_company_debt_snapshot(mid, {
        'report_date': TODAY - timedelta(days=400), 'source': 's', 'source_ref': 'r',
        'total_debt_rub': Decimal('50'), 'coverage_status': 'partial', 'quality_status': 'unverified'})
    return big, mid, small


class TestDebtReport:
    def test_report_content(self, db):
        big, mid, small = _seed_report(db)
        report = build_debt_report(db, TODAY, stale_days=180)
        rows = {r['name']: r for r in report['companies']}

        assert set(rows) == {'Большая Компания', 'Средняя Компания', 'Малая Компания'}
        assert 'НЕ остаток долга' in report['meta']['issued_volume_note']
        b = rows['Большая Компания']
        assert b['issued_bond_volume_rub'] == 1250000000.0 and b['issues_count'] == 2
        assert b['rank'] == 1
        assert b['freshness'] == {'status': 'fresh', 'age_days': 60, 'stale_days': 180}
        assert b['net_debt_rub'] == 600.0 and b['net_debt_source'] == 'derived'
        assert b['net_debt_to_ebitda'] == 3.0
        assert 'coverage_not_full' not in b['unknowns']

        m = rows['Средняя Компания']
        assert m['freshness']['status'] == 'stale' and m['rank'] == 2
        assert m['net_debt_to_ebitda'] is None  # нет net_debt и ebitda — не выводим
        assert {'debt_snapshot_stale', 'net_debt_unknown', 'ebitda_unknown',
                'coverage_not_full'} <= set(m['unknowns'])

        s = rows['Малая Компания']
        assert s['issued_bond_volume_rub'] is None and s['rank'] is None
        assert s['freshness']['status'] == 'missing' and s['latest_snapshot'] is None
        assert {'issued_volume_unknown', 'no_debt_snapshot'} <= set(s['unknowns'])

        assert report['summary']['freshness'] == {'fresh': 1, 'stale': 1, 'missing': 1}
        # доли считаются от суммы известных объёмов: 1.25e9 / (1.25e9 + 1e8)
        assert b['issued_volume_share_pct'] == pytest.approx(92.59, abs=0.01)
        assert report['concentration']['top1_pct'] == pytest.approx(92.59, abs=0.01)
        json.dumps(report)  # сериализуемо

    def test_non_rub_and_matured_issues_excluded_from_volume(self, db):
        big, _, _ = _seed_report(db)
        # USD-выпуск: не в сумме, но выпуск учтён => volume_partial; погашенный — вне отчёта
        _insert_bond(db, isin='RU000TEST006', company_id=big, currency='usd',
                     issue_size=1000000, nominal=Decimal('1000'))
        _insert_bond(db, isin='RU000TEST007', company_id=big, issue_size=1000000,
                     nominal=Decimal('1000'), maturity_date=date.today() - timedelta(days=1))
        db.conn.commit()
        b = {r['name']: r for r in build_debt_report(db, TODAY)['companies']}['Большая Компания']
        assert b['issued_bond_volume_rub'] == 1250000000.0
        assert (b['issues_count'], b['issues_with_volume_count']) == (3, 2)
        assert 'issued_volume_partial' in b['unknowns']

    def test_portfolio_exposure_share(self, db):
        big, _, _ = _seed_report(db)
        _insert_bond(db, isin='RU000TEST005', company_id=big, market_price=Decimal('1000'))
        _insert_position(db, 'RU000TEST005', quantity=10, price=1000)
        db.conn.commit()
        rows = {r['name']: r for r in build_debt_report(db, TODAY)['companies']}
        assert rows['Большая Компания']['portfolio_share_pct'] == 100.0
        assert rows['Малая Компания']['portfolio_share_pct'] == 0.0

    def test_table_and_cli_use_case(self, db, capsys):
        _seed_report(db)

        class _Factory:
            def get_db_connection(self):
                return db
        use_case = CompanyDebtReportUseCase(_Factory())
        use_case.execute(argparse.Namespace(format='table', stale_days=180))
        out = capsys.readouterr().out
        assert 'Большая Компания' in out and 'НЕ остаток долга' in out
        with pytest.raises(ValueError):
            use_case.execute(argparse.Namespace(format='json', stale_days=0))
        assert format_table(build_debt_report(db, TODAY))


# ---------------------------------------------------------------------------
# rebalance-report --debt-overlay
# ---------------------------------------------------------------------------

def _strip_time(report):
    report = json.loads(json.dumps(report))
    report['meta'].pop('generated_at')
    return report


class TestDebtOverlay:
    def test_flag_default_off_in_parser(self):
        parser = argparse.ArgumentParser()
        RebalanceReportUseCase.setup_parser(parser)
        assert parser.parse_args([]).debt_overlay is False
        assert parser.parse_args(['--debt-overlay']).debt_overlay is True
        assert RebalanceReportUseCase.default_args().debt_overlay is False

    def test_off_output_has_no_overlay_and_equals_baseline(self, db):
        _seed_base(db)
        use_case = RebalanceReportUseCase(db=db)
        baseline = _strip_time(use_case.build_report(_make_args()))  # без атрибута debt_overlay
        off = _strip_time(use_case.build_report(_make_args(debt_overlay=False)))
        assert 'debt_overlay' not in off
        assert json.dumps(off, sort_keys=True) == json.dumps(baseline, sort_keys=True)

    def test_on_adds_block_without_changing_plan(self, db, tmp_path):
        _seed_base(db)
        scenario = _write_scenario_file(tmp_path, trades=[{'isin': GM1, 'qty_delta': 5}])
        use_case = RebalanceReportUseCase(db=db)
        off = _strip_time(use_case.build_report(_make_args(scenario=scenario)))
        on = _strip_time(use_case.build_report(_make_args(scenario=scenario, debt_overlay=True)))

        overlay = on.pop('debt_overlay')
        assert on == off  # все остальные блоки (включая scenario valid/feasible/blockers) идентичны
        assert off['scenario']['valid'] is True
        assert overlay['advisory'] is True and overlay['affects_plan_validity'] is False
        assert {i['name'] for i in overlay['issuers']} == {'Альфа Пром', 'Бета Финанс'}
        # данных о долге нет: всё missing, NULL не превращается в 0
        assert overlay['coverage']['freshness']['missing'] == 2
        assert overlay['coverage']['portfolio_share_with_fresh_debt_data_pct'] == 0.0
        assert all(i['net_debt_to_ebitda'] is None and 'no_debt_snapshot' in i['unknowns']
                   for i in overlay['issuers'])

    def test_overlay_without_migration_017_returns_error_block(self, db):
        _seed_base(db)
        db._cursor().execute("DROP TABLE company_debt_snapshots")  # откатывается в обработчике
        use_case = RebalanceReportUseCase(db=db)
        report = use_case.build_report(_make_args(debt_overlay=True))
        assert 'error' in report['debt_overlay'] and 'issuers' not in report['debt_overlay']
        assert report['portfolio']['positions']  # остальной отчёт цел
        assert _snapshot_count(db) == 0  # таблица на месте (DROP откатан)

    def test_overlay_uses_fresh_snapshot(self, db):
        _seed_base(db)
        cur = db._cursor()
        cur.execute("SELECT id FROM companies WHERE name = 'Альфа Пром'")
        alfa = cur.fetchone()['id']
        db.upsert_company_debt_snapshot(alfa, {
            'report_date': date.today() - timedelta(days=30), 'source': 's', 'source_ref': 'r',
            'net_debt_rub': Decimal('300'), 'ebitda_rub': Decimal('100'),
            'coverage_status': 'full', 'quality_status': 'verified'})
        overlay = build_debt_overlay(db, date.today())
        by_name = {i['name']: i for i in overlay['issuers']}
        assert by_name['Альфа Пром']['net_debt_to_ebitda'] == 3.0
        assert by_name['Альфа Пром']['freshness']['status'] == 'fresh'
        assert by_name['Бета Финанс']['freshness']['status'] == 'missing'
        # Альфа 10000 из 15000 портфеля
        assert overlay['coverage']['portfolio_share_with_fresh_debt_data_pct'] == pytest.approx(66.67, abs=0.01)


# ---------------------------------------------------------------------------
# CLI end-to-end (main.py subprocess на тестовой БД)
# ---------------------------------------------------------------------------

def test_cli_import_then_report_json(db, tmp_path):
    cid = _company(db, 'Альфа Пром', '7700000001')
    _insert_bond(db, isin='RU000TEST001', company_id=cid, issue_size=1000, nominal=Decimal('1000'))
    db.conn.commit()  # видимость в subprocess-CLI (отдельное соединение)
    csv_path = tmp_path / 'debt.csv'
    csv_path.write_text(
        'company_inn,source,report_date,source_ref,total_debt_rub,cash_rub\n'
        '7700000001,test-src,2026-06-30,ref-1,100,30\n', encoding='utf-8')
    env = {**os.environ, 'PYTHONPATH': '.', 'POSTGRES_DSN': os.environ['POSTGRES_DSN_TEST']}

    def run(*cmd):
        res = subprocess.run([sys.executable, 'main.py', *cmd], capture_output=True, text=True, env=env)
        assert res.returncode == 0, res.stderr
        return json.loads(res.stdout)  # stdout — чистый JSON

    assert run('import-company-debt', '--file', str(csv_path))['inserted'] == 1
    report = run('company-debt-report', '--format', 'json')
    assert report['schema_version'] == 1
    row = report['companies'][0]
    assert row['name'] == 'Альфа Пром' and row['net_debt_rub'] == 70.0
    assert row['issued_bond_volume_rub'] == 1000000.0
