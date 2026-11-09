"""
Ephemeral context store — core-owned context handle поверх PlanningContext (030.4).

Workflow escape hatch (analysis-snapshot → sandbox Python → plan --context-id)
требует, чтобы финальный plan считался по тому же authoritative context, а не
по данным, вернувшимся из sandbox: обычный SHA/fingerprint не защищает от
«код изменил и данные, и сам hash». Авторитет — только core-owned артефакт:

    core builds PlanningContext X -> store.save(X) -> {context_id,
    context_fingerprint, ...}; sandbox читает ТОЛЬКО copy payload; в core
    возвращается context_id -> store.load(context_id) отдаёт СВОЙ original X.

Гарантии и границы:

- Хранилище двухуровневое: process-local dict (сравнение в одном процессе
  возвращает тот же Python-объект) + ephemeral JSON-артефакт на host
  (несколько CLI-invocations). Persistent snapshot DB не делается (запрещено
  задачей): файлы живут в data/ под корнем приложения, переживают процесс
  и удаляются TTL-cleanup'ом.
- context_id — secrets.token_hex (неугадываемый), трактуется строго как
  opaque identifier: формат жёстко валидируется (32 hex-символа), любая
  другая строка — CONTEXT_NOT_FOUND. В filesystem path id не попадает:
  имя файла — sha256(scope_digest + ':' + context_id), path traversal
  исключён по построению.
- Scoping: артефакт привязан к scope инстанса приложения (по умолчанию —
  digest корня приложения + DSN источника данных; сам DSN нигде не
  хранится). Чужой scope, знающий id, получает ровно тот же ответ
  CONTEXT_NOT_FOUND, что и для неизвестного id, — существование чужого
  context не раскрывается (файл чужого scope даже не разрешается по имени).
- Артефакт принадлежит приложению: файлы 0600, каталог 0700; API записи
  нет (save-once при создании, update/частичная перезапись исключены —
  финальное имя создаётся через O_EXCL-подобный link). Sandbox Python не
  имеет ни пути, ни прав на запись; консистентную подмену «данные+hash»
  ловит не fingerprint, а граница владения файлом — fingerprint при
  загрузке ловит corruption/несогласованную порчу.
- TTL — отдельная ось свежести (не data_gate и не per-instrument
  eligibility): истёкший context отклоняется CONTEXT_STALE и удаляется,
  независимо от того, свежи ли его исходные данные; TTL не переписывает
  зафиксированные в context eligibility reasons (context immutable).
- Versioned payload: канонический расчётный payload (та же структура, что
  canonical_calculation_payload 030.3) хранится с типами
  (Decimal/date/datetime — тегированные строки; float/int/bool/str —
  нативный JSON), так что загрузка восстанавливает PlanningContext
  байт-в-байт по смыслу и сверяет context_fingerprint ПЕРЕД выдачей.
  context_schema_version хранится явно; расчётная политика — не
  «магическая строка»: derive_calculation_policy_version() — digest
  канонических policy-входов (issuer limit, raw universe policy,
  schema/policy версии, rating-scale семантика конфига). Изменение любого
  входа меняет derived version, и старый context не пересчитывается новой
  политикой молча — CONTEXT_VERSION_MISMATCH (получить новый context и
  повторить analysis). Regression-тест дополнительно ломается при
  изменении policy-входов без эффекта на derived version.
- Артефакт не содержит secrets/DSN/tokens и не несёт broker/write
  capabilities — только расчётный срез данных.

Ошибки (machine-readable .code): CONTEXT_NOT_FOUND (id неизвестен/удалён/
невалиден/чужой scope), CONTEXT_STALE (истёк TTL), CONTEXT_VERSION_MISMATCH
(несовместимы schema/policy/store-envelope версии), CONTEXT_FINGERPRINT_
MISMATCH (core-owned артефакт повреждён).

Не делается (границы 030.4): persistent snapshot DB; CLI/factory-проводка
(030.8/030.10); transport/lifecycle для LLM (030.11); обновление context
после создания.
"""
import hashlib
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from src.services.planning_context import (
    CALCULATION_POLICY_VERSION,
    CONTEXT_SCHEMA_VERSION,
    RAW_UNIVERSE_POLICY,
    CashSnapshot,
    DataGateState,
    PlanningContext,
    canonical_datetime_str,
    canonical_decimal_str,
)
from src.services.rebalance_engine import ISSUER_CONCENTRATION_LIMIT_PCT
from src.utils import PROJECT_ROOT

# Версия формата envelope-артефакта store'а (не context_schema_version —
# это версия самой обёртки: envelope keys, теги типов, имя файла).
STORE_SCHEMA_VERSION = 1

# TTL по умолчанию: bounded (жёсткий предел жизни ephemeral context).
# Отдельная ось: не связан ни с 24h окном instrument freshness, ни с
# data_gate. Lifecycle workflow «snapshot → sandbox → plan» измеряется
# минутами, запас — до рабочего дня.
DEFAULT_CONTEXT_TTL_SECONDS = 6 * 60 * 60

# Opaque context_id: 32 hex-символа (secrets.token_hex(16)).
_CONTEXT_ID_PATTERN = re.compile(r'\A[0-9a-f]{32}\Z')

# Теги типов в JSON-артефакте (значения, теряющие тип в JSON).
_TAG_DECIMAL = '__decimal__'
_TAG_DATETIME = '__datetime__'
_TAG_DATE = '__date__'

_MSG_NOT_FOUND = 'Unknown context_id'


# ---------------------------------------------------------------------------
# Ошибки (machine-readable codes для diagnostics 030.8+)
# ---------------------------------------------------------------------------


class ContextStoreError(Exception):
    """Базовая ошибка store'а (не покидает модуль в штатных сценариях)."""

    code = 'CONTEXT_STORE_ERROR'


class ContextNotFoundError(ContextStoreError):
    """id неизвестен/удалён/невалиден/чужой scope. Единый ответ для всех
    этих случаев — существование чужого context не раскрывается."""

    code = 'CONTEXT_NOT_FOUND'


class ContextStaleError(ContextStoreError):
    """Истёк TTL context'а (ось TTL, не data_gate/instrument freshness)."""

    code = 'CONTEXT_STALE'


class ContextVersionMismatchError(ContextStoreError):
    """Несовместимая schema/policy/store версия: старый context не
    пересчитывается новой политикой молча — получить новый context."""

    code = 'CONTEXT_VERSION_MISMATCH'


class ContextFingerprintMismatchError(ContextStoreError):
    """Core-owned артефакт повреждён (corruption/несогласованная порча)."""

    code = 'CONTEXT_FINGERPRINT_MISMATCH'


# ---------------------------------------------------------------------------
# context_id и scope
# ---------------------------------------------------------------------------


def generate_context_id() -> str:
    """Неугадываемый opaque id (secrets, не hash содержимого)."""
    return secrets.token_hex(16)


def validate_context_id(context_id: Any) -> str:
    """Строгая валидация opaque id: только 32 hex-символа.

    Любая другая строка (включая path traversal, слишком короткие/длинные,
    не-hex) — CONTEXT_NOT_FOUND: id никогда не трактуется как path.
    """
    if not isinstance(context_id, str) or not _CONTEXT_ID_PATTERN.fullmatch(
        context_id
    ):
        raise ContextNotFoundError(_MSG_NOT_FOUND)
    return context_id


def default_scope_digest() -> str:
    """Digest scope'а инстанса приложения: корень приложения + DSN источника.

    Граница «чужой profile/owner» на фактическом коде проекта: другой чекаут
    (другой PROJECT_ROOT) и другой источник данных (другой POSTGRES_DSN) —
    другой scope. Хранится/сравнивается ТОЛЬКО sha256-digest: сам DSN в
    артефакт не попадает (запрет secrets).
    """
    seed = f'{PROJECT_ROOT}|{os.environ.get("POSTGRES_DSN", "")}'
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()


def derive_calculation_policy_version(
    rating_scale: Optional[Dict[str, int]] = None,
) -> str:
    """Derived расчётная политика: digest канонических policy-входов.

    Не «магическая строка»: включает schema/policy версии контракта 030.3,
    canonical issuer limit движка 030.2, raw universe policy среза и
    rating-scale семантику (config.yaml — non-secret policy input, реально
    влияющий на rating-фильтры). Изменение любого входа меняет version —
    сохранённые context'ы перестают загружаться (CONTEXT_VERSION_MISMATCH),
    молчаливого пересчёта старого context'а новой политикой нет. Свежесть
    data_gate сюда не входит: gate зафиксирован В самом context'е (и в
    fingerprint), replay его не переклассифицирует.
    """
    scale = {
        str(code): int(score)
        for code, score in sorted((rating_scale or {}).items())
    }
    inputs = {
        'context_schema_version': CONTEXT_SCHEMA_VERSION,
        'calculation_policy_version': CALCULATION_POLICY_VERSION,
        'issuer_concentration_limit_pct': ISSUER_CONCENTRATION_LIMIT_PCT,
        'raw_universe_policy': dict(RAW_UNIVERSE_POLICY),
        'rating_scale': scale,
    }
    payload = json.dumps(
        inputs, sort_keys=True, ensure_ascii=True, separators=(',', ':')
    )
    return 'sha256-' + hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Типизированная сериализация payload (Decimal/date/datetime — теги)
# ---------------------------------------------------------------------------


def _encode_typed(value: Any) -> Any:
    """JSON-safe представление с сохранением типов: Decimal/date/datetime
    → тегированные строки, float/int/bool/str/None — нативно (float JSON
    round-trip точен: shortest round-trip repr)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return {_TAG_DECIMAL: canonical_decimal_str(value)}
    if isinstance(value, datetime):
        return {_TAG_DATETIME: canonical_datetime_str(value)}
    if isinstance(value, date):
        return {_TAG_DATE: value.isoformat()}
    if isinstance(value, Mapping):
        return {str(key): _encode_typed(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_typed(item) for item in value]
    return value


def _decode_typed(value: Any) -> Any:
    """Обратное восстановление типов из тегированного JSON."""
    if isinstance(value, dict):
        if len(value) == 1:
            if _TAG_DECIMAL in value:
                return Decimal(str(value[_TAG_DECIMAL]))
            if _TAG_DATETIME in value:
                return _parse_iso_datetime(value[_TAG_DATETIME])
            if _TAG_DATE in value:
                return date.fromisoformat(str(value[_TAG_DATE]))
        return {key: _decode_typed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_typed(item) for item in value]
    return value


def _parse_iso_datetime(value: Any) -> datetime:
    """canonical ISO ('+00:00', naive=UTC) → aware datetime UTC."""
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _typed_payload(context: PlanningContext) -> Dict[str, Any]:
    """Расчётный payload артефакта: те же ключи/семантика, что
    canonical_calculation_payload (030.3), но с сохранением типов —
    загрузка восстанавливает PlanningContext, а не строковую проекцию.
    Соответствие структурам canonical payload закреплено тестом
    (canonical(typed_payload) == canonical_calculation_payload)."""
    return {
        'context_schema_version': context.context_schema_version,
        'calculation_policy_version': context.calculation_policy_version,
        'as_of': _encode_typed(context.as_of),
        'as_of_date': _encode_typed(context.as_of_date),
        'data_gate': {
            'status': context.data_gate.status,
            'gate_status': context.data_gate.gate_status,
            'is_fresh': context.data_gate.is_fresh,
            'criticals': list(context.data_gate.criticals),
            'warnings': list(context.data_gate.warnings),
        },
        'cash': {
            'cash_available_rub': _encode_typed(context.cash.cash_available_rub),
            'cash_known': context.cash.cash_known,
        },
        'positions_updated_at': _encode_typed(context.positions_updated_at),
        'cash_updated_at': _encode_typed(context.cash_updated_at),
        'universe_policy': _encode_typed(context.universe_policy),
        'held_rows': _encode_typed(list(context.held_rows)),
        'universe_rows': _encode_typed(list(context.universe_rows)),
        'effective_prices': _encode_typed(context.effective_prices),
    }


def _context_from_payload(
    payload: Any, generated_at: datetime
) -> PlanningContext:
    """PlanningContext из типизированного payload (повреждение формы —
    KeyError/TypeError/ValueError, транслируется в FINGERPRINT_MISMATCH)."""
    return PlanningContext(
        context_schema_version=payload['context_schema_version'],
        calculation_policy_version=payload['calculation_policy_version'],
        as_of=_decode_typed(payload['as_of']),
        as_of_date=_decode_typed(payload['as_of_date']),
        # Wall-clock сборки — observability-only (в fingerprint не входит);
        # при загрузке восстанавливается временем создания артефакта.
        generated_at=generated_at,
        positions_updated_at=_decode_typed(payload['positions_updated_at']),
        cash_updated_at=_decode_typed(payload['cash_updated_at']),
        data_gate=DataGateState(
            status=payload['data_gate']['status'],
            gate_status=payload['data_gate']['gate_status'],
            is_fresh=payload['data_gate']['is_fresh'],
            criticals=tuple(payload['data_gate']['criticals']),
            warnings=tuple(payload['data_gate']['warnings']),
        ),
        cash=CashSnapshot(
            cash_available_rub=_decode_typed(payload['cash']['cash_available_rub']),
            cash_known=payload['cash']['cash_known'],
        ),
        held_rows=tuple(_decode_typed(row) for row in payload['held_rows']),
        universe_rows=tuple(_decode_typed(row) for row in payload['universe_rows']),
        effective_prices=_decode_typed(payload['effective_prices']),
        universe_policy=_decode_typed(payload['universe_policy']),
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class EphemeralContextStore:
    """Core-owned ephemeral store PlanningContext'ов (030.4).

    Один экземпляр = один scope. `save()` — единственная запись (write-once,
    id генерируется внутри); `load(context_id)` возвращает immutable
    PlanningContext после проверок scope → versions → TTL → fingerprint.
    Никакого доступа к БД у store'а нет: загрузка никогда не перечитывает
    market/cash/portfolio rows.
    """

    def __init__(
        self,
        root_dir: Optional[Path] = None,
        scope_digest: Optional[str] = None,
        rating_scale: Optional[Dict[str, int]] = None,
        ttl_seconds: int = DEFAULT_CONTEXT_TTL_SECONDS,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.root_dir = Path(root_dir) if root_dir else (
            PROJECT_ROOT / 'data' / 'planning_contexts'
        )
        self.scope_digest = scope_digest or default_scope_digest()
        # Derived policy version фиксируется на store: save и load одного
        # инстанса проверяют одну и ту же политику.
        self.calculation_policy_version = derive_calculation_policy_version(
            rating_scale
        )
        if ttl_seconds <= 0:
            raise ValueError('ttl_seconds must be positive')
        self.ttl_seconds = int(ttl_seconds)
        self._now = now_fn
        # Process-local уровень: (scope_digest, context_id) → (context,
        # expires_at, policy_version). Сравнение в одном процессе возвращает
        # тот же Python-объект.
        self._memory: Dict[Tuple[str, str], Tuple[PlanningContext, datetime, str]] = {}

    # -- save ---------------------------------------------------------------

    def save(self, context: PlanningContext) -> Dict[str, Any]:
        """Сохранить context как immutable artifact; вернуть handle
        (context_id, context_fingerprint, версии, время жизни)."""
        self._ensure_root()
        self.cleanup()
        created_at = self._now()
        expires_at = created_at + timedelta(seconds=self.ttl_seconds)
        fingerprint = context.context_fingerprint
        envelope = {
            'store_schema_version': STORE_SCHEMA_VERSION,
            'scope_digest': self.scope_digest,
            'created_at': canonical_datetime_str(created_at),
            'expires_at': canonical_datetime_str(expires_at),
            'context_fingerprint': fingerprint,
            'calculation_policy_version': self.calculation_policy_version,
            'context': _typed_payload(context),
        }
        raw = json.dumps(
            envelope, ensure_ascii=True, separators=(',', ':')
        ).encode('utf-8')
        for _ in range(5):  # коллизия token_hex практически исключена
            context_id = generate_context_id()
            if self._atomic_create(self._artifact_path(context_id), raw):
                self._memory[(self.scope_digest, context_id)] = (
                    context, expires_at, self.calculation_policy_version,
                )
                return {
                    'context_id': context_id,
                    'context_fingerprint': fingerprint,
                    'context_schema_version': context.context_schema_version,
                    'calculation_policy_version':
                        self.calculation_policy_version,
                    'created_at': created_at,
                    'expires_at': expires_at,
                }
        raise ContextStoreError('could not allocate unique context_id')

    # -- load ---------------------------------------------------------------

    def load(self, context_id: str) -> PlanningContext:
        """Загрузить core-owned context по opaque id.

        Проверки по порядку: формат id → существование файла (scope
        участвует в имени файла) → envelope → scope body → версии → TTL →
        fingerprint. БД не читается вовсе.
        """
        validate_context_id(context_id)

        memory_key = (self.scope_digest, context_id)
        cached = self._memory.get(memory_key)
        if cached is not None:
            context, expires_at, policy_version = cached
            if (
                self._now() < expires_at
                and policy_version == self.calculation_policy_version
            ):
                return context
            del self._memory[memory_key]  # истёк/политика изменилась — мимо

        path = self._artifact_path(context_id)
        try:
            raw = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            raise ContextNotFoundError(_MSG_NOT_FOUND)

        envelope = self._parse_envelope(raw)
        if envelope.get('store_schema_version') != STORE_SCHEMA_VERSION:
            raise ContextVersionMismatchError(
                'store envelope version is not compatible'
            )
        if envelope.get('scope_digest') != self.scope_digest:
            # Чужой scope: ровно тот же ответ, что для неизвестного id —
            # существование чужого context не раскрывается.
            raise ContextNotFoundError(_MSG_NOT_FOUND)
        if (
            envelope.get('calculation_policy_version')
            != self.calculation_policy_version
        ):
            raise ContextVersionMismatchError(
                'calculation policy version is not compatible'
            )

        expires_at = self._envelope_datetime(envelope, 'expires_at')
        if self._now() >= expires_at:
            path.unlink(missing_ok=True)  # cleanup expired
            raise ContextStaleError('context TTL expired')

        payload = envelope.get('context')
        if not isinstance(payload, dict):
            raise ContextFingerprintMismatchError(
                'core-owned context artifact is damaged'
            )
        if payload.get('context_schema_version') != CONTEXT_SCHEMA_VERSION:
            # Несовместимая schema: старый context не интерпретируется
            # молча — получить новый context и повторить analysis.
            raise ContextVersionMismatchError(
                'context schema version is not compatible'
            )

        try:
            context = _context_from_payload(
                payload,
                generated_at=self._envelope_datetime(envelope, 'created_at'),
            )
        except (KeyError, TypeError, ValueError):
            raise ContextFingerprintMismatchError(
                'core-owned context artifact is damaged'
            )
        if context.context_fingerprint != envelope.get('context_fingerprint'):
            raise ContextFingerprintMismatchError(
                'core-owned context artifact is damaged'
            )
        return context

    # -- cleanup ------------------------------------------------------------

    def cleanup(self) -> int:
        """Удалить истёкшие (и повреждённые до нечитаемости) артефакты.
        Активные context'ы не трогает. Возвращает число удалённых файлов."""
        removed = 0
        if not self.root_dir.is_dir():
            return removed
        for path in sorted(self.root_dir.iterdir()):
            if path.name.startswith('.tmp-'):  # орфанённый temp записи
                path.unlink(missing_ok=True)
                removed += 1
                continue
            if path.suffix != '.json':
                continue
            try:
                envelope = self._parse_envelope(path.read_bytes())
                expired = self._now() >= self._envelope_datetime(
                    envelope, 'expires_at'
                )
            except ContextStoreError:
                expired = True  # нечитаемый артефакт никогда не загрузится
            if expired:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    # -- internals ----------------------------------------------------------

    def _ensure_root(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _artifact_path(self, context_id: str) -> Path:
        """Имя файла — sha256(scope ':' id): id (пользовательская строка)
        никогда не становится filesystem path; чужой scope даже не
        разрешает имя файла чужого context'а."""
        validate_context_id(context_id)
        digest = hashlib.sha256(
            f'{self.scope_digest}:{context_id}'.encode('utf-8')
        ).hexdigest()
        return self.root_dir / f'{digest}.json'

    @staticmethod
    def _atomic_create(path: Path, raw: bytes) -> bool:
        """Write-once создание файла 0600: link() атомарен и отвергает
        перезапись существующего имени (частичного обновления context нет)."""
        tmp = path.parent / f'.tmp-{secrets.token_hex(8)}'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _parse_envelope(raw: bytes) -> Dict[str, Any]:
        try:
            envelope = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            raise ContextFingerprintMismatchError(
                'core-owned context artifact is damaged'
            )
        if not isinstance(envelope, dict):
            raise ContextFingerprintMismatchError(
                'core-owned context artifact is damaged'
            )
        return envelope

    def _envelope_datetime(self, envelope: Dict[str, Any], key: str) -> datetime:
        try:
            return _parse_iso_datetime(envelope[key])
        except (KeyError, TypeError, ValueError):
            raise ContextFingerprintMismatchError(
                'core-owned context artifact is damaged'
            )
