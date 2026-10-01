"""
Тесты analysis snapshot — read-only вход для неизвестной аналитики (030.10).

Проверяются требования файла задачи 030.10:
- snapshot содержит schema/policy версии, context_id/fingerprint/as_of,
  canonical data_gate и достаточный universe, но не secrets;
- context_id загружает тот же core-owned PlanningContext/fingerprint (030.4);
- snapshot не ограничен presentation top-N (screener_limit отчёта);
- capabilities/limitations объявлены; canonical NULL сохраняется + reason;
- snapshot различает portfolio presence и structural BUY eligibility;
- анализ, требующий отсутствующего exact coupon schedule, получает data gap
  (ANALYSIS_DATA_UNAVAILABLE), данные не синтезируются;
- default output compact (metadata/preview), полный payload — явно (--full).

Все данные — синтетические (in-memory двойник PortfolioStorage), реальные
составы портфеля и ISIN не используются (AGENTS.md); тесты не требуют
POSTGRES_DSN_TEST. Эфемерный store — во временный каталог (030.4).
"""
import argparse
import contextlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.services.context_store import EphemeralContextStore
from src.services.planning_context import (
    CALCULATION_POLICY_VERSION,
    CONTEXT_SCHEMA_VERSION,
    RAW_UNIVERSE_POLICY,
    PlanningContext,
)
from src.services.rebalance_engine import (
    PLANNING_PRICE_BASIS_HELD_VALUATION,
    PLANNING_PRICE_BASIS_MARKET,
    PLANNING_PRICE_BASIS_MATURED_ZERO,
)
from src.use_cases.analysis_snapshot import (
    ANALYSIS_DATA_UNAVAILABLE,
    AVAILABLE_DATASETS,
    PREVIEW_ROWS,
    SNAPSHOT_SCHEMA_VERSION,
    UNAVAILABLE_DATASETS,
    AnalysisSnapshotUseCase,
)
from src.use_cases.check_db import classify_freshness

AS_OF = datetime(2025, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
AS_OF_DATE = AS_OF.date()

HELD1 = 'RU000A0ZSNP1'      # в портфеле и в canonical buy-пуле
HELD_MATURED = 'RU000A0ZSNP2'  # в портфеле, вне buy-пула (погашена)
NEW1 = 'RU000A0ZSNP3'       # только в buy-пуле
NEW_FLOATER = 'RU000A0ZSNP4'  # в пуле, YTM = NULL + canonical reason

ScreenerLimitDefault = 10  # presentation top-N отчёта (029-T06)


def _held_row(isin, *, qty='10', price='1000', maturity=date(2027, 6, 15)):
    """Синтетическая позиция портфеля (форма get_rebalance_portfolio_rows)."""
    return {
        'isin': isin,
        'ticker': isin[-5:],
        'name': 'Бумага ' + isin,
        'nominal': Decimal('1000'),
        'coupon_rate_percent': Decimal('12.0'),
        'coupon_quantity_per_year': 4,
        'maturity_date': maturity,
        'offer_date': date(2026, 5, 10),
        'risk_level': 2,
        'list_level': 1,
        'amortization_flag': False,
        'floating_coupon_flag': False,
        'perpetual_flag': False,
        'ku': False,
        'market_price': Decimal('1010'),
        'ytm': None,
        'ytm_null_reason': None,
        'ytm_updated_at': None,
        'company_id': 1,
        'issuer': 'Эмитент ' + isin,
        'entity_type': 'corporate',
        'credit_rating': 'AA-',
        'rating_score': 18,
        'quantity': Decimal(qty),
        'value_rub': Decimal(qty) * Decimal(price),
        'valuation_source': 'broker',
        'price': Decimal(price),
    }


def _universe_row(
    isin,
    *,
    market_price='900',
    maturity=date(2027, 6, 15),
    ytm=None,
    ytm_null_reason=None,
    ytm_updated_at=None,
    held_share_pct=None,
    floating_coupon_flag=False,
):
    """Синтетическая строка canonical buy-пула (форма get_bonds_yield_table,
    mode='buy', include_held=True)."""
    return {
        'isin': isin,
        'ticker': isin[-5:],
        'name': 'Бумага ' + isin,
        'nominal': Decimal('1000'),
        'coupon_rate_percent': Decimal('12.0'),
        'coupon_quantity_per_year': 4,
        'maturity_date': maturity,
        'risk_level': 2,
        'list_level': 1,
        'amortization_flag': False,
        'current_price': Decimal(market_price),
        'real_yield_percent': Decimal('12.0'),
        'market_price': Decimal(market_price),
        'ytm': ytm,
        'ytm_null_reason': ytm_null_reason,
        'ytm_updated_at': ytm_updated_at,
        'floating_coupon_flag': floating_coupon_flag,
        'perpetual_flag': False,
        'total_monthly_coupon_payment': Decimal('3.00'),
        'coupon_type': 'Фиксированный',
        'ku': False,
        'issuer': 'Эмитент ' + isin,
        'entity_type': 'corporate',
        'credit_rating': 'AA-',
        'rating_score': 18,
        'held_share_pct': held_share_pct,
    }


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

_CASH_KNOWN = {
    'cash_available_rub': Decimal('50000.00'),
    'cash_updated_at': AS_OF - timedelta(minutes=10),
    'cash_known': True,
}
_CASH_UNKNOWN = {
    'cash_available_rub': None,
    'cash_updated_at': None,
    'cash_known': False,
}


class FakeSnapshotStorage:
    """In-memory двойник PortfolioStorage: контракт PlanningContextBuilder."""

    def __init__(self, *, freshness, cash, held_rows, universe_rows):
        self.now = AS_OF
        self.freshness_data = dict(freshness)
        self.cash_data = dict(cash)
        self.held_rows = list(held_rows)
        self.universe_rows = list(universe_rows)
        self.snapshot_depth = 0
        self.universe_kwargs = None

    @contextlib.contextmanager
    def snapshot_transaction(self):
        self.snapshot_depth += 1
        try:
            yield self
        finally:
            self.snapshot_depth -= 1

    def get_transaction_timestamp(self):
        return {'ts': self.now, 'day': self.now.date()}

    def get_db_freshness(self, stale_threshold_hours=24, now=None):
        return dict(self.freshness_data)

    def get_available_cash_rub(self):
        return dict(self.cash_data)

    def get_rebalance_portfolio_rows(self):
        return [dict(row) for row in self.held_rows]

    def get_bonds_yield_table(self, **kwargs):
        self.universe_kwargs = dict(kwargs)
        return [dict(row) for row in self.universe_rows]


def _make_fake(
    *,
    cash=_CASH_KNOWN,
    held_rows=None,
    universe_rows=None,
):
    if held_rows is None:
        held_rows = [_held_row(HELD1), _held_row(
            HELD_MATURED, maturity=AS_OF_DATE - timedelta(days=1),
        )]
    if universe_rows is None:
        universe_rows = (
            [_universe_row(
                HELD1, market_price='1000', held_share_pct=Decimal('50.0'),
            )]
            + [_universe_row(f'RU000A0ZPAD{i:02d}') for i in range(11)]
            + [_universe_row(
                NEW_FLOATER,
                ytm=None,
                ytm_null_reason='floating_coupon',
                ytm_updated_at=AS_OF - timedelta(hours=3),
                floating_coupon_flag=True,
            )]
        )
    return FakeSnapshotStorage(
        freshness=_FRESH_FRESHNESS, cash=cash,
        held_rows=held_rows, universe_rows=universe_rows,
    )


def _make_use_case(fake, tmp_path):
    store = EphemeralContextStore(
        root_dir=tmp_path / 'planning_contexts',
        scope_digest='test-scope',
        rating_scale={},
    )
    return AnalysisSnapshotUseCase(db=fake, rating_scale={}, store=store)


def _args(**overrides):
    defaults = {'format': 'json', 'full': False, 'requires': None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def snapshot(tmp_path):
    """Полный snapshot на синтетическом срезе + его fake-хранилище."""
    fake = _make_fake()
    use_case = _make_use_case(fake, tmp_path)
    return use_case.build_snapshot(_args(full=True)), fake, use_case


# ---------------------------------------------------------------------------
# Идентификация среза: версии, context_id, fingerprint, as_of, data_gate
# ---------------------------------------------------------------------------


class TestContextIdentity:
    def test_schema_policy_versions_gate_and_as_of(self, snapshot):
        payload, fake, _ = snapshot
        ctx = payload['context']

        assert payload['snapshot_schema_version'] == SNAPSHOT_SCHEMA_VERSION
        assert ctx['context_schema_version'] == CONTEXT_SCHEMA_VERSION
        assert ctx['calculation_policy_version'] == CALCULATION_POLICY_VERSION
        assert ctx['calculation_policy_version_derived'].startswith('sha256-')
        assert ctx['as_of'] == '2025-03-10T12:00:00+00:00'
        assert ctx['as_of_date'] == AS_OF_DATE.isoformat()
        # TTL-окно handle — ось store, отличная от data_gate.
        assert ctx['ttl_seconds'] > 0
        assert ctx['expires_at'] > ctx['created_at']
        assert ctx['positions_updated_at'] == (
            '2025-03-10T11:45:00+00:00'
        )
        assert ctx['cash_updated_at'] == '2025-03-10T11:50:00+00:00'

        criticals, warnings, gate_status, is_fresh = classify_freshness(
            fake.freshness_data, now=AS_OF
        )
        assert ctx['data_gate'] == {
            'status': 'OK',
            'gate_status': gate_status,
            'is_fresh': is_fresh,
            'criticals': list(criticals),
            'warnings': list(warnings),
        }

    def test_context_id_loads_same_core_owned_context(self, snapshot, tmp_path):
        """context_id из payload загружает из store ТОТ ЖЕ core-owned
        PlanningContext с тем же fingerprint (030.4)."""
        payload, _, use_case = snapshot
        context_id = payload['context']['context_id']
        fingerprint = payload['context']['context_fingerprint']

        store = EphemeralContextStore(
            root_dir=tmp_path / 'planning_contexts',
            scope_digest='test-scope',
            rating_scale={},
        )
        loaded = store.load(context_id)
        assert isinstance(loaded, PlanningContext)
        assert loaded.context_fingerprint == fingerprint

    def test_fingerprint_matches_payload_context(self, snapshot):
        payload, _, _ = snapshot
        # Fingerprint в payload — от того же среза (store.save вернул handle
        # с fingerprint контекста, см. 030.4).
        assert len(payload['context']['context_fingerprint']) == 64


# ---------------------------------------------------------------------------
# Universe без presentation top-N
# ---------------------------------------------------------------------------


class TestUniverseNotTopN:
    def test_snapshot_uses_canonical_raw_universe_policy(self, snapshot):
        payload, fake, _ = snapshot
        # Срез запрошен канонической политикой raw buy-пула (та же, что у
        # plan/compare), а не фильтрами скринера отчёта.
        assert fake.universe_kwargs == RAW_UNIVERSE_POLICY
        assert fake.universe_kwargs['limit'] == 10000
        assert fake.universe_kwargs['include_held'] is True

    def test_row_count_exceeds_presentation_screener_limit(self, snapshot):
        """13 строк в срезе при presentation screener_limit=10: snapshot не
        обрезан top-N — все строки canonical buy-пула в payload."""
        payload, _, _ = snapshot
        universe = payload['universe']

        assert universe['row_count'] == 13
        assert ScreenerLimitDefault < universe['row_count']
        assert len(universe['rows']) == universe['row_count']
        assert universe['policy']['limit'] == 10000
        # Политика публикуется рядом: limit — потолок SQL-пула, не top-N.
        assert 'screener_limit' not in universe['policy']


# ---------------------------------------------------------------------------
# Portfolio: cash и позиции
# ---------------------------------------------------------------------------


class TestPortfolio:
    def test_cash_decimal_string_and_known_flag(self, snapshot):
        payload, _, _ = snapshot
        cash = payload['portfolio']['cash']
        assert cash == {
            'cash_available_rub': '50000',
            'cash_known': True,
        }

    def test_unknown_cash_stays_null(self, tmp_path):
        use_case = _make_use_case(
            _make_fake(cash=_CASH_UNKNOWN), tmp_path
        )
        payload = use_case.build_snapshot(_args(full=True))
        assert payload['portfolio']['cash'] == {
            'cash_available_rub': None,
            'cash_known': False,
        }

    def test_position_fields_relay_canonical_slice(self, snapshot):
        """Позиция: canonical оценка view + planning price/basis движка +
        freshness metadata; арифметики в snapshot нет."""
        payload, _, _ = snapshot
        positions = {
            pos['isin']: pos for pos in payload['portfolio']['positions']
        }
        pos = positions[HELD1]
        assert pos['qty'] == '10'
        assert pos['price'] == '1000'
        assert pos['value_rub'] == '10000'
        assert pos['valuation_source'] == 'broker'
        assert pos['offer'] == '2026-05-10'
        assert pos['maturity'] == '2027-06-15'
        assert pos['planning_price'] == 1000.0
        assert pos['planning_price_basis'] == PLANNING_PRICE_BASIS_HELD_VALUATION
        assert pos['issuer'] == 'Эмитент ' + HELD1
        assert pos['credit_rating'] == 'AA-'
        assert pos['rating_score'] == 18

        # Погашенная позиция: planning price 0 по frozen as_of (движок).
        matured = positions[HELD_MATURED]
        assert matured['planning_price_basis'] == (
            PLANNING_PRICE_BASIS_MATURED_ZERO
        )


# ---------------------------------------------------------------------------
# NULL + reason, аналитические поля
# ---------------------------------------------------------------------------


class TestNullPreservationAndAnalytics:
    def test_ytm_null_keeps_canonical_reason(self, snapshot):
        """ytm_percent = NULL сохраняется + canonical ytm_reason; нулём не
        подменяется."""
        payload, _, _ = snapshot
        rows = {row['isin']: row for row in payload['universe']['rows']}
        floater = rows[NEW_FLOATER]
        assert floater['ytm_pct'] is None
        assert floater['ytm_reason'] == 'floating_coupon'

    def test_ytm_and_freshness_metadata_present(self, snapshot):
        payload, _, _ = snapshot
        rows = {row['isin']: row for row in payload['universe']['rows']}
        held_in_pool = rows[HELD1]
        assert isinstance(held_in_pool['ytm_pct'], float)
        assert held_in_pool['ytm_reason'] is None
        # ytm_updated_at есть только у строк с backfill-меткой.
        assert rows[NEW_FLOATER]['ytm_updated_at'] == (
            '2025-03-10T09:00:00+00:00'
        )
        assert held_in_pool['ytm_updated_at'] is None

    def test_buy_candidate_planning_price_from_market(self, snapshot):
        payload, _, _ = snapshot
        rows = {row['isin']: row for row in payload['universe']['rows']}
        assert rows['RU000A0ZPAD00']['planning_price'] == 900.0
        assert rows['RU000A0ZPAD00']['planning_price_basis'] == (
            PLANNING_PRICE_BASIS_MARKET
        )


# ---------------------------------------------------------------------------
# Portfolio presence ≠ structural BUY eligibility (030.6)
# ---------------------------------------------------------------------------


class TestBuyEligibilityDistinction:
    def test_universe_rows_are_structurally_eligible(self, snapshot):
        """Строка canonical buy-пула прошла structural gates buy-выборки в
        SQL на frozen as_of — eligibility=true, reasons=[] (релей buy-path,
        не пересчёт)."""
        payload, _, _ = snapshot
        for row in payload['universe']['rows']:
            assert row['structural_buy_eligible'] is True
            assert row['structural_buy_reasons'] == []

    def test_held_in_pool_vs_out_of_pool(self, snapshot):
        """«Есть в портфеле» ≠ «можно покупать»: позиция в canonical buy-пуле
        eligible, погашенная позиция — в payload портфеля (presence), но
        вне пула → NOT_IN_BUY_POOL (тот же код, что fitter 030.7)."""
        payload, _, _ = snapshot
        positions = {
            pos['isin']: pos for pos in payload['portfolio']['positions']
        }

        in_pool = positions[HELD1]
        assert in_pool['in_buy_universe'] is True
        assert in_pool['structural_buy_eligible'] is True
        assert in_pool['structural_buy_reasons'] == []

        out_of_pool = positions[HELD_MATURED]
        assert out_of_pool['in_buy_universe'] is False
        assert out_of_pool['structural_buy_eligible'] is False
        assert out_of_pool['structural_buy_reasons'] == [
            {'code': 'NOT_IN_BUY_POOL', 'actual': None, 'limit': None}
        ]
        # При этом presence честный: позиция есть в блоке портфеля.
        assert out_of_pool['qty'] == '10'


# ---------------------------------------------------------------------------
# Capabilities / limitations и data gap
# ---------------------------------------------------------------------------


class TestCapabilitiesAndDataGap:
    def test_capabilities_declared(self, snapshot):
        payload, _, _ = snapshot
        caps = payload['capabilities']
        assert caps['read_only'] is True
        assert caps['creates_trades_or_scenarios'] is False
        assert caps['analysis_data_gap_code'] == ANALYSIS_DATA_UNAVAILABLE
        assert caps['available_datasets'] == AVAILABLE_DATASETS
        assert 'ytm' in caps['available_datasets']
        assert 'buy_universe' in caps['available_datasets']
        assert 'web' not in caps  # никакого внешнего/брокерского capability
        # Приватность: приватный portfolio artifact.
        assert 'не отправлять' in caps['privacy']

    def test_limitations_declare_known_gaps(self, snapshot):
        payload, _, _ = snapshot
        codes = {item['code']: item['reason'] for item in payload['limitations']}
        assert codes == UNAVAILABLE_DATASETS
        assert 'exact_coupon_schedule' in codes
        assert 'amortization_schedule' in codes
        assert 'settlement_costs' in codes
        assert 'per_instrument_duration' in codes

    def test_requires_unavailable_dataset_returns_data_gap(self, snapshot):
        """Анализ, требующий exact coupon schedule, получает typed data gap
        (ANALYSIS_DATA_UNAVAILABLE) + какого dataset не хватает; данные не
        синтезируются."""
        payload, _, use_case = snapshot
        result = use_case.build_snapshot(_args(full=True, requires=[
            'ytm', 'exact_coupon_schedule',
        ]))
        analysis = result['analysis']
        assert analysis['status'] == ANALYSIS_DATA_UNAVAILABLE
        assert analysis['requested_datasets'] == ['ytm', 'exact_coupon_schedule']
        assert len(analysis['missing']) == 1
        assert analysis['missing'][0]['code'] == 'exact_coupon_schedule'
        assert analysis['missing'][0]['reason']

        # Никакого синтезированного графика купонов в payload нет: ключей
        # с данными графика не существует (код data gap — не данные).
        def keys_of(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield str(key)
                    yield from keys_of(value)
            elif isinstance(node, list):
                for item in node:
                    yield from keys_of(item)

        all_keys = set(keys_of(result))
        assert not any(
            'schedule' in key.lower()
            for key in all_keys
        ), sorted(all_keys)

    def test_requires_available_datasets_ok(self, snapshot):
        _, _, use_case = snapshot
        result = use_case.build_snapshot(
            _args(requires=['ytm', 'credit_rating', 'buy_universe'])
        )
        assert result['analysis'] == {
            'requested_datasets': ['ytm', 'credit_rating', 'buy_universe'],
            'status': 'OK',
            'missing': [],
        }

    def test_requires_unknown_code_is_data_gap(self, snapshot):
        _, _, use_case = snapshot
        result = use_case.build_snapshot(_args(requires=['no_such_dataset']))
        assert result['analysis']['status'] == ANALYSIS_DATA_UNAVAILABLE
        assert result['analysis']['missing'][0]['code'] == 'no_such_dataset'


# ---------------------------------------------------------------------------
# Compact по умолчанию, полный payload — явно
# ---------------------------------------------------------------------------


class TestCompactDefault:
    def test_default_is_compact_preview(self, snapshot):
        _, fake, use_case = snapshot
        compact = use_case.build_snapshot(_args())
        assert compact['preview'] is True
        assert 'rows' not in compact['universe']
        assert 'positions' not in compact['portfolio']
        assert compact['universe']['row_count'] == 13
        assert len(compact['universe']['rows_preview']) == PREVIEW_ROWS
        assert compact['portfolio']['positions_count'] == 2
        assert (
            len(compact['portfolio']['positions_preview'])
            == 2
        )
        # Metadata в compact полные: capabilities/limitations/context.
        assert compact['capabilities']['read_only'] is True
        assert compact['limitations']
        assert compact['context']['context_id']

    def test_full_payload_has_all_rows(self, snapshot):
        payload, _, _ = snapshot
        assert payload['preview'] is False
        assert len(payload['universe']['rows']) == 13
        assert len(payload['portfolio']['positions']) == 2

    def test_execute_prints_valid_json(self, tmp_path, capsys):
        use_case = _make_use_case(_make_fake(), tmp_path)
        use_case.execute(_args())
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload['artifact'] == 'analysis_snapshot'
        assert payload['preview'] is True


# ---------------------------------------------------------------------------
# Секреты
# ---------------------------------------------------------------------------


class TestNoSecrets:
    def test_payload_contains_no_secrets_or_transport(self, snapshot):
        """Payload не содержит DSN/tokens/credentials/env-имен; это данные
        среза, а не конфигурация доступа."""
        payload, _, _ = snapshot
        raw = json.dumps(payload, ensure_ascii=False)
        for secret_marker in (
            'postgres://', 'POSTGRES_DSN', 'INVEST_TOKEN', 'TELEGRAM_BOT',
            'password', 'secret', 'api_key', 'authorization',
        ):
            assert secret_marker.lower() not in raw.lower(), secret_marker

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    assert 'dsn' not in str(key).lower()
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)


# ---------------------------------------------------------------------------
# CLI-проводка
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_setup_parser_defaults(self):
        parser = argparse.ArgumentParser(add_help=False)
        AnalysisSnapshotUseCase.setup_parser(parser)
        args = parser.parse_args([])
        assert args.format == 'json'
        assert args.full is False
        assert args.requires is None

    def test_setup_parser_full_and_requires(self):
        parser = argparse.ArgumentParser(add_help=False)
        AnalysisSnapshotUseCase.setup_parser(parser)
        args = parser.parse_args(
            ['--full', '--requires', 'ytm, exact_coupon_schedule, ytm']
        )
        assert args.full is True
        # Дубликаты и пустые элементы отсекаются, порядок сохранён.
        assert args.requires == ['ytm', 'exact_coupon_schedule']

    def test_command_registered_in_factory_map(self):
        from src.use_cases.factory import UseCaseFactory

        # Без инстанса фабрики (он требует token/DSN): карта — classmethod
        # уровня экземпляра, но чистый словарь можно получить через __new__.
        factory = UseCaseFactory.__new__(UseCaseFactory)
        use_case_map = UseCaseFactory.get_use_case_map(factory)
        assert 'rebalance-analysis-snapshot' in use_case_map
        assert use_case_map[
            'rebalance-analysis-snapshot'
        ] is AnalysisSnapshotUseCase
        # Существующие команды не тронуты.
        assert 'rebalance-report' in use_case_map
        assert len(use_case_map) == 38
