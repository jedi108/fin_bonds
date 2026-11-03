#!/usr/bin/env python3
"""Тесты rebalance_adapter.py (030.11): whitelist, transport, контракт
stdout/stderr/exit codes, snapshot как artifact.

Запуск (из корня core-репозитория):
  ./venv/bin/python -m pytest skills/scripts/test_rebalance_adapter.py -v

Adapter вызывается как процесс (subprocess) против stub `main.py` в tmp
app-dir (local transport) — проверяется реальный CLI-контракт: stdout —
machine-readable JSON, ошибки — typed envelope, exit codes сохраняются.
Сеть/ssh/БД не нужны.
"""
import hashlib
import json
import os
import pathlib
import shlex
import stat
import subprocess
import sys
from typing import Dict, List, Optional

import pytest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
SKILL_SCRIPTS = (
    SCRIPTS_DIR.parent / 'finance' / 'finbonds-rebalance-tool' / 'scripts'
)
sys.path.insert(0, str(SKILL_SCRIPTS))

import rebalance_adapter as ra  # noqa: E402

ADAPTER = str(SKILL_SCRIPTS / 'rebalance_adapter.py')

ISIN_A = 'RU000A0ZZZ01'
ISIN_B = 'RU000B0YYYY2'
ISIN_C = 'RU000C0XXXX3'
CONTEXT_ID = 'abcdef0123456789abcdef0123456789'

# Полный snapshot payload (факт контракта 030.10: artifact=analysis_snapshot,
# context/portfolio/universe/analysis/capabilities/limitations).
STUB_SNAPSHOT = {
    'artifact': 'analysis_snapshot',
    'snapshot_schema_version': 1,
    'generated_at': '2026-10-01T12:00:00+00:00',
    'preview': False,
    'context': {
        'context_id': CONTEXT_ID,
        'context_fingerprint': 'f' * 64,
        'context_schema_version': 1,
        'calculation_policy_version': 1,
        'as_of': '2026-10-01T12:00:00+00:00',
        'as_of_date': '2026-10-01',
        'created_at': '2026-10-01T12:00:00+00:00',
        'expires_at': '2026-10-01T18:00:00+00:00',
        'ttl_seconds': 21600,
        'data_gate': {'status': 'OK', 'is_fresh': True},
    },
    'portfolio': {
        'cash': {'cash_available_rub': '100000.00', 'cash_known': True},
        'positions_count': 6,
        'positions': [{'isin': 'ISIN%d' % i, 'qty': '10'} for i in range(6)],
    },
    'universe': {
        'policy': {'limit': 10000},
        'row_count': 7,
        'rows': [{'isin': 'UNI%d' % i, 'ytm_pct': 10.0 + i} for i in range(7)],
    },
    'analysis': {'requested_datasets': [], 'status': 'OK', 'missing': []},
    'capabilities': {'read_only': True},
    'limitations': [],
}

# Stub core CLI: поведение через STUB_CASE (см. тест-контракты ниже).
STUB_MAIN = (
    '''
import json, os, sys, time
case = os.environ.get('STUB_CASE', 'ok')
if case == 'exit_fail':
    sys.stderr.write('boom\\n')
    sys.exit(1)
if case == 'exit_fail_json':
    print(json.dumps({'artifact': 'refusal',
                      'diagnostics': [{'code': 'CONTEXT_NOT_FOUND'}]}))
    sys.exit(3)
if case == 'non_json':
    sys.stdout.write('hello\\n')
    sys.exit(0)
if case == 'timeout':
    time.sleep(5)
    print(json.dumps({'ok': True}))
    sys.exit(0)
if case == 'snapshot':
    print(__SNAPSHOT_JSON__)
    sys.exit(0)
if case == 'compare_ctx':
    print(json.dumps({'artifact': 'compare',
                      'compare_schema_version': 1,
                      'context_id': __CONTEXT_ID__,
                      'context_fingerprint': 'f' * 64,
                      'as_of': '2026-10-01',
                      'comparable': True,
                      'diagnostics': [],
                      'delta': {}}))
    sys.exit(0)
print(json.dumps({'ok': True, 'argv': sys.argv[1:]}))
sys.exit(0)
'''
    .replace('__SNAPSHOT_JSON__', repr(json.dumps(STUB_SNAPSHOT)))
    .replace('__CONTEXT_ID__', repr(CONTEXT_ID))
)

# Stub ssh-бинарник (регрессия tilde-дефекта 030.13 без сети): печатает
# JSON с удалённой командной строкой — adapter проходит его как есть.
STUB_SSH = '''#!/usr/bin/env python3
import json, sys
sys.stdout.write(json.dumps({'ok': True, 'remote': sys.argv[-1]}) + '\\n')
'''


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_dir(tmp_path):
    core = tmp_path / 'core'
    core.mkdir()
    (core / 'main.py').write_text(STUB_MAIN, encoding='utf-8')
    return core


def run_adapter(
    args: List[str],
    app_dir: pathlib.Path,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Герметичный вызов adapter'а: local transport на stub app-dir."""
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith('FIN_BONDS')
    }
    env['FIN_BONDS_TRANSPORT'] = 'local'
    env['FIN_BONDS_APP_DIR'] = str(app_dir)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, ADAPTER] + args,
        capture_output=True, text=True, env=env, timeout=60,
    )


def assert_single_json(stdout: str) -> dict:
    """stdout — ровно один валидный JSON, без traceback'ов."""
    assert 'Traceback' not in stdout
    payload = json.loads(stdout)  # бросит исключение при мусоре вокруг
    assert isinstance(payload, dict)
    return payload


# ---------------------------------------------------------------------------
# Разбор argv: typed whitelist
# ---------------------------------------------------------------------------

def test_help_prints_usage_and_exits_zero(tmp_path):
    result = run_adapter(['--help'], tmp_path)
    assert result.returncode == 0
    assert 'Usage' in result.stdout
    assert 'report' in result.stdout and 'plan' in result.stdout


def test_no_subcommand_typed_error(app_dir):
    result = run_adapter([], app_dir)
    assert result.returncode == ra.EXIT_USAGE
    payload = assert_single_json(result.stdout)
    assert payload['artifact'] == 'adapter_error'
    assert payload['error']['code'] == ra.ERR_UNKNOWN_COMMAND


def test_unknown_subcommand_typed_error(app_dir):
    result = run_adapter(['portfolio-secrets'], app_dir)
    assert result.returncode == ra.EXIT_USAGE
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_UNKNOWN_COMMAND


def test_unknown_flag_rejected(app_dir):
    result = run_adapter(['plan', '--scenario', 'x.json'], app_dir)
    assert result.returncode == ra.EXIT_USAGE
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_UNKNOWN_FLAG
    assert '--scenario' in payload['error']['message']


@pytest.mark.parametrize('args', [
    ['report', '--freq-min', 'abc'],
    ['report', '--max-risk', '9'],
    ['report', '--max-listlevel', '4'],
    ['report', '--max-ytm', '-3'],
    ['report', '--min-maturity', '2026-13-99'],
    ['report', '--freq-in', 'a,b'],
    ['plan', '--freq-min', '0'],
    ['plan', '--freq-in', '4,abc'],                 # нечисловая частота
    ['plan', '--max-risk', '9'],                    # вне 1..5
    ['plan', '--max-risk', 'abc'],
    ['plan', '--max-listlevel', '4'],
    ['plan', '--max-ytm', '-3'],
    ['plan', '--min-maturity', '2026-02-30'],       # несуществующая дата
    ['plan', '--sell-all', 'ru000a0zzz01'],          # нижний регистр
    ['plan', '--sell-all', 'RU000A0ZZZ0'],           # 11 символов
    ['plan', '--target-value', 'RU000A0ZZZ01'],      # без '=сумма'
    ['plan', '--target-value', 'RU000A0ZZZ01=-50'],  # отрицательная
    ['plan', '--view', 'everything'],
    ['plan', '--context-id', 'bad id; rm -rf'],
    ['compare', '--a-max-risk', '9'],                # зеркало тоже валидируется
    ['compare', '--b-freq-in', '4,x'],
])
def test_invalid_arguments_typed_error(app_dir, args):
    result = run_adapter(args, app_dir)
    assert result.returncode == ra.EXIT_USAGE
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_INVALID_ARGUMENT


def test_flag_without_value_rejected_with_value(app_dir):
    result = run_adapter(['plan', '--freq-min', '--freq-max', '12'], app_dir)
    assert result.returncode == ra.EXIT_USAGE
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_INVALID_ARGUMENT


def test_duplicate_non_repeatable_flag_rejected(app_dir):
    result = run_adapter(['report', '--include-held', '--include-held'], app_dir)
    assert result.returncode == ra.EXIT_USAGE
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_DUPLICATE_FLAG


def test_flag_at_end_without_value(app_dir):
    result = run_adapter(['plan', '--sell-all'], app_dir)
    assert result.returncode == ra.EXIT_USAGE
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_INVALID_ARGUMENT


def test_parse_appends_format_json():
    parsed = ra.parse_argv(['report', '--include-held'])
    assert parsed.core_tokens == ['--include-held', '--format', 'json']


def test_parse_preserves_repeat_order_priority():
    parsed = ra.parse_argv([
        'plan',
        '--target-value', '%s=100000' % ISIN_A,
        '--sell-all', ISIN_B,
        '--target-value', '%s=200000' % ISIN_C,
    ])
    assert parsed.core_tokens == [
        '--target-value', '%s=100000' % ISIN_A,
        '--sell-all', ISIN_B,
        '--target-value', '%s=200000' % ISIN_C,
        '--format', 'json',
    ]


# ---------------------------------------------------------------------------
# Transport: argv-списки и централизованный safe quoting
# ---------------------------------------------------------------------------

def test_local_argv_is_list_without_shell():
    argv = ra.build_local_argv('rebalance-plan', ['--sell-all', ISIN_A])
    assert isinstance(argv, list)
    assert argv[1:] == ['main.py', 'rebalance-plan', '--sell-all', ISIN_A]


def test_remote_argv_quotes_every_token():
    argv = ra.build_remote_argv(
        'host1', '/home/j/work bonds', 'venv/bin/python',
        'rebalance-plan',
        ['--sell-all', 'AB; $(rm -rf)', '--format', 'json'],
    )
    assert isinstance(argv, list)
    assert argv[0] == 'ssh'
    assert argv[-2] == 'host1'
    remote = argv[-1]
    # Апп-дир и опасные токены экранированы shlex.quote (единственный helper).
    assert "'/home/j/work bonds'" in remote
    assert "'AB; $(rm -rf)'" in remote
    assert 'main.py rebalance-plan' in remote
    assert '--format json' in remote


def test_transport_resolution(monkeypatch):
    monkeypatch.setenv('FIN_BONDS_TRANSPORT', 'auto')
    monkeypatch.delenv('FIN_BONDS_HOST', raising=False)
    assert ra.resolve_transport() == 'local'
    monkeypatch.setenv('FIN_BONDS_HOST', 'host1')
    assert ra.resolve_transport() == 'ssh'
    monkeypatch.delenv('FIN_BONDS_TRANSPORT', raising=False)
    monkeypatch.delenv('FIN_BONDS_HOST', raising=False)
    assert ra.resolve_transport() == 'local'
    monkeypatch.setenv('FIN_BONDS_TRANSPORT', 'bogus')
    with pytest.raises(ra.AdapterError) as exc_info:
        ra.resolve_transport()
    assert exc_info.value.code == ra.ERR_TRANSPORT


def test_local_transport_missing_app_dir(tmp_path):
    empty = tmp_path / 'empty'
    empty.mkdir()
    result = run_adapter(['report'], empty)
    assert result.returncode == ra.EXIT_TRANSPORT
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_TRANSPORT


# ---------------------------------------------------------------------------
# Tilde-дефект транспорта (находка 030.13, фикс 031.4)
# ---------------------------------------------------------------------------

def test_resolve_app_dir_expands_tilde(monkeypatch, tmp_path):
    # '~'-форма дефолта ssh-транспорта разворачивается на стороне adapter'а.
    monkeypatch.delenv('FIN_BONDS_APP_DIR', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    assert ra.resolve_app_dir('ssh') == str(tmp_path / 'work' / 'bonds')
    # Явный абсолютный FIN_BONDS_APP_DIR пользователя не меняется (no-op).
    monkeypatch.setenv('FIN_BONDS_APP_DIR', '/abs/path')
    assert ra.resolve_app_dir('ssh') == '/abs/path'
    # Явная '~'-форма — тот же транспортный дефект, разворачивается тоже.
    monkeypatch.setenv('FIN_BONDS_APP_DIR', '~/custom/bonds')
    assert ra.resolve_app_dir('ssh') == str(tmp_path / 'custom' / 'bonds')


def test_remote_argv_default_app_dir_expanded_before_quote(monkeypatch, tmp_path):
    # Регрессия 030.13: quoted-литерал '~/work/bonds' не появляется в
    # удалённой строке — дефолт разворачивается ДО shlex.quote.
    monkeypatch.delenv('FIN_BONDS_APP_DIR', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    app_dir = ra.resolve_app_dir('ssh')
    remote = ra.build_remote_argv(
        'host1', app_dir, 'venv/bin/python', 'rebalance-plan',
        ['--max-risk', '3', '--format', 'json'],
    )[-1]
    assert "'~/work/bonds'" not in remote
    assert remote.startswith(
        'cd %s &&' % shlex.quote(str(tmp_path / 'work' / 'bonds'))
    )


def test_ssh_transport_tilde_defect_not_reproducible(tmp_path):
    # E2E без сети: ssh-транспорт через stub `ssh` в PATH. До фикса
    # удалённая строка содержала литерал "cd '~/work/bonds'" (tilde под
    # shlex.quote не разворачивался удалённым shell) — теперь туда
    # попадает абсолютный путь, развёрнутый на стороне adapter'а.
    stub_bin = tmp_path / 'bin'
    stub_bin.mkdir()
    stub_ssh = stub_bin / 'ssh'
    stub_ssh.write_text(STUB_SSH, encoding='utf-8')
    stub_ssh.chmod(0o755)
    home = tmp_path / 'home'
    home.mkdir()

    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith('FIN_BONDS')
    }
    env['PATH'] = str(stub_bin) + os.pathsep + env.get('PATH', '')
    env['HOME'] = str(home)
    env['FIN_BONDS_TRANSPORT'] = 'ssh'
    env['FIN_BONDS_HOST'] = 'stub-host'
    result = subprocess.run(
        [sys.executable, ADAPTER, 'plan', '--max-risk', '3'],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0
    payload = assert_single_json(result.stdout)
    remote = payload['remote']
    assert "'~/work/bonds'" not in remote  # tilde-дефект не воспроизводится
    assert remote.startswith(
        'cd %s &&' % shlex.quote(str(home / 'work' / 'bonds'))
    )
    assert 'main.py rebalance-plan' in remote
    assert '--max-risk 3' in remote


# ---------------------------------------------------------------------------
# Контракт результата: pass-through, exit codes, diagnostics
# ---------------------------------------------------------------------------

def test_report_passthrough_exact_json(app_dir):
    result = run_adapter(['report', '--include-held'], app_dir)
    assert result.returncode == 0
    expected = json.dumps({
        'ok': True,
        'argv': ['rebalance-report', '--include-held', '--format', 'json'],
    }) + '\n'
    assert result.stdout == expected


def test_plan_args_reach_core_in_order(app_dir):
    result = run_adapter([
        'plan',
        '--sell-all', ISIN_A,
        '--target-value', '%s=350000' % ISIN_B,
        '--target-value', '%s=200000' % ISIN_C,
        '--view', 'summary',
        '--exclude-sovereign',
    ], app_dir)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload['argv'] == [
        'rebalance-plan',
        '--sell-all', ISIN_A,
        '--target-value', '%s=350000' % ISIN_B,
        '--target-value', '%s=200000' % ISIN_C,
        '--view', 'summary',
        '--exclude-sovereign',
        '--format', 'json',
    ]


def test_plan_candidate_policy_flags_reach_core_in_order(app_dir):
    # 031.1/031.2: candidate-policy и set-частота выражаются флагами plan
    # (имена сверены с `rebalance-plan --help` core).
    result = run_adapter([
        'plan',
        '--target-value', '%s=350000' % ISIN_B,
        '--freq-in', '4,12',
        '--max-risk', '3',
        '--max-listlevel', '2',
        '--max-ytm', '25.5',
        '--include-ku',
        '--min-maturity', '2026-01-01',
        '--max-maturity', '2030-06-30',
    ], app_dir)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload['argv'] == [
        'rebalance-plan',
        '--target-value', '%s=350000' % ISIN_B,
        '--freq-in', '4,12',
        '--max-risk', '3',
        '--max-listlevel', '2',
        '--max-ytm', '25.5',
        '--include-ku',
        '--min-maturity', '2026-01-01',
        '--max-maturity', '2030-06-30',
        '--format', 'json',
    ]


def test_plan_candidate_policy_flag_is_single_shot(app_dir):
    # Каждый policy-флаг задаёт одно значение (не repeatable) — как в core.
    result = run_adapter(['plan', '--max-risk', '2', '--max-risk', '3'], app_dir)
    assert result.returncode == ra.EXIT_USAGE
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_DUPLICATE_FLAG


def test_core_nonzero_with_json_passthrough_and_exit_code(app_dir):
    result = run_adapter(['report'], app_dir, {'STUB_CASE': 'exit_fail_json'})
    assert result.returncode == 3
    payload = assert_single_json(result.stdout)
    # Machine-readable отказ core проходит как есть, envelope не добавляется.
    assert payload['artifact'] == 'refusal'


def test_core_crash_typed_envelope_with_stderr_tail(app_dir):
    result = run_adapter(['report'], app_dir, {'STUB_CASE': 'exit_fail'})
    assert result.returncode == 1
    payload = assert_single_json(result.stdout)
    assert payload['artifact'] == 'adapter_error'
    error = payload['error']
    assert error['code'] == ra.ERR_COMMAND_FAILED
    assert error['exit_code'] == 1
    assert 'boom' in error['stderr_tail']
    assert 'boom' in result.stderr  # diagnostics и в stderr
    assert error['core_command'] == 'rebalance-report'


def test_non_json_stdout_typed_error(app_dir):
    result = run_adapter(['report'], app_dir, {'STUB_CASE': 'non_json'})
    assert result.returncode == ra.EXIT_NON_JSON
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_NON_JSON


def test_timeout_typed_error(app_dir):
    result = run_adapter(
        ['report'], app_dir,
        {'STUB_CASE': 'timeout', 'FIN_BONDS_REBALANCE_TIMEOUT_SEC': '1'},
    )
    assert result.returncode == ra.EXIT_TIMEOUT
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_TIMEOUT


def test_compare_variant_flags_reach_core_in_order(app_dir):
    # 030.9 приземлился параллельно: compare — фактический контракт
    # (--a-*/--b-* зеркала plan, общий --context-id = один PlanningContext).
    result = run_adapter([
        'compare',
        '--a-sell-all', ISIN_A,
        '--a-target-value', '%s=350000' % ISIN_B,
        '--b-freq-max', '12',
        '--b-exclude-sovereign',
        '--context-id', CONTEXT_ID,
    ], app_dir)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload['argv'] == [
        'rebalance-compare',
        '--a-sell-all', ISIN_A,
        '--a-target-value', '%s=350000' % ISIN_B,
        '--b-freq-max', '12',
        '--b-exclude-sovereign',
        '--context-id', CONTEXT_ID,
        '--format', 'json',
    ]


def test_compare_rejects_plain_plan_flags_and_bad_values(app_dir):
    # Флаги compare только в вариантной форме --a-*/--b-*; голый --sell-all
    # (форма plan) не в whitelist — typed-ошибка.
    result = run_adapter(['compare', '--sell-all', ISIN_A], app_dir)
    assert result.returncode == ra.EXIT_USAGE
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_UNKNOWN_FLAG

    result = run_adapter(['compare', '--a-freq-max', 'abc'], app_dir)
    assert result.returncode == ra.EXIT_USAGE
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_INVALID_ARGUMENT


def test_compare_parse_reuses_plan_validators_and_repeatables():
    parsed = ra.parse_argv([
        'compare',
        '--a-target-value', '%s=100' % ISIN_A,
        '--a-target-value', '%s=200' % ISIN_B,   # repeatable, порядок = priority
        '--b-max-position-value', '350000',
    ])
    assert parsed.core_tokens == [
        '--a-target-value', '%s=100' % ISIN_A,
        '--a-target-value', '%s=200' % ISIN_B,
        '--b-max-position-value', '350000',
        '--format', 'json',
    ]
    assert '--a-sell-all' in ra.COMPARE_FLAGS
    assert '--b-exclude-sovereign' in ra.COMPARE_FLAGS
    assert '--view' not in ra.COMPARE_FLAGS  # compare без view (контракт 030.9)


def test_compare_candidate_policy_mirrors_reach_core(app_dir):
    # 031.1: каждый candidate-policy флаг plan имеет зеркала --a-*/--b-*.
    result = run_adapter([
        'compare',
        '--a-max-risk', '3',
        '--a-include-ku',
        '--a-freq-in', '2,4',
        '--b-max-risk', '1',
        '--b-max-listlevel', '3',
        '--b-max-ytm', '18',
        '--b-min-maturity', '2026-01-01',
        '--b-max-maturity', '2030-06-30',
    ], app_dir)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload['argv'] == [
        'rebalance-compare',
        '--a-max-risk', '3',
        '--a-include-ku',
        '--a-freq-in', '2,4',
        '--b-max-risk', '1',
        '--b-max-listlevel', '3',
        '--b-max-ytm', '18',
        '--b-min-maturity', '2026-01-01',
        '--b-max-maturity', '2030-06-30',
        '--format', 'json',
    ]


def test_compare_candidate_policy_mirrors_reuse_plan_validators():
    # Валидаторы зеркал — те же объекты, что у plan (переиспользование,
    # без копий):форма проверяется одинаково на plan и в вариантах.
    assert ra.COMPARE_FLAGS['--a-max-risk'] == ra.PLAN_FLAGS['--max-risk']
    assert ra.COMPARE_FLAGS['--b-max-risk'] == ra.PLAN_FLAGS['--max-risk']
    assert ra.COMPARE_FLAGS['--a-freq-in'] == ra.PLAN_FLAGS['--freq-in']
    for flag in ('--a-max-listlevel', '--a-max-ytm', '--a-include-ku',
                 '--a-min-maturity', '--a-max-maturity', '--b-freq-in'):
        assert flag in ra.COMPARE_FLAGS


def test_compare_context_id_field_passthrough(app_dir):
    # 031.3: envelope compare несёт верхнеуровневый context_id; adapter —
    # прозрачный транспорт: поле доходит до LLM байт-в-байт (exit 0,
    # pass-through контракт), whitelist ничего не вырезает.
    result = run_adapter(
        ['compare', '--context-id', CONTEXT_ID], app_dir,
        {'STUB_CASE': 'compare_ctx'},
    )
    assert result.returncode == 0
    payload = assert_single_json(result.stdout)
    assert payload['artifact'] == 'compare'
    assert payload['context_id'] == CONTEXT_ID
    assert payload['comparable'] is True


# ---------------------------------------------------------------------------
# Snapshot как artifact (030.10/030.11)
# ---------------------------------------------------------------------------

def test_snapshot_full_payload_to_artifact_metadata_to_stdout(app_dir, tmp_path):
    artifact_dir = tmp_path / 'artifacts'
    result = run_adapter(
        ['analysis-snapshot', '--artifact-dir', str(artifact_dir)],
        app_dir, {'STUB_CASE': 'snapshot'},
    )
    assert result.returncode == 0
    envelope = assert_single_json(result.stdout)

    assert envelope['artifact'] == 'analysis_snapshot_handle'
    assert envelope['context']['context_id'] == CONTEXT_ID
    assert envelope['analysis']['status'] == 'OK'
    # LLM видит counts + preview, не весь universe/positions.
    assert envelope['universe']['row_count'] == 7
    assert len(envelope['universe']['rows_preview']) == 5
    assert 'rows' not in envelope['universe']
    assert len(envelope['portfolio']['positions_preview']) == 5
    assert 'positions' not in envelope['portfolio']

    handle = envelope['artifact_file']
    artifact_path = pathlib.Path(handle['path'])
    assert artifact_path.name == 'analysis_snapshot_%s.json' % CONTEXT_ID
    raw = artifact_path.read_bytes()
    assert handle['sha256'] == hashlib.sha256(raw).hexdigest()
    full_payload = json.loads(raw.decode('utf-8'))
    # Полный payload в артефакте — для sandbox, байт-в-байт контракт 030.10.
    assert full_payload['artifact'] == 'analysis_snapshot'
    assert len(full_payload['universe']['rows']) == 7
    # Приватность: файл 0600, каталог 0700.
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(artifact_dir.stat().st_mode) == 0o700


def test_snapshot_requires_accumulated_in_order(app_dir, tmp_path):
    # Разбор: повторения --requires склеиваются в один флаг в порядке
    # следования (порядок семантики core).
    parsed = ra.parse_argv([
        'analysis-snapshot',
        '--requires', 'ytm',
        '--requires', 'credit_rating,risk_list_level',
    ])
    assert parsed.core_tokens == [
        '--requires', 'ytm,credit_rating,risk_list_level',
        '--format', 'json',
    ]
    # E2E: валидированный --requires доходит до core и ответ обрабатывается.
    result = run_adapter(
        ['analysis-snapshot', '--requires', 'ytm,credit_rating'],
        app_dir, {'STUB_CASE': 'snapshot'},
    )
    assert result.returncode == 0
    envelope = assert_single_json(result.stdout)
    assert envelope['artifact'] == 'analysis_snapshot_handle'


def test_snapshot_invalid_dataset_code_rejected(app_dir):
    result = run_adapter(['analysis-snapshot', '--requires', 'bad;code'], app_dir)
    assert result.returncode == ra.EXIT_USAGE
    payload = assert_single_json(result.stdout)
    assert payload['error']['code'] == ra.ERR_INVALID_ARGUMENT
