"""
Тесты canonical rebalance engine (задача 030.2).

Движок `src/services/rebalance_engine.py::RebalanceEngine` — единственный
расчётчик целевого портфеля (правило 030: второго калькулятора нет), извлечён
из RebalanceReportUseCase._build_scenario без изменения поведения. Здесь
проверяется Python API движка:

- planning_prices: цена расчёта и её basis для КАЖДОГО ISIN вселенной
  (фундамент правила «одна цена на ISIN внутри plan», 030.7);
- calculator и validator внутри одного прогона используют одну и ту же цену
  (positions/trades/лимиты/constraint actuals считаются от одного eff_price);
- полный результат одним вызовом (база projections/adapter без shell-парсинга,
  030.8/030.11);
- legacy JSON-контракт блока scenario команды rebalance-report не изменён
  (ключи те же, в том же порядке; planning_prices в JSON не публикуется).

Все данные — синтетические фикстуры на тестовой БД (POSTGRES_DSN_TEST);
реальные составы портфеля и ISIN не используются (AGENTS.md).
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.services.rebalance_engine import (
    PLANNING_PRICE_BASIS_HELD_VALUATION,
    PLANNING_PRICE_BASIS_MARKET,
    PLANNING_PRICE_BASIS_MATURED_ZERO,
    PLANNING_PRICE_BASIS_NOMINAL_FALLBACK,
    CONSTRAINT_POSITION_VALUE_LIMIT,
    RebalanceEngine,
    SCENARIO_LIMIT_STATUS_VIOLATION,
    ScenarioConstraints,
)
from src.use_cases.rebalance_report import RebalanceReportUseCase
from test_rebalance_report import (
    _NOW,
    _insert_bond,
    _insert_company,
    _insert_position,
    _make_args,
)

AL1 = 'RU000A0ENAL1'   # Альфа: держим 10x1000 (view-оценка 10000), купон 20% ежем.
BT2 = 'RU000A0ENBT2'   # Бета: держим 10x500, сделки по ней нет
GM1 = 'RU000A0ENGM1'   # Гамма: покупка 5x900 (market_price каталога)

# Полный Python-контракт результата evaluate_scenario (030.2): блоки расчёта
# legacy scenario + planning_prices.
ENGINE_RESULT_KEYS = {
    'valid', 'feasible', 'errors', 'trades', 'summary', 'budget_check',
    'constraint_checks', 'freq_mix_before', 'freq_mix_after', 'issuer_limits',
    'positions_after', 'planning_prices',
}

# JSON-контракт блока scenario legacy команды (005.2/024) — порядок ключей
# тоже часть контракта ответа; planning_prices/'file' в движковом результате
# нет/'file' добавляет use case.
LEGACY_SCENARIO_KEYS = [
    'file', 'valid', 'feasible', 'errors', 'trades', 'summary', 'budget_check',
    'constraint_checks', 'freq_mix_before', 'freq_mix_after', 'issuer_limits',
    'positions_after',
]


def _seed_base(db):
    """Портфель 15000 ₽ (Альфа 10000 + Бета 5000) + Гамма в каталоге (900 ₽)."""
    alfa = _insert_company(db, 'Альфа Пром')
    beta = _insert_company(db, 'Бета Финанс')
    gamma = _insert_company(db, 'Гамма Холдинг')
    _insert_bond(db, isin=AL1, ticker='ENAL1', name='Альфа 001P-01',
                 company_id=alfa, coupon_quantity_per_year=12,
                 coupon_rate_percent=Decimal('20.0'),
                 ytm=Decimal('15'), ytm_updated_at=_NOW,
                 market_price=Decimal('1000.0'))
    _insert_bond(db, isin=BT2, ticker='ENBT2', name='Бета БО-02',
                 company_id=beta, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('15.0'),
                 ytm=Decimal('12'), ytm_updated_at=_NOW,
                 market_price=Decimal('500.0'))
    _insert_bond(db, isin=GM1, ticker='ENGM1', name='Гамма БО-01',
                 company_id=gamma, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('18.0'),
                 ytm=Decimal('16'), ytm_updated_at=_NOW,
                 market_price=Decimal('900.0'))
    _insert_position(db, AL1, quantity=10, price=1000)
    _insert_position(db, BT2, quantity=10, price=500)
    db.conn.commit()


def _evaluate(db, trades, constraints=None):
    return RebalanceEngine(db).evaluate_scenario(trades, constraints)


# ---------------------------------------------------------------------------
# planning_prices: цена + basis для каждого ISIN (030.2)
# ---------------------------------------------------------------------------

class TestPlanningPrices:
    def test_price_and_basis_present_for_every_isin(self, db):
        """Каждый ISIN вселенной (портфель + сделки) несёт цену и basis."""
        _seed_base(db)
        result = _evaluate(db, [
            {'isin': AL1, 'qty_delta': -4},
            {'isin': GM1, 'qty_delta': 5},
        ])
        prices = result['planning_prices']
        assert set(prices.keys()) == {AL1, BT2, GM1}
        for isin, entry in prices.items():
            assert set(entry.keys()) == {'price', 'basis'}, isin
            assert isinstance(entry['price'], float), isin
            assert isinstance(entry['basis'], str) and entry['basis'], isin

    def test_held_basis_is_view_valuation_per_unit(self, db):
        """Held позиция — каноническая оценка view на штуку (value_rub/qty)."""
        _seed_base(db)
        prices = _evaluate(db, [{'isin': AL1, 'qty_delta': -4}])['planning_prices']
        assert prices[AL1] == {
            'price': 1000.0, 'basis': PLANNING_PRICE_BASIS_HELD_VALUATION,
        }
        # Бета без сделок — тоже часть вселенной, та же held-оценка.
        assert prices[BT2] == {
            'price': 500.0, 'basis': PLANNING_PRICE_BASIS_HELD_VALUATION,
        }

    def test_new_position_basis_is_market_price(self, db):
        """Новая позиция — market_price каталога."""
        _seed_base(db)
        prices = _evaluate(db, [{'isin': GM1, 'qty_delta': 5}])['planning_prices']
        assert prices[GM1] == {
            'price': 900.0, 'basis': PLANNING_PRICE_BASIS_MARKET,
        }

    def test_nominal_fallback_basis_is_explicit_not_silent(self, db):
        """Покупка без market_price: цена = номинал, basis ЯВНО nominal_fallback
        (silent fallback остался только в legacy-расчёте, planner 030.7 видит
        его по basis)."""
        _seed_base(db)
        corp = _insert_company(db, 'Без Рынка')
        _insert_bond(db, isin='RU000A0ENNMP1', ticker='ENNMP1', name='Без Рынка БО-1',
                     company_id=corp, coupon_quantity_per_year=2,
                     coupon_rate_percent=Decimal('13.0'),
                     market_price=None, nominal=Decimal('1000.0'))
        db.conn.commit()
        prices = _evaluate(
            db, [{'isin': 'RU000A0ENNMP1', 'qty_delta': 3}]
        )['planning_prices']
        assert prices['RU000A0ENNMP1'] == {
            'price': 1000.0, 'basis': PLANNING_PRICE_BASIS_NOMINAL_FALLBACK,
        }

    def test_matured_zero_basis(self, db):
        """Погашенная бумага: цена 0 (анти-фантомный P1-guard), basis matured_zero."""
        _seed_base(db)
        corp = _insert_company(db, 'Мертвый Эмитент')
        _insert_bond(db, isin='RU000A0ENDEAD', ticker='ENDEAD', name='Погашенная БО-01',
                     company_id=corp,
                     maturity_date=date.today() - timedelta(days=1),
                     coupon_rate_percent=Decimal('99.0'),
                     market_price=Decimal('900.0'))
        db.conn.commit()
        prices = _evaluate(
            db, [{'isin': 'RU000A0ENDEAD', 'qty_delta': 10}]
        )['planning_prices']
        assert prices['RU000A0ENDEAD'] == {
            'price': 0.0, 'basis': PLANNING_PRICE_BASIS_MATURED_ZERO,
        }


# ---------------------------------------------------------------------------
# Одна цена на ISIN внутри прогона: calculator == validator
# ---------------------------------------------------------------------------

class TestSinglePricePerIsin:
    def _seed_fractional_view(self, db):
        """Held с дробной ценой view (14000/15 = 933.33...) + покупка —
        нетривиальные float-цены, чтобы «одинаковость» не была случайной."""
        _seed_base(db)
        alfa = _insert_company(db, 'Мульти Счёт')
        _insert_bond(db, isin='RU000A0ENMUL1', ticker='ENMUL1', name='Мульти 002P-02',
                     company_id=alfa, coupon_quantity_per_year=4,
                     coupon_rate_percent=Decimal('14.0'),
                     ytm=Decimal('13'), ytm_updated_at=_NOW,
                     market_price=Decimal('850.0'))
        _insert_position(db, 'RU000A0ENMUL1', quantity=10, price=1000, account='a1')
        _insert_position(db, 'RU000A0ENMUL1', quantity=5, price=800, account='a2')
        db.conn.commit()

    def test_constraint_actual_computed_from_calculator_price(self, db):
        """POSITION_VALUE_LIMIT.actual = qty_after * planning_price — та же цена,
        которой calculator считал value_after (второй цены нет)."""
        self._seed_fractional_view(db)
        trades = [
            {'isin': 'RU000A0ENMUL1', 'qty_delta': -1},   # 14 * 933.33... = 13066.67
            {'isin': GM1, 'qty_delta': 5},                # 4500
        ]
        result = _evaluate(db, trades, ScenarioConstraints(max_position_value=5000.0))
        prices = result['planning_prices']

        position_limits = [
            v for v in result['constraint_checks']
            if v['type'] == CONSTRAINT_POSITION_VALUE_LIMIT
        ]
        # Мульти (13066.67) и Альфа (6000) дороже лимита; Гамма (4500) и Бета
        # (ровно 5000, сравнение строгое) — без ложных срабатываний.
        assert {v['isin'] for v in position_limits} == {'RU000A0ENMUL1', AL1}
        for violation in position_limits:
            isin = violation['isin']
            price = prices[isin]['price']
            qty_after = next(
                p['qty'] for p in result['positions_after'] if p['isin'] == isin
            )
            assert violation['actual'] == round(qty_after * price, 2)

    def test_all_outputs_consistent_with_planning_price(self, db):
        """trades echo, positions_after и лимиты эмитента считаются от одной
        и той же planning-цены каждого ISIN."""
        self._seed_fractional_view(db)
        trades = [
            {'isin': 'RU000A0ENMUL1', 'qty_delta': -1},
            {'isin': GM1, 'qty_delta': 5},
        ]
        result = _evaluate(db, trades)
        prices = result['planning_prices']

        by_isin_trades = {t['isin']: t for t in result['trades']}
        by_isin_positions = {p['isin']: p for p in result['positions_after']}
        for isin, entry in prices.items():
            price = entry['price']
            if isin in by_isin_trades:
                trade = by_isin_trades[isin]
                assert trade['price'] == round(price, 4)
                assert trade['value_delta'] == round(trade['qty_delta'] * price, 2)
            position = by_isin_positions[isin]
            assert position['price'] == round(price, 4)
            assert position['value'] == round(position['qty'] * price, 2)

        # Лимит эмитента по сумме ног — тоже от planning-цен (933.33...).
        mul_price = prices['RU000A0ENMUL1']['price']
        leg = result['issuer_limits']['legs'][0]
        assert leg['trade_value'] == round(-1 * mul_price, 2)
        # Топ after_all — «Мульти Счёт»: 14 * 933.33... = 13066.67
        assert result['issuer_limits']['after_all'][0]['issuer'] == 'Мульти Счёт'
        assert result['issuer_limits']['after_all'][0]['value'] == round(
            14 * mul_price, 2
        )
        assert result['issuer_limits']['after_all'][0]['limit_status'] == (
            SCENARIO_LIMIT_STATUS_VIOLATION
        )


# ---------------------------------------------------------------------------
# Python API: полный результат одним вызовом + legacy JSON-контракт
# ---------------------------------------------------------------------------

class TestEngineApiAndLegacyContract:
    def test_full_result_one_call(self, db):
        """evaluate_scenario возвращает весь расчёт одним вызовом (без shell)."""
        _seed_base(db)
        result = _evaluate(db, [{'isin': AL1, 'qty_delta': -4}])
        assert set(result.keys()) == ENGINE_RESULT_KEYS
        assert result['valid'] is True
        assert result['feasible'] is False  # концентрированная фикстура > 15%
        assert result['summary']['portfolio_after'] == 6000.0 + 5000.0

    def test_use_case_scenario_block_equals_engine_result(self, db, tmp_path):
        """Legacy --scenario идёт через engine: блок scenario == результат
        evaluate_scenario на тех же trades/constraints (без дублирования
        математики), planning_prices в JSON не публикуется."""
        _seed_base(db)
        path = tmp_path / 'scenario.json'
        trades = [{'isin': AL1, 'qty_delta': -4}, {'isin': GM1, 'qty_delta': 5}]
        path.write_text(
            '{"trades": [{"isin": "%s", "qty_delta": -4}, '
            '{"isin": "%s", "qty_delta": 5}]}' % (AL1, GM1),
            encoding='utf-8',
        )
        use_case = RebalanceReportUseCase(db=db)
        report = use_case.build_report(_make_args(scenario=str(path)))

        engine_result = _evaluate(db, trades)
        assert report['scenario'] == {
            'file': str(path),
            **{k: v for k, v in engine_result.items() if k != 'planning_prices'},
        }
        assert set(engine_result['planning_prices'].keys()) == {AL1, BT2, GM1}

    def test_legacy_json_scenario_keys_unchanged(self, db, tmp_path):
        """JSON-контракт legacy команды: ни одно поле не удалено/переименовано,
        порядок ключей прежний; planning_prices — только Python API движка."""
        _seed_base(db)
        path = tmp_path / 'scenario.json'
        path.write_text(
            '{"trades": [{"isin": "%s", "qty_delta": -4}]}' % AL1,
            encoding='utf-8',
        )
        report = RebalanceReportUseCase(db=db).build_report(
            _make_args(scenario=str(path))
        )
        assert list(report['scenario'].keys()) == LEGACY_SCENARIO_KEYS

    def test_engine_accepts_plain_constraints_without_cli(self, db):
        """Engine не знает о CLI: ограничения — плоский ScenarioConstraints,
        сделки — список dict'ов; без ограничений formal-проверки не добавить."""
        _seed_base(db)
        result = _evaluate(db, [{'isin': AL1, 'qty_delta': -4}])
        types = {v['type'] for v in result['constraint_checks']}
        assert types == {'ISSUER_LIMIT'}  # только концентрация фикстуры
