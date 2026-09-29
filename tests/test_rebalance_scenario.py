"""
Тесты сценарного режима rebalance-report --scenario (задача 005.2).

Перенос логики scenario_summary.sql (артефакт A2 из 002.4) и simulate_swap.sql
(001) в Python: trades (isin, qty_delta) -> целевой портфель, дельты купонного
потока ₽/мес и ₽/год, лимиты эмитента 15% по каждой ноге и по сумме всех ног
(анти-P6), частотный микс «до/после», CSV целевого портфеля после сделок,
явные ошибки сценария (unknown ISIN, отрицательный остаток, NO_COMPANY_LINK).

Все данные — синтетические фикстуры на тестовой БД (POSTGRES_DSN_TEST);
реальные составы портфеля и ISIN не используются (AGENTS.md).
"""
import csv
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.use_cases.rebalance_report import (
    BOND_ROW_KEYS,
    SCENARIO_ERROR_NEGATIVE_QTY_AFTER,
    SCENARIO_ERROR_NO_COMPANY_LINK,
    SCENARIO_ERROR_UNKNOWN_ISIN,
    SCENARIO_LEG_KEYS,
    SCENARIO_POSITION_KEYS,
    SCENARIO_SUMMARY_KEYS,
    SCENARIO_TRADE_KEYS,
    RebalanceReportUseCase,
)
from test_rebalance_report import (
    _NOW,
    _insert_bond,
    _insert_company,
    _insert_position,
    _make_args,
)


# ---------------------------------------------------------------------------
# Фикстуры (синтетические данные)
# ---------------------------------------------------------------------------

AL1 = 'RU000A0SCAL1'   # Альфа 001P-01: держим 10x1000, купон 20% ежемесячно
BT2 = 'RU000A0SCBT2'   # Бета БО-02: держим 10x500, купон 15% ежеквартально
GM1 = 'RU000A0SCGM1'   # Гамма БО-01: покупка, цена 900, купон 18% кварт.
GM2 = 'RU000A0SCGM2'   # Гамма БО-02: покупка, цена 800, купон 17% раз в полгода


def _seed_base(db):
    """Портфель 15000 ₽ (Альфа 10000 + Бета 5000) + две серии Гаммы в каталоге."""
    alfa = _insert_company(db, 'Альфа Пром')
    beta = _insert_company(db, 'Бета Финанс')
    gamma = _insert_company(db, 'Гамма Холдинг')

    _insert_bond(db, isin=AL1, ticker='SCAL1', name='Альфа 001P-01',
                 company_id=alfa, coupon_quantity_per_year=12,
                 coupon_rate_percent=Decimal('20.0'),
                 ytm=Decimal('15'), ytm_updated_at=_NOW,
                 market_price=Decimal('1000.0'))
    _insert_bond(db, isin=BT2, ticker='SCBT2', name='Бета БО-02',
                 company_id=beta, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('15.0'),
                 ytm=Decimal('12'), ytm_updated_at=_NOW,
                 market_price=Decimal('500.0'))
    _insert_bond(db, isin=GM1, ticker='SCGM1', name='Гамма БО-01',
                 company_id=gamma, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('18.0'),
                 ytm=Decimal('16'), ytm_updated_at=_NOW,
                 market_price=Decimal('900.0'))
    _insert_bond(db, isin=GM2, ticker='SCGM2', name='Гамма БО-02',
                 company_id=gamma, coupon_quantity_per_year=2,
                 coupon_rate_percent=Decimal('17.0'),
                 ytm=Decimal('15.5'), ytm_updated_at=_NOW,
                 market_price=Decimal('800.0'))
    _insert_position(db, AL1, quantity=10, price=1000)
    _insert_position(db, BT2, quantity=10, price=500)
    db.conn.commit()


def _write_scenario_file(tmp_path, trades=None, raw=None) -> str:
    """scenario.json во временном каталоге; raw — готовый JSON-объект."""
    path = tmp_path / 'scenario.json'
    payload = raw if raw is not None else {'trades': trades}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return str(path)


def _build_scenario(db, scenario_path: str, **overrides) -> dict:
    use_case = RebalanceReportUseCase(db=db)
    report = use_case._build_report(_make_args(scenario=scenario_path, **overrides))
    return report


def _by_isin(rows, key='isin'):
    return {row[key]: row for row in rows}


# ---------------------------------------------------------------------------
# Парсер и валидация scenario.json
# ---------------------------------------------------------------------------

class TestScenarioParsing:
    def _parse(self, argv):
        import argparse
        parser = argparse.ArgumentParser()
        RebalanceReportUseCase.setup_parser(parser)
        return parser.parse_args(argv)

    def test_scenario_flag_and_default(self):
        assert self._parse([]).scenario is None
        assert self._parse(['--scenario', '/tmp/s.json']).scenario == '/tmp/s.json'

    def test_load_valid_trades(self, tmp_path):
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': ' RU000A0X ', 'qty_delta': -530},
            {'isin': 'RU000A0Y', 'qty_delta': 5},
        ])
        use_case = RebalanceReportUseCase.__new__(RebalanceReportUseCase)
        trades = use_case._load_scenario_trades(path)
        assert trades == [
            {'isin': 'RU000A0X', 'qty_delta': -530.0},
            {'isin': 'RU000A0Y', 'qty_delta': 5.0},
        ]

    def test_missing_file_rejected(self):
        use_case = RebalanceReportUseCase.__new__(RebalanceReportUseCase)
        with pytest.raises(ValueError, match="файл не найден"):
            use_case._load_scenario_trades('/nonexistent/scenario.json')

    def test_broken_json_rejected(self, tmp_path):
        path = tmp_path / 'broken.json'
        path.write_text('{"trades": [', encoding='utf-8')
        use_case = RebalanceReportUseCase.__new__(RebalanceReportUseCase)
        with pytest.raises(ValueError, match="валидный JSON"):
            use_case._load_scenario_trades(str(path))

    @pytest.mark.parametrize('raw', [
        {'no_trades': 1},
        {'trades': []},
        {'trades': 'all'},
        {'trades': ['RU000A0X']},
        {'trades': [{'qty_delta': 5}]},
        {'trades': [{'isin': 123, 'qty_delta': 5}]},
        {'trades': [{'isin': 'RU000A0X'}]},
        {'trades': [{'isin': 'RU000A0X', 'qty_delta': '-530'}]},
        {'trades': [{'isin': 'RU000A0X', 'qty_delta': None}]},
        {'trades': [{'isin': 'RU000A0X', 'qty_delta': True}]},
    ])
    def test_structural_errors_rejected(self, tmp_path, raw):
        path = _write_scenario_file(tmp_path, raw=raw)
        use_case = RebalanceReportUseCase.__new__(RebalanceReportUseCase)
        with pytest.raises(ValueError):
            use_case._load_scenario_trades(path)


# ---------------------------------------------------------------------------
# Happy path: trades -> целевой портфель и дельты купонного потока (R8)
# ---------------------------------------------------------------------------

class TestScenarioTrades:
    @pytest.fixture(autouse=True)
    def _setup(self, db, tmp_path):
        _seed_base(db)
        # Продажа 4 Альфы (10000 -> 6000) + покупки Гаммы 5x900 и 3x800.
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': AL1, 'qty_delta': -4},
            {'isin': GM1, 'qty_delta': 5},
            {'isin': GM2, 'qty_delta': 3},
        ])
        self.report = _build_scenario(db, path)
        self.scenario = self.report['scenario']

    def test_scenario_block_present_and_valid(self):
        # Аддитивность: блок scenario появился, остальные блоки ядра на месте.
        assert set(self.report.keys()) == {
            'schema_version', 'meta', 'portfolio', 'concentrations',
            'screener', 'redemptions_6m', 'scenario', 'artifacts',
        }
        assert self.scenario['valid'] is True
        assert self.scenario['errors'] == []

    def test_trades_echo_carries_catalog_names(self):
        """Анти-P-D: имена/эмитенты проставляет тулкит, агент ISIN не пишет по памяти."""
        trades = _by_isin(self.scenario['trades'])
        for row in self.scenario['trades']:
            assert set(row.keys()) == set(SCENARIO_TRADE_KEYS)
        assert trades[AL1]['name'] == 'Альфа 001P-01'
        assert trades[AL1]['issuer'] == 'Альфа Пром'
        assert trades[AL1]['action'] == 'sell'
        assert trades[AL1]['qty_before'] == 10.0
        assert trades[AL1]['qty_after'] == 6.0
        assert trades[AL1]['price'] == 1000.0
        assert trades[AL1]['value_delta'] == -4000.0
        assert trades[GM1]['action'] == 'buy'
        assert trades[GM1]['qty_before'] == 0.0
        assert trades[GM1]['qty_after'] == 5.0
        assert trades[GM1]['price'] == 900.0   # покупки — по market_price каталога

    def test_target_portfolio_after_trades(self):
        positions = _by_isin(self.scenario['positions_after'])
        assert [p['isin'] for p in self.scenario['positions_after']] == [
            AL1, BT2, GM1, GM2,  # сортировка по стоимости по убыванию
        ]
        for pos in self.scenario['positions_after']:
            assert set(pos.keys()) == set(SCENARIO_POSITION_KEYS)
        assert positions[AL1]['qty'] == 6.0
        assert positions[AL1]['value'] == 6000.0
        assert positions[AL1]['price'] == 1000.0
        # цена держимой бумаги — каноническая оценка view на штуку
        assert positions[BT2]['value'] == 5000.0
        assert positions[GM1]['qty'] == 5.0
        assert positions[GM1]['value'] == 4500.0
        assert positions[GM2]['value'] == 2400.0
        # купонный поток в ₽/мес по каждой позиции целевого портфеля
        assert positions[AL1]['coupon_month'] == 100.0    # 6*1000*20%/12
        assert positions[GM1]['coupon_month'] == 75.0     # 5*1000*18%/12
        assert positions[GM2]['coupon_month'] == 42.5     # 3*1000*17%/12
        # бейдж сделки в целевом портфеле
        assert positions[AL1]['held_badge'] == 'сделка -4 шт'
        assert positions[GM2]['held_badge'] == 'сделка +3 шт'
        assert positions[BT2]['held_badge'].startswith('held (')

    def test_summary_before_after_and_coupon_deltas(self):
        """R8: дельты купонного потока считает тулкит, а не агент."""
        summary = self.scenario['summary']
        assert set(summary.keys()) == set(SCENARIO_SUMMARY_KEYS)
        assert summary['portfolio_before'] == 15000.0
        assert summary['portfolio_after'] == 17900.0
        # до: 10*1000*20%/12 + 10*500... нет, Бета: 10*1000*15%/12 = 125
        assert summary['coupon_month_before'] == pytest.approx(291.67)
        # после: 100 + 125 + 75 + 42.5
        assert summary['coupon_month_after'] == pytest.approx(342.5)
        assert summary['delta_month'] == pytest.approx(50.83)
        assert summary['coupon_year_before'] == pytest.approx(3500.04)
        assert summary['coupon_year_after'] == pytest.approx(4110.0)
        assert summary['delta_year'] == pytest.approx(summary['delta_month'] * 12, abs=0.02)

    def test_summary_before_matches_core_portfolio(self):
        """Консистентность: «сейчас» в сценарии = портфель ядра (та же оценка)."""
        assert self.scenario['summary']['portfolio_before'] == (
            self.report['portfolio']['total_value']
        )

    def test_freq_mix_before_after(self):
        before = {item['freq']: item['pct'] for item in self.scenario['freq_mix_before']}
        after = {item['freq']: item['pct'] for item in self.scenario['freq_mix_after']}
        # до: Альфа ежемесячная 10000/15000, Бета квартальная 5000/15000
        assert before == {12: 66.67, 4: 33.33}
        # после: quarterly = Гамма-01 4500 + Бета 5000, monthly = Альфа 6000,
        # полугодовые = Гамма-02 2400
        assert after == {4: 53.07, 12: 33.52, 2: 13.41}
        assert sum(after.values()) == pytest.approx(100.0)

    def test_issuer_limits_sum_of_all_legs(self):
        limits = self.scenario['issuer_limits']
        assert limits['limit_pct'] == 15.0
        issuers = {item['issuer']: item for item in limits['after_all']}
        # Гамма по сумме двух ног: (4500+2400)/17900
        assert issuers['Гамма Холдинг']['value'] == 6900.0
        assert issuers['Гамма Холдинг']['pct'] == 38.55
        assert issuers['Гамма Холдинг']['limit_status'] == 'LIMIT_VIOLATION (>15%)'
        assert limits['has_violations'] is True

    def test_duplicate_isin_trades_aggregated(self, db, tmp_path):
        """Две сделки по одному ISIN суммируются (GROUP BY isin, как в A2)."""
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': GM1, 'qty_delta': 5},
            {'isin': GM1, 'qty_delta': 2},
        ])
        scenario = _build_scenario(db, path)['scenario']
        assert scenario['valid'] is True
        positions = _by_isin(scenario['positions_after'])
        assert positions[GM1]['qty'] == 7.0
        assert positions[GM1]['value'] == 6300.0
        # эхо сделок кумулятивно, ноги показывают растущую долю эмитента
        assert [t['qty_after'] for t in scenario['trades']] == [5.0, 7.0]
        pcts = [leg['issuer_pct_after_leg'] for leg in scenario['issuer_limits']['legs']]
        assert pcts[1] > pcts[0]


class TestScenarioLegLimitProgression:
    """Лимит эмитента по каждой ноге и по сумме всех ног (кейс РЕСО из 002).

    База — 6 эмитентов по 50000 ₽ (по 14.25% после сделок, все < 15%):
    проверки лимитов отражают эффект сценария, а не концентрацию фикстуры.
    """

    _FILLERS = ('Епсилон Пром', 'Жету Транс', 'Зета Холдинг', 'Иета Финанс', 'Каппа Строй')

    def _seed(self, db):
        delta_co = _insert_company(db, 'Дельта Лизинг')
        rail_co = _insert_company(db, 'Единый Рельс')
        _insert_bond(db, isin='RU000A0LGDL1', ticker='LGDL1', name='Дельта 001P-01',
                     company_id=delta_co, coupon_quantity_per_year=4,
                     coupon_rate_percent=Decimal('15.0'),
                     ytm=Decimal('13'), ytm_updated_at=_NOW,
                     market_price=Decimal('100.0'))
        _insert_bond(db, isin='RU000A0RALE1', ticker='RALE1', name='Рельс БО-01',
                     company_id=rail_co, coupon_quantity_per_year=2,
                     coupon_rate_percent=Decimal('16.0'),
                     ytm=Decimal('14'), ytm_updated_at=_NOW,
                     market_price=Decimal('100.0'))
        _insert_bond(db, isin='RU000A0RALE2', ticker='RALE2', name='Рельс БО-02',
                     company_id=rail_co, coupon_quantity_per_year=2,
                     coupon_rate_percent=Decimal('15.5'),
                     ytm=Decimal('13.8'), ytm_updated_at=_NOW,
                     market_price=Decimal('100.0'))
        _insert_position(db, 'RU000A0LGDL1', quantity=500, price=100)
        # база без нарушений лимита: 5 эмитентов по 50000 ₽ (500 шт по 100 ₽)
        for i, filler in enumerate(self._FILLERS, start=1):
            co_id = _insert_company(db, filler)
            isin = f'RU000A0FILL{i}'
            _insert_bond(db, isin=isin, ticker=f'FILL{i}', name=f'{filler} БО-01',
                         company_id=co_id, coupon_quantity_per_year=4,
                         coupon_rate_percent=Decimal('14.0'),
                         ytm=Decimal('13.2'), ytm_updated_at=_NOW,
                         market_price=Decimal('100.0'))
            _insert_position(db, isin, quantity=500, price=100)
        db.conn.commit()

    def test_each_leg_pass_and_sum_pass(self, db, tmp_path):
        """РЕСО-кейс: 69 + 441 шт по 100 ₽ — сумма ног 14.53% (< 15%), PASS."""
        self._seed(db)
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': 'RU000A0RALE1', 'qty_delta': 69},
            {'isin': 'RU000A0RALE2', 'qty_delta': 441},
        ])
        limits = _build_scenario(db, path)['scenario']['issuer_limits']
        assert len(limits['legs']) == 2
        for leg in limits['legs']:
            assert set(leg.keys()) == set(SCENARIO_LEG_KEYS)
        # нога 1: 6900/351000; нога 2 (сумма всех ног): 51000/351000 = 14.53%
        assert limits['legs'][0]['issuer_pct_after_leg'] == 1.97
        assert limits['legs'][0]['limit_status'] == 'PASS'
        assert limits['legs'][1]['issuer'] == 'Единый Рельс'
        assert limits['legs'][1]['issuer_pct_after_leg'] == 14.53
        assert limits['legs'][1]['limit_status'] == 'PASS'
        assert len(limits['after_all']) == 7
        rail = [i for i in limits['after_all'] if i['issuer'] == 'Единый Рельс'][0]
        assert rail['pct'] == 14.53 and rail['limit_status'] == 'PASS'
        assert limits['has_violations'] is False

    def test_sum_of_legs_violation_caught(self, db, tmp_path):
        """P6: каждая нога против текущего портфеля проходит, сумма ног — 17.81% > 15%."""
        self._seed(db)
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': 'RU000A0RALE1', 'qty_delta': 400},
            {'isin': 'RU000A0RALE2', 'qty_delta': 250},
        ])
        limits = _build_scenario(db, path)['scenario']['issuer_limits']
        # нога 1: 40000/365000 = 10.96% — PASS; нога 2 (кумулятивно): 17.81%
        assert limits['legs'][0]['issuer_pct_after_leg'] == 10.96
        assert limits['legs'][0]['limit_status'] == 'PASS'
        assert limits['legs'][1]['issuer_pct_after_leg'] == 17.81
        assert limits['legs'][1]['limit_status'] == 'LIMIT_VIOLATION (>15%)'
        rail = [i for i in limits['after_all'] if i['issuer'] == 'Единый Рельс'][0]
        assert rail['limit_status'] == 'LIMIT_VIOLATION (>15%)'
        assert limits['has_violations'] is True

    def test_selling_reduces_issuer_share(self, db, tmp_path):
        self._seed(db)
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': 'RU000A0LGDL1', 'qty_delta': -100},
        ])
        limits = _build_scenario(db, path)['scenario']['issuer_limits']
        # Дельта: было 14.25%, после продажи 40000/290000 = 13.79% — PASS
        assert limits['legs'][0]['issuer_pct_after_leg'] == 13.79
        assert limits['legs'][0]['limit_status'] == 'PASS'


# ---------------------------------------------------------------------------
# Ошибки сценария: unknown ISIN, отрицательный остаток, NO_COMPANY_LINK
# ---------------------------------------------------------------------------

class TestScenarioErrors:
    @pytest.fixture(autouse=True)
    def _setup(self, db):
        _seed_base(db)

    def test_unknown_isin_explicit_error_not_crash(self, db, tmp_path):
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': 'RU000A0NOEXIST', 'qty_delta': 5},
            {'isin': AL1, 'qty_delta': -4},
        ])
        report = _build_scenario(db, path)
        scenario = report['scenario']
        assert scenario['valid'] is False
        assert len(scenario['errors']) == 1
        error = scenario['errors'][0]
        assert error['type'] == SCENARIO_ERROR_UNKNOWN_ISIN
        assert error['isin'] == 'RU000A0NOEXIST'
        assert 'RU000A0NOEXIST' in error['message']
        # эхо сделки: имя подставить не из чего, остатки неизвестны
        unknown_trade = scenario['trades'][0]
        assert unknown_trade['name'] == 'RU000A0NOEXIST'
        assert unknown_trade['qty_after'] is None
        # блоки «после» невалидны (как блоки 1–3 A2 при непустом блоке 0)
        assert scenario['summary'] is None
        assert scenario['freq_mix_before'] is None
        assert scenario['freq_mix_after'] is None
        assert scenario['issuer_limits'] is None
        assert scenario['positions_after'] == []
        # ядро ответа при этом осталось целым
        assert report['portfolio']['total_value'] == 15000.0

    def test_negative_qty_after_single_trade(self, db, tmp_path):
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': AL1, 'qty_delta': -15},   # держим 10
        ])
        scenario = _build_scenario(db, path)['scenario']
        assert scenario['valid'] is False
        assert scenario['errors'][0]['type'] == SCENARIO_ERROR_NEGATIVE_QTY_AFTER
        assert scenario['errors'][0]['isin'] == AL1
        assert 'остаток' in scenario['errors'][0]['message']
        assert scenario['summary'] is None

    def test_negative_qty_after_aggregated_legs(self, db, tmp_path):
        """Две продажи по -8 при позиции 10: ошибка по СУММЕ ног, а не по каждой."""
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': AL1, 'qty_delta': -8},
            {'isin': AL1, 'qty_delta': -8},
        ])
        scenario = _build_scenario(db, path)['scenario']
        assert scenario['valid'] is False
        assert scenario['errors'][0]['type'] == SCENARIO_ERROR_NEGATIVE_QTY_AFTER

    def test_exact_to_zero_is_allowed(self, db, tmp_path):
        """Продажа всей позиции (остаток ровно 0) — валидна, бумага уходит."""
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': AL1, 'qty_delta': -10},
        ])
        scenario = _build_scenario(db, path)['scenario']
        assert scenario['valid'] is True
        assert AL1 not in _by_isin(scenario['positions_after'])
        assert scenario['summary']['portfolio_after'] == 5000.0

    def test_no_company_link_error(self, db, tmp_path):
        """Покупка бумаги без связи с эмитентом — NO_COMPANY_LINK (блок 0 A2)."""
        _insert_bond(db, isin='RU000A0NOLNK', ticker='NOLNK', name='Без Эмитента',
                     company_id=None, coupon_quantity_per_year=4,
                     coupon_rate_percent=Decimal('12.0'),
                     market_price=Decimal('500.0'))
        db.conn.commit()
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': 'RU000A0NOLNK', 'qty_delta': 5},
        ])
        scenario = _build_scenario(db, path)['scenario']
        assert scenario['valid'] is False
        assert scenario['errors'][0]['type'] == SCENARIO_ERROR_NO_COMPANY_LINK

    def test_no_company_link_ignored_when_position_closed(self, db, tmp_path):
        """Продажа в ноль несвязанной позиции — ошибки нет (qty_after = 0)."""
        _insert_bond(db, isin='RU000A0NOLNK', ticker='NOLNK', name='Без Эмитента',
                     company_id=None, coupon_quantity_per_year=4,
                     coupon_rate_percent=Decimal('12.0'),
                     market_price=Decimal('500.0'))
        _insert_position(db, 'RU000A0NOLNK', quantity=5, price=500)
        db.conn.commit()
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': 'RU000A0NOLNK', 'qty_delta': -5},
        ])
        scenario = _build_scenario(db, path)['scenario']
        assert scenario['valid'] is True

    def test_errors_suppress_csv_artifact(self, db, tmp_path):
        csv_path = tmp_path / 'target.csv'
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': 'RU000A0NOEXIST', 'qty_delta': 5},
        ])
        report = _build_scenario(db, path, csv_out=str(csv_path))
        assert report['artifacts']['target_portfolio_csv'] is None
        assert not csv_path.exists()


# ---------------------------------------------------------------------------
# Погашенные бумаги: анти-фантомный guard A2/P1
# ---------------------------------------------------------------------------

class TestScenarioMatured:
    def test_selling_zombie_matured_no_phantom_value(self, db, tmp_path):
        """Продажа зомби-хвоста (погашенная, оценка 0): без фантомной стоимости/куполона."""
        _seed_base(db)
        corp = _insert_company(db, 'Зомби Групп')
        _insert_bond(db, isin='RU000A0ZOMB1', ticker='ZOMB1', name='Монополия 1P-02',
                     company_id=corp,
                     maturity_date=date.today() - timedelta(days=30),
                     coupon_rate_percent=Decimal('10.0'),
                     market_price=Decimal('0'))
        _insert_position(db, 'RU000A0ZOMB1', quantity=35, price=0)
        db.conn.commit()

        path = _write_scenario_file(tmp_path, trades=[
            {'isin': 'RU000A0ZOMB1', 'qty_delta': -35},
        ])
        scenario = _build_scenario(db, path)['scenario']
        assert scenario['valid'] is True
        # «сейчас» — без фантомных 35000: погашенная строка стоит 0
        assert scenario['summary']['portfolio_before'] == 15000.0
        assert scenario['summary']['portfolio_after'] == 15000.0
        assert scenario['summary']['coupon_month_before'] == pytest.approx(291.67)
        assert 'RU000A0ZOMB1' not in _by_isin(scenario['positions_after'])

    def test_buying_matured_bond_is_zeroed_and_excluded(self, db, tmp_path):
        """Покупка погашенной бумаги: value/coupon = 0, из целевого портфеля исключена."""
        _seed_base(db)
        corp = _insert_company(db, 'Мертвый Эмитент')
        _insert_bond(db, isin='RU000A0DEAD9', ticker='DEAD9', name='Погашенная БО-01',
                     company_id=corp,
                     maturity_date=date.today() - timedelta(days=1),
                     coupon_rate_percent=Decimal('99.0'),
                     market_price=Decimal('900.0'))
        db.conn.commit()

        path = _write_scenario_file(tmp_path, trades=[
            {'isin': 'RU000A0DEAD9', 'qty_delta': 10},
        ])
        scenario = _build_scenario(db, path)['scenario']
        assert scenario['valid'] is True   # синтаксически сценарий допустим
        assert scenario['summary']['portfolio_after'] == 15000.0  # покупка не добавила стоимости
        assert 'RU000A0DEAD9' not in _by_isin(scenario['positions_after'])
        assert scenario['summary']['coupon_month_after'] == pytest.approx(291.67)


# ---------------------------------------------------------------------------
# Цены и оценки
# ---------------------------------------------------------------------------

class TestScenarioValuation:
    def test_held_price_is_view_value_per_unit_multi_account(self, db, tmp_path):
        """ISIN на двух счетах: цена «после» = value_rub/qty (каноническая оценка view)."""
        _seed_base(db)
        alfa = _insert_company(db, 'Альфа Пром')
        _insert_bond(db, isin='RU000A0MULAC', ticker='MULAC', name='Альфа 002P-02',
                     company_id=alfa, coupon_quantity_per_year=4,
                     coupon_rate_percent=Decimal('14.0'),
                     ytm=Decimal('13'), ytm_updated_at=_NOW,
                     market_price=Decimal('850.0'))
        _insert_position(db, 'RU000A0MULAC', quantity=10, price=1000, account='a1')
        _insert_position(db, 'RU000A0MULAC', quantity=5, price=800, account='a2')
        db.conn.commit()

        path = _write_scenario_file(tmp_path, trades=[
            {'isin': 'RU000A0MULAC', 'qty_delta': -1},
        ])
        scenario = _build_scenario(db, path)['scenario']
        positions = _by_isin(scenario['positions_after'])
        # 14000/15 = 933.3333 ₽/шт; после продажи 1 шт: 14 * 933.3333 = 13066.67
        assert positions['RU000A0MULAC']['price'] == pytest.approx(933.3333, abs=0.001)
        assert positions['RU000A0MULAC']['value'] == pytest.approx(13066.67, abs=0.01)
        assert positions['RU000A0MULAC']['qty'] == 14.0

    def test_purchase_uses_market_price_with_nominal_fallback(self, db, tmp_path):
        """Покупка без market_price: цена = номинал (фолбэк A2), не ноль."""
        _seed_base(db)
        corp = _insert_company(db, 'Без Рынка')
        _insert_bond(db, isin='RU000A0NOMKT', ticker='NOMKT', name='Без Рынка БО-1',
                     company_id=corp, coupon_quantity_per_year=2,
                     coupon_rate_percent=Decimal('13.0'),
                     market_price=None, nominal=Decimal('1000.0'))
        db.conn.commit()

        path = _write_scenario_file(tmp_path, trades=[
            {'isin': 'RU000A0NOMKT', 'qty_delta': 3},
        ])
        scenario = _build_scenario(db, path)['scenario']
        positions = _by_isin(scenario['positions_after'])
        assert positions['RU000A0NOMKT']['price'] == 1000.0
        assert positions['RU000A0NOMKT']['value'] == 3000.0
        assert scenario['summary']['portfolio_after'] == 18000.0


# ---------------------------------------------------------------------------
# CSV-артефакт целевого портфеля после trades
# ---------------------------------------------------------------------------

class TestScenarioCsv:
    def test_csv_contains_target_portfolio_after_trades(self, db, tmp_path):
        _seed_base(db)
        csv_path = tmp_path / 'target.csv'
        path = _write_scenario_file(tmp_path, trades=[
            {'isin': AL1, 'qty_delta': -4},
            {'isin': GM1, 'qty_delta': 5},
        ])
        report = _build_scenario(db, path, csv_out=str(csv_path))

        assert report['artifacts']['target_portfolio_csv'] == str(csv_path)
        raw = csv_path.read_bytes()
        assert raw.startswith(b'\xef\xbb\xbf')  # UTF-8 BOM
        text = raw.decode('utf-8-sig')
        rows = list(csv.reader(text.strip().splitlines()))
        assert rows[0][0] == 'ISIN' and 'Стоимость, ₽' in rows[0]
        # ИТОГО = стоимость портфеля ПОСЛЕ сделок (6000 + 5000 + 4500)
        total_rows = [row for row in rows if row and row[0] == 'ИТОГО']
        assert len(total_rows) == 1
        assert '15500.00' in total_rows[0]
        # проданная до нуля позиция отсутствует, купленная присутствует
        isins = {row[0] for row in rows[1:-1]}
        assert GM1 in isins
        # бейдж сделки в CSV
        gm1_row = [row for row in rows if row and row[0] == GM1][0]
        assert 'сделка +5 шт' in gm1_row[-1]

    def test_csv_without_scenario_still_snapshot_of_current_portfolio(self, db, tmp_path):
        """Ядро 005.1 не сломано: без --scenario CSV — снапшот текущего портфеля."""
        _seed_base(db)
        csv_path = tmp_path / 'base.csv'
        use_case = RebalanceReportUseCase(db=db)
        report = use_case._build_report(_make_args(csv_out=str(csv_path)))
        assert report['artifacts']['target_portfolio_csv'] == str(csv_path)
        assert 'scenario' not in report
        text = csv_path.read_text(encoding='utf-8-sig')
        assert '15500.00' not in text.split('ИТОГО')[-1]  # там 15000.00
        assert '15000.00' in text.split('ИТОГО')[-1]


# ---------------------------------------------------------------------------
# Анти-P-A: сценарий одной командой без env/psql (subprocess)
# ---------------------------------------------------------------------------

def test_cli_scenario_unknown_isin_returns_json_not_crash(db, tmp_path):
    """Сценарий с неверным ISIN: rc=0, явная ошибка в JSON (а не падение)."""
    path = _write_scenario_file(tmp_path, trades=[
        {'isin': 'RU000A0NOEXIST', 'qty_delta': 5},
    ])
    env = os.environ.copy()
    env['PYTHONPATH'] = '.'
    test_dsn = env.get('POSTGRES_DSN_TEST')
    assert test_dsn, "POSTGRES_DSN_TEST не задан — CLI-тест не может идти на тестовую БД"
    env['POSTGRES_DSN'] = test_dsn

    res = subprocess.run(
        [sys.executable, 'main.py', 'rebalance-report', '--format', 'json',
         '--scenario', path],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, f"Stderr: {res.stderr}"
    data = json.loads(res.stdout)
    assert data['scenario']['valid'] is False
    assert data['scenario']['errors'][0]['type'] == 'UNKNOWN_ISIN'
    assert data['scenario']['summary'] is None


def test_cli_scenario_broken_file_fails_with_clear_error(db, tmp_path):
    """Структурно неверный файл сценария: rc=1 и понятное сообщение в stderr."""
    broken = tmp_path / 'broken.json'
    broken.write_text('не json', encoding='utf-8')
    env = os.environ.copy()
    env['PYTHONPATH'] = '.'
    test_dsn = env.get('POSTGRES_DSN_TEST')
    assert test_dsn, "POSTGRES_DSN_TEST не задан — CLI-тест не может идти на тестовую БД"
    env['POSTGRES_DSN'] = test_dsn

    res = subprocess.run(
        [sys.executable, 'main.py', 'rebalance-report', '--format', 'json',
         '--scenario', str(broken)],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 1
    assert 'валидный JSON' in res.stderr
