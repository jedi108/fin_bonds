"""
Тесты domain intents + normalization (задача 030.5).

`src/services/planning_intents.py` — типизированные DTO intents/ограничений
и нормализация сырого входа от CLI/LLM. Проверяются требования файла задачи:

- DTO иммутабельны (frozen dataclasses, коллекции — tuple);
- один high-level target на ISIN: повторный target того же вида —
  DUPLICATE_TARGET, разных видов — CONFLICTING_INTENT; last-write-wins
  запрещён (ok=False, intents пуст — частичного применения нет);
- normalized intents эхо-возвращаются 1:1 с явным priority;
  allocation_order отдаёт target_value-intents по рангу приоритета;
- defaults candidate-фильтров берутся из canonical источника отчёта
  (RebalanceReportUseCase.default_args — тот же parser, что CLI);
  идентичность значений;
- issuer limit — canonical константа движка (реэкспорт, второй копии
  значения нет);
- target_value: деньги канонизируются в Decimal (int/str/float/Decimal),
  неположительные/нечисловые/NaN → INVALID_INTENT; priority обязателен;
- PortfolioConstraints — relay в canonical ScenarioConstraints движка
  (валидация остаётся в движке).

Нормализатор чистый (без БД) — тесты не требуют POSTGRES_DSN_TEST.
Legacy rebalance-report модуль не трогает; его контракт покрыт
существующими тестами test_rebalance_report/test_rebalance_engine.
"""
import dataclasses
from decimal import Decimal

import pytest

from src.services.planning_intents import (
    INTENT_ERROR_CONFLICTING_INTENT,
    INTENT_ERROR_DUPLICATE_TARGET,
    INTENT_ERROR_INVALID,
    INTENT_TYPE_SELL_ALL,
    INTENT_TYPE_TARGET_VALUE,
    CandidateFilters,
    IntentDiagnostic,
    IntentNormalization,
    PortfolioConstraints,
    SellAllIntent,
    TargetValueIntent,
    allocation_order,
    default_candidate_filters,
    normalize_intents,
)
from src.services.rebalance_engine import (
    ISSUER_CONCENTRATION_LIMIT_PCT,
    ScenarioConstraints,
)
from src.use_cases.rebalance_report import RebalanceReportUseCase

A1 = 'RU000A0ENAL1'
B2 = 'RU000A0ENBT2'


def _tv(isin, value='150000.00', priority=1):
    return {
        'type': INTENT_TYPE_TARGET_VALUE,
        'isin': isin,
        'target_value_rub': value,
        'priority': priority,
    }


def _sell(isin):
    return {'type': INTENT_TYPE_SELL_ALL, 'isin': isin}


def _codes(result):
    return [d.code for d in result.diagnostics]


# ---------------------------------------------------------------------------
# DTO immutable
# ---------------------------------------------------------------------------

DTO_INSTANCES = [
    SellAllIntent(isin=A1),
    TargetValueIntent(isin=A1, target_value_rub=Decimal('150000'), priority=1),
    PortfolioConstraints(max_position_value_rub=Decimal('150000')),
    CandidateFilters(max_risk=1, max_list_level=2, max_ytm_pct=35.0, include_ku=False),
    IntentDiagnostic(code='X', isin=None, message='m'),
    IntentNormalization(intents=(), diagnostics=()),
]


@pytest.mark.parametrize('dto', DTO_INSTANCES, ids=lambda dto: type(dto).__name__)
def test_dto_frozen(dto):
    with pytest.raises(dataclasses.FrozenInstanceError):
        dto.isin = 'RU000CHANGED'


def test_collections_immutable_in_dto():
    constraints = PortfolioConstraints(freq_in=[2, 4])
    assert isinstance(constraints.freq_in, tuple)
    assert constraints.freq_in == (2, 4)


# ---------------------------------------------------------------------------
# Defaults candidate filters — canonical источник (report), идентичность
# ---------------------------------------------------------------------------


class TestDefaultCandidateFilters:
    def test_values_identical_to_canonical_report_args(self):
        """Identity-тест: planner не имеет своей копии defaults."""
        args = RebalanceReportUseCase.default_args()
        filters = default_candidate_filters()
        assert filters.max_risk == args.max_risk
        assert filters.max_list_level == args.max_listlevel
        assert filters.max_ytm_pct == args.max_ytm
        assert filters.include_ku == bool(args.include_ku)
        assert filters.min_maturity == args.min_maturity
        assert filters.max_maturity == args.max_maturity

    def test_deterministic_and_frozen(self):
        first = default_candidate_filters()
        second = default_candidate_filters()
        assert first == second
        with pytest.raises(dataclasses.FrozenInstanceError):
            first.max_risk = 5


# ---------------------------------------------------------------------------
# Issuer limit — canonical константа движка
# ---------------------------------------------------------------------------


def test_issuer_limit_is_engine_constant_reexport():
    """Тот же объект, что в движке: второй копии значения 15% нет."""
    from src.services import planning_intents

    assert planning_intents.ISSUER_CONCENTRATION_LIMIT_PCT is (
        ISSUER_CONCENTRATION_LIMIT_PCT
    )


def test_portfolio_constraints_do_not_carry_issuer_limit():
    """Issuer limit — не пользовательский flag: в DTO его поля нет."""
    assert 'issuer_limit_pct' not in PortfolioConstraints.__dataclass_fields__


# ---------------------------------------------------------------------------
# PortfolioConstraints -> canonical ScenarioConstraints (relay)
# ---------------------------------------------------------------------------


class TestPortfolioConstraintsRelay:
    def test_full_relay_to_engine_constraints(self):
        constraints = PortfolioConstraints(
            freq_min=2,
            freq_max=12,
            freq_in=[2, 4],
            min_credit_rating='AA-',
            exclude_sovereign=True,
            max_position_value_rub=Decimal('150000.00'),
        )
        scenario = constraints.to_scenario_constraints()
        assert isinstance(scenario, ScenarioConstraints)
        assert scenario.freq_min == 2
        assert scenario.freq_max == 12
        assert scenario.freq_in == (2, 4)
        assert scenario.min_credit_rating == 'AA-'
        assert scenario.exclude_sovereign is True
        # Decimal -> float только на границе float-домена движка.
        assert scenario.max_position_value == 150000.0

    def test_empty_constraints_relay_to_engine_defaults(self):
        assert PortfolioConstraints().to_scenario_constraints() == ScenarioConstraints()


# ---------------------------------------------------------------------------
# Нормализация: эхо, деньги, priority
# ---------------------------------------------------------------------------


class TestNormalizeEcho:
    def test_echo_is_one_to_one_in_input_order_with_priority(self):
        result = normalize_intents([
            _tv(A1, '100000', priority=2),
            _sell(B2),
            _tv(B2.replace('BT2', 'GM1'), 50000, priority=1),
        ])
        assert result.ok
        assert not result.diagnostics
        first, sell, third = result.intents
        assert isinstance(first, TargetValueIntent)
        assert first.isin == A1
        assert first.target_value_rub == Decimal('100000')
        assert first.priority == 2
        assert sell == SellAllIntent(isin=B2)
        assert third.priority == 1
        assert third.target_value_rub == Decimal('50000')

    def test_money_canonized_to_decimal(self):
        result = normalize_intents([
            _tv(A1, '150000.50', priority=3),
            _tv(B2, 150000, priority=2),
            _tv('RU000A0ENGM1', 1500.5, priority=1),
            _tv('RU000A0ENXX9', Decimal('999.99'), priority=4),
        ])
        assert result.ok
        values = [i.target_value_rub for i in result.intents]
        assert values == [
            Decimal('150000.50'), Decimal('150000'), Decimal('1500.5'),
            Decimal('999.99'),
        ]
        assert all(isinstance(v, Decimal) for v in values)

    def test_allocation_order_sorts_by_priority_stable(self):
        result = normalize_intents([
            _tv(A1, '1', priority=3),
            _sell(B2),
            _tv('RU000A0ENGM1', '2', priority=1),
            _tv('RU000A0ENXX9', '3', priority=1),
        ])
        ordered = allocation_order(result.intents)
        assert [i.priority for i in ordered] == [1, 1, 3]
        # Равные priority сохраняют порядок эха (стабильность).
        assert [i.isin for i in ordered[:2]] == ['RU000A0ENGM1', 'RU000A0ENXX9']

    def test_empty_input_is_ok_and_empty(self):
        result = normalize_intents([])
        assert result.ok
        assert result.intents == ()


# ---------------------------------------------------------------------------
# Один high-level target на ISIN: DUPLICATE / CONFLICTING, без last-write-wins
# ---------------------------------------------------------------------------


class TestOneTargetPerIsin:
    def test_duplicate_target_value_same_isin(self):
        result = normalize_intents([_tv(A1, '100000', priority=1),
                                    _tv(A1, '200000', priority=2)])
        assert not result.ok
        assert _codes(result) == [INTENT_ERROR_DUPLICATE_TARGET]
        # Last-write-wins запрещён: противоречивый вход не даёт intents.
        assert result.intents == ()
        assert result.diagnostics[0].isin == A1
        assert '100000' not in result.diagnostics[0].message

    def test_duplicate_sell_all_same_isin(self):
        result = normalize_intents([_sell(A1), _sell(A1)])
        assert _codes(result) == [INTENT_ERROR_DUPLICATE_TARGET]
        assert result.diagnostics[0].isin == A1

    def test_conflicting_sell_all_vs_target_value(self):
        result = normalize_intents([_sell(A1), _tv(A1, '100000', priority=1)])
        assert _codes(result) == [INTENT_ERROR_CONFLICTING_INTENT]
        assert result.intents == ()
        assert result.diagnostics[0].isin == A1

    def test_conflicting_target_value_vs_sell_all(self):
        result = normalize_intents([_tv(A1, '100000', priority=1), _sell(A1)])
        assert _codes(result) == [INTENT_ERROR_CONFLICTING_INTENT]

    def test_same_targets_on_different_isins_are_ok(self):
        result = normalize_intents([_tv(A1, '100000', priority=2),
                                    _tv(B2, '50000', priority=1),
                                    _sell('RU000A0ENGM1')])
        assert result.ok
        assert len(result.intents) == 3

    def test_third_intent_reported_against_first_not_last(self):
        """Нет last-write-wins и в диагностике: все повторы видны."""
        result = normalize_intents([
            _tv(A1, '1', priority=1),
            _tv(A1, '2', priority=2),
            _tv(A1, '3', priority=3),
        ])
        assert len(result.diagnostics) == 2
        assert set(_codes(result)) == {INTENT_ERROR_DUPLICATE_TARGET}
        assert result.intents == ()


# ---------------------------------------------------------------------------
# INVALID_INTENT: форма сырого входа
# ---------------------------------------------------------------------------


class TestInvalidIntents:
    @pytest.mark.parametrize('raw', [
        {'type': 'buy', 'isin': A1},                     # неизвестный вид
        {'type': None, 'isin': A1},
        {'isin': A1},                                    # нет type
        {'type': INTENT_TYPE_SELL_ALL},                  # нет isin
        {'type': INTENT_TYPE_SELL_ALL, 'isin': '   '},   # пустой isin
        {'type': INTENT_TYPE_SELL_ALL, 'isin': 42},      # isin не строка
        _tv(A1, None),                                   # нет target_value_rub
        _tv(A1, 0),                                      # неположительный target
        _tv(A1, -5),
        _tv(A1, 'abc'),                                  # не число
        _tv(A1, float('nan')),
        _tv(A1, float('inf')),
        _tv(A1, True),                                   # bool не число
        _tv(A1, '100000', priority=None),                # priority обязателен
        _tv(A1, '100000', priority='1'),                 # priority не int
        _tv(A1, '100000', priority=1.5),
        _tv(A1, '100000', priority=True),
        {'type': INTENT_TYPE_TARGET_VALUE, 'isin': A1,
         'target_value_rub': '100000'},                  # priority отсутствует
        'sell_all',                                      # не Mapping
        42,
    ])
    def test_invalid_intent_reported(self, raw):
        result = normalize_intents([raw])
        assert not result.ok
        assert _codes(result) == [INTENT_ERROR_INVALID]
        assert result.intents == ()

    def test_all_diagnostics_collected_at_once(self):
        """Не fail-fast: полный список проблем за один вызов."""
        result = normalize_intents([
            {'type': 'buy', 'isin': A1},
            _tv(A1, '100000', priority=1),
            _tv(A1, '200000', priority=2),   # duplicate
            _sell(B2),
            _sell(B2),                        # duplicate
        ])
        assert _codes(result) == [
            INTENT_ERROR_INVALID,
            INTENT_ERROR_DUPLICATE_TARGET,
            INTENT_ERROR_DUPLICATE_TARGET,
        ]
        assert result.intents == ()

    def test_invalid_isin_diagnostic_without_isin(self):
        result = normalize_intents([{'type': INTENT_TYPE_SELL_ALL}])
        assert result.diagnostics[0].isin is None

    def test_non_sequence_input_rejected_loudly(self):
        with pytest.raises(TypeError):
            normalize_intents('sell_all')
        with pytest.raises(TypeError):
            normalize_intents({'type': INTENT_TYPE_SELL_ALL, 'isin': A1})
