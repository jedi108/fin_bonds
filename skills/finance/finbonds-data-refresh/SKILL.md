---
name: finbonds-data-refresh
description: "Свежесть рыночных данных fin_bonds и запуск обновления."
version: 0.5.1
author: jedi (jedi108)
license: MIT
platforms: [linux, macos]
metadata:
  tags: "FinBonds, Data, Freshness"
  related_skills: "finbonds-ops, finbonds-rebalance-advisor"
---

# FinBonds Data Refresh Skill

Гарантирует, что риск-аналитика считается по свежим данным: проверяет
актуальность, при необходимости запускает обновляющий пайплайн на хосте.
Запускай перед `finbonds-risk-monitoring`, `finbonds-risk-assessment`,
`finbonds-rebalance-advisor`.

## When to Use

- Перед любым расчётом рисков или ребаланс-сценария по портфелю.
- Пользователь спрашивает «насколько свежие данные» / «когда было обновление».
- Результаты анализа противоречат ожиданиям — первая гипотеза: устаревшие цены.

Don't use for: ручного обновления ради обновления — по расписанию данные
обновляет таймер на хосте; вмешивайся только если данные несвежие
или пользователь явно просит.

## Prerequisites

- Контракт путей и хоста — см. `finbonds-ops`.
- Скрипт гейта лежит в этом скилле: `scripts/freshness_check.sql` относительно
  каталога скилла (`"$FIN_BONDS_SKILLS_DIR/finbonds-data-refresh/scripts/freshness_check.sql"`).
- Дополнительно: понимание пайплайна `run_update.sh` на хосте
  (update-bonds --update-prices → calculate-spread → update-ratings →
  check-offers; шаг update-ratings по умолчанию — портфель, скоупы
  missing/catalog — см. finbonds-ratings).

## How to Run

Быстрый in-container гейт свежести (миллисекунды, без SSH) — основной способ:

`"${FIN_BONDS_PSQL:-psql}" --csv -f "$FIN_BONDS_SKILLS_DIR/finbonds-data-refresh/scripts/freshness_check.sql"`

Скрипт (v2) возвращает строку с заголовком (CSV) колонок:
`status|is_fresh|portfolio_rows|portfolio_total_val|portfolio_zero_rows|zero_rows_isins|bonds_with_price|tradeable_stale_or_missing_prices|market_time|sync_time`.

Контракт статусов (`status`, `is_fresh`):

- `OK` — данные свежие, zero-строк нет; `is_fresh=true`.
- `WARN_ZOMBIE_ROWS` — данные свежие, есть zero-строки, но подозрительных
  (нулевая оценка на НЕпогашенной бумаге) не больше 2; **`is_fresh=true`** —
  зомби-строки (погашенные бумаги / цена 0 от брокера) ребалансировке не
  мешают, вручную не разбирать. Список виновников — `zero_rows_isins`;
  разовая чистка — `main.py cleanup-portfolio --dry-run` на хосте.
- `CRITICAL_ZERO_VALUATION` — больше 2 zero-строк на НЕпогашенных бумагах
  (массовый сбой синка, класс бага из 001); `is_fresh=false`. Порог считается
  по непогашенным, а не по всем строкам (кейс 2026-09-29: 3 zero-строки, из
  них подозрительных 2 → WARN, а не ложный CRITICAL).
- `CRITICAL_NO_PRICES` / `STALE_PRICES` / `STALE_PORTFOLIO` / `EMPTY_PORTFOLIO`
  — `is_fresh=false`.

Принудительное обновление — **строго** через скрипт на хосте (никогда не через
`systemctl`: системный unit требует интерактивной аутентификации и агенту
недоступен), и только если данные несвежие:

`ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && bash run_update.sh'`

**Правило: если `is_fresh == true` — обновление НЕ запускать** (статус
`WARN_ZOMBIE_ROWS` этому не исключение: зомби-строки не делают данные
несвежими). Правило относится к обычному market/portfolio freshness
refresh: `is_fresh` проверяет свежесть цен/каталога/портфеля и возраст
cash snapshot НЕ включает. Данные уже актуальны; лишний прогон тратит
квоты внешних API (MOEX/ЦБ/T-Bank).

**Documented exception: explicit current-cash refresh (029-T16).** При
explicit current-cash intent (`require_current_cash=true` — «текущие
свободные деньги», см. раздел «Current-cash guard» скилла
`finbonds-rebalance-advisor`, 029-T14) правило выше этот сценарий НЕ
блокирует: обновление cash выполняется canonical
`sync-portfolio --source=tbank` на хосте по refresh SOP rebalance-advisor,
полный `run_update.sh` ради этого НЕ запускается. Cash freshness при этом
проверяет `finbonds-rebalance-advisor` через `portfolio.cash_updated_at`
(proof refresh — строгое продвижение метки после sync), а не через
`is_fresh`. Исключение действует только при current-cash intent: обычный
refresh без него (включая `is_fresh == false`) идёт по SOP выше; произвольный
TTL для cash snapshot не вводится.

Углублённая диагностика JSON-меток (на хосте, где есть venv и `.env`) — вторая
точка того же гейта, статусы согласованы с SQL (поле `gate_status` — канонический
статус v2; `portfolio_zero_rows_isins` — список виновников):

`ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py check-db --freshness --json'`

**В SOP ребалансировки отдельный вызов гейта не нужен:** `rebalance-report`
(скилл `finbonds-rebalance-advisor`) несёт тот же контракт гейта в блоке
`meta` (`gate_status`, `is_fresh`, `criticals[]`, `warnings[]` — общая точка
`classify_freshness`, 005.1). Оба способа выше оставь для задач вне
ребалансировки.

## Procedure

1. Сними гейт свежести `freshness_check.sql`. Критерий: одна
   строка результата со статусом и `is_fresh`.
2. Если `is_fresh == true` (в т.ч. статус `WARN_ZOMBIE_ROWS`) — обновление НЕ
   запускать, сообщи пользователю, что данные свежие (при WARN — упомяни
   зомби-строки из `zero_rows_isins` одной фразой, без разбора), и переходи к
   основной задаче. Исключение — explicit current-cash intent
   (`require_current_cash=true`): тогда cash refresh выполняется canonical
   `sync-portfolio --source=tbank` по SOP `finbonds-rebalance-advisor`
   (без `run_update.sh`), см. documented exception в How to Run.
3. Если `is_fresh == false` (статусы `STALE_*` или `CRITICAL_*`): запусти
   обновление строго через `bash run_update.sh` по SSH (команда выше,
   `timeout=900`). Скрипт защищён `flock`: параллельный запуск с таймером
   или другим процессом просто не начнётся повторно. Критерий: скрипт
   завершился без ошибок.
4. Перепроверь гейт повторным вызовом `freshness_check.sql`.
   Критерий: `is_fresh == true` (статус `OK` или `WARN_ZOMBIE_ROWS`;
   `portfolio_zero_rows` при WARN — это зомби, они не блокируют работу).
5. Сообщи пользователю итог: что было несвежим, что обновилось (не логируя и не раскрывая состав портфеля и ISIN).

## Pitfalls

- `run_update.sh` долгий (весь каталог MOEX) — задавай `timeout` не меньше 900s.
- Обновление берёт данные из внешних API (MOEX/ЦБ/T-Bank); лимиты и сетевые
  сбои дают частичные данные — после обновления проверь по гейту, что
  `bonds_with_price` и `market_time` продвинулись, а доля устаревших цен
  (`tradeable_stale_or_missing_prices`) снизилась.
- `WARN_ZOMBIE_ROWS` — НЕ красный статус: данные свежие, продолжай основную
  задачу; zero-строки с ISIN в `zero_rows_isins` — это погашенные бумаги
  (чистятся при синке или `cleanup-portfolio --dry-run`) и легитимно
  занулённые брокером позиции (торговля через оферту). Разбирать вручную
  и «игнорировать с оговоркой» не нужно.
- Статус `CRITICAL_ZERO_VALUATION` (v2: подозрительных zero-строк на
  непогашенных бумагах больше 2) — массовый сбой синка; обновление пайплайна
  пересчитает `current_value` при синке портфеля. Не пытайся восстановить
  оценку ручной арифметикой: цены в БД хранятся в рублях за 1 бумагу, формула
  `quantity * current_price` (см. `finbonds-ops`).
- Вне торговой сессии MOEX (10:00–18:45 МСК) биржевые стаканы пустые —
  отсутствие ликвидности ночью не является ошибкой БД и не требует
  перезапуска обновления.
- Не запускай обновление параллельно таймеру — `flock` в `run_update.sh`
  защищает от двойного прогона и двойного расхода квот API.

## Verification

- `freshness_check.sql` до/после: `is_fresh` стал `true` (статус `OK` или
  `WARN_ZOMBIE_ROWS`), `market_time` и `sync_time` продвинулись,
  `portfolio_total_val` ненулевая.
- `ssh ${FIN_BONDS_HOST} 'tail -20 ${FIN_BONDS_APP_DIR:-$HOME/work/bonds}/cron.log'` не содержит ошибок
  последнего прогона.
