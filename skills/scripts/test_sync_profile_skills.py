#!/usr/bin/env python3
"""Тесты sync_profile_skills.py (005.6): план синка, сверка, идемпотентность.

Запуск: ./venv/bin/python -m pytest skills/scripts/test_sync_profile_skills.py -v
Только локальный режим (--profile-dir) — без сети и docker.
"""
import pathlib
import sys
from typing import Dict, List, Optional

import pytest
import yaml

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import sync_profile_skills as sps  # noqa: E402

SKILLS_HERMES = SCRIPTS_DIR.parent


# ---------------------------------------------------------------------------
# Фикстуры: фейковое каноническое дерево и профиль
# ---------------------------------------------------------------------------

def make_skill(source_root: pathlib.Path, name: str, version: str = '0.1.0',
               body: str = 'Привет', extra_files: Optional[Dict[str, str]] = None):
    skill = source_root / name
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: "Тестовый скил {name}."\n'
        f'version: {version}\nauthor: test\nlicense: MIT\n'
        f'platforms: [linux]\n---\n\n{body}\n', encoding='utf-8')
    for rel, content in (extra_files or {}).items():
        p = skill / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
    return skill


@pytest.fixture
def env(tmp_path: pathlib.Path):
    """(manifest, source_root, profile_root) c 3 каноническими скиллами."""
    source_root = tmp_path / 'finance'
    source_root.mkdir()
    for name in ('alpha-skill', 'beta-skill', 'gamma-skill'):
        make_skill(source_root, name)
    profile_root = tmp_path / 'profile' / 'skills'
    (profile_root / 'finance').mkdir(parents=True)
    manifest_path = tmp_path / 'skills_manifest.yaml'
    manifest_path.write_text(yaml.safe_dump({
        'manifest_version': 1,
        'canonical': {'source_dir': 'finance',
                      'host_source_dir': '/nonexistent',
                      'skills': ['alpha-skill', 'beta-skill', 'gamma-skill']},
        'profile': {'host': 'test@host', 'container': 'test-ai',
                    'skills_root': str(profile_root),
                    'category_dir': 'finance'},
        'profile_only_allowed': [{'name': 'draft-skill', 'note': 'черновик'}],
        'retired': [{'name': 'old-skill', 'replaced_by': 'alpha-skill',
                     'note': 'устарел'}],
    }), encoding='utf-8')
    return sps.load_manifest(manifest_path), source_root, profile_root


def collect(env_tuple):
    manifest, source_root, profile_root = env_tuple
    source = sps.LocalSource(source_root)
    profile = sps.LocalProfile(profile_root, manifest.category_dir)
    source_hashes = {n: source.hashes(n) for n in manifest.skills}
    profile_names = profile.list_skills()
    profile_hashes = {n: profile.hashes(n) for n in
                      set(manifest.skills) | set(manifest.retired_names)}
    return source, profile, source_hashes, profile_names, profile_hashes


# ---------------------------------------------------------------------------
# Манифест
# ---------------------------------------------------------------------------

def test_load_manifest_real():
    manifest = sps.load_manifest(SKILLS_HERMES / 'skills_manifest.yaml')
    # 030.11: счётчик был stale (9) — фактически 12 ещё до задачи;
    # finbonds-rebalance-tool (stable adapter) добавлен в канонический набор.
    assert len(manifest.skills) == 14
    assert 'finbonds-ops' in manifest.skills
    assert 'finbonds-rebalance-advisor' in manifest.skills
    assert 'finbonds-rebalance-tool' in manifest.skills
    assert 'finbonds-buy-candidates' in manifest.skills
    assert manifest.source_dir == 'finance'
    assert manifest.category_dir == 'finance'
    assert manifest.container
    allowed = set(manifest.allowed_names)
    # черновики 003/004 и устаревший скрининг не каноничны и не удаляются синком
    # (finbonds-ratings каноничен давно — из набора profile_only он ушёл)
    assert {'finbonds-broker-reconcile', 'finbonds-web-verify',
            'finbonds-buy-screening'} == allowed


def test_real_tree_matches_manifest():
    """Каноническое дерево skills/finance и манифест согласованы."""
    manifest = sps.load_manifest(SKILLS_HERMES / 'skills_manifest.yaml')
    source = sps.LocalSource(manifest.local_source_root)
    assert sps.check_tree_vs_manifest(manifest, source.list_skills()) == []


def test_frontmatter_version():
    text = '---\nname: x\nversion: 1.2.3\n---\nbody'
    assert sps.read_version(text) == '1.2.3'
    assert sps.read_version(None) == '?'


# ---------------------------------------------------------------------------
# План синка (чистая логика)
# ---------------------------------------------------------------------------

def test_plan_all_install_when_profile_empty(env):
    manifest, _, _ = env
    source, _, src_h, prof_n, prof_h = collect(env)
    plan = sps.build_plan(manifest, src_h, prof_h, prof_n)
    assert [a.kind for a in plan if a.name in manifest.skills] == \
        [sps.INSTALL] * 3


def test_plan_skip_when_identical(env):
    """Идемпотентность: совпадающий профиль даёт только SKIP."""
    manifest, _, _ = env
    source, profile, src_h, prof_n, prof_h = collect(env)
    for a in sps.build_plan(manifest, src_h, prof_h, prof_n):
        if a.kind == sps.INSTALL:
            profile.write_tree(source, a.name)
    _, _, src_h2, prof_n2, prof_h2 = collect(env)
    plan = sps.build_plan(manifest, src_h2, prof_h2, prof_n2)
    kinds = {a.name: a.kind for a in plan}
    assert all(kinds[n] == sps.SKIP for n in manifest.skills)


def test_plan_update_when_profile_stale(env):
    """Изменённый и лишний файл в профиле -> UPDATE (не дубль, а замена)."""
    manifest, source_root, profile_root = env
    source, profile, src_h, prof_n, prof_h = collect(env)
    for a in sps.build_plan(manifest, src_h, prof_h, prof_n):
        profile.write_tree(source, a.name)
    # правим профильный SKILL.md и добавляем лишний файл
    md = profile_root / 'finance' / 'alpha-skill' / 'SKILL.md'
    md.write_text(md.read_text(encoding='utf-8').replace('0.1.0', '9.9.9'),
                  encoding='utf-8')
    (profile_root / 'finance' / 'beta-skill' / 'stale.sql').write_text('-- old',
                                                                       encoding='utf-8')
    _, _, src_h2, prof_n2, prof_h2 = collect(env)
    plan = {a.name: a for a in sps.build_plan(manifest, src_h2, prof_h2, prof_n2)}
    assert plan['alpha-skill'].kind == sps.UPDATE
    assert plan['beta-skill'].kind == sps.UPDATE
    assert 'stale.sql' in plan['beta-skill'].detail
    assert plan['gamma-skill'].kind == sps.SKIP


def test_plan_remove_retired_only(env):
    """retired удаляется; allowed и посторонние синк не трогает."""
    manifest, _, profile_root = env
    # retired-скилл, разрешённый черновик и посторонний — в профиле
    make_skill(profile_root / 'finance', 'old-skill')
    make_skill(profile_root / 'finance', 'draft-skill')
    make_skill(profile_root / 'finance', 'mystery-skill')
    _, _, src_h, prof_n, prof_h = collect(env)
    plan = {a.name: a for a in sps.build_plan(manifest, src_h, prof_h, prof_n)}
    assert plan['old-skill'].kind == sps.REMOVE
    assert plan['draft-skill'].kind == sps.LEAVE_ALLOWED
    assert plan['mystery-skill'].kind == sps.LEAVE_EXTRA


# ---------------------------------------------------------------------------
# Сверка (check)
# ---------------------------------------------------------------------------

def test_check_rows_missing_and_ok(env):
    manifest, _, _ = env
    source, profile, src_h, prof_n, prof_h = collect(env)
    profile.write_tree(source, 'alpha-skill')  # только один скилл поставлен
    _, _, src_h2, prof_n2, prof_h2 = collect(env)
    rows = sps.build_check(manifest, src_h2, prof_h2, prof_n2, None)
    by = {r.name: r for r in rows}
    assert by['alpha-skill'].status == sps.OK
    assert by['beta-skill'].status == sps.MISSING and by['beta-skill'].fatal
    assert by['gamma-skill'].status == sps.MISSING and by['gamma-skill'].fatal


def test_check_soul_reference_reported_non_fatal(env):
    """SOUL.md, ссылающийся на несуществующий finbonds-скилл, — репорт без падения."""
    manifest, _, profile_root = env
    source, profile, src_h, prof_n, prof_h = collect(env)
    for a in sps.build_plan(manifest, src_h, prof_h, prof_n):
        profile.write_tree(source, a.name)
    (profile_root.parent / 'SOUL.md').write_text(
        'Работай скиллами finbonds-ops-ghost и alpha-skill.\n', encoding='utf-8')
    _, _, src_h2, prof_n2, prof_h2 = collect(env)
    rows = sps.build_check(manifest, src_h2, prof_h2, prof_n2, 'finbonds-ops-ghost')
    soul = [r for r in rows if r.status == sps.SOUL_MISS]
    assert len(soul) == 1 and soul[0].name == 'finbonds-ops-ghost'
    assert not soul[0].fatal


def test_check_tree_vs_manifest_both_ways(env):
    manifest, _, _ = env
    errs = sps.check_tree_vs_manifest(manifest, ['alpha-skill', 'beta-skill',
                                                 'gamma-skill', 'delta-skill'])
    assert len(errs) == 1 and 'delta-skill' in errs[0]
    errs = sps.check_tree_vs_manifest(manifest, ['alpha-skill'])
    assert len(errs) == 2  # beta и gamma в манифесте, но не в дереве


# ---------------------------------------------------------------------------
# E2E локального синка через main() (argv, коды выхода, stdout)
# ---------------------------------------------------------------------------

def run_cli(capsys, manifest_path, *extra):
    code = sps.main(['--manifest', str(manifest_path), *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def rows_of(out: str, status: str) -> int:
    """Число строк-статусов (не считая строки «Итого: … (STATUS n)»)."""
    return sum(1 for line in out.splitlines() if line.startswith(status + ' '))


def test_e2e_sync_is_idempotent_and_self_verifying(env, capsys):
    manifest, source_root, profile_root = env
    manifest_path = manifest.path
    args = ('sync', '--profile-dir', str(profile_root), '--no-soul-check')

    code, out, err = run_cli(capsys, manifest_path, *args)
    assert code == 0, err
    assert rows_of(out, 'INSTALL') == 3
    assert 'Самопроверка после синка: расхождений нет.' in out
    # контент действительно скопирован
    copied = (profile_root / 'finance' / 'alpha-skill' / 'SKILL.md').read_text(
        encoding='utf-8')
    assert 'name: alpha-skill' in copied

    # повторный запуск — no-op: только SKIP
    code, out, err = run_cli(capsys, manifest_path, *args)
    assert code == 0, err
    assert rows_of(out, 'INSTALL') == 0 and rows_of(out, 'UPDATE') == 0
    assert rows_of(out, 'SKIP') == 3

    # check после синка — 0 расхождений
    code, out, err = run_cli(capsys, manifest_path, 'check',
                             '--profile-dir', str(profile_root), '--no-soul-check')
    assert code == 0, err
    assert 'Итого: 3/3' in out


def test_e2e_check_detects_drift_and_sync_repairs(env, capsys):
    manifest, source_root, profile_root = env
    manifest_path = manifest.path
    check_args = ('check', '--profile-dir', str(profile_root), '--no-soul-check')

    # пустой профиль -> check падает с перечнем недостающих
    code, out, err = run_cli(capsys, manifest_path, *check_args)
    assert code == 1
    assert rows_of(out, 'MISSING') == 3  # перечисляет, а не молчит

    # синк чинит; затем порча профиля снова детектируется как STALE
    run_cli(capsys, manifest_path, 'sync', '--profile-dir', str(profile_root),
            '--no-soul-check')
    md = profile_root / 'finance' / 'beta-skill' / 'SKILL.md'
    md.write_text(md.read_text(encoding='utf-8') + '\n<!-- tampered -->\n',
                  encoding='utf-8')
    code, out, err = run_cli(capsys, manifest_path, *check_args)
    assert code == 1
    assert any(l.startswith('STALE') and 'beta-skill' in l
               for l in out.splitlines())
    assert 'Итого: 2/3' in out

    # повторный синк перезаписывает (не дублирует) и снова зелёный
    code, out, err = run_cli(capsys, manifest_path, 'sync',
                             '--profile-dir', str(profile_root), '--no-soul-check')
    assert code == 0, err
    assert rows_of(out, 'UPDATE') == 1
    files = sorted(p.name for p in
                   (profile_root / 'finance' / 'beta-skill').iterdir())
    assert files == ['SKILL.md']  # дублей нет
    code, out, err = run_cli(capsys, manifest_path, *check_args)
    assert code == 0


def test_e2e_sync_removes_retired_and_keeps_drafts(env, capsys):
    manifest, source_root, profile_root = env
    manifest_path = manifest.path
    fin = profile_root / 'finance'
    make_skill(fin, 'old-skill')
    make_skill(fin, 'draft-skill')
    make_skill(fin, 'mystery-skill')
    run_cli(capsys, manifest_path, 'sync', '--profile-dir', str(profile_root),
            '--no-soul-check')
    assert not (fin / 'old-skill').exists()          # retired удалён
    assert (fin / 'draft-skill').exists()            # allowed не тронут
    assert (fin / 'mystery-skill').exists()          # посторонний не тронут


def test_e2e_sync_aborts_on_manifest_tree_mismatch(env, capsys):
    manifest, _, profile_root = env
    manifest_path = manifest.path
    data = yaml.safe_load(manifest_path.read_text(encoding='utf-8'))
    data['canonical']['skills'].append('phantom-skill')  # нет в дереве
    manifest_path.write_text(yaml.safe_dump(data), encoding='utf-8')
    code, out, err = run_cli(capsys, manifest_path, 'sync',
                             '--profile-dir', str(profile_root), '--no-soul-check')
    assert code == 2
    assert 'phantom-skill' in err


def test_e2e_check_tree_mismatch_fails_check(env, capsys):
    manifest, _, profile_root = env
    manifest_path = manifest.path
    # лишний каталог с SKILL.md в каноническом дереве, не внесённый в манифест
    make_skill(manifest.local_source_root, 'unlisted-skill')
    code, out, err = run_cli(capsys, manifest_path, 'check',
                             '--profile-dir', str(profile_root), '--no-soul-check')
    assert code == 1
    assert 'unlisted-skill' in out
