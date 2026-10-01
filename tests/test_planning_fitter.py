"""
Тесты auto-fit fitter (задача 030.7).

`src/services/planning_fitter.py::AutoFitFitter` переводит normalized
high-level intents (030.5) в целые количества по единой planning-цене и
валидирует результат ТОЛЬКО каноническим движком (030.2). Проверяются
требования файла задачи:

- target_value → final integer BUY qty считается кодом (LLM qty не передаётся);
- target ниже текущей стоимости даёт net SELL; sell_all → final qty 0;
- один high-level ISIN → одна normalized net trade;
- lower-priority BUY режется первым при budget shortage;
- issuer violation: режутся BUY того же эмитента от lower-priority; issuer
  share пересчитывается на полном final state (headroom, посчитанный один
  раз от исходного портфеля, недостаточен);
- planner не придумывает sell untouched position: untouched violation →
  infeasible (feasible=false), не auto-sell;
- unknown cash не становится 0: budget_check.known=false, feasible=null;
- money sizing на Decimal/integer деньгах (float-релей только canonical цены);
- детерминированный результат на одном PlanningContext;
- final canonical validator вызывается после fit (spy);
- price consistency regression: report price 951.30 vs planning_price
  1103.06 (и 970.80) — qty считается по planning_price, calculator и
  validator на одной цене/basis, final value <= target/max-position, второй
  run с ручным decrement не нужен; расхождение среза — CONTEXT_PRICE_MISMATCH
  и feasible=null;
- ineligible BUY: BUY_NOT_ELIGIBLE с reason codes, без BUY-ноги; PRICE_
  UNAVAILABLE для nominal_fallback/нулевой цены; UNKNOWN_ISIN.

Все данные — синтетические (in-memory двойник PortfolioStorage + реальный
PlanningContextBuilder/RebalanceEngine), реальные составы портфеля и ISIN
не используются (AGENTS.md); тесты не требуют POSTGRES_DSN_TEST.
"""
import contextlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.services.buy_eligibility import BUY_NOT_ELIGIBLE
from src.services.planning_context import PlanningContextBuilder
from src.services.planning_fitter import (
    DIAG_CONTEXT_PRICE_MISMATCH,
    FIT_REASON_BUDGET_CAP,
    FIT_REASON_INTEGER_QTY,
    FIT_REASON_ISSUER_CAP,
    FIT_REASON_POSITION_CAP,
    REASON_NOT_IN_BUY_POOL,
    TARGET_FITTED,
    TARGET_PRICE_UNAVAILABLE,
    TARGET_STATUS_BUY_NOT_ELIGIBLE,
    TARGET_UNKNOWN_ISIN,
    AutoFitFitter,
    _ceil_steps,
    _floor_qty,
    _money,
    _price_decimal,
)
from src.services.planning_intents import (
    CandidateFilters,
    PortfolioConstraints,
    SellAllIntent,
    TargetValueIntent,
)
from src.services.rebalance_engine import (
    CONSTRAINT_ISSUER_LIMIT,
    PLANNING_PRICE_BASIS_HELD_VALUATION,
    PLANNING_PRICE_BASIS_MARKET,
    PLANNING_PRICE_BASIS_MATURED_ZERO,
    PLANNING_PRICE_BASIS_NOMINAL_FALLBACK,
    RebalanceEngine,
)
from test_planning_context import _FRESH_FRESHNESS

AS_OF = datetime(2025, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
AS_OF_DATE = AS_OF.date()
CASH_KNOWN = {
    'cash_available_rub': Decimal('50000.00'),
    'cash_known': True,
    'cash_updated_at': AS_OF - timedelta(minutes=10),
}
CASH_UNKNOWN = {
    'cash_available_rub': None,
    'cash_known': False,
    'cash_updated_at': None,
}


# ---------------------------------------------------------------------------
# Синтетические canonical строки (как из БД: деньги — Decimal)
# ---------------------------------------------------------------------------


def _catalog_fields(
    isin,
    *,
    issuer=None,
    market_price='1000',
    nominal='1000',
    coupon='10.0',
    coupon_freq=4,
    maturity=date(2027, 6, 15),
    ku=False,
    risk_level=1,
    list_level=1,
    currency='rub',
    is_trade_available=True,
    credit_rating='A',
    rating_score=12,
):
    return {
        'isin': isin,
        'ticker': isin[-5:],
        'name': 'Бумага ' + isin,
        'nominal': Decimal(nominal),
        'coupon_rate_percent': Decimal(coupon),
        'coupon_quantity_per_year': coupon_freq,
        'maturity_date': maturity,
        'offer_date': None,
        'risk_level': risk_level,
        'list_level': list_level,
        'amortization_flag': False,
        'floating_coupon_flag': False,
        'perpetual_flag': False,
        'ku': ku,
        'market_price': (
            Decimal(market_price) if market_price is not None else None
        ),
        'currency': currency,
        'is_trade_available': is_trade_available,
        'market_price_updated_at': AS_OF - timedelta(minutes=30),
        'ytm': Decimal('10.0'),
        'ytm_null_reason': None,
        'ytm_updated_at': AS_OF - timedelta(hours=1),
        'company_id': 1,
        'issuer': issuer or ('Эмитент ' + isin),
        'entity_type': 'corporate',
        'credit_rating': credit_rating,
        'rating_score': rating_score,
    }


def _held_row(isin, *, qty, price, valuation_source='market_price', **catalog):
    """Строка портфеля: каноническая оценка view на штуку = value_rub/qty."""
    row = _catalog_fields(isin, **catalog)
    row['quantity'] = Decimal(str(qty))
    row['value_rub'] = Decimal(str(qty)) * Decimal(str(price))
    row['price'] = Decimal(str(price))
    row['valuation_source'] = valuation_source
    return row


def _pool_row(isin, **catalog):
    """Строка canonical raw buy-пула (алиас current_price, как в buy-SQL)."""
    row = _catalog_fields(isin, **catalog)
    row['current_price'] = row['market_price']
    return row


def _equal_base(
    *, count=7, value_qty=12, price='1000', prefix='RU000A0BASE'
):
    """База: count эмитентов в портфеле, каждый ниже лимита 15%.

    7 x 12000 = 84000: доля каждого 14.29% (ниже 15% — базовое состояние
    самосогласовано, иначе любой план был бы infeasible независимо от fit).
    """
    return [
        _held_row(
            f'{prefix}{n}', qty=value_qty, price=price,
            issuer=f'Базовый эмитент {n}',
        )
        for n in range(count)
    ]


class FakeFitterStorage:
    """In-memory двойник PortfolioStorage: контракт builder'а и движка."""

    def __init__(self, *, cash=CASH_KNOWN, held_rows, universe_rows, as_of=AS_OF):
        self.cash_data = dict(cash)
        self.held_rows = list(held_rows)
        self.universe_rows = list(universe_rows)
        self._as_of = as_of

    @contextlib.contextmanager
    def snapshot_transaction(self):
        yield self

    def get_transaction_timestamp(self):
        return {'ts': self._as_of, 'day': self._as_of.date()}

    def get_db_freshness(self, stale_threshold_hours=24, now=None):
        return dict(_FRESH_FRESHNESS)

    def get_available_cash_rub(self):
        return dict(self.cash_data)

    def get_rebalance_portfolio_rows(self):
        return [dict(row) for row in self.held_rows]

    def get_bonds_yield_table(self, **kwargs):
        return [dict(row) for row in self.universe_rows]

    def get_scenario_universe_rows(self, isins):
        by_isin = {row['isin']: row for row in self.universe_rows}
        return [dict(by_isin[isin]) for isin in isins if isin in by_isin]


def _make_fake(held_rows, universe_rows, cash=CASH_KNOWN):
    return FakeFitterStorage(
        cash=cash, held_rows=held_rows, universe_rows=universe_rows
    )


def _build_context(fake):
    return PlanningContextBuilder(fake).build()


def _filters(**overrides):
    params = dict(max_risk=3, max_list_level=3, max_ytm_pct=100.0,
                  include_ku=False)
    params.update(overrides)
    return CandidateFilters(**params)


class SpyEngine(RebalanceEngine):
    """Движок-шпион: фиксирует вызовы canonical валидации."""

    def __init__(self, db):
        super().__init__(db)
        self.calls = []

    def evaluate_scenario(self, trades, constraints=None, as_of_date=None):
        self.calls.append([dict(t) for t in trades])
        return super().evaluate_scenario(trades, constraints,
                                         as_of_date=as_of_date)


def _fit(fake, intents, constraints=None, filters=None):
    context = _build_context(fake)
    engine = SpyEngine(fake)
    fitter = AutoFitFitter(fake, context, engine=engine)
    plan = fitter.fit(
        intents,
        constraints if constraints is not None else PortfolioConstraints(),
        filters if filters is not None else _filters(),
    )
    return plan, context, engine


def _target(plan, isin):
    matches = [t for t in plan['targets'] if t.isin == isin]
    assert len(matches) == 1, isin
    return matches[0]


def _tv(isin, value, priority=1):
    return TargetValueIntent(
        isin=isin, target_value_rub=Decimal(str(value)), priority=priority
    )


# ---------------------------------------------------------------------------
# Money arithmetic: Decimal-домен, rounding policy
# ---------------------------------------------------------------------------


class TestMoneyArithmetic:
    def test_price_relay_is_exact_decimal_from_str(self):
        """Граница float→Decimal — только relay canonical planning_price."""
        assert _price_decimal(1103.06) == Decimal('1103.06')
        assert _price_decimal(970.8) == Decimal('970.8')
        # Не Decimal(float): бинарное представление дало бы хвост.
        assert _price_decimal(1103.06) != Decimal(1103.06)

    def test_floor_qty_on_planning_price(self):
        price = _price_decimal(1103.06)
        assert _floor_qty(Decimal('350000'), price) == 317
        # Точное равенство — граница включительно.
        assert _floor_qty(
            Decimal('317') * price, price
        ) == 317

    def test_ceil_steps_covers_amount_and_at_least_one(self):
        price = Decimal('1000')
        assert _ceil_steps(Decimal('1882.35'), price) == 2
        assert _ceil_steps(Decimal('0.01'), price) == 1
        assert _ceil_steps(Decimal('3000'), price) == 3

    def test_money_rounds_down_to_kopecks(self):
        assert _money(Decimal('349670.0199')) == Decimal('349670.01')
        assert _money(Decimal('100.999')) == Decimal('100.99')

    def test_sizing_inputs_are_decimal(self):
        """Деньги intents — Decimal (контракт 030.5), результат — Decimal."""
        fake = _make_fake(
            held_rows=_equal_base(),
            universe_rows=[_pool_row('RU000A0NEWP1', issuer='Эмитент P')],
        )
        plan, _, _ = _fit(fake, [_tv('RU000A0NEWP1', 2500)])
        target = _target(plan, 'RU000A0NEWP1')
        assert isinstance(target.requested_target_value_rub, Decimal)
        assert target.requested_target_value_rub == Decimal('2500')
        assert isinstance(target.fitted_target_value_rub, Decimal)
        assert isinstance(target.qty_after, int)
        assert isinstance(target.price, float)


# ---------------------------------------------------------------------------
# Quantity semantics: sizing, net SELL, sell_all, одна net-сделка
# ---------------------------------------------------------------------------

NEWP = 'RU000A0NEWP1'


class TestQuantitySemantics:
    def test_target_value_buy_qty_computed_from_planning_price(self):
        fake = _make_fake(
            held_rows=_equal_base(),
            universe_rows=[_pool_row(NEWP, issuer='Эмитент P')],
        )
        plan, context, _ = _fit(fake, [_tv(NEWP, 2500)])
        target = _target(plan, NEWP)
        assert target.status == TARGET_FITTED
        assert target.qty_before == 0
        assert target.qty_after == 2
        assert target.qty_delta == 2
        assert target.fitted_target_value_rub == Decimal('2000.00')
        assert target.fit_reasons == (FIT_REASON_INTEGER_QTY,)
        assert target.price == 1000.0
        assert target.price_basis == PLANNING_PRICE_BASIS_MARKET
        assert target.valuation_source is None  # покупка — не held
        # Каноническая цена контекста — та же, по которой считался sizing.
        assert context.effective_prices[NEWP] == {
            'price': 1000.0, 'basis': PLANNING_PRICE_BASIS_MARKET,
        }
        assert plan['trades'] == ({'isin': NEWP, 'qty_delta': 2},)
        assert plan['feasible'] is True

    def test_exact_target_has_no_integer_qty_reason(self):
        fake = _make_fake(
            held_rows=_equal_base(),
            universe_rows=[_pool_row(NEWP, issuer='Эмитент P')],
        )
        plan, _, _ = _fit(fake, [_tv(NEWP, 2000)])
        target = _target(plan, NEWP)
        assert target.qty_after == 2
        assert target.fit_reasons == ()

    def test_target_below_current_value_gives_net_sell(self):
        held = _equal_base(count=10, value_qty=10)
        isin = held[0]['isin']
        fake = _make_fake(held_rows=held, universe_rows=[])
        plan, _, _ = _fit(fake, [_tv(isin, 3500)])
        target = _target(plan, isin)
        assert target.qty_before == 10
        assert target.qty_after == 3
        assert target.qty_delta == -7
        assert target.price_basis == PLANNING_PRICE_BASIS_HELD_VALUATION
        assert plan['trades'] == ({'isin': isin, 'qty_delta': -7},)
        assert plan['feasible'] is True

    def test_sell_all_gives_final_qty_zero(self):
        held = _equal_base(count=10, value_qty=10)
        isin = held[3]['isin']
        fake = _make_fake(held_rows=held, universe_rows=[])
        plan, _, _ = _fit(fake, [SellAllIntent(isin=isin)])
        target = _target(plan, isin)
        assert target.status == TARGET_FITTED
        assert target.qty_after == 0
        assert target.qty_delta == -10
        assert target.requested_target_value_rub is None
        assert target.fitted_target_value_rub == Decimal('0.00')
        assert plan['trades'] == ({'isin': isin, 'qty_delta': -10},)

    def test_sell_all_on_matured_held_bond(self):
        held = _equal_base() + [
            _held_row(
                'RU000A0MATZ1', qty=10, price='1000',
                maturity=AS_OF_DATE - timedelta(days=1),
                issuer='Эмитент погашен', valuation_source='nominal_fallback',
            ),
        ]
        # Зомби-строка погашенной позиции: стоимость 0 (canonical view).
        held[-1]['value_rub'] = Decimal('0')
        fake = _make_fake(held_rows=held, universe_rows=[])
        plan, context, _ = _fit(fake, [SellAllIntent(isin='RU000A0MATZ1')])
        target = _target(plan, 'RU000A0MATZ1')
        assert target.qty_after == 0
        assert target.price_basis == PLANNING_PRICE_BASIS_MATURED_ZERO
        assert target.price == 0.0
        assert plan['trades'] == (
            {'isin': 'RU000A0MATZ1', 'qty_delta': -10},
        )
        assert plan['feasible'] is True

    def test_one_net_trade_per_isin(self):
        """Один high-level ISIN — одна normalized net trade (netting)."""
        held = _equal_base(count=10, value_qty=10)
        target_isin = held[2]['isin']
        sell_isin = held[4]['isin']
        fake = _make_fake(held_rows=held, universe_rows=[])
        plan, _, _ = _fit(fake, [
            _tv(target_isin, 3500),
            SellAllIntent(isin=sell_isin),
        ])
        isins = [t['isin'] for t in plan['trades']]
        assert isins == [target_isin, sell_isin]
        assert len(isins) == len(set(isins)) == 2

    def test_result_per_target_contract_keys(self):
        fake = _make_fake(
            held_rows=_equal_base(),
            universe_rows=[_pool_row(NEWP, issuer='Эмитент P')],
        )
        plan, _, _ = _fit(fake, [_tv(NEWP, 2500)])
        entry = _target(plan, NEWP).to_dict()
        assert set(entry.keys()) == {
            'isin', 'status', 'requested_target_value_rub',
            'fitted_target_value_rub', 'qty_before', 'qty_after',
            'qty_delta', 'price', 'price_basis', 'valuation_source',
            'fit_reasons', 'buy_reasons',
        }

    def test_held_output_carries_both_valuation_notions(self):
        """Held позиция: valuation_source И planning_price/basis (разные
        понятия) — оба в результате."""
        held = _equal_base(count=10, value_qty=10)
        isin = held[1]['isin']
        fake = _make_fake(
            held_rows=held,
            universe_rows=[_pool_row(isin, issuer=held[1]['issuer'])],
        )
        plan, _, _ = _fit(fake, [_tv(isin, 15000)])  # увеличение 10 → 15
        target = _target(plan, isin)
        assert target.valuation_source == 'market_price'
        assert target.price_basis == PLANNING_PRICE_BASIS_HELD_VALUATION
        assert target.price == 1000.0
        assert target.qty_after == 15  # 15% ровно не превышено (14.29%)

    def test_empty_intents_validate_do_nothing_plan(self):
        fake = _make_fake(held_rows=_equal_base(), universe_rows=[])
        plan, _, engine = _fit(fake, [])
        assert plan['targets'] == ()
        assert plan['trades'] == ()
        assert len(engine.calls) == 1  # финальная canonical валидация была
        assert plan['feasible'] is True


# ---------------------------------------------------------------------------
# Budget fit: lower-priority режется первым, сходимость
# ---------------------------------------------------------------------------


class TestBudgetFit:
    def _budget_fake(self, cash):
        return _make_fake(
            held_rows=_equal_base(),
            universe_rows=[
                _pool_row('RU000A0LOPW1', issuer='Эмитент LO'),
                _pool_row('RU000A0HIPW1', issuer='Эмитент HI'),
            ],
            cash=cash,
        )

    def test_lower_priority_buy_cut_first_on_budget_shortage(self):
        fake = self._budget_fake({'cash_available_rub': Decimal('3000.00'),
                                  'cash_known': True, 'cash_updated_at': None})
        plan, _, _ = _fit(fake, [
            _tv('RU000A0HIPW1', 2500, priority=1),
            _tv('RU000A0LOPW1', 2500, priority=2),
        ])
        hi = _target(plan, 'RU000A0HIPW1')
        lo = _target(plan, 'RU000A0LOPW1')
        # Higher priority цел; lower-priority урезан бюджетом.
        assert hi.qty_after == 2
        assert hi.fit_reasons == (FIT_REASON_INTEGER_QTY,)
        assert lo.qty_after == 1
        assert FIT_REASON_BUDGET_CAP in lo.fit_reasons
        assert plan['trades'] == (
            {'isin': 'RU000A0HIPW1', 'qty_delta': 2},
            {'isin': 'RU000A0LOPW1', 'qty_delta': 1},
        )
        assert plan['validation']['summary']['cash_after_estimate'] == 0.0
        assert plan['feasible'] is True

    def test_budget_cascade_converges_to_zero_increases(self):
        """Каскад: каждый шаг режет lower-priority BUY до пола, пока бюджет
        не сойдётся; план без сделок feasible (канонический green)."""
        fake = _make_fake(
            held_rows=_equal_base(),
            universe_rows=[
                _pool_row('RU000A0HIPW1', issuer='Эмитент HI'),
                _pool_row('RU000A0LOPW1', issuer='Эмитент LO'),
                _pool_row('RU000A0MIDW1', issuer='Эмитент MID'),
            ],
            cash={'cash_available_rub': Decimal('0.00'), 'cash_known': True,
                  'cash_updated_at': None},
        )
        plan, _, _ = _fit(fake, [
            _tv('RU000A0HIPW1', 3000, priority=1),
            _tv('RU000A0LOPW1', 3000, priority=2),
            _tv('RU000A0MIDW1', 3000, priority=3),
        ])
        assert [t.qty_after for t in plan['targets']] == [0, 0, 0]
        assert all(
            FIT_REASON_BUDGET_CAP in t.fit_reasons
            for t in plan['targets']
        )
        assert plan['trades'] == ()
        assert plan['feasible'] is True

    def test_sells_are_never_cut_by_budget(self):
        """Сокращения применяются до увеличений и бюджетом не режутся."""
        held = _equal_base() + [
            _held_row('RU000A0SELL1', qty=10, price='1000',
                      issuer='Эмитент S'),
        ]
        fake = _make_fake(
            held_rows=held,
            universe_rows=[_pool_row('RU000A0BUYW1', issuer='Эмитент B')],
            cash={'cash_available_rub': Decimal('0.00'), 'cash_known': True,
                  'cash_updated_at': None},
        )
        plan, _, _ = _fit(fake, [
            SellAllIntent(isin='RU000A0SELL1'),
            _tv('RU000A0BUYW1', 5000, priority=1),
        ])
        sell = _target(plan, 'RU000A0SELL1')
        buy = _target(plan, 'RU000A0BUYW1')
        assert sell.qty_after == 0
        assert buy.qty_after == 5  # 5000 из выручки продажи
        assert plan['validation']['summary']['cash_after_estimate'] == 5000.0


# ---------------------------------------------------------------------------
# Issuer fit: полный final state, без «headroom посчитанного один раз»
# ---------------------------------------------------------------------------


class TestIssuerFit:
    def _issuer_base(self):
        """10 эмитентов x 10000 (каждый ровно 10%): простор для проверок."""
        return _equal_base(count=10, value_qty=10, price='1000')

    def test_once_computed_headroom_is_insufficient_full_state_recalculates(self):
        """Наивный headroom (0.15*T0 - P0 = 5000 → 5 шт) недостаточен:
        после sell_all знаменатель сжимается, полный final state даёт 4."""
        held = self._issuer_base()
        # held[0] — эмитент P (докупаем), held[9] — эмитент A9 (продаём).
        p_held, a_held = held[0], held[9]
        fake = _make_fake(
            held_rows=held,
            universe_rows=[
                _pool_row('RU000A0PNEW1', issuer=p_held['issuer']),
            ],
        )
        naive_engine = RebalanceEngine(fake)
        naive = naive_engine.evaluate_scenario(
            [
                {'isin': a_held['isin'], 'qty_delta': -10.0},
                {'isin': 'RU000A0PNEW1', 'qty_delta': 5.0},
            ],
            as_of_date=AS_OF_DATE,
        )
        assert naive['issuer_limits']['has_violations'] is True  # 5 шт — violation

        plan, _, _ = _fit(
            fake,
            [SellAllIntent(isin=a_held['isin']),
             _tv('RU000A0PNEW1', 6000, priority=1)],
        )
        target = _target(plan, 'RU000A0PNEW1')
        assert target.qty_after == 4
        assert FIT_REASON_ISSUER_CAP in target.fit_reasons
        assert plan['feasible'] is True
        assert plan['validation']['constraint_checks'] == []

    def test_same_issuer_buys_cut_lower_priority_first(self):
        held = self._issuer_base()
        p_held = held[0]
        fake = _make_fake(
            held_rows=held,
            universe_rows=[
                _pool_row('RU000A0PHIP1', issuer=p_held['issuer']),
                _pool_row('RU000A0PLOP1', issuer=p_held['issuer']),
            ],
        )
        plan, _, _ = _fit(fake, [
            _tv('RU000A0PHIP1', 3000, priority=1),
            _tv('RU000A0PLOP1', 3000, priority=2),
        ])
        hi = _target(plan, 'RU000A0PHIP1')
        lo = _target(plan, 'RU000A0PLOP1')
        assert hi.qty_after == 3
        assert lo.qty_after == 2
        assert FIT_REASON_ISSUER_CAP in lo.fit_reasons
        assert FIT_REASON_ISSUER_CAP not in hi.fit_reasons
        assert plan['feasible'] is True

    def test_untouched_holding_violation_is_infeasible_not_auto_sold(self):
        """Violation от untouched holding не исправляется автопродажей."""
        held = self._issuer_base() + [
            _held_row('RU000A0BIGP1', qty=20, price='1000',
                      issuer='Большой эмитент'),
        ]
        fake = _make_fake(
            held_rows=held,
            universe_rows=[
                _pool_row('RU000A0OKNW1', issuer='Нормальный эмитент'),
            ],
        )
        plan, _, _ = _fit(fake, [_tv('RU000A0OKNW1', 1000, priority=1)])
        # Нарушение эмитента осталось (untouched), план infeasible.
        assert plan['feasible'] is False
        types = [v['type'] for v in plan['validation']['constraint_checks']]
        assert CONSTRAINT_ISSUER_LIMIT in types
        # Автопродажи нет: сделка только запрошенная, BIG не тронут.
        assert plan['trades'] == ({'isin': 'RU000A0OKNW1', 'qty_delta': 1},)
        big = [p for p in plan['validation']['positions_after']
               if p['isin'] == 'RU000A0BIGP1']
        assert big and big[0]['qty'] == 20.0

    def test_issuer_violation_from_buy_of_new_issuer_is_cut(self):
        held = self._issuer_base()
        fake = _make_fake(
            held_rows=held,
            universe_rows=[
                _pool_row('RU000A0WIDE1', issuer='Широкий эмитент'),
            ],
        )
        plan, _, _ = _fit(fake, [_tv('RU000A0WIDE1', 20000, priority=1)])
        target = _target(plan, 'RU000A0WIDE1')
        # 20000/120000 = 16.7% → урезано до допустимой доли: 17шт → 14.29%.
        assert target.qty_after == 17
        assert FIT_REASON_ISSUER_CAP in target.fit_reasons
        assert plan['feasible'] is True


# ---------------------------------------------------------------------------
# Unknown cash: не 0, feasible=null
# ---------------------------------------------------------------------------


class TestUnknownCash:
    def test_unknown_cash_not_zero_feasible_null_no_budget_cuts(self):
        fake = _make_fake(
            held_rows=_equal_base(),
            universe_rows=[_pool_row(NEWP, issuer='Эмитент P')],
            cash=CASH_UNKNOWN,
        )
        plan, _, _ = _fit(fake, [_tv(NEWP, 5000, priority=1)])
        target = _target(plan, NEWP)
        # Sizing по target состоялся; бюджетные урезания не проводились.
        assert target.qty_after == 5
        assert FIT_REASON_BUDGET_CAP not in target.fit_reasons
        assert plan['validation']['budget_check'] == {
            'known': False, 'passed': None,
        }
        assert plan['validation']['summary']['cash_before'] is None
        assert plan['validation']['feasible'] is None
        assert plan['feasible'] is None


# ---------------------------------------------------------------------------
# Eligibility / price policy: без молчаливой подгонки
# ---------------------------------------------------------------------------


class TestEligibilityAndPricePolicy:
    def test_ineligible_buy_rejected_with_reasons_no_leg(self):
        fake = _make_fake(
            held_rows=_equal_base(),
            universe_rows=[
                _pool_row('RU000A0KUBD1', issuer='Эмитент KU', ku=True),
            ],
        )
        plan, _, _ = _fit(fake, [_tv('RU000A0KUBD1', 5000, priority=1)])
        target = _target(plan, 'RU000A0KUBD1')
        assert target.status == BUY_NOT_ELIGIBLE
        assert [(r.code, r.actual, r.limit) for r in target.buy_reasons] == [
            ('ku', True, False),
        ]
        assert target.qty_after == 0
        assert plan['trades'] == ()  # BUY-ноги в плане нет
        assert plan['feasible'] is True  # план без неё валиден

    def test_eligible_with_include_ku_filter(self):
        fake = _make_fake(
            held_rows=_equal_base(),
            universe_rows=[
                _pool_row('RU000A0KUBD1', issuer='Эмитент KU', ku=True),
            ],
        )
        plan, _, _ = _fit(
            fake, [_tv('RU000A0KUBD1', 5000, priority=1)],
            filters=_filters(include_ku=True),
        )
        target = _target(plan, 'RU000A0KUBD1')
        assert target.status == TARGET_FITTED
        assert target.qty_after == 5

    def test_held_isin_outside_buy_pool_is_not_eligible(self):
        """Held-ISIN вне canonical buy-пула: увеличение отклонено (NOT_IN_
        BUY_POOL), сокращение того же ISIN разрешено без eligibility."""
        held = _equal_base(count=10, value_qty=10) + [
            _held_row('RU000A0STALE1', qty=5, price='900',
                      issuer='Эмитент вне пула'),
        ]
        fake = _make_fake(held_rows=held, universe_rows=[])
        plan, _, _ = _fit(fake, [_tv('RU000A0STALE1', 9000, priority=1)])
        target = _target(plan, 'RU000A0STALE1')
        assert target.status == BUY_NOT_ELIGIBLE
        assert [(r.code, r.actual, r.limit) for r in target.buy_reasons] == [
            (REASON_NOT_IN_BUY_POOL, None, None),
        ]
        assert plan['trades'] == ()

        plan2, _, _ = _fit(fake, [SellAllIntent(isin='RU000A0STALE1')])
        sell = _target(plan2, 'RU000A0STALE1')
        assert sell.status == TARGET_FITTED
        assert sell.qty_delta == -5  # sell/reduce без eligibility

    def test_nominal_fallback_buy_sizing_forbidden_price_unavailable(self):
        """BUY sizing по номиналу запрещён в high-level path (explicit basis
        в эхе), sell_all той же позиции работает."""
        held = _equal_base(count=10, value_qty=10) + [
            _held_row('RU000A0NOMF1', qty=5, price='1000',
                      issuer='Эмитент номинал', market_price=None),
        ]
        held[-1]['value_rub'] = Decimal('0')  # оценка view недоступна
        fake = _make_fake(held_rows=held, universe_rows=[])
        plan, _, _ = _fit(fake, [_tv('RU000A0NOMF1', 10000, priority=1)])
        target = _target(plan, 'RU000A0NOMF1')
        assert target.status == TARGET_PRICE_UNAVAILABLE
        assert target.price_basis == PLANNING_PRICE_BASIS_NOMINAL_FALLBACK
        assert target.price == 1000.0
        assert target.fitted_target_value_rub is None
        assert plan['trades'] == ()

        plan2, _, _ = _fit(fake, [SellAllIntent(isin='RU000A0NOMF1')])
        sell = _target(plan2, 'RU000A0NOMF1')
        assert sell.status == TARGET_FITTED
        assert sell.qty_delta == -5

    def test_unknown_isin_reported_and_other_targets_fitted(self):
        fake = _make_fake(
            held_rows=_equal_base(),
            universe_rows=[_pool_row(NEWP, issuer='Эмитент P')],
        )
        plan, _, _ = _fit(fake, [
            _tv('RU000A0MISS9', 5000, priority=2),
            _tv(NEWP, 2000, priority=1),
        ])
        missing = _target(plan, 'RU000A0MISS9')
        assert missing.status == TARGET_UNKNOWN_ISIN
        assert missing.price is None
        assert missing.qty_delta == 0
        assert [d['code'] for d in plan['diagnostics']] == [
            TARGET_UNKNOWN_ISIN,
        ]
        fitted = _target(plan, NEWP)
        assert fitted.qty_after == 2
        assert plan['feasible'] is True

    def test_position_cap_presizes_requested_qty(self):
        fake = _make_fake(
            held_rows=_equal_base(),
            universe_rows=[_pool_row(NEWP, issuer='Эмитент P')],
        )
        plan, _, _ = _fit(
            fake, [_tv(NEWP, 5000, priority=1)],
            constraints=PortfolioConstraints(
                max_position_value_rub=Decimal('2500')
            ),
        )
        target = _target(plan, NEWP)
        assert target.requested_target_value_rub == Decimal('5000')
        assert target.qty_after == 2
        assert FIT_REASON_POSITION_CAP in target.fit_reasons
        assert target.fitted_target_value_rub == Decimal('2000.00')


# ---------------------------------------------------------------------------
# Price consistency regression (951.30 vs 1103.06 / 970.80)
# ---------------------------------------------------------------------------


class TestPriceConsistencyRegression:
    def _regression_fake(self, market_price):
        """Портфель 7 x 300000 (каждый ~14%) + большой cash: BUY 350k
        укладывается и в бюджет, и в лимит эмитента."""
        held = _equal_base(count=7, value_qty=300, price='1000')
        return _make_fake(
            held_rows=held,
            universe_rows=[
                _pool_row('RU000A0ZREG1', issuer='Эмитент Z',
                          market_price=market_price),
            ],
            cash={'cash_available_rub': Decimal('400000.00'),
                  'cash_known': True, 'cash_updated_at': None},
        )

    def _cap(self):
        return PortfolioConstraints(max_position_value_rub=Decimal('350000'))

    def test_qty_computed_from_planning_price_not_report_price(self):
        """Report price 951.30 vs planning_price 1103.06, target 350000:
        qty считается по planning_price (317, не 367), calculator и
        validator на одной цене/basis, один run зелёный."""
        fake = self._regression_fake('1103.06')
        plan, context, _ = _fit(
            fake, [_tv('RU000A0ZREG1', 350000, priority=1)],
            constraints=self._cap(),
        )
        target = _target(plan, 'RU000A0ZREG1')
        price = Decimal('1103.06')
        assert target.price == 1103.06
        assert target.price_basis == PLANNING_PRICE_BASIS_MARKET
        assert target.qty_after == _floor_qty(Decimal('350000'), price) == 317
        # Не qty по цене отчёта 951.30 (floor = 367).
        assert target.qty_after != _floor_qty(Decimal('350000'), Decimal('951.30'))
        assert target.fitted_target_value_rub == Decimal('317') * price
        assert target.fitted_target_value_rub <= Decimal('350000')
        assert plan['trades'] == (
            {'isin': 'RU000A0ZREG1', 'qty_delta': 317},
        )
        # Calculator и validator — одна цена/basis, нарушений нет,
        # второй run с ручным decrement не нужен.
        assert plan['validation']['planning_prices']['RU000A0ZREG1'] == {
            'price': 1103.06, 'basis': PLANNING_PRICE_BASIS_MARKET,
        }
        assert plan['validation']['constraint_checks'] == []
        assert plan['validation']['feasible'] is True
        assert plan['price_consistent'] is True
        assert plan['feasible'] is True

    def test_invariant_with_planning_price_970_80(self):
        """Повтор с planning_price 970.80: проверяется инвариант
        (fitted <= target < (qty+1)*price), не конкретное qty."""
        fake = self._regression_fake('970.80')
        plan, _, _ = _fit(
            fake, [_tv('RU000A0ZREG1', 350000, priority=1)],
            constraints=self._cap(),
        )
        target = _target(plan, 'RU000A0ZREG1')
        price = _price_decimal(target.price)
        assert price == Decimal('970.8')
        qty = Decimal(target.qty_after)
        assert target.fitted_target_value_rub <= Decimal('350000')
        assert (qty + 1) * price > Decimal('350000')
        assert plan['feasible'] is True

    def test_context_price_mismatch_blocks_plan(self):
        """Срез уехал: canonical цена валидации != цене контекста →
        диагностика, price_consistent=false, feasible=null."""
        fake = _make_fake(
            held_rows=_equal_base(count=10, value_qty=10), universe_rows=[]
        )
        context = _build_context(fake)
        isin = fake.held_rows[0]['isin']
        context.effective_prices[isin]['price'] = 2000.0  # срез «уехал»
        engine = SpyEngine(fake)
        plan = AutoFitFitter(fake, context, engine=engine).fit(
            [_tv(isin, 3500, priority=1)]
        )
        assert plan['price_consistent'] is False
        assert [d['code'] for d in plan['diagnostics']] == [
            DIAG_CONTEXT_PRICE_MISMATCH,
        ]
        assert plan['validation']['feasible'] is True  # сам сценарий валиден
        assert plan['feasible'] is None  # …но план по уехавшей цене не выдаётся


# ---------------------------------------------------------------------------
# Детерминизм и canonical validator
# ---------------------------------------------------------------------------


class TestDeterminismAndCanonicalValidation:
    def test_same_context_gives_identical_result(self):
        fake = _make_fake(
            held_rows=_equal_base(count=10, value_qty=10),
            universe_rows=[
                _pool_row('RU000A0PNEW1', issuer='Базовый эмитент 0'),
                _pool_row('RU000A0LOPW1', issuer='Эмитент LO'),
            ],
            cash={'cash_available_rub': Decimal('3000.00'),
                  'cash_known': True, 'cash_updated_at': None},
        )
        intents = [
            SellAllIntent(isin=fake.held_rows[9]['isin']),
            _tv('RU000A0PNEW1', 6000, priority=1),
            _tv('RU000A0LOPW1', 2500, priority=2),
        ]

        def run():
            context = _build_context(fake)
            engine = SpyEngine(fake)
            return AutoFitFitter(fake, context, engine=engine).fit(
                intents, filters=_filters()
            )

        first, second = run(), run()
        assert first['targets'] == second['targets']
        assert first['trades'] == second['trades']
        assert first['feasible'] == second['feasible']
        assert first['validation'] == second['validation']
        assert (first['context_fingerprint']
                == second['context_fingerprint'])

    def test_final_canonical_validator_called_after_fit(self):
        """Fit-цикл валидируется ТОЛЬКО движком: spy фиксирует вызовы
        evaluate_scenario, результат — его канонический ответ."""
        fake = _make_fake(
            held_rows=_equal_base(),
            universe_rows=[
                _pool_row('RU000A0LOPW1', issuer='Эмитент LO'),
                _pool_row('RU000A0HIPW1', issuer='Эмитент HI'),
            ],
            cash={'cash_available_rub': Decimal('3000.00'),
                  'cash_known': True, 'cash_updated_at': None},
        )
        plan, _, engine = _fit(fake, [
            _tv('RU000A0HIPW1', 2500, priority=1),
            _tv('RU000A0LOPW1', 2500, priority=2),
        ])
        # 1 (начальная) + 1 (после budget-урезания) — каждая на полном
        # final state; финальная валидация = результат последнего вызова.
        assert len(engine.calls) == 2
        assert engine.calls[-1] == [
            {'isin': 'RU000A0HIPW1', 'qty_delta': 2.0},
            {'isin': 'RU000A0LOPW1', 'qty_delta': 1.0},
        ]
        assert plan['validation']['summary']['cash_after_estimate'] == 0.0
        assert plan['validation']['feasible'] is True

    def test_raw_and_duplicate_intents_rejected_loudly(self):
        fake = _make_fake(held_rows=_equal_base(), universe_rows=[])
        context = _build_context(fake)
        fitter = AutoFitFitter(fake, context)
        with pytest.raises(TypeError):
            fitter.fit([{'type': 'sell_all', 'isin': 'RU000A0BASE0'}])
        with pytest.raises(TypeError):
            fitter.fit('sell_all')
        with pytest.raises(ValueError):
            fitter.fit([_tv('RU000A0BASE0', 1000, priority=1),
                        _tv('RU000A0BASE0', 2000, priority=2)])
