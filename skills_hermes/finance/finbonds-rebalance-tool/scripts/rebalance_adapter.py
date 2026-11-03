#!/usr/bin/env python3
"""finbonds-rebalance-tool — stable Hermes adapter (задача 030.11).

Единая точка вызова доменных команд ребалансировки core fin_bonds для
агента Hermes: report / plan / compare / analysis-snapshot. Adapter
скрывает transport-детали (ssh-хост, app-dir, venv), принимает ТОЛЬКО
typed whitelist аргументов и приводит ответ к одному контракту:

- stdout — machine-readable JSON (успех: canonical JSON core байт-в-байт;
  отказ adapter'а — typed error envelope);
- stderr — diagnostics (stderr core проходит насквозь + собственные
  строки adapter'а с префиксом);
- exit code: 0 успех (включая machine-readable отказы core вида
  CONTEXT_* — они приходят JSON'ом с exit 0), 2 — ошибки аргументов
  adapter'а, 3 — transport, 4 — не-JSON stdout core, 5 — timeout,
  6 — subcommand недоступен в core; ненулевой exit code core
  передаётся как есть.

Границы (правила 030 №6/№9): adapter ничего не считает (вся арифметика —
в core), не получает DB/API/secrets (DSN читает слой хранения core на
хосте), не подменяет canonical planner/validator и не расширяет
whitelist «на всякий случай». Snapshot (030.10) adapter всегда забирает
полным (--full), сохраняет в локальный artifact-файл (0600) для
sandbox и печатает LLM только metadata/preview + handle артефакта.

Transport: локальный subprocess ВСЕГДА argv-список без shell=True;
для ssh-транспорта удалённая командная строка собирается единственным
helper'ом (_shell_quote_all / build_remote_argv) через shlex.quote —
никакой конкатенации строк от LLM. Standard workflow — без
heredoc/sed/scp.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

ADAPTER_NAME = 'finbonds-rebalance-tool'

# --- exit codes adapter'а (ненулевые; коды core передаются как есть) ---
EXIT_USAGE = 2          # unknown/invalid/duplicate аргумент adapter'а
EXIT_TRANSPORT = 3      # transport не смог запустить core
EXIT_NON_JSON = 4       # stdout core — не валидный JSON
EXIT_TIMEOUT = 5        # core не уложился в таймаут
EXIT_UNAVAILABLE = 6    # subcommand пока нет в core (см. SPEC.unavailable)

# --- коды typed-ошибок envelope'а adapter'а ---
ERR_UNKNOWN_COMMAND = 'ADAPTER_UNKNOWN_COMMAND'
ERR_UNKNOWN_FLAG = 'ADAPTER_UNKNOWN_FLAG'
ERR_INVALID_ARGUMENT = 'ADAPTER_INVALID_ARGUMENT'
ERR_DUPLICATE_FLAG = 'ADAPTER_DUPLICATE_FLAG'
ERR_TRANSPORT = 'ADAPTER_TRANSPORT'
ERR_NON_JSON = 'ADAPTER_NON_JSON'
ERR_TIMEOUT = 'ADAPTER_TIMEOUT'
ERR_COMMAND_FAILED = 'ADAPTER_COMMAND_FAILED'
ERR_UNAVAILABLE = 'ADAPTER_UNAVAILABLE'

STDERR_PREFIX = '[%s]' % ADAPTER_NAME

# --- переменные окружения transport'а (карта — скилл finbonds-ops) ---
ENV_HOST = 'FIN_BONDS_HOST'            # ssh-alias хоста (там, где venv и .env)
ENV_APP_DIR = 'FIN_BONDS_APP_DIR'      # каталог приложения (default ~/work/bonds)
ENV_HOST_PY = 'FIN_BONDS_HOST_PY'      # интерпретатор core на хосте
ENV_TRANSPORT = 'FIN_BONDS_TRANSPORT'  # auto | ssh | local
ENV_TIMEOUT = 'FIN_BONDS_REBALANCE_TIMEOUT_SEC'  # таймаут core-команды
ENV_ARTIFACT_DIR = 'FIN_BONDS_ARTIFACT_DIR'      # каталог snapshot-артефактов

DEFAULT_APP_DIR_REMOTE = '~/work/bonds'
DEFAULT_HOST_PY = 'venv/bin/python'
DEFAULT_TIMEOUT_SEC = 300
DEFAULT_ARTIFACT_DIR = os.path.join('~', '.finbonds', 'artifacts')

# Сколько строк полного snapshot'а попадает в preview metadata-ответа
# (срез presentation-уровня; канонический compact — у core, 030.10).
PREVIEW_ROWS = 5

# --- лёгкие форматные проверки adapter'а (семантику валидирует core) ---
ISIN_RE = re.compile(r'^[A-Z0-9]{12}$')
CONTEXT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
RATING_RE = re.compile(r'^[A-Za-z0-9+-]{1,8}$')
FREQ_IN_RE = re.compile(r'^[0-9]+(?:,[0-9]+)*$')
TARGET_VALUE_RE = re.compile(r'^([A-Z0-9]{12})=([0-9]+(?:\.[0-9]+)?)$')
DATASET_CODE_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')

VIEW_CHOICES = (
    'summary', 'targets', 'trades', 'positions-after', 'violations', 'full',
)


class AdapterError(Exception):
    """Typed-ошибка adapter'а: код + сообщение + exit code (+ stderr_tail)."""

    def __init__(self, code: str, message: str, exit_code: int,
                 stderr_tail: str = ''):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


class ParsedCommand:
    """Результат разбора argv: subcommand + токены core + adapter-опции."""

    def __init__(
        self,
        subcommand: str,
        core_tokens: List[str],
        adapter_opts: Optional[Dict[str, str]] = None,
    ):
        self.subcommand = subcommand
        self.core_tokens = core_tokens
        self.adapter_opts = adapter_opts or {}


# ---------------------------------------------------------------------------
# Typed-валидаторы whitelist'а: возвращают канонический токен для core argv
# или бросают AdapterError(ERR_INVALID_ARGUMENT). Семантическую валидацию
# (шкала рейтинга, существование ISIN, взаимоисключимость фильтров) делает
# core — adapter проверяет только форму, чтобы мусор не уходил в transport.
# ---------------------------------------------------------------------------

def _invalid(message: str) -> AdapterError:
    return AdapterError(ERR_INVALID_ARGUMENT, message, EXIT_USAGE)


def _v_int(minimum: Optional[int] = None, maximum: Optional[int] = None) -> Callable:
    def validate(value: str) -> str:
        try:
            number = int(value)
        except ValueError:
            raise _invalid('ожидалось целое число, получено %r' % value)
        if minimum is not None and number < minimum:
            raise _invalid('ожидалось целое >= %d, получено %d' % (minimum, number))
        if maximum is not None and number > maximum:
            raise _invalid('ожидалось целое <= %d, получено %d' % (maximum, number))
        return str(number)
    return validate


def _v_float_positive(value: str) -> str:
    try:
        number = float(value)
    except ValueError:
        raise _invalid('ожидалось число, получено %r' % value)
    if not number > 0:
        raise _invalid('ожидалось число > 0, получено %r' % value)
    return value


def _v_choice(options: Tuple[str, ...]) -> Callable:
    def validate(value: str) -> str:
        if value not in options:
            raise _invalid(
                'ожидалось одно из %s, получено %r' % ('|'.join(options), value)
            )
        return value
    return validate


def _v_isin(value: str) -> str:
    if not ISIN_RE.match(value):
        raise _invalid(
            'ожидался ISIN (12 символов A-Z0-9, верхний регистр), получено %r' % value
        )
    return value


def _v_target_value(value: str) -> str:
    """'ISIN=RUB' — форма ISIN=положительное число (decimal без экспоненты).

    Порядок повторений adapter сохраняет как есть: порядок = allocation
    priority (контракт rebalance-plan, 030.8).
    """
    if not TARGET_VALUE_RE.match(value):
        raise _invalid(
            "ожидалась форма 'ISIN=сумма-RUB' (напр. RU000A0ZZZ01=350000), "
            'получено %r' % value
        )
    return value


def _v_context_id(value: str) -> str:
    if not CONTEXT_ID_RE.match(value):
        raise _invalid(
            'opaque context_id (латиница/цифры/._-, до 128 символов), '
            'получено %r' % value
        )
    return value


def _v_rating(value: str) -> str:
    if not RATING_RE.match(value):
        raise _invalid('ожидался код рейтинга (напр. AA-), получено %r' % value)
    return value


def _v_freq_in(value: str) -> str:
    if not FREQ_IN_RE.match(value):
        raise _invalid('ожидался список частот через запятую (напр. 2,4), '
                       'получено %r' % value)
    return value


def _v_iso_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise _invalid('ожидалась дата YYYY-MM-DD, получено %r' % value)
    return value


def _v_requires(value: str) -> str:
    """Коды datasets через запятую (контракт --requires, 030.10).

    Повторения флага склеиваются в один '--requires' в порядке следования;
    семантику кодов (ANALYSIS_DATA_UNAVAILABLE) проверяет core.
    """
    codes = [part.strip() for part in value.split(',') if part.strip()]
    if not codes:
        raise _invalid('ожидался непустой список кодов datasets')
    for code in codes:
        if not DATASET_CODE_RE.match(code):
            raise _invalid('некорректный код dataset %r' % code)
    return ','.join(codes)


# ---------------------------------------------------------------------------
# Whitelist subcommand'ов (факт post-031.2 core; имена флагов 1:1 с CLI core,
# сверены с `--help`: candidate-policy plan/compare и set-частота plan —
# 031.1/031.2, зеркала compare — 031.3 ничего в whitelist не добавил).
# Adapter подстраивается к core, не наоборот: расширение списка — только
# вслед за фактическим CLI core.
#
# Формат: flag -> (validator | None, repeatable). validator=None — флаг без
# значения (store_true в core); repeatable — флаг можно повторять
# (порядок повторений сохраняется в core argv как есть).
# ---------------------------------------------------------------------------

REPORT_FLAGS: Dict[str, Tuple[Optional[Callable], bool]] = {
    '--freq-min': (_v_int(minimum=1), False),
    '--freq-max': (_v_int(minimum=1), False),
    '--freq-in': (_v_freq_in, False),
    '--min-credit-rating': (_v_rating, False),
    '--exclude-sovereign': (None, False),
    '--include-ku': (None, False),
    '--include-held': (None, False),
    '--max-risk': (_v_int(minimum=1, maximum=5), False),
    '--max-listlevel': (_v_choice(('1', '2', '3')), False),
    '--max-ytm': (_v_float_positive, False),
    '--min-maturity': (_v_iso_date, False),
    '--max-maturity': (_v_iso_date, False),
    '--screener-limit': (_v_int(minimum=1), False),
    '--max-position-value': (_v_float_positive, False),
    '--redemptions-months': (_v_int(minimum=1), False),
}

PLAN_FLAGS: Dict[str, Tuple[Optional[Callable], bool]] = {
    # Формальные ограничения целевого портфеля (030.8) + set-частота
    # (031.2; взаимоисключимость с --freq-min/--freq-max проверяет core).
    '--freq-min': (_v_int(minimum=1), False),
    '--freq-max': (_v_int(minimum=1), False),
    '--freq-in': (_v_freq_in, False),
    '--min-credit-rating': (_v_rating, False),
    '--exclude-sovereign': (None, False),
    '--max-position-value': (_v_float_positive, False),
    # Candidate-policy покупки (031.1): валидаторы те же, что у report
    # (копий семантики нет); без флагов core берёт canonical defaults
    # (default_candidate_filters). Каждый флаг — одноразовый.
    '--max-risk': (_v_int(minimum=1, maximum=5), False),
    '--max-listlevel': (_v_choice(('1', '2', '3')), False),
    '--max-ytm': (_v_float_positive, False),
    '--include-ku': (None, False),
    '--min-maturity': (_v_iso_date, False),
    '--max-maturity': (_v_iso_date, False),
    # Intents и проекция (030.8).
    '--sell-all': (_v_isin, True),
    '--target-value': (_v_target_value, True),
    '--context-id': (_v_context_id, False),
    '--view': (_v_choice(VIEW_CHOICES), False),
}

# Флаги варианта compare (030.9) — зеркала флагов plan (включая
# candidate-policy и freq_in, 031.1/031.2) без --context-id/--view:
# те же typed-валидаторы (core compare переиспользует границу CLI
# plan — копий семантики флагов нет ни у core, ни у adapter'а).
_VARIANT_PREFIXES = ('a', 'b')
_VARIANT_EXCLUDED = ('--context-id', '--view')

COMPARE_FLAGS: Dict[str, Tuple[Optional[Callable], bool]] = {
    '--%s-%s' % (prefix, suffix.lstrip('-')): spec_entry
    for prefix in _VARIANT_PREFIXES
    for suffix, spec_entry in PLAN_FLAGS.items()
    if suffix not in _VARIANT_EXCLUDED
}
COMPARE_FLAGS['--context-id'] = PLAN_FLAGS['--context-id']

SPEC: Dict[str, Dict[str, Any]] = {
    'report': {
        'core': 'rebalance-report',
        'flags': dict(REPORT_FLAGS),
        'adapter_flags': set(),
        'unavailable': None,
    },
    'plan': {
        'core': 'rebalance-plan',
        'flags': dict(PLAN_FLAGS),
        'adapter_flags': set(),
        'unavailable': None,
    },
    # 030.9 приземлился в core параллельно с этой задачей: compare
    # реализован по фактическому контракту (--a-*/--b-* зеркала plan,
    # общий --context-id — оба варианта на одном PlanningContext).
    'compare': {
        'core': 'rebalance-compare',
        'flags': dict(COMPARE_FLAGS),
        'adapter_flags': set(),
        'unavailable': None,
    },
    'analysis-snapshot': {
        'core': 'rebalance-analysis-snapshot',
        'flags': {
            '--requires': (_v_requires, True),
        },
        # Adapter-only опции (в core argv не попадают).
        'adapter_flags': {'--artifact-dir'},
        'unavailable': None,
    },
}


# ---------------------------------------------------------------------------
# Разбор argv против whitelist'а (собственный парсер: argparse печатает
# usage в stderr и молчит в stdout — контракт adapter'а требует typed
# JSON-ошибку в stdout при любом отказе разбора).
# ---------------------------------------------------------------------------

def parse_argv(argv: List[str]) -> ParsedCommand:
    if not argv:
        raise AdapterError(
            ERR_UNKNOWN_COMMAND,
            'укажите subcommand (%s)' % ', '.join(sorted(SPEC)),
            EXIT_USAGE,
        )

    subcommand = argv[0]
    if subcommand not in SPEC:
        raise AdapterError(
            ERR_UNKNOWN_COMMAND,
            'неизвестный subcommand %r; доступно: %s'
            % (subcommand, ', '.join(sorted(SPEC))),
            EXIT_USAGE,
        )

    spec = SPEC[subcommand]
    if spec['unavailable']:
        raise AdapterError(ERR_UNAVAILABLE, spec['unavailable'], EXIT_UNAVAILABLE)

    core_tokens: List[str] = []
    adapter_opts: Dict[str, str] = {}
    requires_parts: List[str] = []
    seen: set = set()

    index = 1
    while index < len(argv):
        token = argv[index]
        if token in spec['adapter_flags']:
            if token in adapter_opts:
                raise AdapterError(
                    ERR_DUPLICATE_FLAG, 'флаг %s указан повторно' % token,
                    EXIT_USAGE)
            index += 1
            if index >= len(argv) or argv[index].startswith('--'):
                raise AdapterError(
                    ERR_INVALID_ARGUMENT,
                    'флаг %s требует значения (локальный каталог)' % token,
                    EXIT_USAGE,
                )
            adapter_opts[token] = argv[index]
            index += 1
            continue
        if token not in spec['flags']:
            raise AdapterError(
                ERR_UNKNOWN_FLAG,
                '%s: недопустимый флаг для %r; whitelist: %s'
                % (token, subcommand, ', '.join(sorted(spec['flags']))),
                EXIT_USAGE,
            )
        validator, repeatable = spec['flags'][token]
        if not repeatable and token in seen:
            raise AdapterError(
                ERR_DUPLICATE_FLAG, 'флаг %s указан повторно' % token, EXIT_USAGE)
        seen.add(token)

        if validator is None:
            # Флаг без значения (store_true в core).
            core_tokens.append(token)
            index += 1
            continue

        index += 1
        if index >= len(argv) or argv[index].startswith('--'):
            raise AdapterError(
                ERR_INVALID_ARGUMENT,
                'флаг %s требует значения' % token,
                EXIT_USAGE,
            )
        raw_value = argv[index]
        try:
            validated = validator(raw_value)
        except AdapterError as exc:
            exc.message = 'флаг %s: %s' % (token, exc.message)
            raise
        if token == '--requires':
            # Core-опция --requires НЕ append: повторения склеиваются в
            # один флаг, иначе core молча взял бы только последнее.
            requires_parts.append(validated)
        else:
            core_tokens.extend([token, validated])
        index += 1

    if requires_parts:
        core_tokens.extend(['--requires', ','.join(requires_parts)])

    # Формат ответа фиксирован контрактом adapter'а: json (единственный
    # машинный формат core, choices=['json']).
    core_tokens.extend(['--format', 'json'])
    return ParsedCommand(subcommand, core_tokens, adapter_opts)


def usage_text() -> str:
    flags = {
        'analysis-snapshot': '[--requires CODES] [--artifact-dir DIR]',
        'compare': ' '.join(sorted(COMPARE_FLAGS)),
        'plan': ' '.join(sorted(PLAN_FLAGS)),
        'report': ' '.join(sorted(REPORT_FLAGS)),
    }
    lines = [
        '%s — typed adapter доменных команд ребалансировки fin_bonds (030.11).'
        % ADAPTER_NAME,
        '',
        'Usage: rebalance_adapter.py <subcommand> [flags]',
        '',
    ]
    for name in sorted(SPEC):
        lines.append('  %-18s core: %-28s %s'
                     % (name, SPEC[name]['core'], flags[name]))
    lines += [
        '',
        'Environment: %s, %s, %s, %s, %s, %s.'
        % (ENV_HOST, ENV_APP_DIR, ENV_HOST_PY, ENV_TRANSPORT, ENV_TIMEOUT,
           ENV_ARTIFACT_DIR),
        'stdout — machine-readable JSON; stderr — diagnostics; exit codes '
        'см. SKILL.md.',
    ]
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# Transport: локальный subprocess — всегда argv-список; ssh-командная строка
# собирается единственным helper'ом с shlex.quote (централизованный safe
# quoting; строки от LLM сюда не попадают — только валидированные токены).
# ---------------------------------------------------------------------------

def _shell_quote_all(tokens: List[str]) -> str:
    """Единственная точка экранирования удалённой командной строки."""
    return ' '.join(shlex.quote(token) for token in tokens)


def build_remote_argv(
    host: str, app_dir: str, host_py: str,
    core_command: str, core_tokens: List[str],
) -> List[str]:
    """ssh argv (список, без shell=True): cd app_dir && <py> main.py <cmd>."""
    remote = 'cd %s && %s' % (
        shlex.quote(app_dir),
        _shell_quote_all([host_py, 'main.py', core_command] + core_tokens),
    )
    return [
        'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
        host, remote,
    ]


def build_local_argv(core_command: str, core_tokens: List[str]) -> List[str]:
    """Локальный argv (smoke на хосте/в dev): <python> main.py <cmd>."""
    return [sys.executable, 'main.py', core_command] + core_tokens


def resolve_transport() -> str:
    mode = os.environ.get(ENV_TRANSPORT, 'auto').strip().lower()
    if mode not in ('auto', 'ssh', 'local'):
        raise AdapterError(
            ERR_TRANSPORT,
            '%s=%r не поддерживается (auto|ssh|local)' % (ENV_TRANSPORT, mode),
            EXIT_TRANSPORT,
        )
    if mode == 'auto':
        mode = 'ssh' if os.environ.get(ENV_HOST) else 'local'
    return mode


def resolve_app_dir(transport: str) -> str:
    app_dir = os.environ.get(ENV_APP_DIR) or (
        DEFAULT_APP_DIR_REMOTE if transport == 'ssh' else os.getcwd()
    )
    # '~'-форма (дефолт ssh-транспорта или явный FIN_BONDS_APP_DIR)
    # разворачивается на стороне adapter'а ДО shlex.quote: внутри quote
    # удалённый shell tilde не разворачивает (находка 030.13, фикс 031.4).
    # Для абсолютных путей expanduser — no-op: явный абсолютный
    # FIN_BONDS_APP_DIR пользователя работает как раньше.
    return os.path.expanduser(app_dir)


def _resolve_timeout() -> int:
    raw = os.environ.get(ENV_TIMEOUT) or str(DEFAULT_TIMEOUT_SEC)
    try:
        value = int(raw)
    except ValueError:
        raise AdapterError(
            ERR_TRANSPORT, '%s=%r не целое' % (ENV_TIMEOUT, raw), EXIT_TRANSPORT)
    if value < 1:
        raise AdapterError(
            ERR_TRANSPORT, '%s должно быть >= 1' % ENV_TIMEOUT, EXIT_TRANSPORT)
    return value


def run_core(core_command: str, core_tokens: List[str]) -> Tuple[int, str, str]:
    """Запускает core-команду, возвращает (exit_code, stdout, stderr).

    stdout/stderr захватываются целиком: stdout обязан быть валидным JSON
    (проверяет вызывающая сторона), stderr пробрасывается в diagnostics.
    """
    transport = resolve_transport()
    app_dir = resolve_app_dir(transport)
    timeout = _resolve_timeout()

    if transport == 'ssh':
        host = os.environ.get(ENV_HOST) or ''
        if not host:
            raise AdapterError(
                ERR_TRANSPORT, '%s не задан (ssh transport)' % ENV_HOST,
                EXIT_TRANSPORT)
        host_py = os.environ.get(ENV_HOST_PY) or DEFAULT_HOST_PY
        argv = build_remote_argv(host, app_dir, host_py, core_command, core_tokens)
        cwd = None
    else:
        if not os.path.isfile(os.path.join(app_dir, 'main.py')):
            raise AdapterError(
                ERR_TRANSPORT,
                'локальный transport: %s не содержит main.py '
                '(задай %s или выполняй adapter из корня checkout)'
                % (app_dir, ENV_APP_DIR),
                EXIT_TRANSPORT,
            )
        argv = build_local_argv(core_command, core_tokens)
        cwd = app_dir

    _stderr('transport=%s app_dir=%s command=%s' % (transport, app_dir, core_command))
    try:
        completed = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise AdapterError(
            ERR_TIMEOUT,
            'core-команда %s не завершилась за %dс' % (core_command, timeout),
            EXIT_TIMEOUT,
        )
    except OSError as exc:
        raise AdapterError(
            ERR_TRANSPORT,
            'не удалось запустить core (%s): %s' % (argv[0], exc),
            EXIT_TRANSPORT,
        )
    return (
        completed.returncode,
        completed.stdout.decode('utf-8', errors='replace'),
        completed.stderr.decode('utf-8', errors='replace'),
    )


def _stderr(message: str) -> None:
    sys.stderr.write('%s %s\n' % (STDERR_PREFIX, message))


# ---------------------------------------------------------------------------
# Обработка результата core: pass-through для report/plan (canonical JSON
# байт-в-байт, exit code сохраняется); snapshot (030.10) — полный payload
# в artifact-файл, LLM получает metadata/preview + handle.
# ---------------------------------------------------------------------------

def _try_parse_json(stdout: str) -> Optional[Any]:
    if not stdout.strip():
        return None
    try:
        return json.loads(stdout)
    except ValueError:
        return None


def _emit_raw_json(stdout: str) -> None:
    sys.stdout.write(stdout)
    if not stdout.endswith('\n'):
        sys.stdout.write('\n')


def handle_result(
    parsed: ParsedCommand,
    core_command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> int:
    _relay_stderr(stderr)
    payload = _try_parse_json(stdout)

    if exit_code == 0:
        if payload is None:
            raise AdapterError(
                ERR_NON_JSON,
                '%s: stdout пуст или не валидный JSON (контракт: '
                'machine-readable JSON в stdout)' % core_command,
                EXIT_NON_JSON,
            )
        if parsed.subcommand == 'analysis-snapshot':
            return _snapshot_result(payload, core_command,
                                    parsed.adapter_opts.get('--artifact-dir'))
        _emit_raw_json(stdout)
        return 0

    # Ненулевой exit code core. Если core всё же отдал валидный JSON —
    # machine-readable результат уже есть: pass-through + его exit code
    # (envelope adapter'а не добавляется, stdout остаётся одним JSON).
    if payload is not None and parsed.subcommand != 'analysis-snapshot':
        _emit_raw_json(stdout)
        return exit_code

    raise AdapterError(
        ERR_COMMAND_FAILED,
        '%s завершилась с кодом %d%s'
        % (core_command, exit_code,
           '; stdout пуст или не JSON' if payload is None else ''),
        exit_code,
        stderr_tail=stderr,
    )


def _snapshot_result(
    payload: Any,
    core_command: str,
    artifact_dir: Optional[str],
) -> int:
    if not isinstance(payload, dict) or payload.get('artifact') != 'analysis_snapshot':
        raise AdapterError(
            ERR_NON_JSON,
            '%s: ожидался payload с artifact=analysis_snapshot' % core_command,
            EXIT_NON_JSON,
        )
    artifact_path = _write_artifact(payload, artifact_dir)
    sys.stdout.write(
        json.dumps(_snapshot_envelope(payload, artifact_path),
                   ensure_ascii=False, indent=2) + '\n'
    )
    return 0


def _write_artifact(payload: Dict[str, Any], artifact_dir: Optional[str]) -> str:
    """Полный snapshot — приватный локальный файл (0600, каталог 0700).

    Sandbox (python-kernel) читает файл напрямую; наружу (web/external API)
    содержимое не отправляется — правило приватности 030.12.
    """
    context = payload.get('context') or {}
    context_id = str(context.get('context_id') or 'unknown')
    if not CONTEXT_ID_RE.match(context_id):
        context_id = 'unknown'
    directory = os.path.expanduser(
        artifact_dir or os.environ.get(ENV_ARTIFACT_DIR) or DEFAULT_ARTIFACT_DIR
    )
    os.makedirs(directory, mode=0o700, exist_ok=True)
    # context_id валидирован — компонент имени файла безопасен.
    path = os.path.join(directory, 'analysis_snapshot_%s.json' % context_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def _snapshot_envelope(payload: Dict[str, Any], artifact_path: str) -> Dict[str, Any]:
    """Metadata/preview envelope: блоки context/analysis/capabilities/
    limitations — из payload как есть (маленькие); portfolio/universe —
    counts + preview-срез (presentation, не арифметика) + handle артефакта.
    """
    portfolio = payload.get('portfolio') or {}
    universe = payload.get('universe') or {}
    positions = portfolio.get('positions') or []
    rows = universe.get('rows') or []
    with open(artifact_path, 'rb') as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()

    return {
        'artifact': 'analysis_snapshot_handle',
        'snapshot_schema_version': payload.get('snapshot_schema_version'),
        'generated_at': payload.get('generated_at'),
        'context': payload.get('context'),
        'analysis': payload.get('analysis'),
        'capabilities': payload.get('capabilities'),
        'limitations': payload.get('limitations'),
        'portfolio': {
            'cash': portfolio.get('cash'),
            'positions_count': portfolio.get('positions_count', len(positions)),
            'positions_preview': positions[:PREVIEW_ROWS],
        },
        'universe': {
            'policy': universe.get('policy'),
            'row_count': universe.get('row_count', len(rows)),
            'rows_preview': rows[:PREVIEW_ROWS],
        },
        'artifact_file': {
            'path': artifact_path,
            'sha256': digest,
            'usage': (
                'полный snapshot для sandbox/python-kernel; LLM работает '
                'только с metadata/preview; содержимое приватное — наружу '
                'не отправлять'
            ),
        },
    }


def _relay_stderr(stderr: str) -> None:
    """stderr core — насквозь в stderr adapter'а (diagnostics, не контракт)."""
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith('\n'):
            sys.stderr.write('\n')


def emit_error(error: AdapterError, subcommand: Optional[str] = None,
               core_command: Optional[str] = None) -> int:
    """Typed error envelope — machine-readable JSON в stdout adapter'а.

    Traceback'и в stdout не попадают никогда; человеческие diagnostics —
    в stderr. При отказе самой core-команды её exit code сохраняется.
    """
    error_payload = {
        'code': error.code,
        'message': error.message,
        'subcommand': subcommand,
        'core_command': core_command,
        'exit_code': error.exit_code,
    }
    if error.stderr_tail:
        error_payload['stderr_tail'] = error.stderr_tail[-1500:]
    envelope = {
        'artifact': 'adapter_error',
        'adapter': ADAPTER_NAME,
        'error': error_payload,
    }
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, indent=2) + '\n')
    _stderr('%s: %s' % (error.code, error.message))
    return error.exit_code


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('-h', '--help'):
        sys.stdout.write(usage_text())
        return 0

    try:
        parsed = parse_argv(argv)
    except AdapterError as exc:
        return emit_error(exc)

    core_command = SPEC[parsed.subcommand]['core']
    try:
        exit_code, stdout, stderr = run_core(core_command, parsed.core_tokens)
        return handle_result(parsed, core_command, exit_code, stdout, stderr)
    except AdapterError as exc:
        return emit_error(exc, subcommand=parsed.subcommand,
                          core_command=core_command)


if __name__ == '__main__':
    sys.exit(main())
