"""
Тесты rebalance-report — ядра Python-тулкита ребалансировки (задача 005.1).

Покрывают:
- golden-схему JSON-ответа (ключи и типы всех блоков, schema_version);
- фильтры скринера: --freq-max/--freq-in (P-C), --exclude-sovereign (P-G),
  --include-held с бейджем held (анти-P2 из 002), КУ-бумаги по флагу;
- имена/эмитенты из каталога в каждой строке (анти-P-D);
- концентрации (эмитенты/риск/частота, нарушения >15%);
- календарь погашений/оферт redemptions_6m (пул реинвеста);
- CSV-артефакт (UTF-8 BOM, русские заголовки, строка ИТОГО);
- запуск одной CLI-командой без env/psql (анти-P-A, subprocess-тест).

Все данные — синтетические фикстуры на тестовой БД (POSTGRES_DSN_TEST),
реальные составы портфеля не используются.
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.use_cases.rebalance_report import (
    BOND_ROW_KEYS,
    CSV_HEADERS,
    ISSUER_CONCENTRATION_LIMIT_PCT,
    RebalanceReportUseCase,
    SCHEMA_VERSION,
    _add_months,
    _parse_freq_in,
)


# ---------------------------------------------------------------------------
# Фикстуры (синтетические данные)
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc)


def _insert_company(db, name: str, entity_type: str = 'company') -> int:
    cur = db._cursor()
    cur.execute(
        "INSERT INTO companies (name, entity_type) VALUES (%s, %s) RETURNING id",
        (name, entity_type),
    )
    return cur.fetchone()['id']


def _insert_bond(db, **kwargs):
    """Облигация каталога с дефолтами, видимыми buy-выборкой движка."""
    cur = db._cursor()
    defaults = {
        'isin': 'RU000A0TEST1',
        'ticker': 'TEST1',
        'name': 'Тест Бонд 1',
        'currency': 'rub',
        'nominal': Decimal('1000'),
        'maturity_date': date.today() + timedelta(days=730),
        'coupon_quantity_per_year': 12,
        'coupon_rate_percent': Decimal('20.0'),
        'risk_level': 1,
        'list_level': 1,
        'is_trade_available': True,
        'is_for_qualified_investors': False,
        'issue_size': 1000000,
        'market_price': Decimal('950.0'),
        'market_price_updated_at': _NOW,
        'floating_coupon_flag': False,
        'perpetual_flag': False,
        'amortization_flag': False,
        'company_id': None,
        'ytm': None,
        'ytm_updated_at': None,
    }
    defaults.update(kwargs)
    fields = list(defaults.keys())
    placeholders = ', '.join(['%s'] * len(fields))
    col_names = ', '.join(fields)
    cur.execute(
        f"INSERT INTO bonds_catalog ({col_names}) VALUES ({placeholders})",
        [defaults[f] for f in fields],
    )


def _insert_position(db, isin: str, quantity: float, price: float,
                     broker: str = 'TBank', account: str = 't1'):
    """Позиция портфеля; current_value задаётся явно (каноническая оценка view)."""
    cur = db._cursor()
    cur.execute(
        "INSERT INTO portfolio_positions (isin, ticker, name, quantity, average_price, "
        "current_price, current_value, broker_name, account_id, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (isin, 'TICK', 'Позиция', quantity, price, price, quantity * price,
         broker, account, _NOW),
    )


def _make_args(**overrides) -> argparse.Namespace:
    """Аргументы команды по умолчанию (как из CLI)."""
    args = argparse.Namespace(
        format='json',
        freq_max=None,
        freq_in=None,
        exclude_sovereign=False,
        include_held=False,
        include_ku=False,
        min_maturity=None,
        max_maturity=None,
        max_risk=1,
        max_listlevel=2,
        max_ytm=35.0,
        screener_limit=10,
        redemptions_months=6,
        csv_out=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _build_report(db, **overrides) -> dict:
    use_case = RebalanceReportUseCase(db=db)
    return use_case.build_report(_make_args(**overrides))


# ---------------------------------------------------------------------------
# Парсер CLI
# ---------------------------------------------------------------------------

class TestParser:
    def _parse(self, argv):
        parser = argparse.ArgumentParser()
        RebalanceReportUseCase.setup_parser(parser)
        return parser.parse_args(argv)

    def test_defaults(self):
        args = self._parse([])
        assert args.format == 'json'
        assert args.freq_max is None
        assert args.freq_in is None
        assert args.exclude_sovereign is False
        assert args.include_held is False
        assert args.include_ku is False
        assert args.min_maturity is None
        assert args.max_maturity is None
        assert args.max_risk == 1
        assert args.max_listlevel == 2
        assert args.max_ytm == 35.0
        assert args.screener_limit == 10
        assert args.redemptions_months == 6
        assert args.csv_out is None

    def test_freq_in_parses_list(self):
        assert _parse_freq_in('2,4') == [2, 4]
        assert _parse_freq_in('12') == [12]
        assert self._parse(['--freq-in', '2,4']).freq_in == [2, 4]

    def test_freq_flags(self):
        args = self._parse(['--freq-max', '4', '--exclude-sovereign', '--include-held',
                            '--min-maturity', '2026-12-01', '--max-risk', '1',
                            '--csv-out', '/tmp/x.csv'])
        assert args.freq_max == 4
        assert args.exclude_sovereign is True
        assert args.include_held is True
        assert args.min_maturity == '2026-12-01'
        assert args.csv_out == '/tmp/x.csv'

    def test_freq_max_and_freq_in_conflict_detected_at_validation(self):
        # Флаги парсятся; конфликт отлавливает _validate_args (ValueError ->
        # main.py завершает процесс с кодом 1 и понятной ошибкой в stderr).
        args = self._parse(['--freq-max', '4', '--freq-in', '2,4'])
        use_case = RebalanceReportUseCase.__new__(RebalanceReportUseCase)
        with pytest.raises(ValueError):
            use_case._validate_args(args)

    def test_freq_in_bad_value_rejected(self):
        with pytest.raises(SystemExit):
            self._parse(['--freq-in', '2,квартал'])

    def test_bad_maturity_date_rejected(self):
        with pytest.raises(SystemExit):
            self._parse(['--min-maturity', '01.12.2026'])

    def test_validate_args_rejects_zero(self):
        use_case = RebalanceReportUseCase.__new__(RebalanceReportUseCase)
        with pytest.raises(ValueError):
            use_case._validate_args(_make_args(freq_max=0))
        with pytest.raises(ValueError):
            use_case._validate_args(_make_args(redemptions_months=0))
        # взаимоисключающие фильтры частоты в Namespace (мимо argparse)
        with pytest.raises(ValueError):
            use_case._validate_args(_make_args(freq_max=4, freq_in=[2, 4]))


# ---------------------------------------------------------------------------
# Golden-схема JSON
# ---------------------------------------------------------------------------

_ISO_DATE_RE = r'^\d{4}-\d{2}-\d{2}$'


def _seed_golden(db):
    """Минимальный портфель (2 эмитента, 2 позиции) + суверен и флоатер в каталоге."""
    alfa = _insert_company(db, 'Альфа Пром')
    beta = _insert_company(db, 'Бета Финанс')
    minfin = _insert_company(db, 'Минфин России', 'sovereign')

    # В портфеле: ежемесячный фикс (10000) и квартальный фикс (5000)
    _insert_bond(db, isin='RU000A0FIX01', ticker='FIX01', name='Альфа 001P-01',
                 company_id=alfa, coupon_quantity_per_year=12,
                 coupon_rate_percent=Decimal('20.0'),
                 ytm=Decimal('15'), ytm_updated_at=_NOW,
                 maturity_date=date.today() + timedelta(days=90))
    _insert_bond(db, isin='RU000A0FIX02', ticker='FIX02', name='Бета БО-02',
                 company_id=beta, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('15.0'),
                 ytm=Decimal('12'), ytm_updated_at=_NOW,
                 market_price=Decimal('500.0'))
    _insert_position(db, 'RU000A0FIX01', quantity=10, price=1000)
    _insert_position(db, 'RU000A0FIX02', quantity=10, price=500)

    # Вне портфеля: ОФЗ и флоатер — кандидаты скринера
    _insert_bond(db, isin='RU000A0OFZ01', ticker='ОФЗ 26251', name='ОФЗ 26251',
                 company_id=minfin, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('10.0'),
                 ytm=Decimal('10'), ytm_updated_at=_NOW)
    _insert_bond(db, isin='RU000A0FLTR1', ticker='FLTR1', name='Гамма Флоатер',
                 company_id=beta, floating_coupon_flag=True,
                 coupon_rate_percent=None, coupon_spread=Decimal('1.5'))
    cur = db._cursor()
    cur.execute(
        "INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value) "
        "VALUES ('RU000A0FLTR1', CURRENT_DATE, 'floater_coupon_calculator', '18.0')"
    )
    db.conn.commit()


class TestGoldenSchema:
    @pytest.fixture(autouse=True)
    def _setup(self, db):
        _seed_golden(db)
        self.report = _build_report(db)

    def test_top_level_keys_and_version(self):
        assert set(self.report.keys()) == {
            'schema_version', 'meta', 'portfolio', 'concentrations',
            'screener', 'redemptions_6m', 'artifacts',
        }
        assert self.report['schema_version'] == SCHEMA_VERSION == 2

    def test_meta_block(self):
        meta = self.report['meta']
        assert set(meta.keys()) == {
            'generated_at', 'catalog_updated_at', 'positions_updated_at',
            'market_prices_updated_at', 'status', 'gate_status', 'is_fresh',
            'criticals', 'warnings',
        }
        assert isinstance(meta['generated_at'], str)
        assert isinstance(meta['is_fresh'], bool)
        assert isinstance(meta['criticals'], list)
        assert isinstance(meta['warnings'], list)
        assert all(isinstance(item, str) for item in meta['criticals'] + meta['warnings'])
        assert meta['status'] in ('OK', 'WARNING', 'CRITICAL')
        # портфель/каталог только что вставлены, цены свежие -> гейт зелёный
        assert meta['is_fresh'] is True
        assert meta['status'] == 'OK'
        assert meta['gate_status'] == 'OK'
        assert meta['positions_updated_at'] is not None
        assert meta['catalog_updated_at'] is not None

    def test_portfolio_block(self):
        portfolio = self.report['portfolio']
        # 022: cash неизвестен (нет строк в portfolio_cash_balances) — NULL
        assert set(portfolio.keys()) == {
            'total_value', 'securities_value_rub', 'cash_available_rub',
            'investable_total_rub', 'cash_updated_at', 'positions',
        }
        # семантика total_value не меняется — стоимость облигаций (022)
        assert portfolio['total_value'] == 15000.0
        assert portfolio['securities_value_rub'] == portfolio['total_value']
        assert portfolio['cash_available_rub'] is None
        assert portfolio['investable_total_rub'] is None
        assert portfolio['cash_updated_at'] is None

        positions = portfolio['positions']
        assert len(positions) == 2
        for pos in positions:
            assert set(pos.keys()) == set(BOND_ROW_KEYS)
            assert isinstance(pos['isin'], str) and pos['isin']
            # анти-P-D: каждый ISIN снабжён именем из каталога
            assert isinstance(pos['name'], str) and pos['name']
            assert pos['ticker'] is None or isinstance(pos['ticker'], str)
            assert isinstance(pos['qty'], float)
            assert pos['price'] is None or isinstance(pos['price'], float)
            assert isinstance(pos['value'], float)
            assert pos['ytm_pct'] is None or isinstance(pos['ytm_pct'], float)
            assert pos['ytm_reason'] is None or isinstance(pos['ytm_reason'], str)
            assert pos['coupon_pct'] is None or isinstance(pos['coupon_pct'], float)
            assert pos['freq'] is None or isinstance(pos['freq'], int)
            assert pos['coupon_kind'] in ('fix', 'float')
            assert pos['risk_level'] is None or isinstance(pos['risk_level'], int)
            assert pos['list_level'] is None or isinstance(pos['list_level'], int)
            assert pos['maturity'] is None or (
                isinstance(pos['maturity'], str) and len(pos['maturity']) == 10
            )
            assert pos['offer'] is None or isinstance(pos['offer'], str)
            assert isinstance(pos['amort'], bool)
            assert isinstance(pos['ku'], bool)
            assert isinstance(pos['issuer'], str) and pos['issuer']
            assert pos['issuer_pct'] is None or isinstance(pos['issuer_pct'], float)
            assert pos['held_badge'] is None or isinstance(pos['held_badge'], str)

        by_isin = {pos['isin']: pos for pos in positions}
        # единицы YTM — проценты из единого движка (анти-P-E)
        assert by_isin['RU000A0FIX01']['ytm_pct'] == 15.0
        assert by_isin['RU000A0FIX02']['ytm_pct'] == 12.0
        assert by_isin['RU000A0FIX01']['coupon_kind'] == 'fix'
        assert by_isin['RU000A0FIX01']['freq'] == 12
        assert by_isin['RU000A0FIX02']['freq'] == 4
        assert by_isin['RU000A0FIX01']['issuer'] == 'Альфа Пром'
        # бейдж held с долей
        assert by_isin['RU000A0FIX01']['held_badge'] == 'held (66.67%)'
        assert by_isin['RU000A0FIX02']['held_badge'] == 'held (33.33%)'

    def test_concentrations_block(self):
        conc = self.report['concentrations']
        assert set(conc.keys()) == {'issuers', 'risk_mix', 'freq_mix'}

        issuers = conc['issuers']
        assert set(issuers.keys()) == {'limit_pct', 'top', 'violations'}
        assert issuers['limit_pct'] == ISSUER_CONCENTRATION_LIMIT_PCT == 15.0
        for item in issuers['top'] + issuers['violations']:
            assert set(item.keys()) == {'issuer', 'value', 'pct'}
            assert isinstance(item['issuer'], str)
            assert isinstance(item['value'], float)
            assert isinstance(item['pct'], float)
        top_names = [item['issuer'] for item in issuers['top']]
        assert top_names == ['Альфа Пром', 'Бета Финанс']
        # 66.67% и 33.33% — оба эмитента выше лимита 15%
        assert [item['issuer'] for item in issuers['violations']] == ['Альфа Пром', 'Бета Финанс']
        assert issuers['violations'][0]['pct'] == 66.67

        for mix in (conc['risk_mix'], conc['freq_mix']):
            assert sum(item['pct'] for item in mix) == pytest.approx(100.0)
        assert conc['risk_mix'] == [{'risk_level': 1, 'value': 15000.0, 'pct': 100.0}]
        assert {item['freq']: item['pct'] for item in conc['freq_mix']} == {12: 66.67, 4: 33.33}

    def test_screener_block(self):
        screener = self.report['screener']
        assert set(screener.keys()) == {
            'filters', 'candidates_total', 'excluded', 'fixed_matched',
            'floater_matched', 'fixed', 'floater', 'issuers',
        }
        filters = screener['filters']
        assert set(filters.keys()) == {
            'freq_max', 'freq_in', 'exclude_sovereign', 'include_held',
            'include_ku', 'max_risk', 'max_listlevel', 'max_ytm_pct',
            'min_maturity', 'max_maturity', 'screener_limit',
        }
        assert filters['max_ytm_pct'] == 35.0

        excluded = screener['excluded']
        assert set(excluded.keys()) == {
            'held', 'ku', 'sovereign', 'freq', 'risk', 'listlevel',
            'fixed_ytm_missing', 'fixed_max_ytm',
            'floater_coupon_missing', 'floater_max_ytm',
        }
        assert all(isinstance(v, int) for v in excluded.values())
        # арифметика воронки: кандидаты = отсечённые + прошедшие (нет усечения limit)
        assert screener['candidates_total'] == (
            sum(excluded.values()) + screener['fixed_matched'] + screener['floater_matched']
        )
        # КУ нет в каталоге -> ни одной отсечённой по КУ; обе позиции портфеля
        # отсечены как held (по умолчанию держимое в скринере исключено)
        assert excluded['ku'] == 0
        assert excluded['held'] == 2

        for row in screener['fixed'] + screener['floater']:
            assert set(row.keys()) == set(BOND_ROW_KEYS)
            assert isinstance(row['name'], str) and row['name']
            assert isinstance(row['issuer'], str) and row['issuer']

        fixed_isins = [row['isin'] for row in screener['fixed']]
        # из кандидатов остался только ОФЗ (портфельные фиксы — held)
        assert fixed_isins == ['RU000A0OFZ01']
        floater_isins = [row['isin'] for row in screener['floater']]
        assert floater_isins == ['RU000A0FLTR1']
        # YTM флоатера по конвенции проекта не определена, купон есть
        floater_row = screener['floater'][0]
        assert floater_row['ytm_pct'] is None
        assert floater_row['coupon_pct'] == 18.0
        assert floater_row['coupon_kind'] == 'float'

        # агрегат «эмитент -> серии» для явного выбора выпуска (анти-P-D)
        agg = screener['issuers']
        assert agg[0]['issuer'] == 'Минфин России'
        assert agg[0]['best_ytm_pct'] == 10.0
        assert [item['issuer'] for item in agg] == ['Минфин России', 'Бета Финанс']
        for item in agg:
            assert set(item.keys()) == {'issuer', 'best_ytm_pct', 'series'}
            for series in item['series']:
                assert set(series.keys()) == {
                    'isin', 'name', 'ticker', 'coupon_kind', 'coupon_pct',
                    'ytm_pct', 'freq', 'maturity', 'offer', 'risk_level', 'list_level',
                }

    def test_redemptions_block(self):
        red = self.report['redemptions_6m']
        assert set(red.keys()) == {'period_months', 'from', 'to', 'total', 'events'}
        assert red['period_months'] == 6
        assert isinstance(red['total'], float)
        for event in red['events']:
            assert set(event.keys()) == {
                'isin', 'name', 'ticker', 'issuer', 'type', 'date', 'qty', 'value',
            }
            assert event['type'] in ('maturity', 'offer')
            assert isinstance(event['date'], str)
            assert isinstance(event['value'], float)
        # RU000A0FIX01 погашается через 90 дней — в окне 6 месяцев
        assert [e['isin'] for e in red['events']] == ['RU000A0FIX01']
        assert red['events'][0]['type'] == 'maturity'
        assert red['events'][0]['value'] == 10000.0
        assert red['total'] == 10000.0

    def test_artifacts_block(self):
        artifacts = self.report['artifacts']
        assert set(artifacts.keys()) == {'target_portfolio_csv'}
        assert artifacts['target_portfolio_csv'] is None

    def test_execute_prints_valid_json(self, db, capsys):
        use_case = RebalanceReportUseCase(db=db)
        use_case.execute(_make_args())
        data = json.loads(capsys.readouterr().out)
        assert data['schema_version'] == 2
        assert set(data.keys()) == {
            'schema_version', 'meta', 'portfolio', 'concentrations',
            'screener', 'redemptions_6m', 'artifacts',
        }


# ---------------------------------------------------------------------------
# Фильтры скринера (P-C / P-G / анти-P2 / КУ)
# ---------------------------------------------------------------------------

def _seed_filters(db):
    """Каталог для фильтров: ОФЗ, евро-РФ, фиксы 2/4/12, КУ, флоатер, held."""
    minfin = _insert_company(db, 'Минфин России', 'sovereign')
    russia = _insert_company(db, 'Россия', 'sovereign')
    corp = _insert_company(db, 'Дельта Холдинг')

    _insert_bond(db, isin='RU000A0OFZ262', ticker='ОФЗ 26251', name='ОФЗ 26251',
                 company_id=minfin, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('10.0'),
                 ytm=Decimal('10'), ytm_updated_at=_NOW)
    _insert_bond(db, isin='RU000A0RUS27', ticker='Russia-2027', name='Russia 2027',
                 company_id=russia, coupon_quantity_per_year=2,
                 coupon_rate_percent=Decimal('10.5'),
                 ytm=Decimal('10.5'), ytm_updated_at=_NOW)
    _insert_bond(db, isin='RU000A0FIX02', ticker='DL02', name='Дельта 001P-02',
                 company_id=corp, coupon_quantity_per_year=2,
                 coupon_rate_percent=Decimal('17.0'),
                 ytm=Decimal('17'), ytm_updated_at=_NOW)
    _insert_bond(db, isin='RU000A0FIX04', ticker='DL04', name='Дельта БО-04',
                 company_id=corp, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('16.0'),
                 ytm=Decimal('16'), ytm_updated_at=_NOW)
    _insert_bond(db, isin='RU000A0FIX12', ticker='DL12', name='Дельта 01',
                 company_id=corp, coupon_quantity_per_year=12,
                 coupon_rate_percent=Decimal('20.0'),
                 ytm=Decimal('20'), ytm_updated_at=_NOW)
    _insert_bond(db, isin='RU000A0KU001', ticker='KU001', name='Квал Бонд',
                 company_id=corp, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('30.0'),
                 ytm=Decimal('30'), ytm_updated_at=_NOW,
                 is_for_qualified_investors=True)
    _insert_bond(db, isin='RU000A0FLTR2', ticker='FLT2', name='Дельта Флоатер',
                 company_id=corp, coupon_quantity_per_year=12,
                 floating_coupon_flag=True, coupon_rate_percent=None,
                 coupon_spread=Decimal('1.5'))
    cur = db._cursor()
    cur.execute(
        "INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value) "
        "VALUES ('RU000A0FLTR2', CURRENT_DATE, 'floater_coupon_calculator', '18.0')"
    )
    # held-бумага: квартальный фикс уже в портфеле
    _insert_bond(db, isin='RU000A0HELD1', ticker='HELD1', name='Дельта 002P-01',
                 company_id=corp, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('15.0'),
                 ytm=Decimal('15'), ytm_updated_at=_NOW,
                 market_price=Decimal('1000.0'))
    _insert_position(db, 'RU000A0HELD1', quantity=5, price=1000)
    db.conn.commit()


@pytest.fixture
def filters_env(db):
    _seed_filters(db)
    return db


class TestScreenerFilters:
    def test_default_excludes_held_and_ku(self, filters_env):
        report = _build_report(filters_env)
        screener = report['screener']
        all_isins = [r['isin'] for r in screener['fixed'] + screener['floater']]
        assert 'RU000A0HELD1' not in all_isins  # анти-P2: держимое исключено
        assert 'RU000A0KU001' not in all_isins  # КУ исключены по умолчанию
        assert screener['excluded']['held'] == 1
        assert screener['excluded']['ku'] == 1
        # суверены без флага видны
        assert 'RU000A0OFZ262' in [r['isin'] for r in screener['fixed']]

    def test_exclude_sovereign_removes_ofz_and_russia(self, filters_env):
        report = _build_report(filters_env, exclude_sovereign=True)
        screener = report['screener']
        all_rows = screener['fixed'] + screener['floater']
        all_isins = [r['isin'] for r in all_rows]
        assert 'RU000A0OFZ262' not in all_isins  # ОФЗ
        assert 'RU000A0RUS27' not in all_isins   # евро-РФ
        assert screener['excluded']['sovereign'] == 2
        # имена эмитентов в остальных строках — не суверенные
        assert all('Минфин' not in r['issuer'] and r['issuer'] != 'Россия'
                   for r in all_rows)

    def test_freq_max(self, filters_env):
        report = _build_report(filters_env, exclude_sovereign=True, freq_max=4)
        screener = report['screener']
        assert screener['filters']['freq_max'] == 4
        fixed_freqs = {r['isin']: r['freq'] for r in screener['fixed']}
        assert set(fixed_freqs.values()) <= {1, 2, 3, 4}
        assert 'RU000A0FIX12' not in fixed_freqs  # ежемесячный отсечён
        # ежемесячный фикс + ежемесячный флоатер
        assert screener['excluded']['freq'] == 2
        assert screener['floater'] == []
        assert screener['candidates_total'] == (
            sum(screener['excluded'].values())
            + screener['fixed_matched'] + screener['floater_matched']
        )

    def test_freq_in(self, filters_env):
        report = _build_report(filters_env, exclude_sovereign=True, freq_in=[2, 4])
        screener = report['screener']
        assert screener['filters']['freq_in'] == [2, 4]
        fixed = {r['isin']: r['freq'] for r in screener['fixed']}
        assert 'RU000A0FIX12' not in fixed
        assert 'RU000A0FIX02' in fixed and 'RU000A0FIX04' in fixed
        # флоатер с freq=12 тоже отсечён
        assert screener['floater'] == []
        assert screener['excluded']['freq'] == 2

    def test_include_held_gives_badge(self, filters_env):
        report = _build_report(filters_env, include_held=True)
        screener = report['screener']
        held_rows = [r for r in screener['fixed'] if r['isin'] == 'RU000A0HELD1']
        assert len(held_rows) == 1
        badge = held_rows[0]['held_badge']
        assert isinstance(badge, str)
        assert badge.startswith('held (')
        assert badge.endswith('%)')
        # доля в портфеле 100% (единственная позиция фикстуры)
        assert held_rows[0]['held_badge'] == 'held (100.00%)'
        # в скринере по умолчанию — исключена
        default_report = _build_report(filters_env)
        assert 'RU000A0HELD1' not in [r['isin'] for r in default_report['screener']['fixed']]

    def test_include_ku(self, filters_env):
        report = _build_report(filters_env, include_ku=True)
        assert 'RU000A0KU001' in [r['isin'] for r in report['screener']['fixed']]
        assert report['screener']['excluded']['ku'] == 0

    def test_max_ytm_cuts_anomalies(self, filters_env):
        report = _build_report(filters_env, include_ku=True, max_ytm=25.0)
        all_ytms = [r['ytm_pct'] for r in
                    report['screener']['fixed'] + report['screener']['floater']
                    if r['ytm_pct'] is not None]
        assert all(y <= 25.0 for y in all_ytms)
        assert report['screener']['excluded']['fixed_max_ytm'] == 1  # Квал Бонд 30%

    def test_all_rows_carry_catalog_names(self, filters_env):
        """Анти-P-D: у каждой строки ответа есть имя и тикер из каталога."""
        report = _build_report(filters_env, include_held=True, include_ku=True)
        rows = (
            report['portfolio']['positions']
            + report['screener']['fixed'] + report['screener']['floater']
        )
        catalog_names = {
            'RU000A0OFZ262': 'ОФЗ 26251',
            'RU000A0RUS27': 'Russia 2027',
            'RU000A0FIX02': 'Дельта 001P-02',
        }
        for row in rows:
            assert row['name'] == catalog_names.get(row['isin'], row['name'])
            assert row['name']
            assert row['issuer']


# ---------------------------------------------------------------------------
# 018: buy-кандидат — только реальная свежая market price
# ---------------------------------------------------------------------------

class TestBuyPriceGuard:
    """018: canonical screener не отдаёт кандидата без реальной свежей цены.

    Гвард живёт в buy-выборке хранилища (get_bonds_yield_table mode='buy'):
    market_price IS NOT NULL AND > 0 и не старше 24 часов; nominal цену
    кандидата больше не подменяет."""

    def _seed_candidate(self, db, isin: str, **overrides):
        corp = _insert_company(db, 'Прайс Гард Корп')
        defaults = dict(
            company_id=corp,
            coupon_quantity_per_year=4,
            coupon_rate_percent=Decimal('15.0'),
            ytm=Decimal('15'), ytm_updated_at=_NOW,
        )
        defaults.update(overrides)
        _insert_bond(db, isin=isin, ticker=isin[-5:], name=f'Прайс Гард {isin[-5:]}',
                     **defaults)
        db.conn.commit()

    def test_fresh_positive_market_price_passes(self, db):
        self._seed_candidate(db, 'RU000A0PRICE1', market_price=Decimal('950.0'))
        report = _build_report(db)
        screener = report['screener']
        assert [r['isin'] for r in screener['fixed']] == ['RU000A0PRICE1']
        # цена кандидата — реальная market_price, арифметика воронки сходится
        assert screener['fixed'][0]['price'] == 950.0
        assert screener['candidates_total'] == (
            sum(screener['excluded'].values())
            + screener['fixed_matched'] + screener['floater_matched']
        )

    def test_price_fresh_within_24h_window_passes(self, db):
        self._seed_candidate(db, 'RU000A0PRICE2', market_price=Decimal('980.0'),
                             market_price_updated_at=_NOW - timedelta(hours=2))
        report = _build_report(db)
        assert [r['isin'] for r in report['screener']['fixed']] == ['RU000A0PRICE2']

    def test_null_market_price_excluded(self, db):
        self._seed_candidate(db, 'RU000A0PRICEN', market_price=None)
        report = _build_report(db)
        assert report['screener']['candidates_total'] == 0
        assert report['screener']['fixed'] == []
        assert report['screener']['floater'] == []

    def test_zero_and_negative_market_price_excluded(self, db):
        self._seed_candidate(db, 'RU000A0PRICEZ', market_price=Decimal('0'))
        self._seed_candidate(db, 'RU000A0PRICEM', market_price=Decimal('-100'))
        report = _build_report(db)
        assert report['screener']['candidates_total'] == 0
        assert report['screener']['fixed'] == []

    def test_stale_market_price_excluded(self, db):
        self._seed_candidate(db, 'RU000A0PRICEO', market_price=Decimal('950.0'),
                             market_price_updated_at=_NOW - timedelta(hours=25))
        report = _build_report(db)
        assert report['screener']['candidates_total'] == 0
        assert report['screener']['fixed'] == []

    def test_nominal_not_used_as_candidate_price(self, db):
        """Бумага без market_price не попадает в buy-выборку: номинал (1000)
        не подменяет цену ни в SQL-строке, ни в JSON скринера."""
        self._seed_candidate(db, 'RU000A0PRICEX', market_price=None)
        rows = db.get_bonds_yield_table(
            mode='buy', min_maturity=None, max_maturity=None, max_risk=5,
            limit=10, include_held=True,
        )
        assert all(r['isin'] != 'RU000A0PRICEX' for r in rows)
        assert all(r['current_price'] != Decimal('1000') for r in rows)
        report = _build_report(db)
        assert report['screener']['fixed'] == []


# ---------------------------------------------------------------------------
# Концентрации и портфельные кейсы (дистресс-хвост)
# ---------------------------------------------------------------------------

class TestPortfolioCases:
    def test_zombie_matured_position_shown_with_zero_value(self, db):
        """Погашенная бумага с нулевой оценкой видна в портфеле с value=0
        (дистресс-хвост сессии), гейт даёт WARN_ZOMBIE_ROWS, а не CRITICAL."""
        corp = _insert_company(db, 'Зомби Групп')
        # свежий каталог и живая позиция — чтобы гейт был зелёным по ценам
        _insert_bond(db, isin='RU000A0ALIVE', ticker='ALIVE', name='Живая Бумага',
                     company_id=corp, ytm=Decimal('12'), ytm_updated_at=_NOW)
        # погашенная бумага (maturity в прошлом) с zero-строкой
        _insert_bond(db, isin='RU000A0DEAD1', ticker='DEAD1', name='Погашенная 1P-02',
                     company_id=corp, ytm=Decimal('10'), ytm_updated_at=_NOW,
                     maturity_date=date.today() - timedelta(days=30),
                     market_price=Decimal('0'))
        _insert_position(db, 'RU000A0ALIVE', quantity=10, price=1000)
        _insert_position(db, 'RU000A0DEAD1', quantity=35, price=0, )
        db.conn.commit()

        report = _build_report(db)
        meta = report['meta']
        assert meta['gate_status'] == 'WARN_ZOMBIE_ROWS'
        assert meta['status'] == 'WARNING'
        assert meta['is_fresh'] is True

        positions = {p['isin']: p for p in report['portfolio']['positions']}
        assert 'RU000A0DEAD1' in positions          # зомби не спрятан
        assert positions['RU000A0DEAD1']['value'] == 0.0
        assert positions['RU000A0DEAD1']['price'] == 0.0
        assert positions['RU000A0DEAD1']['held_badge'] == 'held (0.00%)'
        # живая позиция: 10000 из 10000 — доля 100%
        assert positions['RU000A0ALIVE']['value'] == 10000.0

    def test_multi_account_positions_aggregated_by_isin(self, db):
        """ISIN на двух счетах не задаваивает строки портфеля (кейс 002.2)."""
        corp = _insert_company(db, 'Двойной Счёт')
        _insert_bond(db, isin='RU000A0TWOAC', ticker='TWOAC', name='Две Строки',
                     company_id=corp, ytm=Decimal('13'), ytm_updated_at=_NOW)
        _insert_position(db, 'RU000A0TWOAC', quantity=5, price=1000, account='a1')
        _insert_position(db, 'RU000A0TWOAC', quantity=2, price=1000, account='a2')
        db.conn.commit()

        report = _build_report(db)
        positions = report['portfolio']['positions']
        assert len(positions) == 1
        assert positions[0]['qty'] == 7.0
        assert positions[0]['value'] == 7000.0


# ---------------------------------------------------------------------------
# redemptions_6m: календарь погашений/оферт (пул реинвеста)
# ---------------------------------------------------------------------------

class TestRedemptions:
    def _seed(self, db):
        corp = _insert_company(db, 'Реинвест Групп')
        # погашение через ~3 месяца — в окне
        _insert_bond(db, isin='RU000A0RED01', ticker='RED01', name='Дарс Выпуск',
                     company_id=corp,
                     maturity_date=date.today() + timedelta(days=92),
                     ytm=Decimal('12'), ytm_updated_at=_NOW)
        # оферта через ~2 месяца, погашение далеко — в окне
        _insert_bond(db, isin='RU000A0RED02', ticker='RED02', name='ПСБ Лизинг',
                     company_id=corp,
                     maturity_date=date.today() + timedelta(days=800),
                     offer_date=date.today() + timedelta(days=60),
                     ytm=Decimal('13'), ytm_updated_at=_NOW)
        # погашение через 8 месяцев — вне окна 6 месяцев
        _insert_bond(db, isin='RU000A0RED03', ticker='RED03', name='Дальний Выпуск',
                     company_id=corp,
                     maturity_date=date.today() + timedelta(days=240),
                     ytm=Decimal('11'), ytm_updated_at=_NOW)
        _insert_position(db, 'RU000A0RED01', quantity=100, price=100)   # 10000
        _insert_position(db, 'RU000A0RED02', quantity=50, price=100)    # 5000
        _insert_position(db, 'RU000A0RED03', quantity=10, price=100)    # 1000
        db.conn.commit()

    def test_window_and_totals(self, db):
        self._seed(db)
        report = _build_report(db)
        red = report['redemptions_6m']
        assert red['period_months'] == 6
        assert red['from'] == date.today().isoformat()
        assert red['to'] == _add_months(date.today(), 6).isoformat()

        events = {e['isin']: e for e in red['events']}
        assert set(events.keys()) == {'RU000A0RED01', 'RU000A0RED02'}
        assert events['RU000A0RED01']['type'] == 'maturity'
        assert events['RU000A0RED02']['type'] == 'offer'
        assert events['RU000A0RED01']['value'] == 10000.0
        assert events['RU000A0RED02']['value'] == 5000.0
        assert red['total'] == 15000.0

    def test_wider_window_includes_far_bond(self, db):
        self._seed(db)
        report = _build_report(db, redemptions_months=12)
        events = {e['isin'] for e in report['redemptions_6m']['events']}
        assert events == {'RU000A0RED01', 'RU000A0RED02', 'RU000A0RED03'}
        assert report['redemptions_6m']['total'] == 16000.0

    def test_maturity_and_offer_both_in_window_single_event(self, db):
        """Обе даты в окне -> одно событие по ближайшей (без задвоения суммы)."""
        corp = _insert_company(db, 'Оферта Плюс')
        _insert_bond(db, isin='RU000A0BOTH1', ticker='BOTH1', name='Обе Даты',
                     company_id=corp,
                     maturity_date=date.today() + timedelta(days=150),
                     offer_date=date.today() + timedelta(days=100),
                     ytm=Decimal('12'), ytm_updated_at=_NOW)
        _insert_position(db, 'RU000A0BOTH1', quantity=10, price=900)
        db.conn.commit()

        report = _build_report(db)
        events = report['redemptions_6m']['events']
        assert len(events) == 1
        assert events[0]['type'] == 'offer'
        assert events[0]['date'] == (date.today() + timedelta(days=100)).isoformat()
        assert report['redemptions_6m']['total'] == 9000.0


# ---------------------------------------------------------------------------
# CSV-артефакт
# ---------------------------------------------------------------------------

class TestCsvArtifact:
    def test_csv_bom_headers_and_total_row(self, db, tmp_path):
        _seed_golden(db)
        csv_path = tmp_path / 'target.csv'
        report = _build_report(db, csv_out=str(csv_path))

        assert report['artifacts']['target_portfolio_csv'] == str(csv_path)
        raw = csv_path.read_bytes()
        assert raw.startswith(b'\xef\xbb\xbf')  # UTF-8 BOM

        text = raw.decode('utf-8-sig')
        lines = text.strip().splitlines()
        # заголовок содержит запятую («Стоимость, ₽») — читаем csv.reader'ом
        parsed = list(csv.reader(lines))
        assert parsed[0] == CSV_HEADERS
        assert 'Стоимость, ₽' in parsed[0]
        # строка ИТОГО с суммой портфеля и долей 100%
        total_rows = [row for row in parsed if row and row[0] == 'ИТОГО']
        assert len(total_rows) == 1
        assert '15000.00' in total_rows[0]
        assert '100.00' in total_rows[0]
        # каждая позиция — отдельная строка с ISIN
        assert 'RU000A0FIX01' in text
        assert 'RU000A0FIX02' in text

    def test_csv_created_in_nested_dir(self, db, tmp_path):
        _seed_golden(db)
        csv_path = tmp_path / 'a' / 'b' / 'target.csv'
        report = _build_report(db, csv_out=str(csv_path))
        assert csv_path.is_file()
        assert report['artifacts']['target_portfolio_csv'] == str(csv_path)


# ---------------------------------------------------------------------------
# Анти-P-A: одна команда без env/psql (CLI subprocess)
# ---------------------------------------------------------------------------

def test_cli_single_command_on_test_db(db, tmp_path):
    """`python main.py rebalance-report --format json` работает одной строкой:
    rc == 0, stdout — чистый валидный JSON с schema_version.

    Фикстура db нужна для чистой тестовой БД (TRUNCATE перед тестом)."""
    python_bin = sys.executable
    env = os.environ.copy()
    env['PYTHONPATH'] = '.'
    test_dsn = env.get('POSTGRES_DSN_TEST')
    assert test_dsn, "POSTGRES_DSN_TEST не задан — CLI-тест не может идти на тестовую БД"
    # DSN только через окружение процесса (load_dotenv не перекрывает его);
    # в командной строке никакого POSTGRES_DSN нет — анти-P-A.
    env['POSTGRES_DSN'] = test_dsn

    res = subprocess.run(
        [python_bin, 'main.py', 'rebalance-report', '--format', 'json'],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, f"Stderr: {res.stderr}"
    data = json.loads(res.stdout)  # stdout — чистый JSON (логи в stderr)
    assert data['schema_version'] == 2
    # пустая тестовая БД: гейт честно кричит, JSON всё равно валиден;
    # cash unknown — NULL, а не 0 (022)
    assert data['meta']['gate_status'] == 'EMPTY_PORTFOLIO'
    assert data['portfolio']['positions'] == []
    assert data['portfolio']['cash_available_rub'] is None
    assert data['portfolio']['investable_total_rub'] is None


# ---------------------------------------------------------------------------
# Программный API отчёта (019)
# ---------------------------------------------------------------------------

class TestProgrammaticApi:
    def test_default_args_match_cli_defaults(self):
        """default_args() — те же defaults, что у CLI-парсера: единственный
        источник значений, exporter не дублирует их."""
        args = RebalanceReportUseCase.default_args()
        parser = argparse.ArgumentParser()
        RebalanceReportUseCase.setup_parser(parser)
        assert vars(args) == vars(parser.parse_args([]))
        # и это канонические значения контракта (см. TestParser.test_defaults)
        assert args.include_held is False
        assert args.screener_limit == 10
        assert args.max_ytm == 35.0

    def test_programmatic_report_equals_cli_json(self, db, capsys):
        """build_report() возвращает тот же dict, что execute() печатает JSON:
        CLI и программный вызов идут через один API."""
        _seed_golden(db)
        use_case = RebalanceReportUseCase(db=db)

        use_case.execute(RebalanceReportUseCase.default_args())
        cli_report = json.loads(capsys.readouterr().out)
        programmatic_report = use_case.build_report(
            RebalanceReportUseCase.default_args()
        )

        # generated_at — время конкретного вызова; остальное идентично
        del cli_report['meta']['generated_at']
        del programmatic_report['meta']['generated_at']
        assert programmatic_report == cli_report


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def test_add_months_calendar_semantics():
    assert _add_months(date(2026, 9, 29), 6) == date(2027, 3, 29)
    assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert _add_months(date(2026, 12, 15), 2) == date(2027, 2, 15)
