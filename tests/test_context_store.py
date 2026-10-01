"""
Тесты EphemeralContextStore: core-owned context handle (задача 030.4).

Проверяются требования файла задачи 030.4:
- context_id загружает тот же core-owned PlanningContext/fingerprint
  (типизированный round-trip: Decimal/date/datetime восстанавливаются);
- context другого profile/owner (scope) недоступен и выглядит ровно как
  NOT_FOUND (существование чужого context не раскрывается);
- изменение analysis payload в sandbox не меняет authoritative context;
- corruption core-owned artifact → CONTEXT_FINGERPRINT_MISMATCH;
- unknown/expired context_id → CONTEXT_NOT_FOUND / CONTEXT_STALE;
- несовместимая schema/policy/store версия → CONTEXT_VERSION_MISMATCH
  (deploy/policy change не меняет семантику созданного context молча);
- изменение policy без эффекта на derived version ловится regression-
  тестом (calculation_policy_version — digest, не «магическая строка»);
- загрузка по context_id не перечитывает свежие market/cash/portfolio
  rows (store вообще не имеет доступа к БД);
- истёкший context отклоняется TTL policy ОТДЕЛЬНО от per-instrument
  freshness (stale data_gate внутри TTL загружается, свежий вне TTL —
  CONTEXT_STALE);
- context_id как path отвергается (path traversal test).

Все данные — синтетические (in-memory двойник PortfolioStorage, как в
tests/test_planning_context.py), реальные составы портфеля и ISIN не
используются (AGENTS.md). Артефакты пишутся в tmp_path, реальный
data/ не затрагивается.
"""
import json
import stat
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.services import context_store as cs
from src.services.context_store import (
    DEFAULT_CONTEXT_TTL_SECONDS,
    ContextFingerprintMismatchError,
    ContextNotFoundError,
    ContextStoreError,
    ContextStaleError,
    ContextVersionMismatchError,
    EphemeralContextStore,
    _context_from_payload,
    _typed_payload,
    derive_calculation_policy_version,
    generate_context_id,
)
from src.services.planning_context import (
    CALCULATION_POLICY_VERSION,
    CONTEXT_SCHEMA_VERSION,
    PlanningContext,
    PlanningContextBuilder,
    canonical_calculation_payload,
)
from src.services.rebalance_engine import PLANNING_PRICE_BASIS_MARKET

AS_OF = datetime(2025, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
AS_OF_DATE = AS_OF.date()
SAVE_TIME = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

HELD1 = 'RU000A0ZHEL1'
NEW1 = 'RU000A0ZNEW1'
NOMINAL1 = 'RU000A0ZNOM1'

_RATING_SCALE_A = {'AAA.RU': 1, 'AA+.RU': 2, 'AA.RU': 3}


def _bond_row(
    isin,
    *,
    qty=None,
    price='1000',
    market_price='1000',
    maturity=date(2027, 6, 15),
    ytm_updated_at=None,
    offer_date=None,
    rating_score=18,
):
    """Синтетическая строка портфеля/каталога (деньги — Decimal, как из БД)."""
    row = {
        'isin': isin,
        'ticker': isin[-5:],
        'name': 'Бумага ' + isin,
        'nominal': Decimal('1000'),
        'coupon_rate_percent': Decimal('12.0'),
        'coupon_quantity_per_year': 4,
        'maturity_date': maturity,
        'offer_date': offer_date,
        'risk_level': 2,
        'list_level': 1,
        'amortization_flag': False,
        'floating_coupon_flag': False,
        'perpetual_flag': False,
        'ku': False,
        'market_price': Decimal(market_price) if market_price is not None else None,
        'ytm': None,
        'ytm_null_reason': None,
        'ytm_updated_at': ytm_updated_at,
        'company_id': 1,
        'issuer': 'Эмитент ' + isin,
        'entity_type': 'corporate',
        'credit_rating': 'AA-',
        'rating_score': rating_score,
        'held_share_pct': None,
    }
    if qty is not None:
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
    'portfolio_positions_rows': 1,
    'portfolio_max_updated_at': AS_OF - timedelta(minutes=15),
    'last_sync_time': AS_OF - timedelta(minutes=15),
    'portfolio_zero_value_positions': 0,
    'portfolio_zero_rows_suspicious': 0,
    'portfolio_zero_value_isins': None,
    'portfolio_total_value': Decimal('10000.00'),
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
    """In-memory двойник PortfolioStorage с журналом чтений: store обязан
    загружать context, не делая НИ ОДНОГО чтения источника данных."""

    def __init__(self, *, freshness, cash, held_rows, universe_rows, as_of=AS_OF):
        self.now = as_of
        self.freshness_data = dict(freshness)
        self.cash_data = dict(cash)
        self.held_rows = list(held_rows)
        self.universe_rows = list(universe_rows)
        self.snapshot_depth = 0
        self.read_log = []

    def _track(self, name):
        self.read_log.append(name)

    def snapshot_transaction(self):
        return self._snapshot()

    def _snapshot(self):
        class _Tx:
            def __enter__(self):
                self.store.snapshot_depth += 1
                return self.store

            def __exit__(self, *exc):
                self.store.snapshot_depth -= 1
                return False

        tx = _Tx()
        tx.store = self
        return tx

    def get_transaction_timestamp(self):
        self._track('get_transaction_timestamp')
        return {'ts': self.now, 'day': self.now.date()}

    def get_db_freshness(self, stale_threshold_hours=24, now=None):
        self._track('get_db_freshness')
        return dict(self.freshness_data)

    def get_available_cash_rub(self):
        self._track('get_available_cash_rub')
        return dict(self.cash_data)

    def get_rebalance_portfolio_rows(self):
        self._track('get_rebalance_portfolio_rows')
        return [dict(row) for row in self.held_rows]

    def get_bonds_yield_table(self, **kwargs):
        self._track('get_bonds_yield_table')
        return [dict(row) for row in self.universe_rows]


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
            _bond_row(
                HELD1,
                qty='10',
                price='1000',
                ytm_updated_at=AS_OF - timedelta(days=1),
                offer_date=date(2026, 12, 31),
            ),
        ],
        universe_rows=universe_rows if universe_rows is not None else [
            _bond_row(NEW1, market_price='900'),
            _bond_row(NOMINAL1, market_price=None),
        ],
        as_of=as_of,
    )


def _build(fake):
    return PlanningContextBuilder(fake).build()


class FakeClock:
    """Управляемые часы store'а (TTL-сценарии)."""

    def __init__(self, start=SAVE_TIME):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)


def _make_store(
    tmp_path,
    *,
    scope='scope-a',
    rating_scale=None,
    ttl_seconds=DEFAULT_CONTEXT_TTL_SECONDS,
    clock=None,
):
    return EphemeralContextStore(
        root_dir=tmp_path / 'planning_contexts',
        scope_digest=scope,
        rating_scale=rating_scale,
        ttl_seconds=ttl_seconds,
        now_fn=clock or FakeClock(),
    )


@pytest.fixture
def context():
    return _build(_make_fake())


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    return _make_store(tmp_path, clock=clock)


@pytest.fixture
def saved(store, context):
    return store.save(context)


def _same_scope_reader(store, *, rating_scale=None):
    """Новый store-инстанс того же scope/root — эквивалент следующего
    CLI-вызова: process-local memory пуст, артефакт читается с диска."""
    return EphemeralContextStore(
        root_dir=store.root_dir,
        scope_digest=store.scope_digest,
        rating_scale=rating_scale,
        ttl_seconds=store.ttl_seconds,
        now_fn=FakeClock(),
    )


# ---------------------------------------------------------------------------
# Round-trip: context_id загружает тот же core-owned context
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_loaded_context_equals_saved_with_same_fingerprint(
        self, store, context, saved
    ):
        """context_id загружает тот же core-owned PlanningContext: поля и
        fingerprint совпадают с оригиналом (analysis payload — это копия,
        authoritative context хранит core)."""
        loaded = store.load(saved['context_id'])

        assert isinstance(loaded, PlanningContext)
        assert loaded.context_fingerprint == context.context_fingerprint
        assert loaded.as_of == context.as_of
        assert loaded.as_of_date == context.as_of_date
        assert loaded.data_gate == context.data_gate
        assert loaded.cash == context.cash
        assert loaded.positions_updated_at == context.positions_updated_at
        assert loaded.cash_updated_at == context.cash_updated_at
        assert loaded.universe_policy == context.universe_policy
        assert loaded.held_rows == context.held_rows
        assert loaded.universe_rows == context.universe_rows
        assert loaded.effective_prices == context.effective_prices

    def test_typed_round_trip_restores_domain_types(self, store, context, saved):
        """Типизированный payload восстанавливает доменные типы: деньги —
        Decimal (не float/строка), даты — date, времена — aware datetime,
        цены движка — float, флаги — bool, уровни — int."""
        loaded = store.load(saved['context_id'])

        held = loaded.held_rows[0]
        assert isinstance(held['nominal'], Decimal)
        assert isinstance(held['quantity'], Decimal)
        assert held['quantity'] == Decimal('10')
        assert isinstance(held['maturity_date'], date)
        assert held['maturity_date'] == date(2027, 6, 15)
        assert isinstance(held['ytm_updated_at'], datetime)
        assert held['ytm_updated_at'].tzinfo is not None
        assert isinstance(held['offer_date'], date)
        assert isinstance(held['risk_level'], int)
        assert isinstance(held['ku'], bool)
        price = loaded.effective_prices[NEW1]
        assert isinstance(price['price'], float)
        assert price['price'] == 900.0
        assert isinstance(loaded.cash.cash_available_rub, Decimal)
        assert loaded.cash.cash_available_rub == Decimal('50000.00')

    def test_in_process_load_returns_same_object(self, store, saved):
        """Process-local уровень: сравнение в одном процессе возвращает тот
        же Python-объект context'а (in-process compare)."""
        first = store.load(saved['context_id'])
        second = store.load(saved['context_id'])
        assert first is second

    def test_save_handle_reports_versions_and_ttl(self, store, context, saved):
        """Handle содержит opaque id, fingerprint context'а, версии и
        границы TTL (analysis payload'а в handle нет — он у snapshot 030.10)."""
        assert len(saved['context_id']) == 32
        int(saved['context_id'], 16)  # hex
        assert saved['context_fingerprint'] == context.context_fingerprint
        assert saved['context_schema_version'] == CONTEXT_SCHEMA_VERSION
        assert saved['calculation_policy_version'] == (
            derive_calculation_policy_version()
        )
        assert (
            saved['expires_at'] - saved['created_at']
        ).total_seconds() == store.ttl_seconds

    def test_context_id_is_random_not_content_derived(self, store, context):
        """id — random/unguessable: один и тот же context сохраняется под
        разными id (id не hash содержимого и не угадывается по данным)."""
        first = store.save(context)
        second = store.save(context)
        assert first['context_id'] != second['context_id']
        assert (
            first['context_fingerprint'] == second['context_fingerprint']
        )

    def test_typed_payload_mirrors_canonical_payload(self, context):
        """Drift-guard: типизированный payload артефакта восстанавливается в
        context с каноническим payload'ом, идентичным оригиналу — структура
        _typed_payload не расходится с canonical_calculation_payload 030.3."""
        reconstructed = _context_from_payload(
            _typed_payload(context), generated_at=context.generated_at
        )
        assert (
            canonical_calculation_payload(reconstructed)
            == canonical_calculation_payload(context)
        )


# ---------------------------------------------------------------------------
# Sandbox payload edits не меняют authoritative context
# ---------------------------------------------------------------------------


class TestSandboxBoundary:
    def test_sandbox_payload_edit_does_not_change_authoritative_context(
        self, store, context, saved
    ):
        """Sandbox получил copy payload, отредактировал цену и вернул её —
        core загружает СВОЙ original: правки копии в authoritative context
        не попадают (fingerprint тот же, цены оригинальные)."""
        sandbox_copy = json.loads(json.dumps(_typed_payload(context)))
        sandbox_copy['effective_prices'][NEW1]['price'] = 1103.06
        sandbox_copy['cash']['cash_available_rub'] = {
            '__decimal__': '999999.99'
        }

        loaded = store.load(saved['context_id'])
        assert loaded.effective_prices[NEW1] == {
            'price': 900.0, 'basis': PLANNING_PRICE_BASIS_MARKET,
        }
        assert loaded.cash.cash_available_rub == Decimal('50000.00')
        assert loaded.context_fingerprint == context.context_fingerprint

    def test_store_accepts_only_opaque_id_not_payload(self, store, saved):
        """У store'а нет API «загрузить по payload»: load принимает только
        opaque context_id; подмена данных через интерфейс невозможна."""
        import inspect

        params = inspect.signature(store.load).parameters
        assert list(params) == ['context_id']


# ---------------------------------------------------------------------------
# Scoping: чужой profile выглядит как NOT_FOUND
# ---------------------------------------------------------------------------


class TestScoping:
    def test_other_scope_loads_as_not_found(self, tmp_path, saved, context):
        """Store другого profile/owner (scope) с тем же root и известным id
        получает ровно тот же ответ, что для неизвестного id: существование
        чужого context не раскрывается."""
        other = _make_store(tmp_path, scope='scope-b')

        with pytest.raises(ContextNotFoundError) as foreign:
            other.load(saved['context_id'])
        with pytest.raises(ContextNotFoundError) as unknown:
            other.load('f' * 32)

        assert str(foreign.value) == str(unknown.value)
        assert foreign.value.code == unknown.value.code == 'CONTEXT_NOT_FOUND'

    def test_scopes_do_not_share_artifact_files(self, tmp_path):
        """Артефакт привязан к scope даже по имени файла (sha256(scope:id)):
        чужой scope физически не разрешает путь чужого context'а."""
        root = tmp_path / 'planning_contexts'
        first = _make_store(tmp_path, scope='scope-a')
        first.save(_build(_make_fake()))
        assert len(list(root.iterdir())) == 1

        second = _make_store(tmp_path, scope='scope-b')
        second.save(_build(_make_fake()))
        assert len(list(root.iterdir())) == 2  # файлы не перезаписали друг друга


# ---------------------------------------------------------------------------
# context_id: opaque, path traversal исключён
# ---------------------------------------------------------------------------


class TestContextIdSecurity:
    @pytest.mark.parametrize(
        'bad_id',
        [
            '..',
            '../..',
            '../../etc/passwd',
            '/etc/passwd',
            'a/../../etc/passwd',
            '.' * 32,
            'g' * 32,        # не hex
            'A' * 32,        # верхний регистр вне формата
            'f' * 31,        # короткий
            'f' * 33,        # длинный
            '',
            'f' * 16 + ';' + '0' * 15,
            None,
            123,
            b'f' * 32,
        ],
    )
    def test_invalid_context_id_is_not_found(self, store, bad_id, saved):
        """Любая строка вне формата 32 hex — CONTEXT_NOT_FOUND: id строго
        opaque и никогда не трактуется как filesystem path."""
        with pytest.raises(ContextNotFoundError):
            store.load(bad_id)

    def test_traversal_attempts_create_no_files(
        self, tmp_path, store, saved
    ):
        """Path traversal не создаёт и не читает файлов вне корня store'а."""
        before = {
            str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')
        }
        for bad in ('../..', '../../etc/passwd', 'xxxxxxxx/../' + 'f' * 32):
            with pytest.raises(ContextNotFoundError):
                store.load(bad)
        after = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')}
        assert after == before

    def test_artifact_filename_never_contains_context_id(
        self, tmp_path, saved
    ):
        """Имя файла артефакта — sha256(scope:id): context_id не является
        частью path (path traversal исключён по построению)."""
        root = tmp_path / 'planning_contexts'
        files = list(root.iterdir())
        assert len(files) == 1
        name = files[0].name
        assert name.endswith('.json')
        assert len(name) == 64 + len('.json')
        int(name.split('.')[0], 16)  # чистый hex-дайджест
        assert saved['context_id'] not in name

    def test_artifact_permissions_are_minimal(self, tmp_path, saved):
        """Артефакт принадлежит приложению: файл 0600, каталог 0700 —
        sandbox без прав записи к core-owned артефакту не допущен."""
        root = tmp_path / 'planning_contexts'
        artifact = next(root.iterdir())
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
        assert stat.S_IMODE(root.stat().st_mode) == 0o700

    def test_artifact_contains_no_secrets(
        self, tmp_path, monkeypatch, context
    ):
        """В артефакте нет secrets/DSN/tokens: scope хранится только как
        sha256-digest, исходный DSN в файл не попадает."""
        monkeypatch.setenv(
            'POSTGRES_DSN', 'postgresql://fin:SuperSecret@db.local/fin'
        )
        store = EphemeralContextStore(
            root_dir=tmp_path / 'default_scope_store',
            now_fn=FakeClock(),
        )
        handle = store.save(context)

        artifact = next((tmp_path / 'default_scope_store').iterdir())
        text = artifact.read_text(encoding='utf-8')
        assert 'SuperSecret' not in text
        assert 'postgresql://' not in text
        assert 'db.local' not in text
        # scope-digest присутствует (сравнимый, но необратимый)
        envelope = json.loads(text)
        assert envelope['scope_digest'] == store.scope_digest
        assert len(envelope['scope_digest']) == 64
        assert handle['context_fingerprint'] == context.context_fingerprint


# ---------------------------------------------------------------------------
# Write-once: частичное обновление context невозможно
# ---------------------------------------------------------------------------


class TestWriteOnce:
    def test_save_never_overwrites_existing_artifact(
        self, tmp_path, monkeypatch, context
    ):
        """save-once: повторная запись под тем же id отвергается, файл
        остаётся исходным (context после создания не обновляется)."""
        store = _make_store(tmp_path)
        first = store.save(context)
        root = tmp_path / 'planning_contexts'
        before = next(root.iterdir()).read_bytes()

        monkeypatch.setattr(cs, 'generate_context_id', lambda: first['context_id'])
        with pytest.raises(ContextStoreError):
            store.save(context)

        assert next(root.iterdir()).read_bytes() == before


# ---------------------------------------------------------------------------
# TTL: отдельная ось свежести + cleanup
# ---------------------------------------------------------------------------


class TestTtl:
    def test_unknown_context_id_is_not_found(self, store):
        with pytest.raises(ContextNotFoundError):
            store.load('f' * 32)

    def test_expired_context_is_stale_and_removed(self, store, saved, clock):
        """Истёкший context — CONTEXT_STALE; артефакт удаляется (cleanup)."""
        clock.advance(hours=7)

        with pytest.raises(ContextStaleError) as exc:
            store.load(saved['context_id'])
        assert exc.value.code == 'CONTEXT_STALE'
        assert not list(store.root_dir.iterdir())

    def test_ttl_is_independent_of_data_gate(
        self, tmp_path, clock, context
    ):
        """Три оси не смешаны: context со stale data_gate внутри TTL
        загружается (TTL не переписывает зафиксированные eligibility
        reasons); context со свежим data_gate вне TTL — CONTEXT_STALE."""
        stale_gate = _build(_make_fake(
            freshness=_freshness(
                last_sync_time=AS_OF - timedelta(days=4),
                portfolio_max_updated_at=AS_OF - timedelta(days=4),
            )
        ))
        assert stale_gate.data_gate.is_fresh is False

        store = _make_store(tmp_path, clock=clock)
        handle = store.save(stale_gate)
        loaded = store.load(handle['context_id'])
        assert loaded.data_gate.is_fresh is False
        assert loaded.data_gate.status == 'CRITICAL'
        assert loaded.effective_prices == stale_gate.effective_prices
        assert loaded.context_fingerprint == stale_gate.context_fingerprint

        fresh_handle = store.save(context)
        clock.advance(hours=7)
        with pytest.raises(ContextStaleError):
            store.load(fresh_handle['context_id'])

    def test_cleanup_removes_expired_and_damaged_keeps_active(
        self, tmp_path, clock, context
    ):
        """cleanup: истёкшие и нечитаемые артефакты удалены, живые — нет;
        орфаненные temp-файлы записи убираются."""
        store = _make_store(tmp_path, clock=clock)
        keep_handle = store.save(context)
        root = tmp_path / 'planning_contexts'
        (root / '.tmp-orphan').write_bytes(b'{}')

        clock.advance(hours=7)
        stale_handle = store.save(context)  # save выполняет opportunistic sweep
        assert len(list(root.iterdir())) == 1  # истёкший удалён при save

        damaged = next(root.iterdir())
        assert damaged.read_bytes()  # артефакт stale_handle
        damaged.write_bytes(b'not json at all')
        (root / '.tmp-orphan2').write_bytes(b'{}')
        removed = store.cleanup()

        assert removed == 2  # повреждённый + temp
        assert not list(root.iterdir())
        # повторный save после cleanup работает
        handle = store.save(context)
        assert store.load(handle['context_id']).context_fingerprint == (
            context.context_fingerprint
        )

    def test_ttl_default_is_bounded(self):
        """TTL по умолчанию ограничен (bounded) и измеряется часами, не
        сутками свежести данных — ось отдельна."""
        assert 0 < DEFAULT_CONTEXT_TTL_SECONDS <= 24 * 3600


# ---------------------------------------------------------------------------
# Corruption core-owned артефакта
# ---------------------------------------------------------------------------


class TestCorruption:
    def test_tampered_market_price_detected(self, tmp_path, saved):
        """Порча расчётных данных артефакта → CONTEXT_FINGERPRINT_MISMATCH
        (fingerprint пересчитывается при загрузке)."""
        store = _make_store(tmp_path)
        path = next(store.root_dir.iterdir())
        envelope = json.loads(path.read_text(encoding='utf-8'))
        envelope['context']['universe_rows'][0]['market_price'] = {
            '__decimal__': '1103.06'
        }
        path.write_text(json.dumps(envelope), encoding='utf-8')

        with pytest.raises(ContextFingerprintMismatchError) as exc:
            _same_scope_reader(store).load(saved['context_id'])
        assert exc.value.code == 'CONTEXT_FINGERPRINT_MISMATCH'

    def test_tampered_fingerprint_field_detected(self, tmp_path, saved):
        """Порча поля fingerprint в envelope → FINGERPRINT_MISMATCH."""
        store = _make_store(tmp_path)
        path = next(store.root_dir.iterdir())
        envelope = json.loads(path.read_text(encoding='utf-8'))
        envelope['context_fingerprint'] = '0' * 64
        path.write_text(json.dumps(envelope), encoding='utf-8')

        with pytest.raises(ContextFingerprintMismatchError):
            _same_scope_reader(store).load(saved['context_id'])

    def test_truncated_artifact_detected(self, tmp_path, saved):
        """Обрезанный/нечитаемый JSON артефакта → FINGERPRINT_MISMATCH."""
        store = _make_store(tmp_path)
        path = next(store.root_dir.iterdir())
        raw = path.read_bytes()
        path.write_bytes(raw[: len(raw) // 2])

        with pytest.raises(ContextFingerprintMismatchError):
            _same_scope_reader(store).load(saved['context_id'])

    def test_damaged_envelope_shape_detected(self, tmp_path, saved):
        """Артефакт без секции context (повреждение формы) →
        FINGERPRINT_MISMATCH, а не произвольное исключение."""
        store = _make_store(tmp_path)
        path = next(store.root_dir.iterdir())
        envelope = json.loads(path.read_text(encoding='utf-8'))
        del envelope['context']
        path.write_text(json.dumps(envelope), encoding='utf-8')

        with pytest.raises(ContextFingerprintMismatchError):
            _same_scope_reader(store).load(saved['context_id'])

    def test_original_context_survives_artifact_corruption(
        self, store, context, saved
    ):
        """Даже испорченный артефакт не подменяет authoritative context:
        in-process экземпляр, создавший context, продолжает отдавать
        оригинал; повреждённый файл при загрузке отвергается."""
        loaded = store.load(saved['context_id'])
        assert loaded.context_fingerprint == context.context_fingerprint

        path = next(store.root_dir.iterdir())
        path.write_bytes(b'corrupted')

        with pytest.raises(ContextFingerprintMismatchError):
            _same_scope_reader(store).load(saved['context_id'])
        assert store.load(saved['context_id']).context_fingerprint == (
            context.context_fingerprint
        )


# ---------------------------------------------------------------------------
# Версии: schema/policy несовместимость и derived policy version
# ---------------------------------------------------------------------------


class TestVersions:
    def test_schema_version_mismatch(self, tmp_path, saved):
        """Context с другой context_schema_version не загружается."""
        store = _make_store(tmp_path)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(cs, 'CONTEXT_SCHEMA_VERSION', CONTEXT_SCHEMA_VERSION + 1)
            other = _make_store(tmp_path)
            # policy digest «выравнивается», чтобы проверить ровно schema-чек
            other.calculation_policy_version = (
                store.calculation_policy_version
            )
            with pytest.raises(ContextVersionMismatchError):
                other.load(saved['context_id'])

    def test_store_envelope_version_mismatch(self, tmp_path, saved):
        """Артефакт будущего формата envelope → CONTEXT_VERSION_MISMATCH
        (молчаливо интерпретировать чужой формат нельзя)."""
        store = _make_store(tmp_path)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(cs, 'STORE_SCHEMA_VERSION', cs.STORE_SCHEMA_VERSION + 1)
            other = _make_store(tmp_path)
            with pytest.raises(ContextVersionMismatchError):
                other.load(saved['context_id'])

    def test_rating_scale_change_is_version_mismatch(self, tmp_path, saved):
        """rating-scale семантика — policy input: изменение шкалы в конфиге
        делает старые context'ы несовместимыми (fail-loud, не молча)."""
        other = _make_store(
            tmp_path, rating_scale={'AAA.RU': 1, 'AA+.RU': 2, 'AA.RU': 4}
        )
        with pytest.raises(ContextVersionMismatchError):
            other.load(saved['context_id'])

    def test_policy_constant_change_is_version_mismatch(
        self, tmp_path, saved, monkeypatch
    ):
        """Deploy/policy change (issuer limit) не меняет семантику уже
        созданного context молча: старый context → CONTEXT_VERSION_MISMATCH,
        получить новый context и повторить analysis."""
        monkeypatch.setattr(cs, 'ISSUER_CONCENTRATION_LIMIT_PCT', 20.0)
        other = _make_store(tmp_path)

        with pytest.raises(ContextVersionMismatchError):
            other.load(saved['context_id'])

    def test_rating_scale_none_and_empty_are_equivalent(self, tmp_path):
        """rating_scale не задан == пустая шкала (одинаковый policy digest)."""
        a = _make_store(tmp_path, scope='s', rating_scale=None)
        b = _make_store(tmp_path, scope='s', rating_scale={})
        assert a.calculation_policy_version == b.calculation_policy_version


class TestPolicyVersionDerivation:
    """calculation_policy_version — не «магическая строка»: regression
    тест ломается, если policy input меняется, а derived version нет."""

    def test_derived_version_is_deterministic_digest(self):
        first = derive_calculation_policy_version(_RATING_SCALE_A)
        second = derive_calculation_policy_version(dict(_RATING_SCALE_A))
        assert first == second
        assert first.startswith('sha256-')
        assert len(first) == len('sha256-') + 16

    def test_each_policy_input_changes_derived_version(self, monkeypatch):
        """Каждый канонический policy input входит в digest: изменение
        любого из них меняет derived version (иначе — regression fail)."""
        base = derive_calculation_policy_version(_RATING_SCALE_A)

        assert derive_calculation_policy_version(
            {'AAA.RU': 1, 'AA+.RU': 2, 'AA.RU': 5}
        ) != base

        variants = [
            ('ISSUER_CONCENTRATION_LIMIT_PCT', 20.0),
            ('CALCULATION_POLICY_VERSION', '2'),
            ('CONTEXT_SCHEMA_VERSION', CONTEXT_SCHEMA_VERSION + 1),
            ('RAW_UNIVERSE_POLICY', dict(cs.RAW_UNIVERSE_POLICY, max_risk=4)),
        ]
        for name, value in variants:
            monkeypatch.setattr(cs, name, value)
            changed = derive_calculation_policy_version(_RATING_SCALE_A)
            assert changed != base, f'policy input {name} not covered by digest'
            monkeypatch.undo()

    def test_saved_context_records_derived_version(self, store, context, saved):
        """Сохранённый context указывает версию однозначно: derived version
        записана в handle и в артефакт, load сверяет её с текущей."""
        assert saved['calculation_policy_version'] == (
            store.calculation_policy_version
        )
        artifact = json.loads(
            next(store.root_dir.iterdir()).read_text(encoding='utf-8')
        )
        assert artifact['calculation_policy_version'] == (
            store.calculation_policy_version
        )
        assert artifact['context']['calculation_policy_version'] == (
            CALCULATION_POLICY_VERSION
        )


# ---------------------------------------------------------------------------
# Загрузка по context_id не перечитывает свежие rows
# ---------------------------------------------------------------------------


class TestNoSourceRereads:
    def test_load_never_touches_storage_and_keeps_frozen_slice(
        self, store, context, saved
    ):
        """plan по context_id обязана считать по original context: store не
        имеет доступа к БД вовсе, а «уехавший» рынок после save не меняет
        загруженный срез (ни цены, ни cash, ни fingerprint)."""
        fake = _make_fake()
        built = _build(fake)
        handle = store.save(built)

        reads_before = list(fake.read_log)
        fake.universe_rows[0]['market_price'] = '1103.06'
        fake.cash_data['cash_available_rub'] = Decimal('1.00')
        fake.now = fake.now + timedelta(hours=3)

        loaded = store.load(handle['context_id'])

        assert fake.read_log == reads_before  # ни одного нового чтения
        assert fake.snapshot_depth == 0
        assert loaded.context_fingerprint == built.context_fingerprint
        assert loaded.universe_rows == built.universe_rows
        assert loaded.cash == built.cash
        assert loaded.as_of == built.as_of


# ---------------------------------------------------------------------------
# Служебное: memory cache уважает TTL и policy
# ---------------------------------------------------------------------------


class TestMemoryCacheBoundaries:
    def test_memory_entry_respects_ttl(self, store, saved, clock):
        store.load(saved['context_id'])  # попадание в process-local cache
        clock.advance(hours=7)
        with pytest.raises(ContextStaleError):
            store.load(saved['context_id'])

    def test_memory_entry_respects_policy_change(self, store, saved):
        store.load(saved['context_id'])
        store.calculation_policy_version = 'sha256-changed'
        with pytest.raises(ContextVersionMismatchError):
            store.load(saved['context_id'])
