---
name: finbonds-sql-recipes
description: "Ad-hoc SQL к БД fin_bonds: pg.sh, рецепты, карта схемы."
version: 0.2.0
author: jedi (jedi108), Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [FinBonds, Bonds, SQL, PostgreSQL, Reference]
    related_skills: [finbonds-ops]
---

# FinBonds SQL Recipes

Кулинарная книга ad-hoc SQL к БД fin_bonds (PostgreSQL): «нулевой рецепт» —
как вообще дотянуться до БД; рецепты (поиск эмитента/бумаги, позиции
портфеля по эмитенту, последнее значение метрики по ISIN, свежесть данных);
каталог готовых `-f`-скриптов всех finbonds-скиллов; карта актуальных колонок
ключевых таблиц/вью; анти-паттерны. Карта среды, классов команд CLI и единиц
измерения — скилл `finbonds-ops` (загружать его первым).

Источник правды карты — миграции `migrations/postgres/001..014` + живая БД.
Документация (включая finbonds-ops) источником схемы не считается: при любом
расхождении сверяться с живой БД (рецепт сверки — ниже). Карта сверена с
живой БД 2026-09-30: все имена/типы/порядок колонок — через
`information_schema.columns`, ключи и домены — через `pg_constraint`,
индексы — через `pg_indexes`; применены миграции 1–17 (`schema_migrations`).

## Доходность ≠ купон (issue #3, обязательно)

- «Доходность» — **только** `ytm_pct`, и всегда с горизонтом из `ytm_to`:
  `offer` → «доходность к оферте <offer>», `maturity` → «доходность к погашению».
- `coupon_pct` называть только «купонная ставка» (или «купон»), никогда не «доходность».
- Есть оферта (`offer` / `offer_date`) — указывай её дату.
- `ytm_check: "mismatch"` — приводи оба числа: наша `ytm_pct` и MOEX `moex_yield_pct`,
  с оговоркой «расхождение из-за методики расчёта»; ни одно не подавай как бесспорное.
  `no_moex_data` — MOEX-доходности нет (нет сделок), достаточно `ytm_pct`.
- Пустой `ytm_pct` (см. `ytm_reason`, напр. флоатер) не заменяй купоном.
- Пример (Эмитент В 001Р-01, RU000A10XYC1): «купон 19%, доходность к оферте 23.11.2026 ≈13.7%».

## When to Use

- Нужен ad-hoc SQL к БД fin_bonds: найти эмитента/бумагу, позиции по
  эмитенту, последнее значение метрики — готовые рецепты в разделе
  «Рецепты»; типы/колонки — «Карта схемы».
- Готовый сценарий (свежесть, скрининг, ребалансировка, риск) — взять скрипт
  из «Каталога готовых `-f` скриптов», а не писать запрос с нуля.
- Первый SQL-запрос в сессии — начать с «Нулевого рецепта» (pg.sh).

Don't use for: запуск команд CLI fin_bonds (finbonds-ops) — для чтения и
аналитики канонический путь это CLI на хосте, SQL не нужен вовсе.

## Нулевой рецепт — точка входа: pg.sh

- БД доступна ТОЛЬКО через враппер
  `/opt/data/profiles/fin_bonds/bin/pg.sh` (контейнер hermes-ai). Враппер сам
  делает `set -a; . /opt/data/profiles/fin_bonds/.env; set +a` и зовёт psql —
  `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` подхватывает libpq.
- `POSTGRES_DSN` в окружении контейнера НЕТ (из compose снят) — голый
  `psql` падает (socket-фолбэк `/var/run/postgresql`, роль не существует).
  В python-kernel `os.environ["POSTGRES_DSN"]` падает `KeyError`; psql-вызовы
  в python-kernel не переносить.

Канонические формы вызова:

1. Машинное чтение — `--csv` + `-f` (файл скрипта):

   `terminal(command="/opt/data/profiles/fin_bonds/bin/pg.sh --csv -f /tmp/q_issuer.sql")`

   `--csv` печатает строку заголовков — вывод читается без исходного SQL.
   Длинный SQL и любой SQL с кириллическими литералами — только файлом
   (`-f`), не inline `-c` (триггерит security-сканер терминала и ломается на
   вложенных кавычках). Параметры запроса — psql-переменные:
   `pg.sh -v isin='RU000A10XYD1' -f /tmp/q.sql`, в скрипте — `:'isin'`.

2. Короткий однострочный SELECT без кириллических литералов — inline `-c`:

   `terminal(command="/opt/data/profiles/fin_bonds/bin/pg.sh --csv -c 'SELECT isin, ticker FROM bonds_catalog LIMIT 3;'")`

Свои черновые скрипты класть в `/tmp` контейнера; готовые `-f`-скрипты всех
finbonds-скиллов лежат в профиле:
`/opt/data/profiles/fin_bonds/skills/finance/<скилл>/scripts/<файл>`
(полный каталог — в разделе «Каталог готовых `-f` скриптов» ниже).

## Рецепты

Каждый рецепт этого раздела выполнен на живой БД через `pg.sh --csv` без
ошибок (2026-09-30, см. Verification). Скрипты-шаблоны живут в `scripts/`
скилла; после деплоя путь в контейнере:
`/opt/data/profiles/fin_bonds/skills/finance/finbonds-sql-recipes/scripts/`.

### Рецепт 1 — найти эмитента/бумагу (scripts/issuer_lookup.sql)

Поиск по ISIN/тикеру/имени бумаги и по имени/полному имени эмитента
(связка `bonds_catalog.company_id → companies`). Вывод: блок 1 — найденные
бумаги с эмитентом; блок 2 — найденные эмитенты (`companies`) со счётчиками
бумаг каталога (всего/живых).

`terminal(command="/opt/data/profiles/fin_bonds/bin/pg.sh --csv -v q='RU000A10XYD1' -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-sql-recipes/scripts/issuer_lookup.sql")`

Параметры: `q` — обязательная подстрока для `ILIKE '%...%'`; `limit` —
максимум строк на блок (default 30). Кириллические значения `q` — ТОЛЬКО
через файл-обёртку (анти-паттерн «inline -c с кириллицей»): в `/tmp/q.sql`
написать

```
\set q 'Система'
\ir /opt/data/profiles/fin_bonds/skills/finance/finbonds-sql-recipes/scripts/issuer_lookup.sql
```

и выполнить `pg.sh --csv -f /tmp/q.sql`. Если по имени не находится —
искать часть слова без падежного окончания («Система», не «Системы»):
совпадение регистронезависимое, но морфологии нет.

### Рецепт 2 — позиции портфеля по эмитенту (scripts/positions_by_issuer.sql)

Оценка — канонический view `v_portfolio_positions_valuation` (готовые
`company_id`/`company_name`, `position_value_rub`, `position_status`,
`valuation_source`). Эмитент ищется так же, как в рецепте 1 (по
`companies.name`/`full_name` или по бумаге каталога). Вывод: блок 1 — сводка
по найденным эмитентам (живая оценка, доля портфеля); блок 2 — позиции
(брокер/счёт/статус/valuation_source).

`terminal(command="/opt/data/profiles/fin_bonds/bin/pg.sh --csv -v q='Глобал' -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-sql-recipes/scripts/positions_by_issuer.sql")`

Фильтр `position_status` — ОБЯЗАТЕЛЬНЫЙ пункт рецепта (миграция 010):
по умолчанию скрипт показывает только `position_status = 'active'`; без
фильтра зомби-позиции (`matured`/`closed`, оценка 0 или фантом) портят
суммы и доли. Значение `-v status='all'` снимает фильтр — только для аудита
зомби-позиций, не для оценки портфеля. К полю `portfolio_positions.status`
таблицы — то же правило (`WHERE status = 'active'`).

### Паттерн «последнее значение метрики по ISIN»

Канонический вид (выборка ограничена заведомо числовым `metric_name` —
фильтра по имени достаточно; правила — «Карта схемы» → `monitoring_checks`):

```sql
SELECT DISTINCT ON (isin) isin, metric_value::numeric AS calc_rate
FROM monitoring_checks
WHERE metric_name = 'floater_coupon_calculator'
ORDER BY isin, check_date DESC;
```

Если выборка НЕ ограничена числовым `metric_name` — regex-фильтр
ОБЯЗАТЕЛЕН ДО каста (`'N/A'::numeric` падает), а `DISTINCT ON` — по
`(isin, metric_name)`, иначе по одному ISIN останется случайная метрика:

```sql
SELECT DISTINCT ON (isin, metric_name) isin, metric_name,
       metric_value::numeric AS value_num
FROM monitoring_checks
WHERE metric_name IN ('floater_coupon_calculator', 'moex_listlevel', 'tinkoff.risk_level')
  AND metric_value ~ '^[0-9]+([.][0-9]+)?$'
ORDER BY isin, metric_name, check_date DESC;
```

Так достают: текущую ставку флоатера (`floater_coupon_calculator`),
`moex_listlevel`, `tinkoff.risk_level` — тот же паттерн внутри view
`v_portfolio_positions_valuation` и скриптов `buy_candidates.sql` /
`simulate_swap.sql` / `scenario_summary.sql`. Числовой колонки
`metric_value_num` в схеме НЕТ — источник числа только `metric_value` +
явный `::numeric` (решение 002.7).

### Рецепт 3 — свежесть данных (не дублировать)

Свежесть проверяется готовым скриптом `freshness_check.sql` скилла
`finbonds-data-refresh` (путь — в каталоге ниже), контракт статусов v2
(`OK` / `WARN_ZOMBIE_ROWS` / `CRITICAL_*` / `STALE_*` / `EMPTY_PORTFOLIO`,
задача 002.1) — в SKILL.md `finbonds-data-refresh`. Запуск:

`terminal(command="/opt/data/profiles/fin_bonds/bin/pg.sh --csv -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-data-refresh/scripts/freshness_check.sql")`

## Каталог готовых `-f` скриптов

Все 8 SQL-скриптов finbonds-скиллов. Общая форма запуска:
`pg.sh --csv [-v name=value ...] -f /opt/data/profiles/fin_bonds/skills/finance/<скилл>/scripts/<файл>`
(в таблице путь от `/opt/data/profiles/fin_bonds/skills/finance/`).

| Скилл | Скрипт | Параметры `-v` | Что возвращает |
|---|---|---|---|
| finbonds-data-refresh | `scripts/freshness_check.sql` | — | одна строка: статус свежести (контракт v2, 002.1), `is_fresh`, счётчики каталога/портфеля, нулевые строки и их ISIN |
| finbonds-buy-candidates | `scripts/buy_candidates.sql` | `max_risk` (1), `coupon_freq` (0=любая), `coupon_type` (ALL/FIX/FLOAT), `ofz_mode` (all/only/exclude), `limit` (30), `include_held` (false) | скрининг кандидатов на покупку вне портфеля; с `include_held=true` — с бейджем «held (N%)» |
| finbonds-portfolio-export | `scripts/rebalance_export.sql` | — | построчная выгрузка портфеля для внешнего советника: доли, купоны, тип, погашение, рейтинг, риск, exit_loss |
| finbonds-rebalance-advisor | `scripts/simulate_swap.sql` | `sell_isin`, `sell_qty`, `buy_isin`; двухногая покупка: `buy_isin_2` + `buy_amount_2` | симуляция свопа: суммы ног, купонный инкремент ₽/мес и ₽/квартал, пост-трейд доля эмитента (лимит 15%) |
| finbonds-rebalance-advisor | `scripts/scenario_summary.sql` | сценарий сделок — отдельный файл с `TEMP TABLE planned(isin text, qty_delta numeric)` (см. шапку скрипта), оба файла в ОДНОЙ psql-сессии | блоки 0–3: ошибки сценария, сводка «сейчас → после» (купон ₽/мес, дюрация), целевой портфель, концентрации эмитентов |
| finbonds-portfolio-summary | `scripts/portfolio_summary.sql` | — | сводка по брокерам/счетам + итоги портфеля (вложено/текущая/PnL) + метка свежести синка |
| finbonds-ratings | `scripts/ratings_freshness.sql` | — | гейт свежести рейтингов: `rating_history_rows`, `max_rating_date`, `na_tail`, `never_checked`, `alive_catalog` |
| finbonds-risk-assessment | `scripts/risk_snapshot.sql` | — | риск-снапшот: концентрации эмитентов, распределение `risk_level`, взвешенная дюрация, liquidity loss, свежесть данных |

Шаблоны этой книги — `scripts/issuer_lookup.sql` и
`scripts/positions_by_issuer.sql` (рецепты 1–2 выше).

## Анти-паттерны

«Так делать нельзя» + чем заменить:

1. **Голый `psql` без враппера.** Контейнерное окружение кредов fin_bonds
   не содержит: падение на socket-fallback `/var/run/postgresql` (локально)
   или `FATAL: role ... does not exist` (на хосте). Замена: только
   `/opt/data/profiles/fin_bonds/bin/pg.sh` — он сам source-ит профильный
   `.env`, `PG*` подхватывает libpq.
2. **Inline `-c` с кириллицей.** Триггерит security-сканер терминала
   (гомоглифы/юникод в кириллице) и ломается на вложенных кавычках.
   Замена: SQL целиком в файл + `pg.sh --csv -f file.sql`; параметры —
   psql-переменные `-v`; кириллические значения параметров — файл-обёртка
   с `\set` (рецепт 1).
3. **Деление цен/сумм в рублях на 100.** Номинал и цены уже в рублях
   (`nominal` = руб, `market_price`/`current_price` = руб за бумагу);
   деление уводит оценку в 100 раз. Замена: значения использовать как есть;
   расхождение решать запросом через view, не ментальным пересчётом.
4. **Смешивание YTM-единиц.** После миграции 013 `bonds_catalog.ytm` —
   ПРОЦЕНТЫ годовых (`24.01` = 24.01%); в логах и заметках до 2026-09-29 —
   доли единицы (`0.24` = 24%). Замена: не умножать/делить YTM на 100 ни в
   какую сторону; сомнение разрешает `doc/units_conventions.md` (finbonds-ops).
5. **`COALESCE(metric_value, <numeric>)` без каста.** `ERROR: COALESCE types
   text and numeric cannot be matched` — `metric_value` имеет тип TEXT.
   Замена: `metric_value::numeric`; если выборка не ограничена числовым
   `metric_name` — regex-фильтр `'~ ^[0-9]+([.][0-9]+)?$'` ДО каста
   (паттерн «последнее значение метрики» выше).
6. **Оценка портфеля без фильтра статуса позиций.** Без
   `position_status = 'active'` (view) / `status = 'active'` (таблица)
   зомби-позиции `matured`/`closed` попадают в суммы, средние и доли
   (миграция 010). Замена: фильтр в каждом оценочном/аналитическом запросе;
   view статус НЕ фильтрует сам.

## Карта схемы

Самопроверка карты (подставить нужное table_name):

`pg.sh --csv -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='bonds_catalog' ORDER BY ordinal_position;"`

### bonds_catalog — каталог облигаций, 33 колонки

| # | Колонка | Тип | Смысл |
|---|---------|-----|-------|
| 1 | isin | text | PK |
| 2 | figi | text | |
| 3 | ticker | text | |
| 4 | name | text | |
| 5 | currency | text | |
| 6 | nominal | numeric | номинал, руб |
| 7 | maturity_date | date | |
| 8 | offer_date | date | |
| 9 | coupon_quantity_per_year | integer | купонов в год |
| 10 | coupon_rate_percent | numeric | %, каноническая точность 4 знака (011) |
| 11 | coupon_type | text | 'FIX'/'FLOAT' (backfill 011), синхронно с floating_coupon_flag |
| 12 | list_level | integer | 1–3, MOEX |
| 13 | risk_level | integer | 1–5, Т-Банк |
| 14 | coupon_spread | numeric | спред флоатера к ключевой ставке |
| 15 | is_trade_available | boolean | |
| 16 | is_for_qualified_investors | boolean | флаг КУ |
| 17 | issue_size | bigint | объём выпуска |
| 18 | created_at | timestamptz | |
| 19 | updated_at | timestamptz | |
| 20 | floating_coupon_flag | boolean | |
| 21 | perpetual_flag | boolean | |
| 22 | amortization_flag | boolean | |
| 23 | market_price | numeric | руб за бумагу |
| 24 | market_price_source | text | |
| 25 | market_price_updated_at | timestamptz | |
| 26 | company_id | bigint | FK → companies(id) |
| 27 | duration_macaulay | numeric | дюрация Маколея, лет |
| 28 | duration_modified | numeric | модифицированная дюрация, лет |
| 29 | duration_null_reason | text | причина NULL дюрации |
| 30 | duration_updated_at | timestamptz | |
| 31 | ytm | numeric | ПРОЦЕНТЫ годовых (013): 24.01 = 24.01% |
| 32 | ytm_null_reason | text | причина осознанного NULL |
| 33 | ytm_updated_at | timestamptz | |

Единицы и запрет пересчётов — finbonds-ops («Канонические размерности и
валюты», «Единицы YTM»): YTM не умножать/делить на 100, цены — рубли за
бумагу. `ytm`/`duration_*` NULL — осознанные, с причиной в
`ytm_null_reason`/`duration_null_reason`: NULL не подменять нулём и не
«дорассчитывать» упрощёнными формулами. Наблюдаемые значения
`ytm_null_reason` (сверка 2026-09-30): `floating_coupon`,
`amortization_schedule_missing`, `perpetual`,
`market_price_missing_or_nonpositive`, `coupon_frequency_missing`,
`coupon_rate_missing_or_negative`, `maturity_missing_or_past`.

### portfolio_positions — позиции портфеля, 19 колонок

| # | Колонка | Тип | Смысл |
|---|---------|-----|-------|
| 1 | id | bigint | PK |
| 2 | isin | text | NOT NULL; FK не имеет, но связан с bonds_catalog |
| 3 | ticker | text | |
| 4 | name | text | |
| 5 | quantity | numeric | шт |
| 6 | average_price | numeric | руб за бумагу |
| 7 | current_price | numeric | руб за бумагу |
| 8 | currency | text | |
| 9 | yield_to_maturity | numeric | |
| 10 | portfolio_percent | numeric | % портфеля |
| 11 | current_value | numeric | руб за позицию |
| 12 | broker_name | text | NOT NULL |
| 13 | instrument_type | text | |
| 14 | created_at | timestamptz | |
| 15 | updated_at | timestamptz | триггер update_positions_updated_at |
| 16 | liquidity_loss_ratio | numeric | |
| 17 | coupon_rate_percent | numeric | |
| 18 | account_id | text | NOT NULL DEFAULT '' (006); '' — Excel-импорт/старые строки |
| 19 | status | text | NOT NULL DEFAULT 'active'; CHECK: active/matured/closed (010) |

Ключевые правила:

- UNIQUE-ключ (006) — тройка `(isin, broker_name, account_id)`: одна бумага
  живёт в нескольких строках (счета/брокеры). Агрегировать SUM/GROUP BY,
  не рассчитывать на одну строку на ISIN.
- `status` (010): в оценочных/аналитических запросах фильтр по статусу
  ОБЯЗАТЕЛЕН (`WHERE status = 'active'`, у view — `position_status`),
  иначе matured/closed (зомби-позиции с нулевой или фантомной оценкой)
  попадут в расчёт.
- Позиция может существовать без строки в bonds_catalog (FK на каталог нет):
  view собирает её через LEFT JOIN, часть колонок будет NULL.

### companies — справочник эмитентов, 7 колонок

| # | Колонка | Тип | Смысл |
|---|---------|-----|-------|
| 1 | id | bigint | PK; на него ссылается bonds_catalog.company_id |
| 2 | name | text | NOT NULL, краткое имя |
| 3 | full_name | text | |
| 4 | inn | text | UNIQUE |
| 5 | entity_type | text | NOT NULL DEFAULT 'company'; домен: company/individual/region/sovereign |
| 6 | created_at | timestamptz | |
| 7 | updated_at | timestamptz | |

### monitoring_checks — метрики мониторинга; metric_value — TEXT

| # | Колонка | Тип | Смысл |
|---|---------|-----|-------|
| 1 | id | bigint | PK |
| 2 | isin | text | NOT NULL |
| 3 | check_date | date | NOT NULL |
| 4 | metric_name | text | NOT NULL |
| 5 | metric_value | text | ЧИСЛА ХРАНЯТСЯ СТРОКОЙ |

UNIQUE `(isin, check_date, metric_name)`.

`metric_name`, наблюдаемые в живой БД (2026-09-30): `credit_rating` (текст:
`'AA-'`, `'N/A'`), `floater_coupon_calculator` (текущая ставка флоатера,
число строкой), `moex_listlevel` (число строкой), `liquidity_analyzer`,
`moex_coupon`, `tinkoff.risk_level`.

Правила арифметики (решение 002.7):

1. Любое использование `metric_value` в арифметике или COALESCE с
   numeric-выражением — только через явный каст `metric_value::numeric`.
2. Если выборка не ограничена заведомо числовым `metric_name`, фильтровать
   ДО каста — `'N/A'::numeric` падает:
   `WHERE metric_value ~ '^[0-9]+([.][0-9]+)?$'`.
3. Числовую метрику доставать каноническим паттерном «последнее значение
   по ISIN» (так делают view `v_portfolio_positions_valuation` и скрипты
   других скиллов):

   ```sql
   SELECT DISTINCT ON (isin) isin, metric_value::numeric AS calc_rate
   FROM monitoring_checks
   WHERE metric_name = 'floater_coupon_calculator'
   ORDER BY isin, check_date DESC;
   ```

4. Числовой колонки `metric_value_num` в схеме НЕТ и вводить её не нужно
   (решение 002.7): без одновременной правки писца она была бы пустой.
   Единственный источник числа — `metric_value` + явный `::numeric`.

### VIEW v_portfolio_positions_valuation — каноническая оценка, 22 колонки

Единственная каноническая оценка позиции; собирать оценку вручную из таблиц
не нужно. Порядок/имена сверены с живой БД 2026-09-30 (миграции 009+010+014;
CREATE OR REPLACE VIEW дописывает колонки только в конец).

| # | Колонка | Тип | Смысл |
|---|---------|-----|-------|
| 1 | isin | text | |
| 2 | broker_name | text | |
| 3 | account_id | text | |
| 4 | ticker | text | |
| 5 | name | text | |
| 6 | quantity | numeric | |
| 7 | average_price | numeric | |
| 8 | current_price | numeric | |
| 9 | nominal | numeric | из bonds_catalog |
| 10 | market_price | numeric | из bonds_catalog |
| 11 | position_value_rub | numeric | каноническая оценка позиции, руб, ROUND(...,2) |
| 12 | liquidity_loss_ratio | numeric | |
| 13 | duration_modified | numeric | |
| 14 | ytm | numeric | проценты годовых |
| 15 | risk_level | integer | |
| 16 | list_level | integer | |
| 17 | coupon_rate_percent | numeric | каталог, иначе последняя ставка флоатера из monitoring_checks (::numeric) |
| 18 | coupon_quantity_per_year | integer | |
| 19 | company_id | bigint | готовая связка на эмитента |
| 20 | company_name | text | COALESCE(companies.name, каталог, позиция) |
| 21 | position_status | text | = portfolio_positions.status |
| 22 | valuation_source | text | broker_value/broker_price/market_price/nominal_fallback/zero |

- `position_value_rub` — cascade-фолбэк: `current_value` брокера →
  `quantity*current_price` → `quantity*market_price` → `quantity*nominal`
  (только непогашенная бумага — guard миграции 010) → 0. `valuation_source`
  зеркалит ветку COALESCE: случай «price_rub = 0 при value_rub > 0» —
  легитимный `broker_value`, а не ошибка.
- View НЕ фильтрует статус: для оценки живого портфеля добавлять
  `WHERE position_status = 'active'` самостоятельно.

### Остальные таблицы (кратко)

- `company_debt_snapshots` (017): `id` bigserial PK, `company_id` bigint NOT
  NULL (FK companies), `report_date` date NOT NULL, `source` text NOT NULL,
  `retrieved_at` timestamptz, суммы numeric в RUB (NULL = неизвестно, не 0):
  `total_debt_rub`, `bond_debt_rub`, `bank_debt_rub`, `lease_debt_rub`,
  `cash_rub`, `net_debt_rub`, `revenue_rub`, `ebitda_rub`,
  `issued_bond_volume_rub`, `outstanding_bond_debt_rub`; `coverage_status`
  (full|partial|unknown), `quality_status` (verified|unverified|estimated),
  `source_ref`, `notes`. UNIQUE `(company_id, report_date, source)`.
  Последний снимок компании — по `report_date`, затем `retrieved_at`:

  ```sql
  SELECT DISTINCT ON (s.company_id)
         c.name, s.report_date, s.source, s.net_debt_rub, s.ebitda_rub,
         s.outstanding_bond_debt_rub, s.coverage_status, s.quality_status
  FROM company_debt_snapshots s
  JOIN companies c ON c.id = s.company_id
  ORDER BY s.company_id, s.report_date DESC, s.retrieved_at DESC;
  ```

  Оговорка: `issued_bond_volume_rub` в снимке — оценка размещённого объёма
  (размер выпуска × номинал), НЕ остаток долга и не леверидж; реальный остаток
  облигаций — `outstanding_bond_debt_rub`. Нет строки/старый `report_date` =
  неизвестно. Не считать net_debt/EBITDA самому: готовый отчёт —
  `company-debt-report` (скилл `finbonds-company-debt`).

- `rating_history` (001+005+012): `id` bigint PK, `isin` text NOT NULL (FK
  bonds_catalog), `rating_date` date NOT NULL, `rating_code` text,
  `rating_score` integer. UNIQUE-индекс `uq_rating_history_isin_date_code
  (isin, rating_date, rating_code)` — писатель идемпотентен (012); «последний
  рейтинг» — `ORDER BY rating_date DESC, id DESC`. Рейтинги наполняются
  только по бумагам портфеля (002.6): для кандидатов вне портфеля рейтинга
  нет, прокси кредитного риска — `risk_level`.
- `risk_history` (001+005): `id` bigint PK, `isin` text NOT NULL (FK),
  `risk_date` date NOT NULL, `risk_level` integer.
- `listlevel_history` (001+005): `id` bigint PK, `isin` text NOT NULL (FK),
  `check_date` date NOT NULL, `list_level` integer.
- `calculated_coupons` (001+005): `id` bigint PK, `isin` text NOT NULL (FK),
  `check_date` date NOT NULL, `calculated_rate` numeric NOT NULL, UNIQUE
  `(isin, check_date)`. На проде пуста: текущая ставка флоатера живёт в
  `monitoring_checks` (`floater_coupon_calculator`).
- `liquidity_history` (001+005): `id` bigint PK, `isin` text NOT NULL (FK),
  `"timestamp"` timestamptz NOT NULL (ключевое слово — только в кавычках),
  `base_volume` numeric NOT NULL, `market_exit_value` numeric NOT NULL,
  `loss_ratio` numeric NOT NULL, `depth_level` integer NOT NULL,
  `data_source` text NOT NULL.
- Служебная `schema_migrations` (журнал ранера миграций): `version` integer,
  `name` text, `applied_at` timestamptz. Применены 1–17 (сверка 2026-11-03);
  ad-hoc не мутировать.

## Pitfalls

- Ловушки ad-hoc SQL собраны в разделе «Анти-паттерны» выше; правила
  `monitoring_checks.metric_value` (TEXT) — в «Карте схемы» ниже.
- `liquidity_history."timestamp"` — ключевое слово SQL, только в кавычках.
- Связи: все history-таблицы и `monitoring_checks` ссылаются на каталог по
  `isin` (FK на bonds_catalog); каталог → эмитент — через `company_id`.
- Конфиденциальность: ISIN/составы портфеля наружу (web, публичные
  репозитории) не публиковать.

## Verification

- `make validate-skills` (корень репозитория fin_bonds) — зелёный.
- Рецепты проверены на живой БД через `pg.sh --csv` без ошибок (2026-09-30):
  `issuer_lookup.sql` (латиница `-v q=RU000A10XYD1` и кириллица через
  файл-обёртку `\set q 'Система'`), `positions_by_issuer.sql` (кириллица
  `\set q 'Глобал'` и аудит `-v status=all` — зомби-позиция matured/zero
  видна только в нём), паттерн «последнее значение метрики» — канонический
  и с regex-гвардом до каста (смешанные числовые `metric_name`), плюс
  готовый `freshness_check.sql` (статус v2). Свежесть цен (WARN/STALE)
  статусом ошибки не считается.
- Карта сверена с живой БД 2026-09-30: `information_schema.columns` по всем
  таблицам/вью (имена, типы, порядок), `pg_constraint` (UNIQUE/CHECK/FK),
  `pg_indexes` (rating_history), `schema_migrations` (1–17). Повторная
  сверка — рецептом из раздела «Карта схемы».
