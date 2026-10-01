"""
Тесты rebalance-plan CLI — high-level планирование одной командой (030.8).

Проверяются требования файла задачи 030.8:
- report → plan: canonical valid/feasible без scenario.json и qty_delta от
  агента (use case — тонкая композиция builder/store/fitter/engine);
- sales + purchases отражены в budget summary; known-cash feasible план
  имеет cash_after_estimate >= 0;
- финальные позиции проходят canonical constraints (re-validate движком);
- target CSV пишется существующим canonical writer'ом без post-processing;
- compact views соответствуют full result (срезы одного payload'а);
- duplicate/conflict/invalid intents — machine-readable diagnostics без
  traceback и без постройки плана;
- --context-id загружает тот же core-owned context (030.4), ошибки CONTEXT_*
  машиночитаемы; priority назначается на границе CLI из порядка
  повторения --target-value (первый = 1 = самый приоритетный);
- legacy `rebalance-report --scenario` работает без изменения контракта
  (additive-правило 030 №2).

Все данные — синтетические (in-memory двойник PortfolioStorage + реальный
PlanningContextBuilder/RebalanceEngine/AutoFitFitter), реальные составы
портфеля и ISIN не используются (AGENTS.md); тесты не требуют
POSTGRES_DSN_TEST. Эфемерный store — во временный каталог (030.4).
"""
import argparse
import contextlib
import csv
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.services.context_store import EphemeralContextStore
from src.services.planning_context import PlanningContextBuilder
from src.services.rebalance_engine import (
    SCENARIO_ERROR_UNKNOWN_ISIN,
    SCENARIO_POSITION_KEYS,
    SCENARIO_SUMMARY_KEYS,
    SCENARIO_TRADE_KEYS,
    RebalanceEngine,
)
from src.use_cases.rebalance_report import RebalanceReportUseCase
from src.use_cases.rebalance_plan import (
    PLAN_SCHEMA_VERSION,
    VIEW_CHOICES,
    RebalancePlanUseCase,
)
from test_analysis_snapshot import _FRESH_FRESHNESS

AS_OF = datetime(2025, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
AS_OF_DATE = AS_OF.date()

# 7 базовых эмитентов x 12000 = 84000: доля каждого 14.29% (ниже 15%);
# cash 50000 — известный (feasible-ось определена).
BASE_PREFIX = 'RU000A0BASE'
NEW1 = 'RU000A0NEW01'   # канонический buy-пул, покупка до target 12000
HELD_NP = 'RU000A0NOTPL'  # в портфеле, ВНЕ buy-пула (NOT_IN_BUY_POOL для BUY)

RATING_SCALE = {'AA-.RU': 10, 'A.RU': 8}


# ---------------------------------------------------------------------------
# Синтетические canonical строки (форма строк БД: деньги — Decimal)
# ---------------------------------------------------------------------------


def _catalog_fields(
    isin,
    *,
    issuer=None,
    market_price='1000',
    coupon='10.0',
    coupon_freq=4,
    maturity=date(2027, 6, 15),
    risk_level=1,
    list_level=1,
    credit_rating='A',
    rating_score=8,
):
    return {
        'isin': isin,
        'ticker': isin[-5:],
        'name': 'Бумага ' + isin,
        'nominal': Decimal('1000'),
        'coupon_rate_percent': Decimal(coupon),
        'coupon_quantity_per_year': coupon_freq,
        'maturity_date': maturity,
        'offer_date': None,
        'risk_level': risk_level,
        'list_level': list_level,
        'amortization_flag': False,
        'floating_coupon_flag': False,
        'perpetual_flag': False,
        'ku': False,
        'market_price': (
            Decimal(market_price) if market_price is not None else None
        ),
        'currency': 'rub',
        'is_trade_available': True,
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


def _pool_row(isin, *, held_share_pct=None, **catalog):
    """Строка canonical raw buy-пула (алиас current_price, как в buy-SQL)."""
    row = _catalog_fields(isin, **catalog)
    row['current_price'] = row['market_price']
    row['held_share_pct'] = held_share_pct
    return row


def _base_held():
    """7 базовых позиций: 7 x 12000, каждая 14.29% (< 15% лимита эмитента)."""
    return [
        _held_row(
            f'{BASE_PREFIX}{n}', qty=12, price='1000',
            issuer=f'Базовый эмитент {n}',
        )
        for n in range(7)
    ]


def _base_universe(with_new1=True, with_base_pool=True):
    """Buy-пул: базовые позиции (held_share_pct) + опционально NEW1."""
    rows = []
    if with_base_pool:
        rows += [
            _pool_row(
                f'{BASE_PREFIX}{n}', market_price='1000',
                issuer=f'Базовый эмитент {n}',
                held_share_pct=Decimal('14.29'),
            )
            for n in range(7)
        ]
    if with_new1:
        rows.append(_pool_row(NEW1, market_price='1000',
                              issuer='Новый эмитент 1'))
    return rows


_CASH_KNOWN = {
    'cash_available_rub': Decimal('50000.00'),
    'cash_known': True,
    'cash_updated_at': AS_OF - timedelta(minutes=10),
}


class FakePlanStorage:
    """In-memory двойник PortfolioStorage: контракт builder'а, движка,
    fitter'а и legacy отчёта."""

    def __init__(self, *, cash=_CASH_KNOWN, held_rows, universe_rows):
        self.cash_data = dict(cash)
        self.held_rows = list(held_rows)
        self.universe_rows = list(universe_rows)

    @contextlib.contextmanager
    def snapshot_transaction(self):
        yield self

    def get_transaction_timestamp(self):
        return {'ts': AS_OF, 'day': AS_OF_DATE}

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

    def get_portfolio_redemption_events(self, months):
        return []


def _make_fake(*extra_held, universe_rows=None, cash=_CASH_KNOWN,
               held_rows=None):
    return FakePlanStorage(
        cash=cash,
        held_rows=(
            list(held_rows) if held_rows is not None
            else _base_held() + list(extra_held)
        ),
        universe_rows=_base_universe() if universe_rows is None else universe_rows,
    )


def _make_use_case(fake, tmp_path, rating_scale=None):
    store = EphemeralContextStore(
        root_dir=tmp_path / 'planning_contexts',
        scope_digest='test-scope',
        rating_scale=rating_scale or {},
    )
    return RebalancePlanUseCase(
        db=fake, rating_scale=rating_scale or {}, store=store
    )


def _args(**overrides):
    defaults = {
        'freq_min': None,
        'freq_max': None,
        'min_credit_rating': None,
        'exclude_sovereign': False,
        'max_position_value': None,
        'sell_all': None,
        'target_value': None,
        'context_id': None,
        'view': 'full',
        'format': 'json',
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# Канонический план задачи: продать BASE0 (12000), купить NEW1 до 12000.
# Итог: 6 x 12000 + NEW1 12000 = 84000, каждая доля 14.29%, cash 50000.
_SELL_BASE0 = [f'{BASE_PREFIX}0']
_TARGET_NEW1 = [f'{NEW1}=12000']


@pytest.fixture
def plan(tmp_path):
    """Полный план на синтетическом срезе + его fake-хранилище."""
    fake = _make_fake()
    use_case = _make_use_case(fake, tmp_path)
    payload = use_case.build_plan(
        _args(sell_all=list(_SELL_BASE0), target_value=list(_TARGET_NEW1))
    )
    return payload, fake, use_case


# ---------------------------------------------------------------------------
# CLI-проводка: парсер, priority из порядка --target-value, фабрика
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_setup_parser_defaults(self):
        parser = argparse.ArgumentParser(add_help=False)
        RebalancePlanUseCase.setup_parser(parser)
        args = parser.parse_args([])
        assert args.freq_min is None
        assert args.freq_max is None
        assert args.min_credit_rating is None
        assert args.exclude_sovereign is False
        assert args.max_position_value is None
        assert args.sell_all is None
        assert args.target_value is None
        assert args.context_id is None
        assert args.view == 'full'
        assert args.format == 'json'

    def test_repeated_flags_accumulate_in_order(self):
        parser = argparse.ArgumentParser(add_help=False)
        RebalancePlanUseCase.setup_parser(parser)
        args = parser.parse_args([
            '--sell-all', 'RU1', '--sell-all', 'RU2',
            '--target-value', 'RU3=350000', '--target-value', 'RU4=200000',
        ])
        # Повторяемые флаги аккумулируются в порядке вызова.
        assert args.sell_all == ['RU1', 'RU2']
        assert args.target_value == ['RU3=350000', 'RU4=200000']

    def test_target_value_order_becomes_priority(self, tmp_path):
        """Priority присваивается НА границе CLI: первый --target-value = 1
        (меньше = выше allocation priority, контракт 030.5)."""
        fake = _make_fake()
        use_case = _make_use_case(fake, tmp_path)
        payload = use_case.build_plan(_args(
            target_value=[f'{NEW1}=12000', f'{BASE_PREFIX}1=13000'],
        ))
        intents = payload['intents']
        assert [i['priority'] for i in intents] == [1, 2]
        assert [i['isin'] for i in intents] == [NEW1, f'{BASE_PREFIX}1']
        assert intents[0]['target_value_rub'] == '12000'

    def test_view_choices_and_default(self):
        assert VIEW_CHOICES == (
            'summary', 'targets', 'trades', 'positions-after', 'violations',
            'full',
        )

    def test_command_registered_in_factory_map(self):
        from src.use_cases.factory import UseCaseFactory

        factory = UseCaseFactory.__new__(UseCaseFactory)
        use_case_map = UseCaseFactory.get_use_case_map(factory)
        assert use_case_map['rebalance-plan'] is RebalancePlanUseCase
        # Additive: существующие команды не тронуты.
        assert 'rebalance-report' in use_case_map
        assert 'rebalance-analysis-snapshot' in use_case_map
        assert len(use_case_map) == 39  # + rebalance-compare (030.9)

    def test_constraint_flag_validation_value_errors(self, tmp_path):
        """Неверная комбинация флагов-ограничений — ValueError по конвенции
        legacy отчёта (шкала рейтингов из конфига)."""
        fake = _make_fake()
        use_case = _make_use_case(fake, tmp_path, rating_scale=RATING_SCALE)
        with pytest.raises(ValueError, match='--freq-min'):
            use_case.build_plan(_args(freq_min=0))
        with pytest.raises(ValueError, match='--freq-min'):
            use_case.build_plan(_args(freq_min=12, freq_max=4))
        with pytest.raises(ValueError, match='--max-position-value'):
            use_case.build_plan(_args(max_position_value=0))
        with pytest.raises(ValueError, match='--min-credit-rating'):
            use_case.build_plan(_args(min_credit_rating='ZZZ'))
        # Валидный код шкалы проходит.
        use_case.build_plan(
            _args(min_credit_rating='AA-', target_value=list(_TARGET_NEW1))
        )


# ---------------------------------------------------------------------------
# report → plan: canonical valid/feasible (standard path, 2 domain-вызова)
# ---------------------------------------------------------------------------


class TestReportThenPlan:
    def test_report_then_plan_valid_feasible(self, tmp_path):
        """report → plan на одном срезе: оба canonical; план структурно
        валиден и осуществим без scenario.json/qty_delta от агента."""
        fake = _make_fake()
        report = RebalanceReportUseCase(fake).build_report(
            RebalanceReportUseCase.default_args()
        )
        assert report['schema_version'] == 2
        assert 'scenario' not in report  # базовый отчёт без сценария

        use_case = _make_use_case(fake, tmp_path)
        payload = use_case.build_plan(
            _args(sell_all=list(_SELL_BASE0), target_value=list(_TARGET_NEW1))
        )
        assert payload['artifact'] == 'rebalance_plan'
        assert payload['plan_schema_version'] == PLAN_SCHEMA_VERSION
        assert payload['valid'] is True
        assert payload['feasible'] is True
        assert payload['price_consistent'] is True
        assert payload['diagnostics'] == []

    def test_execute_prints_valid_json(self, tmp_path, capsys):
        use_case = _make_use_case(_make_fake(), tmp_path)
        use_case.execute(_args(
            sell_all=list(_SELL_BASE0), target_value=list(_TARGET_NEW1)
        ))
        payload = json.loads(capsys.readouterr().out)
        assert payload['artifact'] == 'rebalance_plan'
        assert payload['feasible'] is True


# ---------------------------------------------------------------------------
# Budget summary и известный cash
# ---------------------------------------------------------------------------


class TestBudgetSummary:
    def test_sales_and_purchases_in_budget_summary(self, plan):
        """Продажи и покупки ног отражены в summary (деньги — оценки движка:
        qty * planning_price)."""
        payload, _, _ = plan
        summary = payload['summary']
        assert summary['sales_value'] == 12000.0
        assert summary['purchases_value'] == 12000.0
        assert summary['portfolio_before'] == 84000.0
        assert summary['portfolio_after'] == 84000.0
        assert summary['cash_after_estimate'] == 50000.0

        actions = {
            (t['isin'], t['action']): t['value_delta']
            for t in payload['trades']
        }
        assert actions[(f'{BASE_PREFIX}0', 'sell')] == -12000.0
        assert actions[(NEW1, 'buy')] == 12000.0

    def test_known_cash_feasible_plan_has_nonnegative_cash_after(self, plan):
        payload, _, _ = plan
        assert payload['feasible'] is True
        assert payload['summary']['cash_after_estimate'] >= 0
        assert payload['budget_check'] == {'known': True, 'passed': True}


# ---------------------------------------------------------------------------
# Финальные позиции проходят canonical constraints
# ---------------------------------------------------------------------------


class TestFinalPositionsCanonical:
    def test_no_violations_and_engine_revalidate_passes(self, plan):
        """constraint_checks пуст; повторная canonical валидация движком на
        сделках плана (те же constraints, frozen as_of) — feasible."""
        payload, fake, _ = plan
        assert payload['violations'] == []
        assert payload['valid'] is True

        engine = RebalanceEngine(fake)
        trades = [
            {'isin': t['isin'], 'qty_delta': t['qty_delta']}
            for t in payload['trades']
        ]
        from src.services.planning_intents import PortfolioConstraints
        revalidated = engine.evaluate_scenario(
            trades,
            PortfolioConstraints(freq_min=4, freq_max=12)
            .to_scenario_constraints(),
            as_of_date=AS_OF_DATE,
        )
        assert revalidated['valid'] is True
        assert revalidated['feasible'] is True
        assert revalidated['constraint_checks'] == []

    def test_target_portfolio_rows_shape(self, plan):
        """positions_after — canonical строки ядра (BOND_ROW_KEYS +
        coupon_month), как у scenario.positions_after отчёта."""
        payload, _, _ = plan
        positions = payload['positions_after']
        assert len(positions) == 7  # 6 базовых + NEW1
        for pos in positions:
            assert set(pos) == set(SCENARIO_POSITION_KEYS)
        values = {pos['isin']: pos['value'] for pos in positions}
        assert values[NEW1] == 12000.0
        # Проданная позиция (итог 0) в целевом портфеле отсутствует.
        assert all(
            pos['isin'] != f'{BASE_PREFIX}0' for pos in positions
        )

    def test_target_csv_via_canonical_writer(self, plan, tmp_path):
        """CSV целевого портфеля пишется существующим canonical writer'ом
        отчёта без post-processing (файл задачи)."""
        payload, _, _ = plan
        path = tmp_path / 'plan_target.csv'
        written = RebalanceReportUseCase._write_target_csv(
            str(path),
            payload['positions_after'],
            payload['summary']['portfolio_after'],
        )
        assert written == str(path)
        with open(path, encoding='utf-8-sig') as f:
            rows = list(csv.reader(f))
        assert rows[0][0] == 'ISIN'
        # Позиции целевого портфеля + строка ИТОГО.
        assert len(rows) == len(payload['positions_after']) + 2
        assert rows[-1][0] == 'ИТОГО'
        assert float(rows[-1][6]) == payload['summary']['portfolio_after']


# ---------------------------------------------------------------------------
# Compact views соответствуют full result
# ---------------------------------------------------------------------------


class TestViewsMatchFull:
    def test_every_view_is_subset_of_full(self, tmp_path):
        fake = _make_fake()
        use_case = _make_use_case(fake, tmp_path)
        full = use_case.build_plan(
            _args(sell_all=list(_SELL_BASE0), target_value=list(_TARGET_NEW1))
        )
        for view in VIEW_CHOICES:
            part = use_case.build_plan(
                _args(
                    sell_all=list(_SELL_BASE0),
                    target_value=list(_TARGET_NEW1),
                    view=view,
                )
            )
            assert part['view'] == view
            # Envelope одинаков (идентификация среза + вердикт).
            for key in (
                'artifact', 'plan_schema_version', 'context_fingerprint',
                'as_of', 'data_gate', 'feasible', 'valid',
                'price_consistent', 'diagnostics',
            ):
                assert part[key] == full[key], (view, key)
            # Блоки view — те же объекты данных, что в full.
            for block in (
                'summary', 'budget_check', 'targets', 'trades',
                'positions_after', 'violations',
            ):
                if block in part:
                    assert part[block] == full[block], (view, block)

    def test_compact_views_do_not_leak_other_blocks(self, tmp_path):
        use_case = _make_use_case(_make_fake(), tmp_path)
        args_kwargs = dict(sell_all=list(_SELL_BASE0), target_value=list(_TARGET_NEW1))
        assert 'targets' not in use_case.build_plan(_args(view='summary', **args_kwargs))
        assert 'summary' not in use_case.build_plan(_args(view='targets', **args_kwargs))
        assert 'positions_after' not in use_case.build_plan(
            _args(view='trades', **args_kwargs)
        )
        assert 'targets' not in use_case.build_plan(
            _args(view='positions-after', **args_kwargs)
        )
        assert 'trades' not in use_case.build_plan(
            _args(view='violations', **args_kwargs)
        )


# ---------------------------------------------------------------------------
# Machine-readable diagnostics: duplicate/conflict/invalid
# ---------------------------------------------------------------------------


class TestInvalidIntentDiagnostics:
    def test_duplicate_target_is_machine_readable(self, tmp_path):
        use_case = _make_use_case(_make_fake(), tmp_path)
        payload = use_case.build_plan(_args(
            target_value=[f'{NEW1}=12000', f'{NEW1}=20000'],
        ))
        codes = [d['code'] for d in payload['diagnostics']]
        assert codes == ['DUPLICATE_TARGET']
        assert payload['diagnostics'][0]['isin'] == NEW1
        assert payload['diagnostics'][0]['message']
        # План не построен; вердикт — честные null.
        assert payload['feasible'] is None
        assert payload['valid'] is None
        assert 'targets' not in payload
        assert 'summary' not in payload

    def test_conflicting_intent_is_machine_readable(self, tmp_path):
        use_case = _make_use_case(_make_fake(), tmp_path)
        payload = use_case.build_plan(_args(
            sell_all=[NEW1], target_value=[f'{NEW1}=12000'],
        ))
        codes = [d['code'] for d in payload['diagnostics']]
        assert codes == ['CONFLICTING_INTENT']
        assert payload['feasible'] is None

    def test_invalid_target_value_forms(self, tmp_path):
        use_case = _make_use_case(_make_fake(), tmp_path)
        for spec, isin in (
            ('NEW1=abc', 'NEW1'),
            ('NEW1=', 'NEW1'),
            ('=12000', None),
            ('NEW1', None),
            ('NEW1=-5', 'NEW1'),
            ('NEW1=0', 'NEW1'),
        ):
            payload = use_case.build_plan(_args(target_value=[spec]))
            codes = [d['code'] for d in payload['diagnostics']]
            assert codes == ['INVALID_TARGET_VALUE'], spec
            assert payload['diagnostics'][0]['isin'] == isin, spec
            assert payload['feasible'] is None

    def test_empty_sell_all_flag(self, tmp_path):
        use_case = _make_use_case(_make_fake(), tmp_path)
        payload = use_case.build_plan(_args(sell_all=['  ']))
        assert payload['diagnostics'][0]['code'] == 'INVALID_INTENT'


# ---------------------------------------------------------------------------
# --context-id: тот же core-owned context (030.4), ошибки CONTEXT_*
# ---------------------------------------------------------------------------


class TestContextId:
    def test_context_id_loads_same_frozen_slice(self, tmp_path):
        """План по context_id считается по ТОМУ ЖЕ frozen срезу, что и
        свежая сборка на неизменных данных: тот же fingerprint/вердикт."""
        fake = _make_fake()
        use_case = _make_use_case(fake, tmp_path)
        fresh = use_case.build_plan(
            _args(sell_all=list(_SELL_BASE0), target_value=list(_TARGET_NEW1))
        )

        context = PlanningContextBuilder(fake).build()
        handle = use_case._store.save(context)
        by_id = use_case.build_plan(_args(
            sell_all=list(_SELL_BASE0),
            target_value=list(_TARGET_NEW1),
            context_id=handle['context_id'],
        ))
        assert by_id['context_fingerprint'] == fresh['context_fingerprint']
        assert by_id['feasible'] == fresh['feasible'] is True
        assert by_id['targets'] == fresh['targets']
        assert by_id['context']['context_id'] == handle['context_id']

    def test_unknown_context_id_machine_readable(self, tmp_path):
        use_case = _make_use_case(_make_fake(), tmp_path)
        payload = use_case.build_plan(
            _args(context_id='f' * 32, target_value=list(_TARGET_NEW1))
        )
        codes = [d['code'] for d in payload['diagnostics']]
        assert codes == ['CONTEXT_NOT_FOUND']
        assert payload['feasible'] is None
        assert payload['context_fingerprint'] is None

    def test_stale_context_machine_readable(self, tmp_path):
        """Истёкший TTL context — CONTEXT_STALE (ось store, не data_gate)."""
        fake = _make_fake()
        store = EphemeralContextStore(
            root_dir=tmp_path / 'planning_contexts',
            scope_digest='test-scope',
            ttl_seconds=60,
        )
        use_case = RebalancePlanUseCase(db=fake, rating_scale={}, store=store)
        context = PlanningContextBuilder(fake).build()
        handle = store.save(context)

        stale_store = EphemeralContextStore(
            root_dir=tmp_path / 'planning_contexts',
            scope_digest='test-scope',
            rating_scale={},
            ttl_seconds=60,
            now_fn=lambda: datetime.now(timezone.utc) + timedelta(hours=1),
        )
        stale_use_case = RebalancePlanUseCase(
            db=fake, rating_scale={}, store=stale_store
        )
        payload = stale_use_case.build_plan(
            _args(context_id=handle['context_id'], target_value=list(_TARGET_NEW1))
        )
        codes = [d['code'] for d in payload['diagnostics']]
        assert codes == ['CONTEXT_STALE']

    def test_malformed_context_id_is_not_found(self, tmp_path):
        use_case = _make_use_case(_make_fake(), tmp_path)
        payload = use_case.build_plan(_args(context_id='../../etc'))
        assert payload['diagnostics'][0]['code'] == 'CONTEXT_NOT_FOUND'


# ---------------------------------------------------------------------------
# Plan diagnostics: NOT_IN_BUY_POOL / UNKNOWN_ISIN
# ---------------------------------------------------------------------------


class TestPlanDiagnostics:
    def test_increase_of_out_of_pool_holding_rejected(self, tmp_path):
        """Held-ISIN вне canonical buy-пула: увеличение — BUY_NOT_ELIGIBLE с
        structured reasons (NOT_IN_BUY_POOL), BUY-ноги в плане нет."""
        held = _base_held() + [_held_row(
            HELD_NP, qty=10, price='1000', issuer='Вне пула эмитент',
        )]
        fake = _make_fake(held_rows=held)
        use_case = _make_use_case(fake, tmp_path)
        payload = use_case.build_plan(_args(target_value=[f'{HELD_NP}=20000']))

        target = next(
            t for t in payload['targets'] if t['isin'] == HELD_NP
        )
        assert target['status'] == 'BUY_NOT_ELIGIBLE'
        assert target['buy_reasons'] == [
            {'code': 'NOT_IN_BUY_POOL', 'actual': None, 'limit': None}
        ]
        diag = next(
            d for d in payload['diagnostics'] if d['isin'] == HELD_NP
        )
        assert diag['code'] == 'BUY_NOT_ELIGIBLE'
        assert diag['requested'] == '20000'
        assert diag['reasons'] == target['buy_reasons']
        # BUY-ноги нет: план по остальным валиден.
        assert all(t['isin'] != HELD_NP for t in payload['trades'])
        assert payload['valid'] is True

    def test_unknown_isin_diagnostic(self, tmp_path):
        use_case = _make_use_case(_make_fake(), tmp_path)
        payload = use_case.build_plan(
            _args(target_value=['RU000A0GHOST1=5000'])
        )
        target = payload['targets'][0]
        assert target['status'] == 'UNKNOWN_ISIN'
        codes = [d['code'] for d in payload['diagnostics']]
        assert codes == [SCENARIO_ERROR_UNKNOWN_ISIN]
        assert payload['diagnostics'][0]['isin'] == 'RU000A0GHOST1'
        # Неизвестный target пропущен, план по остальным не строится
        # (intents только с unknown ISIN не дают сделок).
        assert payload['trades'] == []


# ---------------------------------------------------------------------------
# Observability (full view)
# ---------------------------------------------------------------------------


class TestObservability:
    def test_full_view_carries_debug_contract(self, plan):
        payload, _, _ = plan
        # Constraints и candidate-filters echo.
        assert payload['constraints']['freq_min'] is None
        assert payload['filters']['max_risk'] == 1  # canonical defaults отчёта
        # Normalized ordered intents с priority.
        assert payload['intents'] == [
            {'type': 'sell_all', 'isin': f'{BASE_PREFIX}0'},
            {'type': 'target_value', 'isin': NEW1,
             'target_value_rub': '12000', 'priority': 1},
        ]
        # PlanningContext timestamps + canonical data_gate + cash.
        context = payload['context']
        assert context['as_of'] == '2025-03-10T12:00:00+00:00'
        assert context['generated_at']
        assert context['positions_updated_at']
        assert context['cash_updated_at']
        assert context['data_gate']['status'] in ('OK', 'WARNING', 'CRITICAL')
        assert context['cash'] == {'cash_available_rub': '50000',
                                   'cash_known': True}
        # Requested vs fitted + planning price/basis + valuation_source +
        # fit reasons.
        target = next(
            t for t in payload['targets'] if t['isin'] == NEW1
        )
        assert target['requested_target_value_rub'] == '12000'
        assert target['fitted_target_value_rub'] == '12000'
        assert target['qty_before'] == 0
        assert target['qty_after'] == 12
        assert target['price'] == 1000.0
        assert target['price_basis'] == 'market_price'
        assert target['fit_reasons'] == []
        sell_target = next(
            t for t in payload['targets'] if t['isin'] == f'{BASE_PREFIX}0'
        )
        assert sell_target['valuation_source'] == 'market_price'
        assert sell_target['qty_delta'] == -12
        # Финальный вердикт валидации.
        assert payload['feasible'] is True
        assert payload['valid'] is True

    def test_payload_contains_no_secrets(self, plan):
        payload, _, _ = plan
        raw = json.dumps(payload, ensure_ascii=False)
        for marker in (
            'postgres://', 'POSTGRES_DSN', 'INVEST_TOKEN', 'TELEGRAM_BOT',
            'password', 'secret', 'authorization',
        ):
            assert marker.lower() not in raw.lower(), marker

    def test_money_of_planning_layer_is_decimal_strings(self, plan):
        payload, _, _ = plan
        target = next(
            t for t in payload['targets']
            if t['isin'] == NEW1  # sell_all не имеет requested value
        )
        assert isinstance(target['requested_target_value_rub'], str)
        assert isinstance(target['fitted_target_value_rub'], str)
        assert isinstance(payload['context']['cash']['cash_available_rub'], str)
        assert isinstance(
            payload['constraints']['max_position_value_rub'], (str, type(None))
        )


# ---------------------------------------------------------------------------
# Регрессия хранения: buy-выборка несёт колонки gates (контракт 030.6)
# ---------------------------------------------------------------------------


class TestBuyPoolRowContract:
    """Строки canonical buy-пула SQL несут колонки structural gates
    (currency/is_trade_available/market_price_updated_at): eligibility-гейт
    fitter'а переоценивает gates построчно (030.6/030.7) — строки без этих
    колонок давали бы ложные UNSUPPORTED_CURRENCY/MARKET_PRICE_STALE
    (smoke 030.8 на реальном SQL). Тест на БД — выполняется с
    POSTGRES_DSN_TEST (см. conftest)."""

    def test_buy_rows_carry_structural_gate_columns(self, db):
        from test_rebalance_report import _insert_bond, _insert_company

        corp = _insert_company(db, 'Гейт Колумс Корп')
        _insert_bond(
            db, isin='RU000A0GATEC1', ticker='GATEC1', name='Гейт Колумс 1',
            company_id=corp, market_price=Decimal('950.0'),
            ytm=Decimal('12'), ytm_updated_at=datetime.now(timezone.utc),
        )
        db.conn.commit()
        rows = db.get_bonds_yield_table(
            mode='buy', min_maturity=None, max_maturity=None, max_risk=5,
            limit=10, include_held=True,
        )
        row = next(r for r in rows if r['isin'] == 'RU000A0GATEC1')
        assert row['currency'] == 'rub'
        assert row['is_trade_available'] is True
        assert row['market_price_updated_at'] is not None

        # Построчная оценка gates на этой строке — без ложных отказов.
        from src.use_cases.bond_filters import structural_buy_violations
        assert structural_buy_violations(row, date.today()) == ()


# ---------------------------------------------------------------------------
# Legacy `rebalance-report --scenario` не изменён (additive-правило)
# ---------------------------------------------------------------------------


class TestLegacyScenarioUnchanged:
    def test_scenario_block_contract_unchanged(self, tmp_path):
        fake = _make_fake()
        scenario_path = tmp_path / 'scenario.json'
        scenario_path.write_text(json.dumps({'trades': [
            {'isin': f'{BASE_PREFIX}0', 'qty_delta': -6},
        ]}), encoding='utf-8')
        report_use_case = RebalanceReportUseCase(fake)
        args = RebalanceReportUseCase.default_args()
        args.scenario = str(scenario_path)
        scenario = report_use_case._build_scenario(args)

        assert scenario['file'] == str(scenario_path)
        # Топ-ключи блока — прежний контракт (005.2/024); 'leg' — ключ строк
        # внутри issuer_limits, не топ-уровень.
        assert set(scenario) == {
            'file', 'valid', 'feasible', 'errors', 'trades', 'summary',
            'budget_check', 'constraint_checks', 'freq_mix_before',
            'freq_mix_after', 'issuer_limits', 'positions_after',
        }
        # Engine-ключ planning_prices остаётся частью Python API — в JSON
        # блока scenario не публикуется (контракт команды не менялся).
        assert 'planning_prices' not in scenario
        for trade in scenario['trades']:
            assert set(trade) == set(SCENARIO_TRADE_KEYS)
        for summary_key in SCENARIO_SUMMARY_KEYS:
            assert summary_key in scenario['summary']
        assert scenario['valid'] is True
