"""
Тесты PlanningContext: builder + fingerprint (задача 030.3).

PlanningContext — один согласованный срез данных для plan/compare: frozen
as_of, согласованный DB snapshot (read-only REPEATABLE READ), canonical
data gate, raw universe до variant-specific screening, effective prices,
детерминированный context_fingerprint.

Проверяются требования файла задачи 030.3:
- as_of заморожен внутри одного context; все чтения — внутри snapshot-
  транзакции; time-sensitive правила (окно свежести, YTM fallback) получают
  один и тот же as_of;
- три оси свежести не смешаны: data_gate — отдельное поле; feasible движка
  не маскирует stale gate;
- replay через несколько минут не меняет eligibility внутри контекста;
- fingerprint стабилен при другом presentation order, меняется при изменении
  рыночных данных и не зависит от intents/constraints; Decimal/datetime
  canonicalization покрыта;
- raw universe достаточен для двух вариантов с разными фильтрами.

Все данные — синтетические (in-memory двойник PortfolioStorage), реальные
составы портфеля и ISIN не используются (AGENTS.md); тесты не требуют
POSTGRES_DSN_TEST. Гарантия REPEATABLE READ на живом PostgreSQL покрыта
самой реализацией snapshot_transaction (см. src/storage.py) — её контракт
проверяется фактом вызова в момент сборки.
"""
import contextlib
import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.use_cases.calculate_ytm as ytm_module
from src.services.planning_context import (
    CALCULATION_POLICY_VERSION,
    CONTEXT_SCHEMA_VERSION,
    RAW_UNIVERSE_POLICY,
    CashSnapshot,
    DataGateState,
    PlanningContext,
    PlanningContextBuilder,
    canonical_calculation_payload,
    canonical_datetime_str,
    canonical_decimal_str,
    compute_context_fingerprint,
)
from src.services.rebalance_engine import (
    PLANNING_PRICE_BASIS_HELD_VALUATION,
    PLANNING_PRICE_BASIS_MARKET,
    PLANNING_PRICE_BASIS_MATURED_ZERO,
    PLANNING_PRICE_BASIS_NOMINAL_FALLBACK,
    RebalanceEngine,
    ScenarioConstraints,
)
from src.use_cases.calculate_ytm import CalculateYtmUseCase
from src.use_cases.check_db import classify_freshness

AS_OF = datetime(2025, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
AS_OF_DATE = AS_OF.date()

HELD1 = 'RU000A0ZHEL1'
HELD2 = 'RU000A0ZHEL2'
NEW1 = 'RU000A0ZNEW1'          # покупка: свежая market_price
MATURED1 = 'RU000A0ZMAT1'      # погашена ДО as_of (guard по frozen as_of)
NOMINAL1 = 'RU000A0ZNOM1'      # покупка без market_price (nominal fallback)


def _bond_row(
    isin,
    *,
    qty=None,
    price='1000',
    market_price='1000',
    maturity=date(2027, 6, 15),
    coupon='12.0',
    nominal='1000',
    ytm_updated_at=None,
    risk_level=2,
    list_level=1,
    ku=False,
    credit_rating='AA-',
    rating_score=18,
):
    """Синтетическая строка портфеля/каталога (деньги — Decimal, как из БД)."""
    row = {
        'isin': isin,
        'ticker': isin[-5:],
        'name': 'Бумага ' + isin,
        'nominal': Decimal(nominal),
        'coupon_rate_percent': Decimal(coupon),
        'coupon_quantity_per_year': 4,
        'maturity_date': maturity,
        'offer_date': None,
        'risk_level': risk_level,
        'list_level': list_level,
        'amortization_flag': False,
        'floating_coupon_flag': False,
        'perpetual_flag': False,
        'ku': ku,
        'market_price': Decimal(market_price) if market_price is not None else None,
        'ytm': None,
        'ytm_null_reason': None,
        'ytm_updated_at': ytm_updated_at,
        'company_id': 1,
        'issuer': 'Эмитент ' + isin,
        'entity_type': 'corporate',
        'credit_rating': credit_rating,
        'rating_score': rating_score,
        'held_share_pct': None,
    }
    if qty is not None:
        # Позиция портфеля: каноническая оценка view на штуку = value_rub/qty.
        row['quantity'] = Decimal(qty)
        row['value_rub'] = Decimal(qty) * Decimal(price)
        row['price'] = Decimal(price)
    return row


_FRESH_FRESHNESS = {
    'bonds_catalog_rows': 120,
    'bonds_catalog_max_updated_at': AS_OF - timedelta(hours=2),
    'bonds_with_market_price': 100,
    'market_price_max_updated_at': AS_OF - timedelta(minutes=30),
    'bonds_with_stale_market_price': 0,
    'bonds_tradeable_count': 100,
    'bonds_tradeable_stale_or_missing': 0,
    'max_market_price_time': AS_OF - timedelta(minutes=30),
    'portfolio_positions_rows': 2,
    'portfolio_max_updated_at': AS_OF - timedelta(minutes=15),
    'last_sync_time': AS_OF - timedelta(minutes=15),
    'portfolio_zero_value_positions': 0,
    'portfolio_zero_rows_suspicious': 0,
    'portfolio_zero_value_isins': None,
    'portfolio_total_value': Decimal('20000.00'),
    'monitoring_checks_max_check_date': AS_OF_DATE,
}

_CASH = {
    'cash_available_rub': Decimal('50000.00'),
    'cash_updated_at': AS_OF - timedelta(minutes=10),
    'cash_known': True,
}


def _freshness(**overrides):
    data = dict(_FRESH_FRESHNESS)
    data.update(overrides)
    return data


class FakePlanningStorage:
    """In-memory двойник PortfolioStorage: контракт, нужный builder/engine.

    Фиксирует: не был ли read вне snapshot-транзакции, с каким now вызван
    get_db_freshness и с какими параметрами запрошен raw universe.
    """

    def __init__(self, *, freshness, cash, held_rows, universe_rows, as_of=AS_OF):
        self.now = as_of
        self.freshness_data = dict(freshness)
        self.cash_data = dict(cash)
        self.held_rows = list(held_rows)
        self.universe_rows = list(universe_rows)
        self.snapshot_depth = 0
        self.read_log = []
        self.reads_outside_snapshot = []
        self.freshness_calls = []
        self.universe_kwargs = None

    def _track(self, name):
        self.read_log.append(name)
        if self.snapshot_depth == 0:
            self.reads_outside_snapshot.append(name)

    @contextlib.contextmanager
    def snapshot_transaction(self):
        self.snapshot_depth += 1
        try:
            yield self
        finally:
            self.snapshot_depth -= 1

    def get_transaction_timestamp(self):
        self._track('get_transaction_timestamp')
        return {'ts': self.now, 'day': self.now.date()}

    def get_db_freshness(self, stale_threshold_hours=24, now=None):
        self._track('get_db_freshness')
        self.freshness_calls.append(
            {'now': now, 'stale_threshold_hours': stale_threshold_hours}
        )
        return dict(self.freshness_data)

    def get_available_cash_rub(self):
        self._track('get_available_cash_rub')
        return dict(self.cash_data)

    def get_rebalance_portfolio_rows(self):
        self._track('get_rebalance_portfolio_rows')
        return [dict(row) for row in self.held_rows]

    def get_bonds_yield_table(self, **kwargs):
        self._track('get_bonds_yield_table')
        self.universe_kwargs = dict(kwargs)
        return [dict(row) for row in self.universe_rows]

    def get_scenario_universe_rows(self, isins):
        self._track('get_scenario_universe_rows')
        by_isin = {row['isin']: row for row in self.universe_rows}
        return [dict(by_isin[isin]) for isin in isins if isin in by_isin]


def _make_fake(
    *,
    freshness=None,
    cash=_CASH,
    held_rows=None,
    universe_rows=None,
    as_of=AS_OF,
):
    return FakePlanningStorage(
        freshness=freshness if freshness is not None else _freshness(),
        cash=cash,
        held_rows=held_rows if held_rows is not None else [
            _bond_row(HELD1, qty='10', price='1000'),
            _bond_row(HELD2, qty='10', price='500'),
        ],
        universe_rows=universe_rows if universe_rows is not None else [
            _bond_row(NEW1, market_price='900'),
            _bond_row(MATURED1, market_price='900',
                      maturity=AS_OF_DATE - timedelta(days=1)),
            _bond_row(NOMINAL1, market_price=None),
        ],
        as_of=as_of,
    )


def _build(fake):
    return PlanningContextBuilder(fake).build()


@pytest.fixture
def ytm_spy(monkeypatch):
    """Шпион на cashflow-решатель: фиксирует valuation_date вызовов."""
    calls = []
    real = ytm_module.calculate_bond_cashflow_metrics

    def spy(*args, **kwargs):
        calls.append(kwargs.get('valuation_date'))
        return real(*args, **kwargs)

    monkeypatch.setattr(ytm_module, 'calculate_bond_cashflow_metrics', spy)
    return calls


# ---------------------------------------------------------------------------
# Frozen as_of и согласованный snapshot
# ---------------------------------------------------------------------------

class TestFrozenAsOfAndSnapshot:
    def test_as_of_frozen_in_one_context(self):
        """as_of заморожен внутри одного context: повторные чтения — тот же
        as_of; новый срез после продвижения часов контекст не меняет."""
        fake = _make_fake()
        context = _build(fake)

        assert context.as_of == AS_OF
        assert context.as_of_date == AS_OF_DATE
        assert context.as_of == context.as_of  # повторное чтение

        fake.now = fake.now + timedelta(minutes=10)  # replay-часы уехали
        assert context.as_of == AS_OF
        assert context.as_of_date == AS_OF_DATE

    def test_all_reads_inside_snapshot_transaction(self):
        """Согласованное чтение: все источники среза читаются внутри
        snapshot-транзакции, по выходе сессия возвращена в исходное состояние."""
        fake = _make_fake()
        _build(fake)

        assert fake.reads_outside_snapshot == []
        assert fake.snapshot_depth == 0  # context manager закрылся
        assert set(fake.read_log) == {
            'get_transaction_timestamp', 'get_db_freshness',
            'get_available_cash_rub', 'get_rebalance_portfolio_rows',
            'get_bonds_yield_table',
        }

    def test_time_sensitive_rules_use_one_as_of(self, ytm_spy):
        """SQL/Python time-sensitive правила получают один и тот же as_of:
        as_of контекста == транзакционный timestamp среза, и ровно он уходит
        в окно свежести (SQL-сторона) и в YTM fallback (Python-сторона)."""
        fake = _make_fake()
        context = _build(fake)

        assert context.as_of == fake.get_transaction_timestamp()['ts']
        assert fake.freshness_calls[0]['now'] == context.as_of
        assert set(ytm_spy) == {context.as_of_date}


# ---------------------------------------------------------------------------
# Canonical data gate
# ---------------------------------------------------------------------------

class TestDataGate:
    def test_context_contains_canonical_gate_state(self):
        """Контекст несёт canonical data freshness state — ровно то, что
        возвращает classify_freshness (второго набора thresholds нет)."""
        fake = _make_fake()
        context = _build(fake)

        criticals, warnings, gate_status, is_fresh = classify_freshness(
            fake.freshness_data, now=AS_OF
        )
        assert context.data_gate == DataGateState(
            status='OK',
            gate_status=gate_status,
            is_fresh=is_fresh,
            criticals=tuple(criticals),
            warnings=tuple(warnings),
        )
        assert context.data_gate.is_fresh is True

    def test_fresh_context_from_stale_db_stays_stale(self):
        """Новый context из устаревшей БД не становится свежим от того, что
        его сборка только что была: data_gate остаётся stale/CRITICAL."""
        stale = _freshness(
            last_sync_time=AS_OF - timedelta(days=4),
            portfolio_max_updated_at=AS_OF - timedelta(days=4),
        )
        context = _build(_make_fake(freshness=stale))

        assert context.data_gate.is_fresh is False
        assert context.data_gate.gate_status == 'STALE_PORTFOLIO'
        assert context.data_gate.status == 'CRITICAL'
        assert context.data_gate.criticals
        # Срез при этом собран: данные захвачены как есть (stale gate не
        # «чинится» и не блокирует сборку контекста).
        assert len(context.held_rows) == 2

    def test_freshness_window_uses_frozen_as_of(self):
        """Окно свежести (24h) — параметризовано frozen as_of, не wall-clock."""
        fake = _make_fake()
        _build(fake)
        assert fake.freshness_calls == [
            {'now': AS_OF, 'stale_threshold_hours': 24}
        ]

    def test_ytm_fallback_receives_as_of_date(self, ytm_spy):
        """Скрытый второй clock устранён: временный YTM fallback получает
        PlanningContext.as_of_date (ytm_updated_at IS NULL → расчёт на лету)."""
        context = _build(_make_fake())
        assert context.as_of_date == AS_OF_DATE
        assert ytm_spy, 'ожидался хотя бы один on-the-fly YTM расчёт'
        assert set(ytm_spy) == {AS_OF_DATE}

    def test_legacy_ytm_fallback_keeps_wall_clock(self, ytm_spy):
        """Legacy path без as_of_date не изменён: valuation_date — date.today()
        (прежнее поведение report/calculate-ytm)."""
        fake = _make_fake()
        row = _bond_row(NEW1, market_price='900')
        CalculateYtmUseCase(fake)._enrich_bonds_with_ytm(
            [row], SimpleNamespace(min_ytm=None)
        )
        assert ytm_spy == [date.today()]


# ---------------------------------------------------------------------------
# Оси не смешаны: feasible ≠ data gate
# ---------------------------------------------------------------------------

class TestAxesNotMixed:
    def test_feasible_true_does_not_mask_stale_gate(self):
        """feasible (финансовая ось движка) и data gate — разные поля разных
        объектов: валидный сценарий feasible=true на stale-срезе не превращает
        gate в свежий, и наоборот."""
        stale = _freshness(
            last_sync_time=AS_OF - timedelta(days=4),
            portfolio_max_updated_at=AS_OF - timedelta(days=4),
        )
        # 8 равных эмитентов по 12.5% — лимит 15% не нарушен, сценарий без
        # сделок financial-valid => feasible=true.
        held = [
            _bond_row(f'RU000A0ZEQ0{i}', qty='10', price='125') for i in range(1, 9)
        ]
        fake = _make_fake(freshness=stale, held_rows=held)

        context = _build(fake)
        result = RebalanceEngine(fake).evaluate_scenario(
            [], as_of_date=context.as_of_date
        )

        assert result['feasible'] is True
        assert context.data_gate.is_fresh is False
        assert context.data_gate.gate_status == 'STALE_PORTFOLIO'
        # Контекст вообще не содержит финансовой оси — смешивать нечего.
        assert not hasattr(context, 'feasible')

    def test_data_gate_is_not_derived_from_cash_or_positions(self):
        """Data gate — canonical state исходных данных, а не производное от
        содержимого среза: unknown cash / пустой universe его не формируют."""
        unknown_cash = {
            'cash_available_rub': None, 'cash_updated_at': None, 'cash_known': False,
        }
        context = _build(_make_fake(cash=unknown_cash, universe_rows=[]))
        assert context.data_gate.is_fresh is True
        assert context.data_gate.gate_status == 'OK'
        assert context.cash == CashSnapshot(cash_available_rub=None, cash_known=False)


# ---------------------------------------------------------------------------
# Effective prices = eligibility, зафиксированная в context
# ---------------------------------------------------------------------------

class TestEffectivePricesFrozen:
    def test_effective_price_and_basis_per_isin(self):
        """Цена расчёта + basis на каждый ISIN среза — тем же калькулятором,
        что у движка (held_valuation / market_price / nominal_fallback /
        matured_zero), matured-guard — относительно frozen as_of."""
        context = _build(_make_fake())
        prices = context.effective_prices

        assert prices[HELD1] == {
            'price': 1000.0, 'basis': PLANNING_PRICE_BASIS_HELD_VALUATION,
        }
        assert prices[NEW1] == {
            'price': 900.0, 'basis': PLANNING_PRICE_BASIS_MARKET,
        }
        # Погашенная ДО as_of — 0 (P1-guard) именно по frozen as_of_date.
        assert prices[MATURED1] == {
            'price': 0.0, 'basis': PLANNING_PRICE_BASIS_MATURED_ZERO,
        }
        assert prices[NOMINAL1] == {
            'price': 1000.0, 'basis': PLANNING_PRICE_BASIS_NOMINAL_FALLBACK,
        }

    def test_held_isin_in_universe_has_single_canonical_price(self):
        """Held ISIN попадает и в raw universe (include_held), но цена — одна:
        каноническая оценка view, дублей в effective_prices нет."""
        held = [_bond_row(HELD1, qty='10', price='1000')]
        universe = [
            _bond_row(HELD1, market_price='999'),   # тот же ISIN в buy-выборке
            _bond_row(NEW1, market_price='900'),
        ]
        context = _build(_make_fake(held_rows=held, universe_rows=universe))

        assert sum(1 for isin in context.effective_prices if isin == HELD1) == 1
        assert context.effective_prices[HELD1][
            'basis'] == PLANNING_PRICE_BASIS_HELD_VALUATION

    def test_replay_minutes_later_keeps_eligibility_frozen(self, ytm_spy):
        """Replay через несколько минут НЕ меняет instrument eligibility внутри
        context: цены/basis/as_of первого среза заморожены, новый срез — новый
        fingerprint."""
        fake = _make_fake()
        context = _build(fake)
        frozen_prices = dict(context.effective_prices)
        frozen_fingerprint = context.context_fingerprint

        # Рынок уехал, часы тикают — оригинальный context неизменен.
        fake.now = fake.now + timedelta(minutes=10)
        fake.universe_rows[0]['market_price'] = Decimal('1103.06')
        rebuilt_prices = dict(_build(fake).effective_prices)

        assert context.effective_prices == frozen_prices
        assert context.as_of == AS_OF
        assert context.context_fingerprint == frozen_fingerprint
        assert rebuilt_prices[NEW1] == {
            'price': 1103.06, 'basis': PLANNING_PRICE_BASIS_MARKET,
        }

    def test_engine_as_of_date_hook_for_matured_guard(self):
        """evaluate_scenario принимает as_of_date (030.3): matured-guard
        считается относительно frozen as_of, а не wall-clock."""
        fake = _make_fake()
        engine = RebalanceEngine(fake)
        result = engine.evaluate_scenario(
            [{'isin': MATURED1, 'qty_delta': 1}],
            as_of_date=AS_OF_DATE,
        )
        assert result['planning_prices'][MATURED1] == {
            'price': 0.0, 'basis': PLANNING_PRICE_BASIS_MATURED_ZERO,
        }


# ---------------------------------------------------------------------------
# context_fingerprint
# ---------------------------------------------------------------------------

class TestFingerprint:
    def test_fingerprint_stable_across_presentation_order(self):
        """Fingerprint стабилен при одинаковых данных+policy независимо от
        dict/list presentation order и от generated_at."""
        context = _build(_make_fake())
        same_reordered = PlanningContext(
            context_schema_version=context.context_schema_version,
            calculation_policy_version=context.calculation_policy_version,
            as_of=context.as_of,
            as_of_date=context.as_of_date,
            generated_at=context.generated_at + timedelta(minutes=3),  # не входит
            positions_updated_at=context.positions_updated_at,
            cash_updated_at=context.cash_updated_at,
            data_gate=context.data_gate,
            cash=context.cash,
            held_rows=tuple(
                dict(reversed(list(row.items())))
                for row in reversed(context.held_rows)
            ),
            universe_rows=tuple(
                dict(reversed(list(row.items())))
                for row in reversed(context.universe_rows)
            ),
            effective_prices={
                isin: context.effective_prices[isin]
                for isin in reversed(list(context.effective_prices))
            },
            universe_policy=dict(reversed(list(context.universe_policy.items()))),
        )
        assert same_reordered.context_fingerprint == context.context_fingerprint

    def test_fingerprint_changes_with_market_data(self):
        """Изменение рыночных данных среза меняет fingerprint (даже при том же
        as_of и policy)."""
        base = _build(_make_fake())
        moved = _build(_make_fake(
            universe_rows=[
                _bond_row(NEW1, market_price='1103.06'),
                _bond_row(MATURED1, market_price='900',
                          maturity=AS_OF_DATE - timedelta(days=1)),
                _bond_row(NOMINAL1, market_price=None),
            ],
        ))
        assert moved.context_fingerprint != base.context_fingerprint

    def test_fingerprint_changes_with_as_of(self):
        """Сдвиг frozen as_of меняет fingerprint — разные срезы несравнимы."""
        base = _build(_make_fake())
        later = _build(_make_fake(as_of=AS_OF + timedelta(minutes=5)))
        assert later.context_fingerprint != base.context_fingerprint

    def test_fingerprint_independent_of_intents_and_constraints(self):
        """Intents/constraints варианта не входят в fingerprint: разные
        ScenarioConstraints на одном контексте не меняют его fingerprint —
        сценарии с разными rating thresholds на одном срезе сравнимы."""
        fake = _make_fake()
        context = _build(fake)
        before = context.context_fingerprint

        engine = RebalanceEngine(fake)
        engine.evaluate_scenario(
            [{'isin': NEW1, 'qty_delta': 1}], ScenarioConstraints(freq_min=2),
            as_of_date=AS_OF_DATE,
        )
        engine.evaluate_scenario(
            [{'isin': NEW1, 'qty_delta': 2}],
            ScenarioConstraints(exclude_sovereign=True, min_credit_rating='AA-'),
            as_of_date=AS_OF_DATE,
        )
        assert context.context_fingerprint == before

    def test_payload_excludes_generated_at_and_fingerprint(self):
        """generated_at в payload отсутствует (на расчёт не влияет); сам
        fingerprint в payload не входит (нет цикла по построению)."""
        context = _build(_make_fake())
        payload = canonical_calculation_payload(context)
        assert 'generated_at' not in payload
        assert 'context_fingerprint' not in payload
        # Контекст с другим wall-clock сборки — тот же fingerprint.
        shifted = dataclasses.replace(context, generated_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
        assert compute_context_fingerprint(shifted) == context.context_fingerprint

    def test_metadata_minimum_present(self):
        """Metadata-минимум контракта: generated_at / as_of /
        positions_updated_at / cash_updated_at / context_schema_version /
        calculation_policy_version."""
        context = _build(_make_fake())
        assert context.context_schema_version == CONTEXT_SCHEMA_VERSION
        assert context.calculation_policy_version == CALCULATION_POLICY_VERSION
        assert context.generated_at.tzinfo is not None
        assert context.as_of == AS_OF
        assert context.positions_updated_at == (
            AS_OF - timedelta(minutes=15)
        )
        assert context.cash_updated_at == AS_OF - timedelta(minutes=10)


# ---------------------------------------------------------------------------
# Каноническая сериализация (Decimal / datetime / скаляры)
# ---------------------------------------------------------------------------

class TestCanonicalSerialization:
    def test_decimal_scale_and_exponent_insensitive(self):
        """Decimal → детерминированная decimal string: масштаб ('951.30' ==
        '951.3'), экспоненциальная форма ('100' == '1E+2' == '100.00'),
        '-0' == '0' — float не используется."""
        assert canonical_decimal_str(Decimal('951.30')) == canonical_decimal_str(
            Decimal('951.3')
        )
        assert canonical_decimal_str(Decimal('100')) == canonical_decimal_str(
            Decimal('1E+2')
        ) == canonical_decimal_str(Decimal('100.00')) == '100'
        assert canonical_decimal_str(Decimal('-0.00')) == canonical_decimal_str(
            Decimal('0')
        ) == '0'
        assert canonical_decimal_str(Decimal('951.30')) == '951.3'

    def test_datetime_naive_and_zones_canonical_to_utc_iso(self):
        """datetime → canonical ISO/UTC: naive трактуется как UTC, прочие зоны
        приводятся к UTC; date — 'YYYY-MM-DD'."""
        assert canonical_datetime_str(datetime(2026, 10, 1, 12, 0, 0)) == (
            canonical_datetime_str(datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc))
        )
        assert canonical_datetime_str(
            datetime(2026, 10, 1, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        ) == '2026-10-01T12:00:00+00:00'

    def test_scalars_unambiguous_in_serialization(self):
        """Однозначное представление None/bool/int/string: true ≠ 1 ≠ "1",
        null ≠ false ≠ 0."""
        encoded = json.dumps(
            [True, 1, '1', None, False, 0, '0'],
            sort_keys=True, separators=(',', ':'), ensure_ascii=True,
        )
        assert encoded == '[true,1,"1",null,false,0,"0"]'

    def test_payload_decimals_are_strings_not_floats(self):
        """Деньги среза попадают в payload строками Decimal (не float):
        cash '50000.00' сериализуется детерминированной decimal string без
        float-погрешностей."""
        context = _build(_make_fake())
        payload = json.dumps(
            canonical_calculation_payload(context), sort_keys=True,
            separators=(',', ':'),
        )
        assert '"cash_available_rub":"50000"' in payload
        assert '"cash_available_rub":50000' not in payload


# ---------------------------------------------------------------------------
# Raw universe до variant-specific screening
# ---------------------------------------------------------------------------

class TestRawUniverseForCompare:
    def test_universe_fetched_wide_open(self):
        """Контекст запрашивает канонический raw universe с широкими
        структурными порогами (та же политика, что у legacy screener'а):
        вариантные фильтры применяются позже, к одним и тем же строкам."""
        fake = _make_fake()
        _build(fake)
        assert fake.universe_kwargs == RAW_UNIVERSE_POLICY
        assert fake.universe_kwargs['max_risk'] == 5
        assert fake.universe_kwargs['max_listlevel'] == 3
        assert fake.universe_kwargs['include_held'] is True
        assert fake.universe_kwargs['min_maturity'] is None
        assert fake.universe_kwargs['max_maturity'] is None

    def test_two_variants_screen_same_raw_universe(self):
        """Raw universe достаточен для двух вариантов с разными фильтрами:
        strict не удаляет заранее строки, нужные relaxed — оба варианта
        скринируют один и тот же список контекста."""
        universe = [
            _bond_row('RU000A0ZVAR1', risk_level=1, ku=False),
            _bond_row('RU000A0ZVAR2', risk_level=3, ku=False),  # нужен relaxed
            _bond_row('RU000A0ZVAR3', risk_level=1, ku=True),   # нужен relaxed с KU
            _bond_row('RU000A0ZVAR4', risk_level=2, ku=False),
        ]
        context = _build(_make_fake(universe_rows=universe))
        rows = context.universe_rows

        strict = [r for r in rows if r['risk_level'] <= 1 and not r['ku']]
        relaxed = [r for r in rows if r['risk_level'] <= 3]

        assert {r['isin'] for r in strict} == {'RU000A0ZVAR1'}
        assert {r['isin'] for r in relaxed} == {
            'RU000A0ZVAR1', 'RU000A0ZVAR2', 'RU000A0ZVAR3', 'RU000A0ZVAR4',
        }
        # Строки, отсечённые strict-вариантом, физически в контексте:
        assert {r['isin'] for r in rows} >= (
            {r['isin'] for r in relaxed} - {r['isin'] for r in strict}
        )


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

class TestImmutability:
    def test_context_is_frozen(self):
        """Immutable PlanningContext: запись атрибута запрещена."""
        context = _build(_make_fake())
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.as_of = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.data_gate = context.data_gate
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.cash = CashSnapshot(None, False)

    def test_gate_and_cash_states_are_frozen(self):
        gate = DataGateState.from_classify((), (), 'OK', True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            gate.is_fresh = False
        cash = CashSnapshot(Decimal('1.00'), True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            cash.cash_known = False
