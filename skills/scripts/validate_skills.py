#!/usr/bin/env python3
"""Валидация скиллов HERMES (fin_bonds).

Проверяет каждый SKILL.md в дереве skills:
- frontmatter: обязательные ключи name/description/version/author/license/
  platforms, description не длиннее 60 символов и заканчивается точкой;
- все scripts/*.sql скилла: файл не пуст, psql-блоки \\if/\\endif сбалансированы,
  скобки сбалансированы (после вырезания комментариев и строковых литералов);
- все scripts/*.sql, упомянутые в SKILL.md, существуют на диске в дереве
  скиллов (ссылки вида `scripts/<file>.sql` и полные in-container пути);
- doc-drift guard (015, предупреждение): имена таблиц в SQL-упоминаниях
  SKILL.md (позиции FROM/JOIN/UPDATE/INTO) сверяются с фиксированным
  списком таблиц/вью из миграций 001-017 (+ служебная schema_migrations).
  Это НЕ SQL-линтер: колонки и прочие упоминания не проверяются; находка —
  WARN в stderr, на exit code не влияет. Канон карты схемы — скилл
  finbonds-sql-recipes.

Выход с ненулевым кодом при любой ошибке. Используется в make validate-skills
(основной репозиторий) и в pre-commit hook репозитория.
"""
import pathlib
import re
import sys

MAX_DESCRIPTION = 60
REQUIRED_KEYS = ('name', 'description', 'version', 'author', 'license', 'platforms')
SQL_REF_RE = re.compile(r'scripts/[A-Za-z0-9_.-]+\.sql')

# Таблицы/вью из миграций migrations/postgres/001-017 + служебная
# schema_migrations (журнал ранера). Канон карты — скилл
# finbonds-sql-recipes («Карта схемы», сверена с живой БД).
KNOWN_TABLES = frozenset({
    'bonds_catalog',
    'portfolio_positions',
    'calculated_coupons',
    'risk_history',
    'monitoring_checks',
    'rating_history',
    'listlevel_history',
    'liquidity_history',
    'companies',
    'company_debt_snapshots',
    'schema_migrations',
    'v_portfolio_positions_valuation',
})
SQL_TABLE_REF_RE = re.compile(
    r'\b(?:FROM|JOIN|UPDATE|INTO)\s+([a-z_][a-z0-9_.]*)', re.IGNORECASE)


def parse_frontmatter(text: str) -> dict:
    if not text.startswith('---'):
        raise ValueError('файл должен начинаться с "---" (frontmatter)')
    match = re.search(r'\n---\s*\n', text[3:])
    if not match:
        raise ValueError('нет закрывающего "---" у frontmatter')
    raw = text[3:match.start() + 3]
    try:
        import yaml
        data = yaml.safe_load(raw) or {}
    except ImportError:
        # Fallback без PyYAML: достаточно верхнеуровневых ключей.
        data = {}
        for line in raw.splitlines():
            if not line or line[0].isspace():
                continue
            key, _, value = line.partition(':')
            data[key.strip()] = value.strip().strip('"\'')
    if not isinstance(data, dict):
        raise ValueError('frontmatter не является отображением ключ: значение')
    return data


def strip_sql_literals(sql: str) -> str:
    """Вырезает строковые литералы и комментарии — остаётся только код."""
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":  # экранирование ''
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif sql.startswith('--', i):
            j = sql.find('\n', i)
            i = n if j == -1 else j
        elif sql.startswith('/*', i):
            j = sql.find('*/', i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


def validate_sql(path: pathlib.Path) -> list:
    errors = []
    try:
        sql = path.read_text(encoding='utf-8')
    except Exception as e:
        return [f'не читается: {e}']
    if not sql.strip():
        return ['файл пуст']
    depth_if = 0
    for line in sql.splitlines():
        for token in line.strip().lower().split():
            if token == '\\if':
                depth_if += 1
            elif token == '\\endif':
                depth_if -= 1
                if depth_if < 0:
                    errors.append('\\endif без \\if')
                    depth_if = 0
    if depth_if:
        errors.append(f'незакрытые \\if-блоки ({depth_if})')
    code = strip_sql_literals(sql)
    if code.count('(') != code.count(')'):
        errors.append(f'несбалансированные скобки '
                      f'({code.count("(")}/{code.count(")")})')
    return errors


def validate(path: pathlib.Path, root: pathlib.Path, sql_index: dict) -> tuple:
    rel = path.relative_to(root)
    errors = []
    warnings = []
    try:
        text = path.read_text(encoding='utf-8')
        data = parse_frontmatter(text)
    except Exception as e:
        return [f'{rel}: некорректный frontmatter: {e}']

    for key in REQUIRED_KEYS:
        if data.get(key) in (None, '', []):
            errors.append(f'{rel}: отсутствует обязательный ключ "{key}"')
    description = data.get('description') or ''
    if len(description) > MAX_DESCRIPTION:
        errors.append(f'{rel}: description длиннее {MAX_DESCRIPTION} символов '
                      f'({len(description)})')
    elif description and not description.endswith('.'):
        errors.append(f'{rel}: description должен заканчиваться точкой')

    # Синтаксическая проверка всех SQL-скриптов скилла.
    for sql_path in sorted((path.parent / 'scripts').glob('*.sql')):
        for e in validate_sql(sql_path):
            errors.append(f'{sql_path.relative_to(root)}: {e}')

    # Все упомянутые в SKILL.md скрипты должны существовать на диске.
    for ref in SQL_REF_RE.findall(text):
        name = ref.rsplit('/', 1)[-1]
        if name not in sql_index:
            errors.append(f'{rel}: упомянутый скрипт не найден на диске: {ref}')

    # Doc-drift guard (015): имена таблиц в SQL-упоминаниях текста SKILL.md
    # — только из фиксированного списка миграций 001-017. Предупреждение,
    # не ошибка: полноценный SQL-линтер в скоуп не входит. Системные схемы
    # (information_schema и т.п.) — не таблицы проекта, пропускаются.
    for raw in SQL_TABLE_REF_RE.findall(text):
        parts = raw.lower().split('.')
        if len(parts) > 1 and parts[0] in ('information_schema', 'pg_catalog',
                                           'pg_toast'):
            continue
        name = parts[-1].strip('"')
        if name not in KNOWN_TABLES:
            warnings.append(
                f'{rel}: упоминание неизвестной таблицы "{name}" '
                f'(нет в миграциях 001-017; канон карты схемы — скилл '
                f'finbonds-sql-recipes)')
    return errors, warnings


def check_manifest(root: pathlib.Path, skill_dirs: set) -> list:
    """Манифест skills_manifest.yaml покрывает все каталоги скиллов (005.6).

    Манифест — источник правды канонического набора для синка профиля
    (scripts/sync_profile_skills.py); скилл без записи в манифесте не будет
    развёрнут в профиль агента.
    """
    path = root / 'skills_manifest.yaml'
    if not path.is_file():
        return ['нет skills_manifest.yaml — манифест канонического набора '
                'обязателен (см. 005.6)']
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except ImportError:
        # PyYAML нет — согласованность манифеста проверит sync_profile_skills.py
        return []
    except Exception as e:
        return [f'skills_manifest.yaml не читается: {e}']
    canonical = data.get('canonical') or {}
    listed = [str(s) for s in (canonical.get('skills') or [])]
    source_dir = str(canonical.get('source_dir') or 'finance')
    if not listed:
        return ['skills_manifest.yaml: canonical.skills пуст']
    errors = []
    for name in sorted(skill_dirs - set(listed)):
        errors.append(f'скилл "{name}" есть в дереве, но отсутствует в '
                      f'skills_manifest.yaml (canonical.skills) — синк профиля '
                      f'его не развернёт')
    for name in sorted(set(listed) - skill_dirs):
        errors.append(f'скилл "{name}" есть в skills_manifest.yaml, но нет '
                      f'{source_dir}/{name}/SKILL.md')
    return errors


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    skills = sorted(root.rglob('SKILL.md'))
    if not skills:
        print(f'ОШИБКА: SKILL.md не найдены в {root}', file=sys.stderr)
        return 1
    sql_index = {p.name: p for p in root.rglob('scripts/*.sql')}

    failed = 0
    warned = 0
    for path in skills:
        errors, warnings = validate(path, root, sql_index)
        if errors:
            failed += 1
            for error in errors:
                print(f'FAIL {error}', file=sys.stderr)
        else:
            print(f'OK   {path.relative_to(root)}')
        for warning in warnings:
            warned += 1
            print(f'WARN {warning}', file=sys.stderr)
    summary = f'Итого: {len(skills) - failed}/{len(skills)} скиллов валидны'
    if warned:
        summary += f' ({warned} предупрежд.)'
    print(summary)

    manifest_errors = check_manifest(root, {p.parent.name for p in skills})
    for error in manifest_errors:
        print(f'FAIL {error}', file=sys.stderr)
    if not manifest_errors:
        print(f'Манифест: OK '
              f'({len({p.parent.name for p in skills})} скиллов покрыты)')
    return 1 if (failed or manifest_errors) else 0


if __name__ == '__main__':
    sys.exit(main())
