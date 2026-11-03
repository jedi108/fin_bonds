#!/usr/bin/env python3
"""Синк скиллов профиля агента Hermes с каноническим манифестом fin_bonds (005.6).

Профиль агента (`/opt/data/profiles/fin_bonds/skills/`) наполняется отдельно
от канонического дерева skills_hermes и разъезжается с ним: часть SOP-скиллов
в профиле отсутствует, часть — устаревшие черновики. Скрипт приводит профиль
в соответствие манифесту `skills_manifest.yaml` и проверяет целостность.

Подкоманды:
  check — сверка профиля с манифестом (только чтение); перечисляет
          MISSING/STALE/RETIRED, exit 1 при любом расхождении, 0 — всё сходится.
  sync  — приводит профиль к манифесту: ставит отсутствующие скиллы,
          обновляет расхожие, удаляет retired. Идемпотентен: повторный
          запуск без изменений в каноническом дереве — no-op (SKIP).

Режимы доступа к профилю:
  --profile-dir DIR — локальный каталог скиллов профиля (тесты/отладка);
  без него — по сети: ssh на Hermes-хост + `docker exec` в контейнер
  (каталог профиля принадлежит uid контейнера, напрямую недоступен).

Источник канонического контента:
  локально — дерево skills_hermes рядом с манифестом (или --source-dir);
  по сети  — копия skills_hermes на хосте (canonical.host_source_dir),
  обновляемая rsync в приватном деплой-скрипте (политика: деплой по git,
  не ручными копиями).

Коды выхода: 0 — ок; 1 — расхождение (check); 2 — операционная ошибка.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

MANIFEST_NAME = 'skills_manifest.yaml'

# Статусы check'а.
OK, MISSING, STALE, RETIRED, ALLOWED, EXTRA, SOUL_MISS, SOURCE = (
    'OK', 'MISSING', 'STALE', 'RETIRED', 'ALLOWED', 'EXTRA', 'SOUL', 'SOURCE')
# Действия sync'а.
INSTALL, UPDATE, SKIP, REMOVE = 'INSTALL', 'UPDATE', 'SKIP', 'REMOVE'
LEAVE_ALLOWED, LEAVE_EXTRA = 'LEAVE_ALLOWED', 'LEAVE_EXTRA'

FINBONDS_REF_RE = re.compile(r'finbonds-[a-z0-9-]+')


# ---------------------------------------------------------------------------
# Манифест
# ---------------------------------------------------------------------------

@dataclass
class AllowedSkill:
    name: str
    note: str = ''


@dataclass
class RetiredSkill:
    name: str
    replaced_by: str = ''
    note: str = ''


@dataclass
class Manifest:
    path: pathlib.Path
    skills: List[str]
    source_dir: str
    host_source_dir: str
    host: str
    container: str
    skills_root: str
    category_dir: str
    allowed: List[AllowedSkill] = field(default_factory=list)
    retired: List[RetiredSkill] = field(default_factory=list)

    @property
    def local_source_root(self) -> pathlib.Path:
        """Каноническое дерево рядом с манифестом (skills_hermes/<source_dir>)."""
        return self.path.parent / self.source_dir

    @property
    def allowed_names(self) -> List[str]:
        return [a.name for a in self.allowed]

    @property
    def retired_names(self) -> List[str]:
        return [r.name for r in self.retired]


def _require(obj, key: str, ctx: str):
    if not isinstance(obj, dict) or key not in obj:
        raise ValueError(f'{ctx}: отсутствует ключ "{key}"')
    return obj[key]


def load_manifest(path: pathlib.Path) -> Manifest:
    if yaml is None:
        raise RuntimeError(
            'нужен PyYAML (pip install pyyaml) — используйте venv репозитория')
    raw = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    canonical = _require(raw, 'canonical', 'манифест')
    profile = _require(raw, 'profile', 'манифест')
    skills = [str(s) for s in _require(canonical, 'skills', 'canonical')]
    if not skills:
        raise ValueError('canonical.skills пуст')
    allowed = [AllowedSkill(name=str(a['name']), note=str(a.get('note', '')))
               for a in (raw.get('profile_only_allowed') or [])]
    retired = [RetiredSkill(name=str(r['name']),
                            replaced_by=str(r.get('replaced_by', '')),
                            note=str(r.get('note', '')))
               for r in (raw.get('retired') or [])]
    return Manifest(
        path=path.resolve(),
        skills=skills,
        source_dir=str(_require(canonical, 'source_dir', 'canonical')),
        host_source_dir=os.environ.get('HERMES_SKILLS_SRC') or str(_require(canonical, 'host_source_dir', 'canonical')),
        host=os.environ.get('HERMES_HOST') or str(_require(profile, 'host', 'profile')),
        container=str(_require(profile, 'container', 'profile')),
        skills_root=str(_require(profile, 'skills_root', 'profile')),
        category_dir=str(_require(profile, 'category_dir', 'profile')),
        allowed=allowed,
        retired=retired,
    )


# ---------------------------------------------------------------------------
# Frontmatter (минимально: name/version — достаточно для отчёта)
# ---------------------------------------------------------------------------

def read_frontmatter(text: str) -> Dict[str, str]:
    if not text.startswith('---'):
        return {}
    match = re.search(r'\n---\s*\n', text[3:])
    if not match:
        return {}
    data: Dict[str, str] = {}
    for line in text[3:match.start() + 3].splitlines():
        key, sep, value = line.partition(':')
        if sep and key == key.strip() and key and not line[0].isspace():
            data[key.strip()] = value.strip().strip('"\'')
    return data


def read_version(md_text: Optional[str]) -> str:
    if not md_text:
        return '?'
    return read_frontmatter(md_text).get('version') or '?'


# ---------------------------------------------------------------------------
# Доступ к каноническому дереву (источник)
# ---------------------------------------------------------------------------

class LocalSource:
    """Каноническое дерево в локальной ФС (тесты/отладка/локальный прогон)."""

    def __init__(self, root: pathlib.Path):
        self.root = root

    def describe(self) -> str:
        return str(self.root)

    def list_skills(self) -> List[str]:
        if not self.root.is_dir():
            raise FileNotFoundError(f'нет каталога {self.root}')
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and (p / 'SKILL.md').is_file())

    def hashes(self, name: str) -> Dict[str, str]:
        return _hash_dir_local(self.root / name)

    def read_skill_md(self, name: str) -> Optional[str]:
        p = self.root / name / 'SKILL.md'
        return p.read_text(encoding='utf-8') if p.is_file() else None


class RemoteSource:
    """Копия skills_hermes на Hermes-хосте (обновляется rsync)."""

    def __init__(self, host: str, root: str):
        self.host = host
        self.root = root.rstrip('/')

    def describe(self) -> str:
        return f'{self.host}:{self.root}'

    def list_skills(self) -> List[str]:
        q_root = shlex.quote(self.root)
        cmd = (f'for d in {q_root}/*/; do '
               f'test -f "${{d}}SKILL.md" && basename "${{d}}"; done')
        rc, out, err = _run(['ssh', self.host, cmd])
        if rc != 0:
            raise RuntimeError(f'нет доступа к {self.describe()}: '
                               f'{err.strip() or out.strip()}')
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    def hashes(self, name: str) -> Dict[str, str]:
        q_root, q_name = shlex.quote(self.root), shlex.quote(name)
        cmd = (f'cd {q_root} && find {q_name} -type f -print0 2>/dev/null | '
               f'xargs -0 -r sha256sum')
        rc, out, _ = _run(['ssh', self.host, cmd])
        if rc != 0:
            raise RuntimeError(f'не удалось посчитать хеши {name} на хосте')
        return _parse_sha256sum(out, prefix=name + '/')

    def read_skill_md(self, name: str) -> Optional[str]:
        rc, out, _ = _run(['ssh', self.host,
                           f'cat {shlex.quote(self.root + "/" + name + "/SKILL.md")}'])
        return out if rc == 0 else None


# ---------------------------------------------------------------------------
# Доступ к профилю
# ---------------------------------------------------------------------------

class LocalProfile:
    """Профиль в локальной ФС (--profile-dir; тесты/отладка)."""

    def __init__(self, skills_root: pathlib.Path, category_dir: str):
        self.skills_root = skills_root
        self.finance = skills_root / category_dir

    def describe(self) -> str:
        return str(self.finance)

    def list_skills(self) -> List[str]:
        if not self.finance.is_dir():
            return []
        return sorted(p.name for p in self.finance.iterdir() if p.is_dir())

    def hashes(self, name: str) -> Dict[str, str]:
        return _hash_dir_local(self.finance / name)

    def read_file(self, relpath: str) -> Optional[str]:
        p = self.finance / relpath
        return p.read_text(encoding='utf-8') if p.is_file() else None

    def write_tree(self, source: 'LocalSource', name: str) -> None:
        import shutil
        dest = self.finance / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source.root / name, dest)

    def remove_tree(self, name: str) -> None:
        import shutil
        dest = self.finance / name
        if dest.exists():
            shutil.rmtree(dest)

    def read_soul(self) -> Optional[str]:
        p = self.skills_root.parent / 'SOUL.md'
        return p.read_text(encoding='utf-8') if p.is_file() else None


class RemoteProfile:
    """Профиль в контейнере hermes-ai на Hermes-хосте (ssh + docker exec)."""

    def __init__(self, host: str, container: str, skills_root: str,
                 category_dir: str):
        self.host = host
        self.container = container
        self.skills_root = skills_root.rstrip('/')
        self.finance = f'{self.skills_root}/{category_dir}'

    def describe(self) -> str:
        return f'{self.host}:{self.container}:{self.finance}'

    def _exec(self, inner: str) -> Tuple[int, str, str]:
        cmd = (f'docker exec {shlex.quote(self.container)} '
               f'sh -c {shlex.quote(inner)}')
        return _run(['ssh', self.host, cmd])

    def list_skills(self) -> List[str]:
        rc, out, _ = self._exec(f'ls -1 {shlex.quote(self.finance)} 2>/dev/null')
        if rc != 0:
            return []
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    def hashes(self, name: str) -> Dict[str, str]:
        q = shlex.quote(self.finance)
        inner = (f'cd {q} 2>/dev/null && find {shlex.quote(name)} -type f '
                 f'-print0 2>/dev/null | xargs -0 -r sha256sum')
        rc, out, _ = self._exec(inner)
        if rc != 0:
            return {}
        return _parse_sha256sum(out, prefix=name + '/')

    def read_file(self, relpath: str) -> Optional[str]:
        rc, out, _ = self._exec(
            f'cat {shlex.quote(self.finance + "/" + relpath)} 2>/dev/null')
        return out if rc == 0 else None

    def write_tree(self, source: 'RemoteSource', name: str) -> None:
        """Копия скилла с хоста (копия skills_hermes) в контейнер через tar.

        tar читает файлы на хосте и по stdin отдаёт их docker exec —
        одна ssh-сессия; права владельца выравниваются по каталогу профиля
        (в контейнере это uid/gid пользователя hermes, не root).
        """
        q_src, q_fin = shlex.quote(source.root), shlex.quote(self.finance)
        # Каталог назначения создаётся ВНУТРИ контейнера (путь контейнера
        # может не существовать на хосте).
        rc, _, err = self._exec(f'mkdir -p {q_fin}')
        if rc != 0:
            raise RuntimeError(f'mkdir -p {self.finance} в контейнере: {err.strip()}')
        stream = (f'cd {q_src} && tar -cf - {shlex.quote(name)} | '
                  f'docker exec -i {shlex.quote(self.container)} '
                  f'tar -xf - -C {q_fin}')
        rc, _, err = _run(['ssh', self.host, stream])
        if rc != 0:
            raise RuntimeError(f'tar-копия {name} в контейнер: {err.strip()}')
        # Владелец — как у профиля (uid/gid пользователя hermes в контейнере),
        # не root: эталон — корень скиллов профиля, иначе каталог finance.
        q_root = shlex.quote(self.skills_root)
        inner = (f'owner="$(stat -c %u:%g {q_root} 2>/dev/null '
                 f'|| stat -c %u:%g {q_fin})"; '
                 f'chown -R "$owner" '
                 f'{shlex.quote(self.finance + "/" + name)}')
        rc, _, err = self._exec(inner)
        if rc != 0:
            raise RuntimeError(f'chown {name}: {err.strip()}')

    def remove_tree(self, name: str) -> None:
        rc, _, err = self._exec(
            f'rm -rf -- {shlex.quote(self.finance + "/" + name)}')
        if rc != 0:
            raise RuntimeError(f'rm -rf {name}: {err.strip()}')

    def read_soul(self) -> Optional[str]:
        rc, out, _ = self._exec(
            f'cat {shlex.quote(self.skills_root + "/../SOUL.md")} 2>/dev/null')
        return out if rc == 0 else None


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _run(cmd: List[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _hash_dir_local(path: pathlib.Path) -> Dict[str, str]:
    if not path.is_dir():
        return {}
    out: Dict[str, str] = {}
    for p in sorted(path.rglob('*')):
        if p.is_file() and not p.is_symlink():
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            out[str(p.relative_to(path))] = digest
    return out


def _parse_sha256sum(text: str, prefix: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        digest, _, path = line.partition('  ')
        if len(digest) == 64 and path.startswith(prefix):
            rel = path[len(prefix):]
            if rel:  # '-' (пустой stdin) не релевантен
                out[rel] = digest
    return out


# ---------------------------------------------------------------------------
# Чистая логика: план синка и сверка (тестируется без IO)
# ---------------------------------------------------------------------------

@dataclass
class Action:
    kind: str      # INSTALL / UPDATE / SKIP / REMOVE / LEAVE_ALLOWED / LEAVE_EXTRA
    name: str
    detail: str = ''


@dataclass
class CheckRow:
    status: str    # OK / MISSING / STALE / RETIRED / ALLOWED / EXTRA / SOUL / SOURCE
    name: str
    detail: str = ''
    fatal: bool = False


def build_plan(manifest: Manifest,
               source_hashes: Dict[str, Dict[str, str]],
               profile_hashes: Dict[str, Dict[str, str]],
               profile_names: List[str]) -> List[Action]:
    """Что нужно сделать, чтобы профиль соответствовал манифесту.

    Идемпотентность: совпадающее содержимое (набор файлов + sha256) даёт SKIP,
    т.е. повторный запуск ничего не пишет и не дублирует.
    """
    actions: List[Action] = []
    profile_set = set(profile_names)
    for name in manifest.skills:
        src = source_hashes.get(name)
        prof = profile_hashes.get(name)
        if not src:
            continue  # разнобой дерево<->манифест: репортит check_tree_vs_manifest
        if not prof:
            actions.append(Action(INSTALL, name, 'в профиле нет'))
            continue
        if prof == src:
            actions.append(Action(SKIP, name, 'без изменений'))
        else:
            only_src = sorted(set(src) - set(prof))
            only_prof = sorted(set(prof) - set(src))
            changed = sorted(k for k in set(src) & set(prof)
                             if src[k] != prof[k])
            actions.append(Action(UPDATE, name,
                                  _diff_detail(only_src, only_prof, changed)))
    for name in manifest.retired_names:
        if name in profile_set:
            actions.append(Action(REMOVE, name, 'retired: удаление из профиля'))
    for name in sorted(profile_set - set(manifest.skills)
                       - set(manifest.retired_names)):
        if name in manifest.allowed_names:
            note = next(a.note for a in manifest.allowed if a.name == name)
            actions.append(Action(LEAVE_ALLOWED, name, note))
        else:
            actions.append(Action(LEAVE_EXTRA, name,
                                  'нет в манифесте — синк не трогает'))
    return actions


def _diff_detail(only_src: List[str], only_prof: List[str],
                 changed: List[str]) -> str:
    parts = []
    if changed:
        parts.append('изменены: ' + ', '.join(changed[:5]) +
                     ('…' if len(changed) > 5 else ''))
    if only_src:
        parts.append('новые: ' + ', '.join(only_src[:5]))
    if only_prof:
        parts.append('лишние в профиле: ' + ', '.join(only_prof[:5]))
    return '; '.join(parts) if parts else 'содержимое различается'


def build_check(manifest: Manifest,
                source_hashes: Dict[str, Dict[str, str]],
                profile_hashes: Dict[str, Dict[str, str]],
                profile_names: List[str],
                soul_text: Optional[str]) -> List[CheckRow]:
    rows: List[CheckRow] = []
    profile_set = set(profile_names)
    for name in manifest.skills:
        src = source_hashes.get(name) or {}
        prof = profile_hashes.get(name) or {}
        if not src:
            rows.append(CheckRow(SOURCE, name,
                                 'нет в каноническом дереве — обновите манифест '
                                 'или дерево', fatal=True))
            continue
        if not prof:
            rows.append(CheckRow(MISSING, name, 'в профиле нет', fatal=True))
            continue
        if prof == src:
            rows.append(CheckRow(OK, name, 'совпадает с деревом'))
        else:
            rows.append(CheckRow(STALE, name,
                                 'содержимое отличается от канонического дерева',
                                 fatal=True))
    for r in manifest.retired:
        if r.name in profile_set:
            replaced = f' (заменён на {r.replaced_by})' if r.replaced_by else ''
            rows.append(CheckRow(RETIRED, r.name,
                                 f'в профиле, манифест требует удаления{replaced}',
                                 fatal=True))
    for a in manifest.allowed:
        if a.name in profile_set:
            rows.append(CheckRow(ALLOWED, a.name,
                                 a.note or 'черновик профиля (не канонический)'))
    for name in sorted(profile_set - set(manifest.skills)
                       - set(manifest.retired_names)
                       - set(manifest.allowed_names)):
        rows.append(CheckRow(EXTRA, name,
                             'есть в профиле, нет в манифесте '
                             '(ни canonical, ни allowed, ни retired)'))
    if soul_text is not None:
        known = set(manifest.skills) | set(manifest.allowed_names) \
            | set(manifest.retired_names)
        for ref in sorted(set(FINBONDS_REF_RE.findall(soul_text))):
            if ref not in known:
                rows.append(CheckRow(SOUL_MISS, ref,
                                     'упомянут в SOUL.md, но отсутствует '
                                     'в манифесте и профиле'))
    return rows


def check_tree_vs_manifest(manifest: Manifest, source_names: List[str]) -> List[str]:
    """Дерево skills_hermes и манифест должны описывать один набор."""
    errors = []
    tree = set(source_names)
    for name in sorted(tree - set(manifest.skills)):
        errors.append(f'скилл "{name}" есть в дереве {manifest.source_dir}/, '
                      f'но отсутствует в манифесте {manifest.path.name}')
    for name in sorted(set(manifest.skills) - tree):
        errors.append(f'скилл "{name}" есть в манифесте, но нет каталога '
                      f'{manifest.source_dir}/{name}/SKILL.md')
    return errors


# ---------------------------------------------------------------------------
# Рендеринг
# ---------------------------------------------------------------------------

def render_check(manifest: Manifest, source, profile, rows: List[CheckRow],
                 tree_errors: List[str]) -> str:
    lines = [
        f'Манифест: {manifest.path} ({len(manifest.skills)} канонических скиллов)',
        f'Источник: {source.describe()}',
        f'Профиль:  {profile.describe()}',
        '',
    ]
    for e in tree_errors:
        lines.append(f'TREE-FAIL {e}')
    for r in rows:
        lines.append(f'{r.status:<9} {r.name}' + (f' — {r.detail}' if r.detail else ''))
    lines.append('')
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    canonical_rows = [r for r in rows if r.status in (OK, MISSING, STALE, SOURCE)]
    ok = sum(1 for r in canonical_rows if r.status == OK)
    drift = {k: v for k, v in sorted(counts.items()) if k not in (OK, ALLOWED)}
    summary = ', '.join(f'{k} {v}' for k, v in drift.items()) if drift \
        else 'нет'
    lines.append(f'Итого: {ok}/{len(manifest.skills)} канонических скиллов '
                 f'совпадают с деревом; расхождений: '
                 f'{sum(1 for r in rows if r.fatal) + len(tree_errors)} ({summary})')
    return '\n'.join(lines)


def render_plan(actions: List[Action], dry_run: bool) -> str:
    lines = []
    for a in actions:
        lines.append(f'{a.kind:<14} {a.name}' + (f' — {a.detail}' if a.detail else ''))
    counts: Dict[str, int] = {}
    for a in actions:
        counts[a.kind] = counts.get(a.kind, 0) + 1
    summary = ', '.join(f'{k} {v}' for k, v in sorted(counts.items())) \
        or 'профиль уже соответствует манифесту'
    lines.append(f'Итого: {summary}')
    if dry_run:
        lines.append('DRY-RUN: ничего не записано (повторите без --dry-run)')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _pick(args, manifest: Manifest):
    """Собрать пару (источник, профиль) по режиму запуска."""
    if args.profile_dir:
        source_root = pathlib.Path(args.source_dir).resolve() if args.source_dir \
            else manifest.local_source_root
        source = LocalSource(source_root)
        profile = LocalProfile(pathlib.Path(args.profile_dir).resolve(),
                               manifest.category_dir)
    else:
        if args.source_dir:
            raise SystemExit('--source-dir допустим только вместе с --profile-dir '
                             '(по сети источник — копия skills_hermes на хосте)')
        source = RemoteSource(manifest.host,
                              manifest.host_source_dir + '/' + manifest.source_dir)
        profile = RemoteProfile(manifest.host, manifest.container,
                                manifest.skills_root, manifest.category_dir)
    return source, profile


def _collect(source, profile, manifest: Manifest):
    source_names = source.list_skills()
    tree_errors = check_tree_vs_manifest(manifest, source_names)
    source_hashes = {n: source.hashes(n) for n in manifest.skills}
    profile_names = profile.list_skills()
    profile_hashes = {n: profile.hashes(n) for n in
                      set(manifest.skills) | set(manifest.retired_names)}
    return source_names, tree_errors, source_hashes, profile_names, profile_hashes


def _enrich_versions(rows: List[CheckRow], source, profile) -> None:
    """Подставить версии из SKILL.md обеих сторон в MISSING/STALE/OK."""
    for r in rows:
        if r.status not in (OK, MISSING, STALE):
            continue
        src_ver = read_version(source.read_skill_md(r.name))
        prof_ver = read_version(profile.read_file(f'{r.name}/SKILL.md'))
        if r.status == OK:
            r.detail = f'v{src_ver} совпадает с деревом'
        elif r.status == MISSING:
            r.detail = f'в профиле нет (в дереве v{src_ver})'
        else:
            r.detail = f'профиль v{prof_ver} ≠ дерево v{src_ver}'


def cmd_check(args, manifest: Manifest) -> int:
    source, profile = _pick(args, manifest)
    _, tree_errors, source_hashes, profile_names, profile_hashes = \
        _collect(source, profile, manifest)
    soul = None if args.no_soul_check else profile.read_soul()
    rows = build_check(manifest, source_hashes, profile_hashes,
                       profile_names, soul)
    _enrich_versions(rows, source, profile)
    print(render_check(manifest, source, profile, rows, tree_errors))
    fatal = sum(1 for r in rows if r.fatal) + len(tree_errors)
    return 1 if fatal else 0


def cmd_sync(args, manifest: Manifest) -> int:
    source, profile = _pick(args, manifest)
    _, tree_errors, source_hashes, profile_names, profile_hashes = \
        _collect(source, profile, manifest)
    if tree_errors:
        for e in tree_errors:
            print(f'TREE-FAIL {e}', file=sys.stderr)
        print('Синк прерван: сначала приведите манифест и дерево в соответствие.',
              file=sys.stderr)
        return 2
    actions = build_plan(manifest, source_hashes, profile_hashes, profile_names)
    if not args.dry_run:
        for a in actions:
            if a.kind in (INSTALL, UPDATE):
                profile.write_tree(source, a.name)
            elif a.kind == REMOVE:
                profile.remove_tree(a.name)
    print(render_plan(actions, args.dry_run))
    # Самопроверка: после реальной записи расхождений быть не должно.
    if not args.dry_run and any(a.kind in (INSTALL, UPDATE, REMOVE)
                                for a in actions):
        _, _, src_h, prof_n, prof_h = _collect(source, profile, manifest)
        fatal = [r for r in build_check(manifest, src_h, prof_h, prof_n, None)
                 if r.fatal]
        if fatal:
            print('ОШИБКА: после синка остались расхождения:', file=sys.stderr)
            for r in fatal:
                print(f'{r.status} {r.name} {r.detail}', file=sys.stderr)
            return 2
        print('Самопроверка после синка: расхождений нет.')
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Синк скиллов профиля агента Hermes с манифестом fin_bonds')
    parser.add_argument('--manifest', default=None,
                        help=f'путь к манифесту (по умолчанию {MANIFEST_NAME} '
                             f'рядом со скриптом)')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('check', 'sync'):
        p = sub.add_parser(name)
        p.add_argument('--profile-dir', default=None,
                       help='локальный каталог skills-профиля (иначе — '
                            'ssh+docker по манифесту)')
        p.add_argument('--source-dir', default=None,
                       help='локальный канонический каталог '
                            '(по умолчанию — skills_hermes/finance рядом с '
                            'манифестом; только с --profile-dir)')
        p.add_argument('--dry-run', action='store_true',
                       help='(sync) только показать план')
        p.add_argument('--no-soul-check', action='store_true',
                       help='не сверять упоминания finbonds-* в SOUL.md')
    args = parser.parse_args(argv)
    script_dir = pathlib.Path(__file__).resolve().parent
    manifest_path = pathlib.Path(args.manifest) if args.manifest \
        else script_dir.parent / MANIFEST_NAME
    try:
        manifest = load_manifest(manifest_path)
    except Exception as e:
        print(f'ОШИБКА: манифест {manifest_path}: {e}', file=sys.stderr)
        return 2
    try:
        if args.command == 'check':
            return cmd_check(args, manifest)
        return cmd_sync(args, manifest)
    except (RuntimeError, FileNotFoundError) as e:
        print(f'ОШИБКА: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
