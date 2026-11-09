"""
Тесты BUY eligibility gate (задача 030.6): structural + policy.

`src/services/buy_eligibility.py` + canonical таблица gates и построчные
policy-правила скринера в `src/use_cases/bond_filters.py`. Проверяются
требования файла задачи 030.6:

- structural gate = extract из buy-path, не копия: SQL buy-выборки
  хранилища собирается из тех же фрагментов таблицы gates (source-тесты
  связки), предикаты зеркалят фактические gates 030.1;
- missing/stale market price → BUY_NOT_ELIGIBLE с MARKET_PRICE_MISSING |
  MARKET_PRICE_STALE; номинал gate не читает (никакого молчаливого
  nominal fallback);
- matured / perpetual / is_trade_available=false / вне широких пределов
  пула — структурный отказ с конкретной причиной;
- все time-sensitive причины — от frozen as_of (не wall-clock);
- policy eligibility — построчные правила скринера (одна реализация:
  source-тесты + identity причин с ключами excluded отчёта);
  include_held/screener_limit — не eligibility;
- candidate-only max_risk/max_list_level/max_ytm не применяются к другим
  бумагам (оценка чистая, per-ISIN);
- lifecycle: gate обязателен только при qty_delta > 0; sell/reduce и no-op
  не блокируются; held-increase проходит тот же gate без льгот;
- reason codes машиночитаемы: code/actual/limit присутствуют.

Все данные — синтетические canonical raw-строки (деньги — Decimal, как из
БД); реальные составы портфеля и ISIN не используются (AGENTS.md); тесты
не требуют POSTGRES_DSN_TEST. Legacy low-level scenario не затронут — его
контракт покрыт существующими test_rebalance_scenario/test_rebalance_engine.
"""
import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from src.services import buy_eligibility as gate_module
from src.services.buy_eligibility import (
    BUY_ELIGIBLE,
    BUY_NOT_ELIGIBLE,
    BuyEligibility,
    BuyEligibilityReason,
    buy_gate_applies,
    evaluate_buy_eligibility,
    structural_buy_eligible,
    structural_buy_reasons,
)
from src.services.planning_intents import CandidateFilters, default_candidate_filters
from src.use_cases import bond_filters
from src.use_cases import rebalance_report
from src import storage as storage_module

AS_OF = datetime(2025, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
AS_OF_DATE = AS_OF.date()

ISIN = 'RU000A0ZGATE1'

FILTERS = CandidateFilters(
    max_risk=1, max_list_level=2, max_ytm_pct=35.0, include_ku=False,
)


def _row(**overrides):
    """Canonical raw-строка каталога: структурно покупаема на AS_OF."""
    row = {
        'isin': ISIN,
        'ticker': 'GATE1',
        'name': 'Тестовая бумага',
        'nominal': Decimal('1000'),
        'currency': 'rub',
        'is_trade_available': None,
        'perpetual_flag': False,
        'maturity_date': date(2027, 6, 15),
        'risk_level': 1,
        'list_level': 1,
        'coupon_rate_percent': Decimal('12.0'),
        'coupon_quantity_per_year': 4,
        'floating_coupon_flag': False,
        'market_price': Decimal('990'),
        'market_price_updated_at': AS_OF - timedelta(hours=1),
        'ytm_percent': 12.5,
        'ku': False,
        'entity_type': 'corporate',
        'issuer': 'Тестовый эмитент',
    }
    row.update(overrides)
    return row


def _codes(result):
    return [reason.code for reason in result.reasons]


def _codes_with_values(result):
    return [(r.code, r.actual, r.limit) for r in result.reasons]


# ---------------------------------------------------------------------------
# Structural BUY gate: по одной причине на gate
# ---------------------------------------------------------------------------


class TestStructuralGate:
    def test_eligible_row_passes(self):
        result = evaluate_buy_eligibility(ISIN, _row(), AS_OF_DATE, FILTERS)
        assert result.status == BUY_ELIGIBLE
        assert result.buy_eligible is True
        assert result.structural_eligible is True
        assert result.policy_eligible is True
        assert result.reasons == ()

    def test_unsupported_currency(self):
        result = evaluate_buy_eligibility(
            ISIN, _row(currency='usd'), AS_OF_DATE, FILTERS
        )
        assert result.status == BUY_NOT_ELIGIBLE
        assert _codes_with_values(result) == [
            ('UNSUPPORTED_CURRENCY', 'usd', 'rub')
        ]

    def test_trade_unavailable_false_rejected(self):
        # is_trade_available = false не проходит; NULL проходит (не
        # ужесточать молча) — _row() с None уже покрыт test_eligible_row.
        result = evaluate_buy_eligibility(
            ISIN, _row(is_trade_available=False), AS_OF_DATE, FILTERS
        )
        assert _codes(result) == ['TRADE_UNAVAILABLE']
        assert structural_buy_eligible(_row(is_trade_available=True), AS_OF_DATE)

    def test_matured_rejected(self):
        # Погашена ДО as_of: прямой high-level target не обходит gate.
        result = evaluate_buy_eligibility(
            ISIN, _row(maturity_date=AS_OF_DATE - timedelta(days=1)),
            AS_OF_DATE, FILTERS,
        )
        code, actual, limit = _codes_with_values(result)[0]
        assert (result.status, code) == (BUY_NOT_ELIGIBLE, 'MATURED')
        assert actual == (AS_OF_DATE - timedelta(days=1)).isoformat()
        assert limit == AS_OF_DATE.isoformat()

    def test_maturity_boundary_today_passes(self):
        # SQL: maturity >= CURRENT_DATE — погашение в день as_of проходит.
        assert structural_buy_eligible(
            _row(maturity_date=AS_OF_DATE), AS_OF_DATE
        )

    def test_matured_skips_policy(self):
        # Погашенная КУ-бумага вне max_risk: только structural причины,
        # policy поверх structural не оценивается.
        result = evaluate_buy_eligibility(
            ISIN,
            _row(maturity_date=AS_OF_DATE - timedelta(days=30), ku=True,
                 risk_level=5),
            AS_OF_DATE, FILTERS,
        )
        assert _codes(result) == ['MATURED']
        assert result.policy_reasons == ()

    def test_perpetual_rejected(self):
        result = evaluate_buy_eligibility(
            ISIN, _row(perpetual_flag=True), AS_OF_DATE, FILTERS
        )
        assert _codes(result) == ['PERPETUAL_NOT_BUYABLE']

    def test_coupon_data_missing(self):
        result = evaluate_buy_eligibility(
            ISIN, _row(coupon_rate_percent=None), AS_OF_DATE, FILTERS
        )
        assert _codes(result) == ['COUPON_DATA_MISSING']

    def test_market_price_missing_nominal_not_used(self):
        # 018: нет свежей market_price — покупки нет; номинал gate не читает
        # (никакого молчаливого nominal fallback), даже при валидном номинале.
        result = evaluate_buy_eligibility(
            ISIN, _row(market_price=None, nominal=Decimal('1000')),
            AS_OF_DATE, FILTERS,
        )
        assert result.status == BUY_NOT_ELIGIBLE
        assert _codes_with_values(result) == [('MARKET_PRICE_MISSING', None, 0)]

    def test_market_price_zero_rejected(self):
        result = evaluate_buy_eligibility(
            ISIN, _row(market_price=Decimal('0')), AS_OF_DATE, FILTERS
        )
        assert _codes(result) == ['MARKET_PRICE_MISSING']

    def test_market_price_stale_relative_to_as_of(self):
        # 25 часов до as_of — вне окна 24h ОТНОСИТЕЛЬНО frozen as_of.
        stale = _row(market_price_updated_at=AS_OF - timedelta(hours=25))
        result = evaluate_buy_eligibility(ISIN, stale, AS_OF, FILTERS)
        code, actual, limit = _codes_with_values(result)[0]
        assert (result.status, code) == (BUY_NOT_ELIGIBLE, 'MARKET_PRICE_STALE')
        assert actual == (AS_OF - timedelta(hours=25)).isoformat()
        assert limit == (AS_OF - timedelta(hours=24)).isoformat()
        # 23 часа до as_of — окно пройдено.
        fresh = _row(market_price_updated_at=AS_OF - timedelta(hours=23))
        assert structural_buy_eligible(fresh, AS_OF)

    def test_risk_out_of_pool_even_with_relaxed_policy(self):
        # Широкие пределы пула — structural: прямой запрос с max_risk=6
        # риск-6 бумагу не «вернёт» в gate (screener pool её не показывает).
        result = evaluate_buy_eligibility(
            ISIN, _row(risk_level=6),
            AS_OF_DATE,
            CandidateFilters(max_risk=6, max_list_level=3, max_ytm_pct=100.0,
                             include_ku=True),
        )
        assert _codes_with_values(result) == [('RISK_OUT_OF_POOL', 6, 5)]

    def test_risk_none_out_of_pool(self):
        # SQL: bc.risk_level <= %s — NULL не проходит (как в SQL).
        result = evaluate_buy_eligibility(
            ISIN, _row(risk_level=None), AS_OF_DATE, FILTERS
        )
        assert _codes_with_values(result) == [('RISK_OUT_OF_POOL', None, 5)]

    def test_list_level_out_of_pool(self):
        result = evaluate_buy_eligibility(
            ISIN, _row(list_level=4), AS_OF_DATE, FILTERS
        )
        assert _codes_with_values(result) == [('LIST_LEVEL_OUT_OF_POOL', 4, 3)]
        # NULL list_level проходит (SQL: list_level IS NULL OR ...).
        assert structural_buy_eligible(_row(list_level=None), AS_OF_DATE)

    def test_multiple_structural_reasons_in_canonical_order(self):
        result = evaluate_buy_eligibility(
            ISIN,
            _row(currency='usd', perpetual_flag=True,
                 market_price_updated_at=None),
            AS_OF_DATE, FILTERS,
        )
        assert _codes(result) == [
            'UNSUPPORTED_CURRENCY', 'PERPETUAL_NOT_BUYABLE',
            'MARKET_PRICE_STALE',
        ]


# ---------------------------------------------------------------------------
# Frozen as_of: time-sensitive причины не зависят от wall-clock
# ---------------------------------------------------------------------------


class TestFrozenAsOf:
    def test_matured_computed_from_as_of_not_wall_clock(self):
        # Бумага, погашенная после as_of (2025) и до wall-clock (2026):
        # структурно покупаема В КОНТЕКСТЕ среза.
        row = _row(maturity_date=date(2026, 1, 15))
        assert structural_buy_eligible(row, AS_OF_DATE)
        # Тот же row в новом контексте с продвинутым as_of — уже погашена.
        assert not structural_buy_eligible(row, date(2026, 6, 1))

    def test_stale_computed_from_as_of_not_wall_clock(self):
        # Цена, записанная за час до as_of-2025: свежа в том контексте,
        # даже если wall-clock ушёл на годы вперёд.
        row = _row(market_price_updated_at=AS_OF - timedelta(hours=1))
        assert structural_buy_eligible(row, AS_OF)
        assert not structural_buy_eligible(row, AS_OF + timedelta(days=2))

    def test_reasons_deterministic_for_same_input(self):
        row = _row()
        first = evaluate_buy_eligibility(ISIN, row, AS_OF, FILTERS)
        second = evaluate_buy_eligibility(ISIN, dict(row), AS_OF, FILTERS)
        assert first == second


# ---------------------------------------------------------------------------
# Policy eligibility: одна реализация со скринером
# ---------------------------------------------------------------------------


class TestPolicyEligibility:
    def test_defaults_from_canonical_source(self):
        # default_candidate_filters() (030.5) — canonical defaults отчёта;
        # eligibility по ним совпадает с дефолтным скринером.
        filters = default_candidate_filters()
        assert (filters.max_risk, filters.max_list_level) == (1, 2)
        assert (filters.max_ytm_pct, filters.include_ku) == (35.0, False)
        assert filters.min_maturity is None and filters.max_maturity is None

    def test_policy_risk_narrows_pool(self):
        result = evaluate_buy_eligibility(
            ISIN, _row(risk_level=2), AS_OF_DATE, FILTERS
        )
        assert result.structural_eligible is True
        assert _codes_with_values(result) == [('risk', 2, 1)]

    def test_policy_listlevel_narrows_pool(self):
        result = evaluate_buy_eligibility(
            ISIN, _row(list_level=3), AS_OF_DATE, FILTERS
        )
        assert _codes_with_values(result) == [('listlevel', 3, 2)]

    def test_policy_ku_default_excluded(self):
        result = evaluate_buy_eligibility(ISIN, _row(ku=True), AS_OF_DATE,
                                          FILTERS)
        assert _codes_with_values(result) == [('ku', True, False)]
        included = CandidateFilters(
            max_risk=1, max_list_level=2, max_ytm_pct=35.0, include_ku=True,
        )
        assert evaluate_buy_eligibility(ISIN, _row(ku=True), AS_OF_DATE,
                                        included).buy_eligible is True

    def test_policy_fixed_ytm_ceiling_and_missing(self):
        result = evaluate_buy_eligibility(
            ISIN, _row(ytm_percent=40.0), AS_OF_DATE, FILTERS
        )
        assert _codes_with_values(result) == [('fixed_max_ytm', 40.0, 35.0)]
        missing = evaluate_buy_eligibility(
            ISIN, _row(ytm_percent=None), AS_OF_DATE, FILTERS
        )
        assert _codes_with_values(missing) == [('fixed_ytm_missing', None, 35.0)]

    def test_policy_floater_uses_coupon_rate(self):
        floater = _row(floating_coupon_flag=True, ytm_percent=None,
                       coupon_rate_percent=Decimal('40.0'))
        result = evaluate_buy_eligibility(ISIN, floater, AS_OF_DATE, FILTERS)
        assert _codes_with_values(result) == [
            ('floater_max_ytm', 40.0, 35.0)
        ]
        # Флоатер вовсе без купонных данных не доходит до policy: строка не
        # проходит structural COUPON_DATA_MISSING (как SQL buy-выборки —
        # COALESCE IS NOT NULL; floater_coupon_missing у скринера остаётся
        # защитным счётчиком, для canonical строк недостижимым).
        no_coupon = evaluate_buy_eligibility(
            ISIN, dict(floater, coupon_rate_percent=None), AS_OF_DATE, FILTERS
        )
        assert _codes_with_values(no_coupon) == [
            ('COUPON_DATA_MISSING', None, None)
        ]

    def test_policy_maturity_window(self):
        min_filter = CandidateFilters(
            max_risk=1, max_list_level=2, max_ytm_pct=35.0, include_ku=False,
            min_maturity='2028-01-01',
        )
        result = evaluate_buy_eligibility(
            ISIN, _row(maturity_date=date(2027, 6, 15)), AS_OF_DATE, min_filter
        )
        assert _codes_with_values(result) == [
            ('min_maturity', '2027-06-15', '2028-01-01')
        ]
        max_filter = CandidateFilters(
            max_risk=1, max_list_level=2, max_ytm_pct=35.0, include_ku=False,
            max_maturity='2026-01-01',
        )
        capped = evaluate_buy_eligibility(
            ISIN, _row(maturity_date=date(2027, 6, 15)), AS_OF_DATE, max_filter
        )
        assert _codes_with_values(capped) == [
            ('max_maturity', '2027-06-15', '2026-01-01')
        ]

    def test_policy_identity_with_screener_rules(self):
        # Одна реализация: причины eligibility — те же (key, actual, limit),
        # что возвращает построчная функция скринера на той же строке.
        row = _row(risk_level=3)
        rejection = bond_filters.screener_policy_rejection(
            row, include_ku=FILTERS.include_ku, max_risk=FILTERS.max_risk,
            max_listlevel=FILTERS.max_list_level,
        )
        result = evaluate_buy_eligibility(ISIN, row, AS_OF_DATE, FILTERS)
        assert _codes_with_values(result) == [
            (rejection.key, rejection.actual, rejection.limit)
        ]

    def test_freq_sovereign_rating_not_candidate_policy(self):
        # freq/sovereign/rating — final-portfolio constraints (030.7): в
        # candidate-policy не применяются, суверен без exclude-политики
        # структурно и policy покупаем.
        sovereign = _row(entity_type='sovereign', issuer='Минфин России',
                         name='ОФЗ 26251')
        result = evaluate_buy_eligibility(ISIN, sovereign, AS_OF_DATE, FILTERS)
        assert result.buy_eligible is True


# ---------------------------------------------------------------------------
# Presentation/discovery controls — НЕ eligibility
# ---------------------------------------------------------------------------


class TestPresentationControlsNotEligibility:
    def test_held_share_is_not_policy_rejection(self):
        # held-бейдж скринера (include_held — presentation control) не
        # превращается в eligibility запрет.
        held = _row(held_share_pct=Decimal('5.00'))
        assert evaluate_buy_eligibility(
            ISIN, held, AS_OF_DATE, FILTERS
        ).buy_eligible is True

    def test_policy_rules_have_no_discovery_parameters(self):
        signature = inspect.signature(bond_filters.screener_policy_rejection)
        assert 'include_held' not in signature.parameters
        assert 'screener_limit' not in signature.parameters
        assert 'limit' not in signature.parameters

    def test_candidate_filters_do_not_carry_discovery_controls(self):
        # screener_limit/сортировка top-N — контракт отчёта, не фильтров.
        fields = {f.name for f in CandidateFilters.__dataclass_fields__.values()}
        assert 'screener_limit' not in fields
        assert 'include_held' not in fields


# ---------------------------------------------------------------------------
# Lifecycle: gate только для BUY/increase
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_buy_gate_applies_only_to_increase(self):
        assert buy_gate_applies(1) is True
        assert buy_gate_applies(0.5) is True
        assert buy_gate_applies(0) is False      # no-op не блокируется
        assert buy_gate_applies(-5) is False     # sell/reduce без gate

    def test_reduction_of_ineligible_held_bond_allowed(self):
        # Плохая/погашенная удерживаемая бумага: eligibility отрицательная,
        # но сокращение (qty_delta < 0) gate не проходит — продаёмо.
        ineligible = _row(maturity_date=AS_OF_DATE - timedelta(days=1))
        result = evaluate_buy_eligibility(ISIN, ineligible, AS_OF_DATE, FILTERS)
        assert result.status == BUY_NOT_ELIGIBLE
        assert buy_gate_applies(-10) is False

    def test_held_increase_same_gate_as_new_position(self):
        # Held increase проходит тот же gate без льгот: у held-строки со
        # stale ценой та же структурная причина, что у новой позиции.
        stale_held = _row(market_price_updated_at=AS_OF - timedelta(hours=30),
                          held_share_pct=Decimal('3.00'))
        held_result = evaluate_buy_eligibility(ISIN, stale_held, AS_OF, FILTERS)
        new_result = evaluate_buy_eligibility(
            ISIN, dict(stale_held, held_share_pct=None), AS_OF, FILTERS
        )
        assert held_result == new_result
        assert _codes(held_result) == ['MARKET_PRICE_STALE']

    def test_candidate_filters_not_applied_to_untouched_holdings(self):
        # Candidate-only фильтры — оценка конкретной бумаги: чужие строки
        # (untouched holdings) gate'ом не фильтруются и не переписываются.
        strict = CandidateFilters(max_risk=1, max_list_level=1,
                                  max_ytm_pct=10.0, include_ku=False)
        candidate = evaluate_buy_eligibility(
            ISIN, _row(risk_level=2, list_level=2, ytm_percent=9.0),
            AS_OF_DATE, strict,
        )
        holding = evaluate_buy_eligibility(
            'RU000A0ZHELD9', _row(isin='RU000A0ZHELD9', ytm_percent=9.0),
            AS_OF_DATE, strict,
        )
        assert candidate.buy_eligible is False
        assert holding.buy_eligible is True  # другая бумага — своя оценка


# ---------------------------------------------------------------------------
# Machine-readable reasons
# ---------------------------------------------------------------------------


class TestMachineReadableReasons:
    def test_structural_reason_codes_canonical(self):
        codes = {gate.code for gate in bond_filters.STRUCTURAL_BUY_GATES}
        assert codes == {
            'UNSUPPORTED_CURRENCY', 'TRADE_UNAVAILABLE', 'MATURED',
            'PERPETUAL_NOT_BUYABLE', 'COUPON_DATA_MISSING',
            'MARKET_PRICE_MISSING', 'MARKET_PRICE_STALE',
            'RISK_OUT_OF_POOL', 'LIST_LEVEL_OUT_OF_POOL',
        }

    def test_all_reasons_carry_code_actual_limit(self):
        violating = _row(currency='usd', risk_level=6,
                         market_price=Decimal('0'))
        result = evaluate_buy_eligibility(ISIN, violating, AS_OF_DATE, FILTERS)
        assert result.status == BUY_NOT_ELIGIBLE
        for reason in result.reasons:
            assert isinstance(reason, BuyEligibilityReason)
            assert isinstance(reason.code, str) and reason.code
            # Поля присутствуют у каждой причины (значение может быть None,
            # где неприменимо).
            assert 'actual' in reason.__dict__
            assert 'limit' in reason.__dict__

    def test_buy_not_eligible_label_canonical(self):
        assert BUY_NOT_ELIGIBLE == 'BUY_NOT_ELIGIBLE'
        assert gate_module.BUY_ELIGIBLE == 'BUY_ELIGIBLE'
        result = evaluate_buy_eligibility(
            ISIN, _row(perpetual_flag=True), AS_OF_DATE, FILTERS
        )
        assert result.status == BUY_NOT_ELIGIBLE
        assert isinstance(result, BuyEligibility)


# ---------------------------------------------------------------------------
# Extract, не копия: связка SQL buy-path и Python gate
# ---------------------------------------------------------------------------


class TestSingleImplementationWiring:
    def test_storage_buy_where_composed_from_gate_table(self):
        # Правило 030 №1: SQL buy-выборки собирается из фрагментов той же
        # таблицы gates, по которой оценивается строка; ручной копии условий
        # в buy-ветке хранилища нет (проверяется source самой ветки).
        source = inspect.getsource(
            storage_module.PortfolioStorage.get_bonds_yield_table
        )
        assert 'STRUCTURAL_BUY_WHERE_SQL' in source
        assert 'BUY_MATURITY_FLOOR_SQL' in source
        # Явно выписанные legacy-условия из WHERE buy-ветки не вернулись.
        assert "bc.market_price_updated_at >= CURRENT_TIMESTAMP" not in source
        assert "(bc.is_trade_available IS TRUE" not in source

    def test_gate_fragments_are_canonical_sql_conditions(self):
        fragments = ' '.join(gate.sql for gate in bond_filters.STRUCTURAL_BUY_GATES)
        # Фактические gates канонического buy-path (030.1).
        assert "bc.currency = 'rub'" in fragments
        assert ('bc.is_trade_available IS TRUE '
                'OR bc.is_trade_available IS NULL') in fragments
        assert 'bc.perpetual_flag IS NOT TRUE' in fragments
        assert 'bc.maturity_date >= CURRENT_DATE' in fragments
        assert 'bc.risk_level <= %s' in fragments
        assert ('COALESCE(bc.coupon_rate_percent, '
                'mc_floater.metric_value::numeric) IS NOT NULL') in fragments
        assert '(bc.market_price IS NOT NULL AND bc.market_price > 0)' in fragments
        assert ("bc.market_price_updated_at >= CURRENT_TIMESTAMP "
                "- INTERVAL '24 hours'") in fragments
        assert '(bc.list_level IS NULL OR bc.list_level <= %s)' in fragments

    def test_maturity_floor_is_replaceable_by_policy_window(self):
        # Нижняя граница — единственный gate, который policy min_maturity
        # заменяет параметром (семантика 002.3/P9.5); fixed-блок её не содержит.
        matured = next(gate for gate in bond_filters.STRUCTURAL_BUY_GATES
                       if gate.code == 'MATURED')
        assert matured.sql == bond_filters.BUY_MATURITY_FLOOR_SQL
        assert bond_filters.BUY_MATURITY_FLOOR_SQL \
            not in bond_filters.STRUCTURAL_BUY_WHERE_SQL
        assert bond_filters.STRUCTURAL_BUY_WHERE_SQL.startswith("bc.currency")

    def test_screener_uses_shared_policy_implementation(self):
        # Скринер отчёта и eligibility — одна построчная реализация.
        screener_source = inspect.getsource(
            rebalance_report.RebalanceReportUseCase._build_screener
        )
        assert 'screener_policy_rejection(' in screener_source
        assert "excluded[rejection.key] += 1" in screener_source
        for method in ('_finish_fixes', '_finish_floaters'):
            source = inspect.getsource(
                getattr(rebalance_report.RebalanceReportUseCase, method)
            )
            assert 'screener_ytm_rejection(' in source

    def test_pool_limits_single_source(self):
        # Широкие пределы пула — одна копия: SQL-параметры raw universe
        # (planning_context), reэкспорт отчёта и предикаты gate.
        assert rebalance_report._SCREENER_MAX_RISK is bond_filters.POOL_MAX_RISK
        assert rebalance_report._SCREENER_MAX_LISTLEVEL \
            is bond_filters.POOL_MAX_LISTLEVEL
        assert bond_filters.MARKET_PRICE_FRESHNESS_HOURS == 24
        assert bond_filters.BUY_UNIVERSE_CURRENCY == 'rub'

    def test_permissive_scenario_rows_carry_gate_fields(self):
        # get_scenario_universe_rows отдаёт поля structural gate (030.6):
        # классификация причин возможна по canonical permissive-строке.
        source = inspect.getsource(
            storage_module.PortfolioStorage.get_scenario_universe_rows
        )
        for field in ('bc.currency', 'bc.is_trade_available',
                      'bc.market_price_updated_at'):
            assert field in source
