---
name: finbonds-rebalance-tool
description: "Стабильный adapter доменных команд ребалансировки."
version: 0.1.4
author: jedi (jedi108)
license: MIT
platforms: [linux, macos]
metadata:
  tags: "FinBonds, Bonds, Rebalance, CLI, Safety"
  related_skills: "finbonds-ops, finbonds-rebalance-advisor, finbonds-data-refresh"
---

# FinBonds Rebalance Tool (stable adapter)

Typed adapter доменных команд ребалансировки core fin_bonds (030.11):
`report` / `plan` / `compare` / `analysis-snapshot`. Скрывает transport
(ssh-хост, app-dir, venv), принимает только whitelist аргументов, отдаёт
machine-readable JSON. Обычная ребалансировка = `report` → `plan`, 0 ad-hoc
кода; heredoc/sed/scp и ручной shell plumbing в standard workflow не нужны.

## When to Use

- Любая ребалансировочная задача: канонический отчёт, fitted план по
  intents, A/B альтернативы (после 030.9), read-only анализ (snapshot).
- Когда нужно вернуть plan по frozen-контексту: `analysis-snapshot` →
  `--context-id` в `plan` (030.4).
- Don't use for: ad-hoc SQL (скилл `finbonds-sql-recipes`), обновления
  данных (`finbonds-data-refresh`), мутирующих команд — adapter read-only.

## Prerequisites

- Переменные окружения профиля (карта — `finbonds-ops`):
  `FIN_BONDS_HOST` (ssh-alias), опционально `FIN_BONDS_APP_DIR`
  (default `~/work/bonds`), `FIN_BONDS_HOST_PY` (default `venv/bin/python`).
- `FIN_BONDS_TRANSPORT` = `auto` (default: ssh при заданном `FIN_BONDS_HOST`,
  иначе local) | `ssh` | `local`. Local — для smoke на хосте.
- `FIN_BONDS_REBALANCE_TIMEOUT_SEC` (default 300), `FIN_BONDS_ARTIFACT_DIR`
  (default `~/.finbonds/artifacts`).
- `FIN_BONDS_APP_DIR` с `~` adapter разворачивает САМ (`expanduser` до
  `shlex.quote`, 031.4) — против HOME того процесса, где запущен adapter,
  а не против HOME удалённого shell. Где запущен adapter → что задать:

  | Где запущен adapter | `FIN_BONDS_APP_DIR` |
  |---|---|
  | Хост / машина владельца (HOME=/home/<user>) | `~` ок: дефолт `~/work/bonds` → `/home/<user>/work/bonds` |
  | Песочница/контейнер агента с другим HOME | только явный абсолютный путь на хосте (`/home/<user>/work/bonds`); иначе `~` развернётся в HOME песочницы → typed-отказ |

  Явный абсолютный путь работает отовсюду как раньше.
- DSN/секреты adapter'у не нужны: слой хранения core читает `.env` на хосте
  сам. Adapter ничего не считает — вся арифметика в core.

## How to Run

Скрипт: `"$FIN_BONDS_SKILLS_DIR/finbonds-rebalance-tool/scripts/rebalance_adapter.py"`

`python3 "$FIN_BONDS_SKILLS_DIR/finbonds-rebalance-tool/scripts/rebalance_adapter.py" report --freq-max 12`

Контракт ответа: **stdout — machine-readable JSON, stderr — diagnostics,
exit code сохраняется от core** (ожидаемые отказы core — `CONTEXT_*`,
`BUY_NOT_ELIGIBLE`, `UNKNOWN_ISIN`... — приходят JSON'ом с exit 0).

### Whitelist subcommand'ов

| Subcommand | Core | Флаги (только эти; повторяемые отмечены `+`) |
|---|---|---|
| `report` | `rebalance-report` | `--freq-min`, `--freq-max`, `--freq-in`, `--min-credit-rating`, `--exclude-sovereign`, `--include-ku`, `--include-held`, `--max-risk`, `--max-listlevel`, `--max-ytm`, `--min-maturity`, `--max-maturity`, `--screener-limit`, `--max-position-value`, `--redemptions-months` |
| `plan` | `rebalance-plan` | `--freq-min`, `--freq-max`, `--freq-in`, `--min-credit-rating`, `--exclude-sovereign`, `--max-position-value`, `--max-risk`, `--max-listlevel`, `--max-ytm`, `--include-ku`, `--min-maturity`, `--max-maturity`, `--sell-all+ ISIN`, `--target-value+ ISIN=RUB`, `--context-id`, `--view` |
| `compare` | `rebalance-compare` | вариантные зеркала plan: `--a-sell-all+`, `--a-target-value+`, `--a-freq-min`, `--a-freq-max`, `--a-freq-in`, `--a-min-credit-rating`, `--a-exclude-sovereign`, `--a-max-position-value`, `--a-max-risk`, `--a-max-listlevel`, `--a-max-ytm`, `--a-include-ku`, `--a-min-maturity`, `--a-max-maturity` и то же для `--b-*`; общий `--context-id` (оба варианта на одном PlanningContext; в ответе — верхнеуровневый `context_id`, 031.3) |
| `analysis-snapshot` | `rebalance-analysis-snapshot` | `--requires+ CODES`, adapter-флаг `--artifact-dir DIR` |

Не входит в whitelist: `--csv-out`, `--scenario` (файлы на хосте — transport
файлов вне контракта; сценарный путь покрывает `plan --target-value`).
Неизвестный/невалидный аргумент — typed-ошибка `ADAPTER_*` (JSON в stdout,
exit 2), не traceback.

### Примеры

Обычная ребалансировка (baseline):

`python3 .../rebalance_adapter.py report --include-held --max-position-value 350000`

`python3 .../rebalance_adapter.py plan --sell-all RU000A0ZZZ01 --target-value RU000B0YYYY2=350000 --target-value RU000C0XXXX3=200000`

Порядок повторений `--target-value` = allocation priority (первый — самый
приоритетный) — adapter сохраняет его как есть.

Candidate-policy покупки (031.1/031.2): политика подбирается по отчёту и
повторяется ФЛАГАМИ plan — без них core считает по canonical defaults
(max-risk 1, KU исключены); `--freq-in` взаимоисключим с
`--freq-min/--freq-max` (конфликт ловит core):

`python3 .../rebalance_adapter.py plan --target-value RU000B0YYYY2=350000 --max-risk 3 --max-ytm 30 --include-ku --min-maturity 2026-01-01 --freq-in 4,12`

A/B альтернативы на одном контексте (030.9): варианты отличаются только
префиксом `--a-`/`--b-`; оба считаются по одному `--context-id` (или по
свежей сборке) — сравнение разных `context_fingerprint` core отвергает
сам (`CONTEXT_MISMATCH`):

`python3 .../rebalance_adapter.py compare --a-target-value RU000B0YYYY2=350000 --b-target-value RU000B0YYYY2=500000 --b-exclude-sovereign --context-id abcdef0123456789abcdef0123456789`

Анализ по frozen-контексту (sandbox escape hatch, 030.10/030.12):

`python3 .../rebalance_adapter.py analysis-snapshot --requires ytm,credit_rating`

## Ответ adapter'а на analysis-snapshot

Полный snapshot НЕ попадает в prompt: adapter сохраняет его в приватный
локальный файл-артефакт (`0600`) и печатает metadata/preview envelope:
`context` (context_id/fingerprint/as_of/data_gate/TTL), `analysis`,
`capabilities`, `limitations`, counts + `*_preview` (первые 5 строк),
`artifact_file` `{path, sha256, usage}`. Sandbox/python-kernel читает файл
артефакта напрямую; содержимое приватное — наружу (web/external API) не
отправлять. Далее `plan --context-id <id из ответа>` считает по тому же
core-owned контексту.

## Коды ошибок и exit codes

| Exit | Код envelope'а (`artifact: adapter_error`) | Смысл |
|---|---|---|
| 0 | — | успех; отказы core — machine-readable JSON с exit 0 |
| 2 | `ADAPTER_UNKNOWN_COMMAND` / `ADAPTER_UNKNOWN_FLAG` / `ADAPTER_INVALID_ARGUMENT` / `ADAPTER_DUPLICATE_FLAG` | невалидные аргументы adapter'а |
| 3 | `ADAPTER_TRANSPORT` | ssh/local transport не смог запустить core |
| 4 | `ADAPTER_NON_JSON` | stdout core — не валидный JSON (контракт нарушен) |
| 5 | `ADAPTER_TIMEOUT` | core не уложилась в `FIN_BONDS_REBALANCE_TIMEOUT_SEC` |
| код core | `ADAPTER_COMMAND_FAILED` | core упала (stdout пуст/не JSON); код сохраняется, `stderr_tail` в envelope |

При ненулевом exit code core С валидным JSON в stdout adapter пробрасывает
JSON как есть и сохраняет exit code (второй envelope не добавляется).

## Procedure

1. Свежесть данных — `report` сам несёт `meta.gate_status` / `data_gate`;
   CRITICAL — сначала `finbonds-data-refresh`, не интерпретировать цифры.
2. Обычная ребалансировка: `report` → выбрать intents (ISIN только из
   canonical output) → `plan` (порядок `--target-value` = priority).
3. Альтернативы: `compare` с вариантами `--a-*`/`--b-*` (один
   PlanningContext через общий `--context-id`); без `--context-id` оба
   варианта на одной свежей сборке. Готовые plan results с разными
   `context_fingerprint` напрямую сравнивать нельзя.
4. Нестандартная аналитика: `analysis-snapshot` → sandbox по артефакту →
   proposed intents обратно через `plan` (границы — скилл
   `finbonds-rebalance-advisor`, 030.12: capability ladder — snapshot
   только после typed domain operations и compact projections; fallback —
   только read-only exploration над canonical artifact, без DB/API/
   secrets/сети, с явной imputation-политикой и provenance-блоком;
   повторяющийся fallback-паттерн — promote gap, а не новый ad-hoc
   «скрытый API»).
5. Перед рекомендацией: `feasible` (валидация) и `data_gate` (свежесть) —
   разные оси; проверять обе.

## Pitfalls

- Adapter — не калькулятор: любые деньги/qty/delta считает core. Ручной
  пересчёт поверх ответа — нарушение правила 030 №7.
- Не расширять командную строку самостоятельно (`;`, `&&`, `|`, подстановки)
  — whitelist отклонит; строки от LLM в shell не попадают (argv-list,
  удалённая строка собирается shlex.quote централизованно).
- `plan` без `--context-id` собирает свежий контекст: два вызова в разное
  время = разные `context_fingerprint` — сравнивать их напрямую нельзя.
- Артефакт snapshot'а приватный: не отправлять наружу и не публиковать.
- `cd: <HOME песочницы>/work/bonds: No such file or directory` +
  `ADAPTER_COMMAND_FAILED` при запуске adapter из песочницы агента —
  неявный (дефолтный `~`) или неверный app_dir: tilde развернулся против
  HOME песочницы агента, на хосте такого пути нет. Это не
  дефект adapter/core; чинится явным абсолютным `FIN_BONDS_APP_DIR`
  (таблица — Prerequisites, карта окружения — `finbonds-ops`).
- `DEBUG: Parsed arguments:` в stderr core — норма, не ошибка.

## Verification

- `python3 .../rebalance_adapter.py --help` печатает usage и exit 0.
- `report --freq-min abc` → JSON-объект `adapter_error` с кодом
  `ADAPTER_INVALID_ARGUMENT`, exit 2, stdout — валидный JSON.
- `plan --sell-all ru000a0zzz01` (нижний регистр) → `ADAPTER_INVALID_ARGUMENT`.
- `compare --sell-all ...` (голая форма plan) → `ADAPTER_UNKNOWN_FLAG` —
  только вариантные `--a-*`/`--b-*`.
- `plan --max-risk 3 --include-ku` → флаги доходят до core; `plan --max-risk 9`
  → `ADAPTER_INVALID_ARGUMENT`; `plan --max-risk 2 --max-risk 3` →
  `ADAPTER_DUPLICATE_FLAG`.
- `compare`-ответ содержит верхнеуровневый `context_id` (031.3) —
  pass-through без изменений.
- ssh-транспорт без явного `FIN_BONDS_APP_DIR`: в удалённой строке
  развёрнутый абсолютный путь, не литерал `'~/work/bonds'` (tilde-дефект
  030.13 не воспроизводится).
- `analysis-snapshot` → envelope с `artifact_file.sha256`; файл существует,
  валидный JSON, права 0600.
