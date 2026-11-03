---
name: finbonds-ops
description: "Карта CLI fin_bonds: команды, права, границы записи."
version: 0.5.7
author: jedi (jedi108)
license: MIT
platforms: [linux, macos]
metadata:
  tags: "FinBonds, Bonds, CLI, Safety"
  related_skills: "finbonds-data-refresh, finbonds-risk-monitoring, finbonds-rebalance-advisor, finbonds-rebalance-tool, finbonds-sql-recipes"
---

# FinBonds Ops Skill

Базовая карта проекта fin_bonds (мониторинг портфеля облигаций РФ): где лежат
код и данные, какие команды CLI безопасны, какие мутируют состояние. Остальные
скиллы `finbonds-*` опираются на этот контракт — загружай его первым.

## When to Use

- Любая задача про портфель облигаций, каталог bonds, рейтинги, оферты, YTM.
- Перед запуском любой команды fin_bonds, если неизвестен её класс безопасности.
- Разбор ошибки команды fin_bonds (куда смотреть: логи, БД, cron).

Don't use for: задач fin-ml (другой контур, скиллы S*); торговых операций —
проект только читает данные и считает аналитику, сделок не совершает.

## Prerequisites

Окружение (все переменные задаёт развёртывание, скилл их не создаёт):

| Переменная | Назначение |
|---|---|
| `FIN_BONDS_PSQL` | исполняемый файл вместо `psql`; в командах `"${FIN_BONDS_PSQL:-psql}"`. По умолчанию — обычный `psql` с подключением из libpq-окружения (`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` или `PGSERVICE`); развёртывание может указать на враппер, который сам подгружает креды. DSN в команду не вставлять |
| `FIN_BONDS_SKILLS_DIR` | каталог с установленными папками `finbonds-*`; пути скриптов — `"$FIN_BONDS_SKILLS_DIR/<скилл>/scripts/<файл>"` (в кавычках) |
| `FIN_BONDS_ROOT` | необязательный локальный read-only checkout fin_bonds |
| `FIN_BONDS_HOST` | ssh-alias хоста, где запущено приложение (venv и `.env`). Всё, что требует записи или интернета, выполняется там |
| `FIN_BONDS_APP_DIR` | каталог приложения на хосте (default `$HOME/work/bonds`); подставляется в ssh-командах `cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds}` |
| `FIN_BONDS_TRANSPORT`, `FIN_BONDS_HOST_PY` | транспорт и python adapter'а — см. `finbonds-rebalance-tool` |

- Команды ниже написаны для приложения на удалённом хосте: `ssh ${FIN_BONDS_HOST} '...'`.
  Если агент работает на той же машине, что и fin_bonds, убери ssh-обёртку и
  выполняй внутреннюю команду в `FIN_BONDS_APP_DIR`.
- `POSTGRES_DSN` — секрет; хранится в `.env` приложения на хосте. В команды не
  вставлять дословно и по возможности не использовать вовсе (psql — через
  `"${FIN_BONDS_PSQL:-psql}"`).
- Семантики разворачивания `FIN_BONDS_APP_DIR` РАЗЛИЧАЮТСЯ (032.1): в прямых
  ssh-командах ops-паттерн разворачивает УДАЛЁННЫЙ shell (HOME=/home/<user>) —
  home-relative формы (`$HOME/work/bonds`, `~/work/bonds`) работают; adapter
  (`finbonds-rebalance-tool`) разворачивает переменную САМ (`expanduser`) ДО
  отправки — против HOME своего процесса, поэтому агенту в песочнице с другим
  HOME нужен явный абсолютный путь (`/home/<user>/work/bonds`). Таблица и
  pitfall — `finbonds-rebalance-tool` (Prerequisites).
- Нужны: `psql`, `python3` у агента; на хосте — `venv/bin/python`, `psql`.

## How to Run

Чтение и аналитика — канонический путь: CLI на хосте, **без DSN и без psql**
(слой хранения сам читает `.env` на хосте).

Доменные команды ребалансировки (`report` / `plan` / `compare` /
`analysis-snapshot`) агент вызывает только через stable adapter — скилл
`finbonds-rebalance-tool` (030.11): typed whitelist аргументов, subprocess
argv без shell, snapshot как локальный artifact для sandbox; SOP и
capability ladder — `finbonds-rebalance-advisor` (030.12). Прямой
ssh-вызов ниже — общий случай для остальных read-only команд
(НЕ ребалансировочных):

`ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py check-db --freshness --json'`

Машинные форматы для агента (stdout — чистый JSON, логи — только в stderr):
`rebalance-report --format json` (тулкит ребалансировки, 005.1),
`calculate-ytm --mode buy --format json`, `check-db --freshness --json`
(гейт свежести).

psql-путь — **fallback** для ad-hoc SQL вне SOP тулкита (через
`"${FIN_BONDS_PSQL:-psql}"`; карта — ниже):

`"${FIN_BONDS_PSQL:-psql}" -c 'SELECT COUNT(*) FROM bonds_catalog;'`

CLI-команды, пишущие файлы (`export-*`, `generate-plots`), — на хосте:

`ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py check-db'`

## Карта DSN: где берётся DSN (P-A, 005.1)

- **Канонический путь — вообще без DSN.** Команды `rebalance-report`,
  `calculate-ytm`, `check-db` выполняются одной ssh-строкой на хосте: слой
  хранения (`src/storage.py`) сам читает `POSTGRES_DSN` из `.env` на хосте
  (`~/work/bonds/.env`). Агенту не нужны ни psql, ни переменная в командной
  строке.
- **psql-фолбэк:** на хосте сначала `source ~/work/bonds/.env` (или
  `set -a; . ~/work/bonds/.env; set +a`); у агента — `"${FIN_BONDS_PSQL:-psql}"`
  с `PG*` из libpq-окружения или враппером развёртывания. **Голый `psql` без
  кредов не работает**: в свежей shell переменные пусты (локально —
  сокет-ошибка `/var/run/postgresql`, на хосте — `FATAL: role "<user>" does
  not exist`).
- `POSTGRES_DSN` — секрет: не вставлять в команды дословно, не логировать,
  не публиковать.
- **Локальный CLI против прод-БД не поддерживается — решение 017
  (2026-09-30): «только через хост».** С dev-машины прод-БД недоступна по
  сети: postgres слушает только loopback и внутренние bridge-сети хоста, порт 5432
  снаружи закрыт (ufw). Локальный `.env` dev-машины — теневая копия
  SOPS-мастера (`make sops-decrypt` / `make env-push` доставляет его на
  хост), поэтому его `POSTGRES_DSN` валиден с хоста, не локально: локальный
  прогон CLI-команд к БД падает — это **документированная осознанная
  недоступность, не баг** (характерная ошибка: `FATAL: role "fin_bonds"
  does not exist` от локального postgres dev-машины). «Чинить» DSN в
  локальном `.env` для локального использования нельзя: следующий
  `env-push`/`sops-encrypt` унёс бы значение на прод. SSH-туннель
  (`ssh -N -L 5433:127.0.0.1:5432 ${FIN_BONDS_HOST}`) технически возможен,
  но осознанно отклонён: ломает одно-мастеровую схему env и даёт пишущую
  боевую роль на ноутбуке; пересмотреть при появлении реальной потребности
  в локальных запросах (тогда — отдельная read-only роль на проде).

## Как агенту выполнять SQL

Точка входа ad-hoc SQL — скилл `finbonds-sql-recipes`: «нулевой рецепт»
(`$FIN_BONDS_PSQL`), готовые рецепты, карта актуальных колонок таблиц/вью и каталог
готовых `-f`-скриптов; карту схемы этот скилл не дублирует. Ниже — правила
среды, применяются вместе с ним.

psql-путь — fallback (см. «Карта DSN»); в SOP ребалансировки SQL не нужен
вовсе (скилл `finbonds-rebalance-advisor`, тулкит `rebalance-report`).
Три правила среды (002.7, P9.1–P9.3) — применять до первого ad-hoc запроса,
не переоткрывать опытным путём:

1. **psql — только через shell-команду, через `"${FIN_BONDS_PSQL:-psql}"`.**
   Окружение агента может не содержать кредов fin_bonds; `PG*` берёт libpq
   или враппер развёртывания. В песочнице кода агента чтение
   `os.environ["POSTGRES_DSN"]` может падать `KeyError`. psql-вызовы в
   песочницу кода не переносить.
2. **Длинный SQL с кириллическими литералами — через `-f` файл, не inline
   `-c`.** Длинный `psql -c "SELECT ... WHERE name LIKE '%АФК Система%'"`
   триггерит security-сканер терминала (гомоглифы/юникод в кириллице) и
   ломается на вложенных кавычках. Паттерн: написать SQL в файл целиком
   (кавычки без экранирования), затем выполнить файл; параметры запроса —
   через psql-переменные `-v` (рабочий пример — `simulate_swap.sql` с
   `:'buy_isin'`):

   `"${FIN_BONDS_PSQL:-psql}" -v isin='RU000A...' -f /tmp/q_issuer.sql`

   Короткий однострочный SELECT без кириллических литералов допустимо звать
   inline `-c`.
3. **Табличный вывод — через `--csv`.** `psql --csv` печатает строку
   заголовков — значения читаются без чтения исходного SQL (фикс P9.3;
   прежний `-t -A -F'|'` тоже допустим, но столбцы без имён). Гейт свежести
   вызывается только так — контракт статусов v2 в `finbonds-data-refresh`
   (002.1):

   `"${FIN_BONDS_PSQL:-psql}" --csv -f "$FIN_BONDS_SKILLS_DIR/finbonds-data-refresh/scripts/freshness_check.sql"`

## Quick Reference

Классы команд `python3 main.py <cmd>`:

| Класс | Команды |
|---|---|
| Read-only (безопасно) | `check-db` (вкл. `--freshness --json`), `export-bonds`, `export-portfolio`, `export-monitoring`, `generate-plots`, `cleanup-portfolio --dry-run` |
| Аналитика (читает, пишет отчёты) | `calculate-ytm`, `calculate-spread`, `calculate-monthly-profit`, `analyze-liquidity`, `analyze-buy-candidates`, `calculate-duration`, `rebalance-report` (CSV — только с `--csv-out`) |
| Внешний transport | `sync-chatgpt-portfolio` — читает PostgreSQL и перезаписывает только заранее настроенную Google Sheet; БД не меняет |
| Мутирующие БД | `update-bonds`, `update-ratings` (скоупы `--scope portfolio/missing/catalog`, `--isin`, `--limit`, `--delay`), `update-market-prices`, `update-liquidity`, `update-floaters`, `sync-portfolio`, `add-bond`, `set-spread`, `sync-offers-to-calendar`, `check-changes`, `check-offers`, `seed-data`, `cleanup-portfolio`, `link-companies`, `migrate-db`, `import-from-sqlite` |
| Пайплайн обновления (хост) | `ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && bash run_update.sh'` — единственный способ запустить полное обновление; **не через `systemctl`** (системный unit недоступен агенту без интерактивной аутентификации) |
| Деструктивная | `clear-data` — только по явному запросу пользователя |
| Диагностика API | `test-cbr`, `test-moex`, `test-moex-update-logic`, `test-tbank`, `test-smartlab`, `test-parser` |

Таблицы БД: `bonds_catalog`, `portfolio_positions`, `companies` (справочник
эмитентов, связка `bonds_catalog.company_id → companies.id`),
`calculated_coupons`, `risk_history`, `monitoring_checks`,
`rating_history`, `listlevel_history`, `liquidity_history`,
`schema_migrations` (служебная, журнал ранера миграций — ad-hoc не
мутировать); view `v_portfolio_positions_valuation` — каноническая
оценка позиций (`position_value_rub`). Исключение по типам:
`monitoring_checks.metric_value` — TEXT (см. одноимённый раздел ниже).

Источники таблиц (002.6): `rating_history` наполняет `update-ratings`
(шаг 4 `run_update.sh`): найденный рейтинг пишется дважды — в
`monitoring_checks` (`metric_name='credit_rating'`) и в `rating_history`
(код нормализуется к шкале `rating_scale` из конфига; `'N/A'` и коды вне
шкалы — только в `monitoring_checks`). Повторный прогон за тот же день не
плодит дубли (уникальный индекс `isin+rating_date+rating_code`, миграция
012). Ночной прогон покрывает бумаги портфеля; дозаполнение каталога —
скоупы `--scope missing` (последняя проверка `'N/A'` или не было) и
`--scope catalog` (весь живой каталог, разовый бэкфилл), точечно — `--isin`
(работает и вне портфеля); SOP — скилл `finbonds-ratings`. Базовый
кредитный фильтр вне портфеля — по-прежнему `risk_level`.

## Канонические размерности и валюты

Все денежные поля БД хранятся в рублях, без масштабирования:

- `current_price` = **рубли за 1 шт.**
- `market_price` = **рубли за 1 шт.**
- `current_value` = **рубли за всю позицию** (`quantity * current_price`).
- Оценка позиции для аналитики — `position_value_rub` из view
  `v_portfolio_positions_valuation`.
- Цены в JSON тулкита (`price` в `rebalance-report`/`calculate-ytm`) и
  `average_price` позиций — тоже **рубли за бумагу**, не % от номинала.

**Категорический запрет:** агенту строго запрещено делить цены или суммы на
100 или 100 000 (номинал уже в рублях, цены — в рублях за бумагу). Любое
расхождение оценок решается запросом к БД через view, а не ментальным
пересчётом.

## Единицы YTM: проценты годовых (миграция 013, 005.4)

- **Канон: `bonds_catalog.ytm` хранится в ПРОЦЕНТАХ годовых** —
  `24.01` = 24.01%. Единицы унифицированы миграцией
  `013_ytm_percent_units.sql`: до неё колонка хранила долю единицы
  (`0.24` = 24%) и любой потребитель, смешавший SQL-колонку с выводом CLI,
  молча ошибался в 100 раз (ловушка P-E).
- Писатель колонки один — `PortfolioStorage.update_bonds_derived_metrics`;
  cashflow-движок (`src/services/cashflow.py`) внутри считает в долях,
  умножение на 100 — только на границе записи.
- Всё, что печатает CLI и отдаёт JSON тулкита (`ytm_pct`, `*_pct`; флаги
  `--min-ytm`/`--max-ytm`), — проценты. БД и CLI согласованы 1:1 —
  **умножать/делить YTM на 100 при переходе между источниками запрещено**.
- `bonds_catalog.ytm_null_reason` — осознанный NULL YTM:
  `floating_coupon` (флоатер: будущие ставки неизвестны), `perpetual`
  (бессрочная), `amortization_schedule_missing` (амортизатор без графика),
  `market_price_missing_or_nonpositive`. NULL не подменять нулём и не
  «дорассчитывать» упрощёнными формулами.
- У флоатеров `coupon_rate_percent`/текущая ставка — только на текущий
  купон; YTM у них не определена (`ytm_null_reason=floating_coupon`).
- Канонический справочник единиц — `doc/units_conventions.md` в репозитории
  fin_bonds.

## Заполненность полей каталога (bonds_catalog)

Какие поля реально заполнены и чем (002.5, 005.4) — прежде чем строить
ad-hoc SQL:

- `ytm` / `ytm_null_reason` — проценты годовых (см. «Единицы YTM»);
  NULL — всегда с причиной из списка выше.
- `risk_level` (1–5, Т-Банк) и `list_level` (1–3, MOEX) — заполнены;
  связка «риск-уровень + листинг» — канонический прокси кредитного риска:
  рейтинги в БД (`rating_history`) наполняются только по бумагам портфеля
  (002.6), для кандидатов вне портфеля рейтинга нет.
- `is_for_qualified_investors` — флаг бумаги для квалифицированных
  инвесторов (в CLI-флагах жаргонно «ku»: `--include-ku`); тулкит по
  умолчанию КУ-бумаги исключает (`--include-ku` включает).
- `offer_date`, `maturity_date`, `amortization_flag`, `nominal`,
  `perpetual_flag`, `issue_size` — из каталога MOEX; для амортизаторов YTM
  и дюрация ограничены (график амортизации не интегрирован, 002-P8).
- `coupon_type` — **заполнен** с миграции 011: значения `'FIX'`/`'FLOAT'`,
  синхронно с `floating_coupon_flag` (backfill + вывод из флага при записи
  каталога). Фильтр `coupon_type='FLOAT'` согласован с
  `floating_coupon_flag IS TRUE` — доля флоатеров не 0.0%; скрининги
  (`buy_candidates.sql`, `rebalance_export.sql`) по-прежнему считают через
  флаг — оба источника равнозначны.
- `coupon_rate_percent` — каноническая точность **4 знака после запятой**
  (нормализация ROUND(...,4) на всех каналах записи: MOEX-формула, готовый
  COUPONPERCENT, `update-floaters`). Значения длиннее
  (25.00250000000000000000000001) — артефакт до 002.5; на проде чистятся
  миграцией 011.
- `coupon_spread` — только у флоатеров: авто `calculate-spread` /
  `update-floaters`, иначе ручной `set-spread`.
- `market_price` — пишется автоматически (`update-bonds --update-prices`,
  шаг 1 `run_update.sh`, метка `market_price_updated_at`); NULL — только
  если котировок нет. Текущая ставка флоатера живёт в `monitoring_checks`
  (metric_name `floater_coupon_calculator`), не в `coupon_rate_percent`.

## monitoring_checks.metric_value — TEXT: всегда ::numeric в арифметике

`monitoring_checks.metric_value` имеет тип **TEXT** (схема, миграция 001) и
хранит и числовые, и текстовые метрики, поэтому «просто ALTER TYPE numeric»
невозможен — каст упадёт на текстовых строках:

- `floater_coupon_calculator`, `moex_listlevel` — числовые значения строкой
  (`'15.2'`);
- `credit_rating` — текстовые (`'AA-'`, `'N/A'`; коды вне шкалы `rating_scale`
  и `N/A` пишутся только сюда — см. 002.6).

Прямая арифметика/COALESCE с numeric-колонками ломается:
`COALESCE(metric_value, coupon_rate_percent)` →
`ERROR: COALESCE types text and numeric cannot be matched`.

Правила ad-hoc SQL (002.7, P5):

1. Любое использование `metric_value` в арифметике или COALESCE с
   numeric-выражением — только через явный каст `metric_value::numeric`.
2. Числовую метрику доставать каноническим паттерном «последнее значение по
   ISIN» — так делают view `v_portfolio_positions_valuation`,
   `buy_candidates.sql`, `simulate_swap.sql`, `scenario_summary.sql`:

   ```sql
   SELECT DISTINCT ON (isin) isin, metric_value::numeric AS calc_rate
   FROM monitoring_checks
   WHERE metric_name = 'floater_coupon_calculator'
   ORDER BY isin, check_date DESC;
   ```

3. Если выборка не ограничена числовым `metric_name`, фильтровать до каста —
   `'N/A'::numeric` падает: добавить
   `WHERE metric_value ~ '^[0-9]+([.][0-9]+)?$'`.
4. Отдельная числовая колонка `metric_value_num` (опция 002.7) **не введена**:
   без одновременной правки писцов (`storage.add_monitoring_check`) она была
   бы пустой после бэкфилла. Единственный источник — `metric_value`; правило
   `::numeric` из п. 1 обязательно.

## Procedure

1. Определи класс команды по таблице выше. Критерий: не запускаешь ничего из
   «мутирующих» и `clear-data` без явного запроса пользователя в текущем
   диалоге.
2. Для чтения и аналитики используй CLI на хосте через ssh (DSN не нужен;
   машинные форматы — «How to Run»); psql — только для ad-hoc
   SQL вне SOP тулкита. Критерий: команда выполнена там, где есть её
   зависимости, и не упала по правам на read-only mount.
3. При ошибке: `main.py` печатает `DEBUG: Parsed arguments:` в stderr — это
   норма, не баг; логи cron-обновления — `cron.log` в корне проекта на хосте.

## Pitfalls

- Read-only checkout (`FIN_BONDS_ROOT`): команды, пишущие файлы (`export-*`,
  `generate-plots`), на нём упадут — запускай их на хосте.
- Основная база данных — PostgreSQL (`fin_bonds`). Все числовые поля типизированы (numeric/double precision/int), кроме `monitoring_checks.metric_value` — TEXT, в арифметике только через `::numeric` (см. одноимённый раздел).
- Длинный ad-hoc SQL с кириллическими литералами в `psql -c` ломается на security-сканере и кавычках — писать в файл и звать `psql -f` (см. «Как агенту выполнять SQL»).
- YTM в старых логах, выгрузках и заметках (до миграции 013, 2026-09-29) может быть в долях (`0.24` = 24%) — не смешивать с текущими процентами; сомнение разрешает `doc/units_conventions.md`.
- Конфиденциальность: ISIN портфеля, составы позиций и содержимое приватного workspace
  наружу (web, публичные репозитории) не публиковать.
- Регулярное обновление данных уже идёт по таймеру на хосте
  (`fin_bonds.timer`); не запускай `update-*` параллельно ему.

## Verification

- `ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py check-db'` завершается без ошибок.
- `main.py rebalance-report --help` и `main.py calculate-ytm --help`
  показывают флаги, как в скиллах (расхождение = проект обновился — сверься
  с `src/use_cases/factory.py`).
- Список команд в `main.py --help` соответствует карте (расхождение = проект
  обновился — сверься с `src/use_cases/factory.py`).
