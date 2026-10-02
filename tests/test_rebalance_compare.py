"""
Тесты rebalance-compare — A/B сравнение на одном PlanningContext (030.9).

Проверяются требования файла задачи 030.9:
- preferred-путь: ОДИН PlanningContext, две альтернативы с РАЗНЫМИ
  constraints внутри него → корректная delta + одинаковый
  context_fingerprint; каждый plan result в ответе;
- fingerprint не включает intents/variant constraints (один context —
  один fingerprint независимо от вариантов);
- raw universe до variant screening: strict (include_ku=False) и relaxed
  (include_ku=True) варианты скринируют один набор исходных строк —
  strict не удалил строки, нужные relaxed;
- fallback: готовые plan results с разным context_fingerprint →
  CONTEXT_MISMATCH, delta не публикуется (null), метрики каждого варианта
  остаются его собственными canonical выходами;
- минимальные метрики сравнения из canonical outputs (без «третьей формулы»);
- delta — decimal-строки (правило 030 №3), canonical значения движка —
  как есть;
- CLI: prefixed-флаги a/b, machine-readable diagnostics с атрибутом
  variant, CONTEXT_*, фабричная карта (additive);
- legacy команды не тронуты.

Все данные — синтетические (in-memory двойник PortfolioStorage + реальный
PlanningContextBuilder/RebalanceEngine/AutoFitFitter), реальные составы
портфеля и ISIN не используются (AGENTS.md); тесты не требуют
POSTGRES_DSN_TEST. Эфемерный store — во временный каталог (030.4).
"""
import argparse
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from src.services.context_store import EphemeralContextStore
from src.services.planning_context import PlanningContextBuilder
from src.services.planning_fitter import AutoFitFitter
from src.services.planning_intents import (
    CandidateFilters,
    PortfolioConstraints,
    TargetValueIntent,
)
from src.services.scenario_compare import (
    COMPARE_ERROR_CONTEXT_MISMATCH,
    COMPARE_SCHEMA_VERSION,
    CompareVariant,
    build_compare,
    compare_plan_results,
    extract_plan_metrics,
)
from src.use_cases.factory import UseCaseFactory
from src.use_cases.rebalance_compare import RebalanceCompareUseCase
from test_rebalance_plan import (
    AS_OF,
    NEW1,
    _make_fake,
)

BASE_COUNT = 7


def _context(fake):
    """Один PlanningContext на сравнение (030.3)."""
    return PlanningContextBuilder(fake).build()


def _intent(value='12000', priority=1):
    return TargetValueIntent(
        isin=NEW1, target_value_rub=Decimal(value), priority=priority
    )


def _make_compare_use_case(fake, tmp_path, store=None):
    store = store or EphemeralContextStore(
        root_dir=tmp_path / 'planning_contexts',
        scope_digest='test-scope',
        rating_scale={},
    )
    return RebalanceCompareUseCase(db=fake, rating_scale={}, store=store)


def _parse_cli(argv):
    parser = argparse.ArgumentParser(add_help=False)
    RebalanceCompareUseCase.setup_parser(parser)
    return parser.parse_args(argv)


# Требуемые метрики сравнения (файл задачи) + вердикты вариантов.
_METRIC_KEYS = {
    'feasible', 'valid',
    'securities_value', 'cash_after_estimate',
    'coupon_month', 'coupon_year', 'delta_month', 'delta_year',
    'positions_count', 'max_issuer_share_pct', 'violations_count',
    'changed_isins',
}


# ---------------------------------------------------------------------------
# Preferred-путь: один context, разные constraints, корректная delta
# ---------------------------------------------------------------------------


class TestPreferredPath:
    @pytest.fixture
    def outcome(self):
        """Relaxed (target 12000) vs strict (target 12000 + cap 6000) на
        ОДНОМ context: 7 базовых позиций по 12000 (84000), NEW1 по 1000."""
        fake = _make_fake()
        context = _context(fake)
        variants = [
            CompareVariant(name='a', intents=[_intent('12000')]),
            CompareVariant(
                name='b', intents=[_intent('12000')],
                constraints=PortfolioConstraints(
                    max_position_value_rub=Decimal('6000')
                ),
            ),
        ]
        return build_compare(fake, context, variants), context

    def test_same_fingerprint_and_comparable(self, outcome):
        result, context = outcome
        assert result['compare_schema_version'] == COMPARE_SCHEMA_VERSION
        assert result['comparable'] is True
        assert result['diagnostics'] == []
        assert result['context_fingerprint'] == context.context_fingerprint
        # Оба варианта считаны по одному срезу (fingerprint контекста).
        for name in ('a', 'b'):
            assert result['variants'][name][
                'context_fingerprint'
            ] == context.context_fingerprint

    def test_minimal_metrics_from_canonical_outputs(self, outcome):
        result, _ = outcome
        for name in ('a', 'b'):
            assert _METRIC_KEYS <= set(result['metrics'][name])
        a = result['metrics']['a']
        # 84000 базовых + NEW1 12 x 1000 = 96000 (canonical summary движка).
        assert a['securities_value'] == 96000.0
        assert a['cash_after_estimate'] == 38000.0  # 50000 - 12000
        # Купон: 7 базовых x 100 + NEW1 100 = 800/мес (canonical coupon_month_after).
        assert a['coupon_month'] == 800.0
        assert a['coupon_year'] == 9600.0
        assert a['positions_count'] == 8
        assert a['violations_count'] == 0
        assert a['changed_isins'] == [NEW1]
        assert a['feasible'] is True and a['valid'] is True
        # max issuer share — canonical issuer_pct строк positions_after (025).
        assert a['max_issuer_share_pct'] == 12.5  # 12000/96000

    def test_correct_delta_decimal_strings(self, outcome):
        """Delta = strict − relaxed по canonical значениям; деньги —
        decimal-строки (правило 030 №3)."""
        delta = outcome[0]['delta']
        assert delta['b']['baseline'] == 'a'
        assert delta['b']['securities_value'] == '-6000'  # 90000 − 96000
        assert delta['b']['cash_after_estimate'] == '6000'  # 44000 − 38000
        assert delta['b']['coupon_month'] == '-50'  # 750 − 800
        assert delta['b']['coupon_year'] == '-600'
        assert delta['b']['delta_month'] == '-50'
        assert delta['b']['delta_year'] == '-600'
        assert delta['b']['positions_count'] == 0
        # Cap 6000 нарушается нетронутыми позициями 7 x 12000 — canonical
        # validator даёт 7 violations, strict-вариант неосуществим
        # (auto-продажа без intent запрещена, правило P0.3).
        assert delta['b']['violations_count'] == 7
        assert outcome[0]['metrics']['a']['violations_count'] == 0
        assert outcome[0]['metrics']['b']['feasible'] is False
        assert outcome[0]['metrics']['a']['feasible'] is True
        assert delta['b']['max_issuer_share_pct'] == '0.83'  # 13.33 − 12.5
        # Канонические метрики самих вариантов не переформатированы (float
        # релей движка), новая арифметика — decimal-строки.
        assert isinstance(outcome[0]['metrics']['b']['securities_value'], float)
        assert isinstance(delta['b']['securities_value'], str)

    def test_each_plan_result_included(self, outcome):
        """build_compare возвращает каждый plan result в контракте fitter'а
        (030.7) как есть."""
        result, _ = outcome
        strict = result['variants']['b']
        assert set(strict) == {
            'context_fingerprint', 'as_of', 'targets', 'trades',
            'diagnostics', 'price_consistent', 'validation', 'feasible',
        }
        target = next(t for t in strict['targets'] if t.isin == NEW1)
        # Cap 6000: fitter pre-size (POSITION_CAP), fitted 6 x 1000; план
        # структурно валиден, но неосуществим из-за нетронутых позиций.
        assert target.fitted_target_value_rub == Decimal('6000')
        assert 'POSITION_CAP' in target.fit_reasons
        assert strict['validation']['valid'] is True
        assert strict['feasible'] is False
        assert all(
            v['type'] == 'POSITION_VALUE_LIMIT'
            for v in strict['validation']['constraint_checks']
        )

    def test_requires_two_variants_and_unique_names(self):
        fake = _make_fake()
        context = _context(fake)
        with pytest.raises(ValueError, match='две или более'):
            build_compare(fake, context, [CompareVariant('a', [_intent()])])
        duplicates = [
            CompareVariant('same', [_intent()]),
            CompareVariant('same', []),
        ]
        with pytest.raises(ValueError, match='уникальны'):
            build_compare(fake, context, duplicates)


# ---------------------------------------------------------------------------
# Fingerprint не включает intents/variant constraints; raw universe общий
# ---------------------------------------------------------------------------


class TestFingerprintContract:
    def test_fingerprint_excludes_intents_and_constraints(self):
        """Разные intents и constraints на одном context → один и тот же
        context_fingerprint (fingerprint — свойства среза, не запроса)."""
        fake = _make_fake()
        context = _context(fake)
        variants = [
            CompareVariant(name='a', intents=[_intent('12000')]),
            CompareVariant(
                name='b', intents=[],
                constraints=PortfolioConstraints(
                    freq_min=4, freq_max=12, exclude_sovereign=True,
                ),
            ),
        ]
        result = build_compare(fake, context, variants)
        assert result['comparable'] is True
        assert result['delta']['b']['baseline'] == 'a'
        for name in ('a', 'b'):
            assert result['variants'][name][
                'context_fingerprint'
            ] == context.context_fingerprint

    def test_different_slices_give_different_fingerprints(self):
        """Контр-проверка осмысленности: другой срез (cash) → другой
        fingerprint (иначе проверка 030.9 ничего бы не ловила)."""
        context1 = _context(_make_fake())
        context2 = _context(_make_fake(cash={
            'cash_available_rub': Decimal('77777.00'),
            'cash_known': True,
            'cash_updated_at': AS_OF - timedelta(minutes=10),
        }))
        assert context1.context_fingerprint != context2.context_fingerprint

    def test_strict_and_relaxed_share_raw_universe(self):
        """Raw universe до variant screening (файл задачи): KU-бумага в одном
        context.universe_rows; relaxed (include_ku=True) покупает, strict
        (include_ku=False) — BUY_NOT_ELIGIBLE; strict не удалил заранее
        строки, нужные relaxed."""
        ku_isin = 'RU000A0KUBND'
        from test_rebalance_plan import _pool_row

        ku_row = _pool_row(ku_isin, market_price='1000', issuer='КУ Эмитент')
        ku_row['ku'] = True  # policy-признак КУ (structural gate его не читает)
        fake = _make_fake(universe_rows=[
            *[
                _pool_row(
                    f'RU000A0BASE{n}', market_price='1000',
                    issuer=f'Базовый эмитент {n}',
                    held_share_pct=Decimal('14.29'),
                )
                for n in range(BASE_COUNT)
            ],
            _pool_row(NEW1, market_price='1000', issuer='Новый эмитент 1'),
            ku_row,
        ])
        context = _context(fake)
        # Строка KU в canonical raw universe среза — до variant screening.
        assert any(row['isin'] == ku_isin for row in context.universe_rows)

        def filters(include_ku):
            return CandidateFilters(
                max_risk=1, max_list_level=1, max_ytm_pct=35.0,
                include_ku=include_ku,
            )

        result = build_compare(fake, context, [
            CompareVariant(
                name='relaxed',
                intents=[TargetValueIntent(
                    isin=ku_isin, target_value_rub=Decimal('5000'), priority=1,
                )],
                filters=filters(include_ku=True),
            ),
            CompareVariant(
                name='strict',
                intents=[TargetValueIntent(
                    isin=ku_isin, target_value_rub=Decimal('5000'), priority=1,
                )],
                filters=filters(include_ku=False),
            ),
        ])
        relaxed_target = next(
            t for t in result['variants']['relaxed']['targets']
            if t.isin == ku_isin
        )
        strict_target = next(
            t for t in result['variants']['strict']['targets']
            if t.isin == ku_isin
        )
        assert relaxed_target.status == 'FITTED'
        assert strict_target.status == 'BUY_NOT_ELIGIBLE'
        # canonical policy-причина скринера (bond_filters): ключ 'ku'.
        assert strict_target.buy_reasons[0].code == 'ku'
        # Отказ policy-фильтра strict не ломает план и не трогает строки,
        # нужные relaxed (обе альтернативы на одном context).
        assert result['comparable'] is True
        assert any(row['isin'] == ku_isin for row in context.universe_rows)


# ---------------------------------------------------------------------------
# Fallback: готовые plan results, CONTEXT_MISMATCH при разных fingerprint
# ---------------------------------------------------------------------------


class TestFallbackContextMismatch:
    @pytest.fixture
    def fit_results(self):
        """Два plan result (контракт fitter 030.7) по РАЗНЫМ срезам."""
        fake1 = _make_fake()
        fake2 = _make_fake(cash={
            'cash_available_rub': Decimal('77777.00'),
            'cash_known': True,
            'cash_updated_at': AS_OF - timedelta(minutes=10),
        })
        intents = [_intent('12000')]
        return (
            AutoFitFitter(fake1, _context(fake1)).fit(intents),
            AutoFitFitter(fake2, _context(fake2)).fit(intents),
        )

    def test_mismatch_rejects_delta(self, fit_results):
        result1, result2 = fit_results
        assert result1['context_fingerprint'] != result2['context_fingerprint']
        outcome = compare_plan_results([result1, result2], names=['a', 'b'])
        assert outcome['comparable'] is False
        assert outcome['context_fingerprint'] is None
        # Финансовая delta не публикуется как сопоставимая.
        assert outcome['delta'] is None
        codes = [d['code'] for d in outcome['diagnostics']]
        assert codes == [COMPARE_ERROR_CONTEXT_MISMATCH]
        diagnostic = outcome['diagnostics'][0]
        assert diagnostic['context_fingerprints'] == sorted({
            result1['context_fingerprint'], result2['context_fingerprint'],
        })
        assert diagnostic['isin'] is None and diagnostic['message']

    def test_mismatch_keeps_per_variant_metrics(self, fit_results):
        """Метрики каждого варианта — его собственные canonical выходы;
        delta запрещена, метрики нет."""
        result1, result2 = fit_results
        outcome = compare_plan_results([result1, result2], names=['a', 'b'])
        assert outcome['metrics']['a']['securities_value'] == 96000.0
        assert outcome['metrics']['b']['cash_after_estimate'] == 65777.0
        assert outcome['metrics']['a']['changed_isins'] == [NEW1]

    def test_same_fingerprint_fallback_comparable(self, fit_results):
        """Fallback на одном срезе (дважды подогнанные результаты того же
        context) — сравнимы, delta по canonical значениям."""
        result1, _ = fit_results
        fake = _make_fake()
        same = AutoFitFitter(fake, _context(fake)).fit([_intent('12000')])
        # result1 собран на идентичных синтетических данных → тот же fingerprint.
        outcome = compare_plan_results(
            [result1, same], names=['first', 'second']
        )
        assert outcome['comparable'] is True
        assert outcome['context_fingerprint'] == result1['context_fingerprint']
        assert outcome['delta']['second']['baseline'] == 'first'
        assert outcome['delta']['second']['securities_value'] == '0'

    def test_flat_projection_shape_accepted(self):
        """Плоская canonical проекция plan (030.8) — допустимая форма
        fallback-входа (violations вместо constraint_checks)."""
        def block(fingerprint, securities):
            return {
                'context_fingerprint': fingerprint,
                'feasible': True,
                'valid': True,
                'summary': {
                    'securities_after': securities,
                    'cash_after_estimate': 0.0,
                    'coupon_month_after': 100.0,
                    'coupon_year_after': 1200.0,
                    'delta_month': 10.0,
                    'delta_year': 120.0,
                },
                'trades': [{'isin': NEW1, 'qty_delta': 1.0}],
                'positions_after': [{'isin': NEW1, 'issuer_pct': 12.5}],
                'violations': [],
            }
        outcome = compare_plan_results(
            [block('f' * 64, 1000.0), block('f' * 64, 1500.0)],
            names=['a', 'b'],
        )
        assert outcome['comparable'] is True
        assert outcome['delta']['b']['securities_value'] == '500'
        assert outcome['metrics']['b']['violations_count'] == 0

    def test_input_validation(self, fit_results):
        with pytest.raises(ValueError, match='две или более'):
            compare_plan_results([fit_results[0]])
        with pytest.raises(ValueError, match='по одному имени'):
            compare_plan_results(list(fit_results), names=['only'])
        with pytest.raises(ValueError, match='уникальны'):
            compare_plan_results(list(fit_results), names=['x', 'x'])
        without_fingerprint = {'summary': None, 'trades': [],
                               'positions_after': []}
        with pytest.raises(TypeError, match='context_fingerprint'):
            compare_plan_results([without_fingerprint, without_fingerprint])
        with pytest.raises(TypeError, match='Mapping'):
            compare_plan_results(['not-a-result', 'not-a-result'])


# ---------------------------------------------------------------------------
# Метрики: чтение canonical блоков без «третьей формулы»
# ---------------------------------------------------------------------------


class TestMetricsExtraction:
    def test_invalid_plan_metrics_are_honest_nones(self):
        """Структурно невалидный сценарий: summary/проверки отсутствуют —
        violations_count None (не «нет нарушений»), деньги None."""
        metrics = extract_plan_metrics({
            'context_fingerprint': 'f' * 64,
            'feasible': None,
            'trades': [{'isin': NEW1, 'qty_delta': 1.0}],
            'validation': {
                'valid': False,
                'summary': None,
                'positions_after': [],
                'constraint_checks': None,
            },
        })
        assert metrics['securities_value'] is None
        assert metrics['violations_count'] is None
        assert metrics['positions_count'] == 0
        assert metrics['changed_isins'] == [NEW1]

    def test_unknown_cash_delta_is_none(self):
        """Unknown cash в одном из вариантов → delta None (не 0)."""
        baseline = {'cash_after_estimate': None, 'securities_value': 100.0,
                    'coupon_month': 1.0, 'coupon_year': 12.0,
                    'delta_month': 1.0, 'delta_year': 12.0,
                    'max_issuer_share_pct': 10.0,
                    'positions_count': 3, 'violations_count': 0}
        variant = dict(baseline, cash_after_estimate=50.0)
        from src.services.scenario_compare import _metric_delta
        delta = _metric_delta(baseline, variant)
        assert delta['cash_after_estimate'] is None
        assert delta['securities_value'] == '0'
        assert delta['positions_count'] == 0


# ---------------------------------------------------------------------------
# CLI: prefixed-флаги, diagnostics, factory (additive)
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_setup_parser_defaults(self):
        args = _parse_cli([])
        for prefix in ('a', 'b'):
            assert getattr(args, f'{prefix}_sell_all') is None
            assert getattr(args, f'{prefix}_target_value') is None
            assert getattr(args, f'{prefix}_freq_min') is None
            assert getattr(args, f'{prefix}_freq_max') is None
            assert getattr(args, f'{prefix}_freq_in') is None
            assert getattr(args, f'{prefix}_min_credit_rating') is None
            assert getattr(args, f'{prefix}_exclude_sovereign') is False
            assert getattr(args, f'{prefix}_max_position_value') is None
        assert args.context_id is None
        assert args.format == 'json'

    def test_repeated_flags_accumulate(self):
        args = _parse_cli([
            '--a-sell-all', 'RU1',
            '--b-target-value', f'{NEW1}=12000',
            '--b-target-value', 'RU2=9000',
            '--b-exclude-sovereign', '--b-max-position-value', '350000',
        ])
        assert args.a_sell_all == ['RU1']
        assert args.b_target_value == [f'{NEW1}=12000', 'RU2=9000']
        assert args.b_exclude_sovereign is True
        assert args.b_max_position_value == 350000.0

    def test_setup_parser_candidate_policy_defaults(self):
        """031.1: зеркала candidate-policy флагов default=None — canonical
        defaults решает _filters_from_args (копий значений в CLI нет)."""
        args = _parse_cli([])
        for prefix in ('a', 'b'):
            assert getattr(args, f'{prefix}_max_risk') is None
            assert getattr(args, f'{prefix}_max_listlevel') is None
            assert getattr(args, f'{prefix}_max_ytm') is None
            assert getattr(args, f'{prefix}_include_ku') is None
            assert getattr(args, f'{prefix}_min_maturity') is None
            assert getattr(args, f'{prefix}_max_maturity') is None

    def test_candidate_policy_mirror_flags_parse(self):
        args = _parse_cli([
            '--a-max-risk', '3', '--a-include-ku',
            '--a-min-maturity', '2026-01-01',
            '--b-max-ytm', '50', '--b-max-listlevel', '3',
            '--b-max-maturity', '2030-06-01',
        ])
        assert args.a_max_risk == 3
        assert args.a_include_ku is True
        assert args.a_min_maturity == '2026-01-01'
        assert args.b_max_ytm == 50.0
        assert args.b_max_listlevel == 3
        assert args.b_max_maturity == '2030-06-01'

    def test_freq_in_mirror_flags_parse(self):
        """031.2: зеркала --a-freq-in/--b-freq-in — parsing semantics канона
        отчёта (CSV-список целых)."""
        args = _parse_cli(['--a-freq-in', '2,4'])
        assert args.a_freq_in == [2, 4]
        with pytest.raises(SystemExit):
            _parse_cli(['--b-freq-in', ''])

    def test_candidate_policy_flag_validation_value_error(self, tmp_path):
        """Sanity-порог candidate-фильтра — конвенция legacy отчёта
        (те же правила, что rebalance-plan)."""
        use_case = _make_compare_use_case(_make_fake(), tmp_path)
        with pytest.raises(ValueError, match='--max-ytm'):
            use_case.build_compare(_parse_cli(['--a-max-ytm', '0']))

    def test_command_registered_in_factory_map(self):
        factory = UseCaseFactory.__new__(UseCaseFactory)
        use_case_map = UseCaseFactory.get_use_case_map(factory)
        assert use_case_map['rebalance-compare'] is RebalanceCompareUseCase
        # Additive: существующие команды не тронуты.
        assert 'rebalance-report' in use_case_map
        assert 'rebalance-plan' in use_case_map
        assert 'rebalance-analysis-snapshot' in use_case_map
        assert len(use_case_map) == 39

    def test_build_compare_ab(self, tmp_path):
        """A/B через CLI-флаги: один context, разные ограничения вариантов,
        comparable + delta."""
        fake = _make_fake()
        use_case = _make_compare_use_case(fake, tmp_path)
        payload = use_case.build_compare(_parse_cli([
            '--a-target-value', f'{NEW1}=12000',
            '--b-target-value', f'{NEW1}=12000',
            '--b-max-position-value', '6000',
        ]))
        assert payload['artifact'] == 'rebalance_compare'
        assert payload['comparable'] is True
        assert payload['diagnostics'] == []
        assert payload['metrics']['a']['securities_value'] == 96000.0
        assert payload['metrics']['b']['securities_value'] == 90000.0
        assert payload['delta']['b']['securities_value'] == '-6000'
        # Проекции вариантов — canonical блоки 030.8 (деньги planning-слоя —
        # decimal-строки).
        assert payload['variants']['a']['targets'][0][
            'fitted_target_value_rub'
        ] == '12000'
        assert payload['context_fingerprint'] == _context(fake).context_fingerprint

    def test_candidate_policy_ab_variants(self, tmp_path):
        """Варианты различаются candidate-policy — валидный A/B (031.1):
        a (--a-include-ku) покупает KU-бумагу, b (canonical default) —
        BUY_NOT_ELIGIBLE ('ku'); echo фильтров вариантов различен, raw
        universe общий (strict не удалил строки, нужные a)."""
        ku_isin = 'RU000A0KUBND'
        from test_rebalance_plan import _pool_row

        ku_row = _pool_row(ku_isin, market_price='1000', issuer='КУ Эмитент')
        ku_row['ku'] = True  # policy-признак КУ (structural gate его не читает)
        fake = _make_fake(universe_rows=[
            *[
                _pool_row(
                    f'RU000A0BASE{n}', market_price='1000',
                    issuer=f'Базовый эмитент {n}',
                    held_share_pct=Decimal('14.29'),
                )
                for n in range(BASE_COUNT)
            ],
            ku_row,
        ])
        use_case = _make_compare_use_case(fake, tmp_path)
        payload = use_case.build_compare(_parse_cli([
            '--a-include-ku',
            '--a-target-value', f'{ku_isin}=5000',
            '--b-target-value', f'{ku_isin}=5000',
        ]))
        assert payload['comparable'] is True
        assert payload['diagnostics'] == []
        a_block, b_block = payload['variants']['a'], payload['variants']['b']
        # Per-variant candidate-policy echo (canonical default у b).
        assert a_block['filters']['include_ku'] is True
        assert b_block['filters']['include_ku'] is False
        assert a_block['targets'][0]['status'] == 'FITTED'
        assert b_block['targets'][0]['status'] == 'BUY_NOT_ELIGIBLE'
        assert b_block['targets'][0]['buy_reasons'][0]['code'] == 'ku'
        # KU-покупка только у a: 8 позиций против 7.
        assert payload['metrics']['a']['positions_count'] == 8
        assert payload['metrics']['b']['positions_count'] == 7

    def test_execute_prints_valid_json(self, tmp_path, capsys):
        use_case = _make_compare_use_case(_make_fake(), tmp_path)
        use_case.execute(_parse_cli([
            '--a-target-value', f'{NEW1}=12000',
            '--b-target-value', f'{NEW1}=6000',
        ]))
        payload = json.loads(capsys.readouterr().out)
        assert payload['artifact'] == 'rebalance_compare'
        assert payload['comparable'] is True
        assert payload['delta']['b']['securities_value'] == '-6000'

    def test_invalid_intent_attributed_to_variant(self, tmp_path):
        """Диагностика границы несёт variant; оба варианта собираются до
        отказа; план не строится (честные null)."""
        use_case = _make_compare_use_case(_make_fake(), tmp_path)
        payload = use_case.build_compare(_parse_cli([
            '--a-target-value', 'RU1=abc',
            '--b-sell-all', '  ',
        ]))
        codes = {(d['code'], d['variant']) for d in payload['diagnostics']}
        assert codes == {('INVALID_TARGET_VALUE', 'a'), ('INVALID_INTENT', 'b')}
        assert payload['comparable'] is None
        assert payload['variants'] is None
        assert payload['delta'] is None
        assert payload['context_fingerprint'] is None

    def test_duplicate_intent_in_variant(self, tmp_path):
        use_case = _make_compare_use_case(_make_fake(), tmp_path)
        payload = use_case.build_compare(_parse_cli([
            '--a-target-value', f'{NEW1}=100', '--a-target-value', f'{NEW1}=200',
            '--b-target-value', f'{NEW1}=6000',
        ]))
        diagnostic = payload['diagnostics'][0]
        assert diagnostic['code'] == 'DUPLICATE_TARGET'
        assert diagnostic['variant'] == 'a'
        assert diagnostic['isin'] == NEW1
        assert payload['variants'] is None

    def test_unknown_context_id_machine_readable(self, tmp_path):
        use_case = _make_compare_use_case(_make_fake(), tmp_path)
        payload = use_case.build_compare(_parse_cli([
            '--context-id', 'f' * 32,
            '--a-target-value', f'{NEW1}=12000',
            '--b-target-value', f'{NEW1}=6000',
        ]))
        assert [d['code'] for d in payload['diagnostics']] == ['CONTEXT_NOT_FOUND']
        assert payload['comparable'] is None

    def test_context_id_loads_same_slice(self, tmp_path):
        """Оба варианта по --context-id (030.4) — тот же frozen срез, что и
        свежая сборка: тот же fingerprint и та же delta."""
        fake = _make_fake()
        store = EphemeralContextStore(
            root_dir=tmp_path / 'planning_contexts',
            scope_digest='test-scope',
            rating_scale={},
        )
        context = _context(fake)
        handle = store.save(context)
        argv = [
            '--context-id', handle['context_id'],
            '--a-target-value', f'{NEW1}=12000',
            '--b-target-value', f'{NEW1}=12000',
            '--b-max-position-value', '6000',
        ]
        by_id = _make_compare_use_case(fake, tmp_path, store=store
                                       ).build_compare(_parse_cli(argv))
        fresh = _make_compare_use_case(_make_fake(), tmp_path
                                       ).build_compare(_parse_cli(argv[2:]))
        assert by_id['context_fingerprint'] == fresh['context_fingerprint']
        assert by_id['delta'] == fresh['delta']

    def test_variant_flag_validation_value_error(self, tmp_path):
        """Неверная комбинация флагов-ограничений — ValueError по конвенции
        legacy отчёта (те же правила, что rebalance-plan)."""
        use_case = _make_compare_use_case(_make_fake(), tmp_path)
        with pytest.raises(ValueError, match='--freq-min'):
            use_case.build_compare(_parse_cli(['--b-freq-min', '0']))
        with pytest.raises(ValueError, match='--freq-min'):
            use_case.build_compare(_parse_cli([
                '--a-freq-min', '12', '--a-freq-max', '4',
            ]))
        with pytest.raises(ValueError, match='--max-position-value'):
            use_case.build_compare(_parse_cli(['--a-max-position-value', '0']))

    def test_freq_in_conflicts_with_freq_range_per_variant(self, tmp_path):
        """031.2: --{prefix}-freq-in взаимоисключим с --{prefix}-freq-min/max —
        тот же reused _validate_args плана (ValueError, конвенция отчёта)."""
        use_case = _make_compare_use_case(_make_fake(), tmp_path)
        with pytest.raises(ValueError, match='--freq-min'):
            use_case.build_compare(_parse_cli([
                '--a-freq-min', '4', '--a-freq-in', '2,4',
            ]))
        with pytest.raises(ValueError, match='--freq-max'):
            use_case.build_compare(_parse_cli([
                '--b-freq-max', '12', '--b-freq-in', '2,4',
            ]))

    def test_freq_in_ab_variant(self, tmp_path):
        """Варианты различаются freq-ограничением — валидный A/B (031.2):
        a без списка проходит (freq=4 у всех бумаг среза), b с --b-freq-in 2
        даёт canonical COUPON_FREQUENCY_NOT_IN_LIST на каждую позицию целевого
        портфеля; delta/metrics читают canonical outputs движка."""
        fake = _make_fake()
        use_case = _make_compare_use_case(fake, tmp_path)
        payload = use_case.build_compare(_parse_cli([
            '--a-target-value', f'{NEW1}=12000',
            '--b-target-value', f'{NEW1}=12000',
            '--b-freq-in', '2',
        ]))
        assert payload['comparable'] is True
        assert payload['diagnostics'] == []
        a_block, b_block = payload['variants']['a'], payload['variants']['b']
        assert a_block['violations'] == []
        assert a_block['feasible'] is True
        assert len(b_block['violations']) == len(b_block['positions_after']) == 8
        assert all(
            v['type'] == 'COUPON_FREQUENCY_NOT_IN_LIST'
            for v in b_block['violations']
        )
        assert payload['metrics']['a']['violations_count'] == 0
        assert payload['metrics']['b']['violations_count'] == 8
        assert payload['delta']['b']['violations_count'] == 8

    def test_payload_contains_no_secrets(self, tmp_path):
        use_case = _make_compare_use_case(_make_fake(), tmp_path)
        payload = use_case.build_compare(_parse_cli([
            '--a-target-value', f'{NEW1}=12000',
            '--b-target-value', f'{NEW1}=6000',
        ]))
        raw = json.dumps(payload, ensure_ascii=False)
        for marker in (
            'postgres://', 'POSTGRES_DSN', 'INVEST_TOKEN', 'TELEGRAM_BOT',
            'password', 'secret', 'authorization',
        ):
            assert marker.lower() not in raw.lower(), marker
