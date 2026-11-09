# skills — Agent Skills для fin_bonds

Набор из 14 скиллов `finbonds-*` (`finance/<name>/SKILL.md`: YAML-frontmatter,
инструкции, SQL- и Python-скрипты) для работы с портфелем облигаций
[fin_bonds](../README.md): контроль рисков, свежесть данных, ребалансировка.
Скиллы не привязаны к конкретному агенту: подходят Claude Code, Codex,
OpenCode и любому агенту, который читает `SKILL.md`.

## Состав

| Скилл | Роль |
|---|---|
| `finbonds-ops` | карта проекта: команды, классы безопасности, границы записи |
| `finbonds-data-refresh` | свежесть данных перед любой аналитикой |
| `finbonds-risk-monitoring` | события риска: рейтинги, купоны, листинг, оферты |
| `finbonds-company-debt` | долговая нагрузка эмитентов: снимки долга, `company-debt-report` (read-only) |
| `finbonds-risk-assessment` | сводный риск-профиль (SQL-снапшот портфеля) |
| `finbonds-event-calendar` | купоны/оферты/амортизации/погашения, денежный поток |
| `finbonds-rebalance-advisor` | отклонения от лимитов + сценарий ребалансировки |
| `finbonds-rebalance-tool` | adapter доменных команд ребалансировки: report/plan/compare/snapshot, typed whitelist, snapshot как artifact |
| `finbonds-buy-candidates` | скринер кандидатов на покупку + инкремент купонного потока |
| `finbonds-chatgpt-portfolio-export` | текущий портфель → фиксированная Google Sheet для ChatGPT |
| `finbonds-portfolio-export` | список портфеля (цены, стоимость, YTM) в Markdown |
| `finbonds-portfolio-summary` | сводка по портфелям: вложено, текущая стоимость, PnL по счетам |
| `finbonds-ratings` | кредитные рейтинги: гейт свежести, скоупы `update-ratings`, интерпретация шкал |
| `finbonds-sql-recipes` | ad-hoc SQL: рецепты, каталог `-f`-скриптов, анти-паттерны, карта схемы |

## Требования

- Развёрнутый fin_bonds: checkout с venv и `.env` (`POSTGRES_DSN`) —
  установка в [корневом README](../README.md). Скиллы вызывают
  `python3 main.py <command>` и читают PostgreSQL.
- `psql` там, где агент выполняет SQL-скрипты.
- Опционально `ssh`, если приложение живёт на другой машине, чем агент.

## Установка

Агенты ожидают плоскую раскладку `<dir>/<name>/SKILL.md`, а в репозитории
дерево `skills/finance/<name>/`. Поэтому каждый каталог `finbonds-*` нужно
положить (symlink или копия) прямо в каталог скиллов агента:

```bash
SRC="$PWD/skills/finance"          # из корня checkout fin_bonds
DST="$HOME/.claude/skills"         # см. таблицу ниже
mkdir -p "$DST"
for d in "$SRC"/finbonds-*; do ln -sfn "$d" "$DST/$(basename "$d")"; done
# если агент не следует symlink'ам:
# for d in "$SRC"/finbonds-*; do cp -R "$d" "$DST/"; done
```

| Агент | Глобально | В проекте |
|---|---|---|
| Claude Code | `~/.claude/skills/` | `<project>/.claude/skills/` |
| Codex | `~/.agents/skills/` | `<repo>/.agents/skills/` |
| OpenCode | `~/.config/opencode/skills/` | `.opencode/skills/` (читает также `.claude/skills` и `.agents/skills`) |

Поддержка symlink'ов зависит от агента и версии; если скиллы не видны —
используйте `cp -R` (и повторяйте копирование после обновления репозитория).

После установки задайте `FIN_BONDS_SKILLS_DIR` = каталог-цель (абсолютный путь):
скрипты в `SKILL.md` вызываются как
`"$FIN_BONDS_SKILLS_DIR/<skill>/scripts/x.sql"`, в том числе между скиллами.
В Claude Code переменные можно задать в `settings.json` в блоке `env`
(только абсолютные пути, без `~` и `$HOME`).

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `FIN_BONDS_SKILLS_DIR` | каталог, куда установлены папки `finbonds-*` |
| `FIN_BONDS_PSQL` | исполняемый файл вместо `psql` (`"${FIN_BONDS_PSQL:-psql}"`). По умолчанию — обычный `psql` с libpq-переменными (`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` или `PGSERVICE`), либо ваш враппер, подгружающий креды. DSN в команду не передавать |
| `FIN_BONDS_ROOT` | необязательно: локальный read-only checkout fin_bonds |
| `FIN_BONDS_HOST` | ssh-алиас машины, где запущено приложение (venv + `.env`) |
| `FIN_BONDS_APP_DIR` | каталог приложения на этой машине (default `$HOME/work/bonds`) |
| `FIN_BONDS_TRANSPORT` | только adapter ребалансировки: `auto` (default: `ssh`, если задан `FIN_BONDS_HOST`, иначе `local`) / `ssh` / `local` |
| `FIN_BONDS_HOST_PY` | только adapter: интерпретатор core (default `venv/bin/python`) |
| `FIN_BONDS_REBALANCE_TIMEOUT_SEC`, `FIN_BONDS_ARTIFACT_DIR` | только adapter: таймаут core (default 300) и каталог snapshot-артефактов |

Подробности по adapter — в `finance/finbonds-rebalance-tool/SKILL.md`.

**Локально или удалённо.** Команды в скиллах записаны как
`ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && ...'`.
Если агент запущен на той же машине, что и приложение, ssh-обёртку
отбросьте и выполняйте часть после `&&` в каталоге приложения.

## Безопасность

- По умолчанию всё read-only. Мутирующие команды (обновление данных,
  запись в БД) — только по явной просьбе пользователя; классы команд и
  границы записи описаны в `finbonds-ops`.
- DSN и пароли — секрет: только `.env` приложения или libpq-окружение,
  никогда в тексте команды, скилла или ответа агента.
- В скиллах нет реальных ISIN, составов портфеля, хостов и путей машины:
  примеры синтетические, всё конкретное задаётся переменными окружения.

## Разработка скиллов

**Frontmatter** (обязательны `name`, `description`, `version`, `author`,
`license`, `platforms`):

- `name` совпадает с именем каталога;
- `description` — не длиннее 60 символов, заканчивается точкой;
- значения в `metadata` — строки (например `tags: "FinBonds, CLI"`).

**Структура тела** — как в существующих скиллах: `When to Use`,
`Prerequisites`, `How to Run`, `Procedure`, `Pitfalls`, `Verification`
(не все разделы нужны каждому скиллу).

**Валидация** — `make validate-skills`
(`./venv/bin/python skills/scripts/validate_skills.py`). Проверяет:

- frontmatter: обязательные ключи, длина и точка в `description`;
- SQL в `scripts/*.sql`: файл не пуст, `\if`/`\endif` и скобки сбалансированы;
- все `scripts/<file>.sql`, упомянутые в `SKILL.md`, существуют;
- агент-нейтральность: в `SKILL.md` и `scripts/*` нет привязки к
  конкретному рантайму (пути контейнера, имя рантайма, его tool-синтаксис);
- WARN (не влияет на код выхода): таблицы в `FROM/JOIN/UPDATE/INTO`
  из текста `SKILL.md`, которых нет в миграциях.

**Pre-commit hook** — `make hooks-install` (валидация при коммитах,
затрагивающих `skills/`).

**Тесты** — `./venv/bin/python -m pytest skills/scripts`.

## Лицензия

MIT.
