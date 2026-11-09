---
name: finbonds-buy-candidates
description: "Скринер кандидатов на покупку и инкремент денежного потока."
version: 0.2.2
author: jedi (jedi108)
license: MIT
platforms: [linux, macos]
metadata:
  tags: "FinBonds, Screener, Buy, Yield, Cashflow"
  related_skills: "finbonds-ops, finbonds-data-refresh, finbonds-rebalance-advisor, finbonds-portfolio-export"
---

# FinBonds Buy Candidates Screener Skill

Находит лучшие доступные для покупки облигации вне текущего портфеля с низким кредитным риском, свежими ценами и рассчитывает прогнозный валовой инкремент купонного денежного потока (+Δ₽/мес, +Δ₽/кв). Только аналитический совет (Advisory only).

## Доходность ≠ купон (issue #3, обязательно)

- «Доходность» — **только** `ytm_pct`, и всегда с горизонтом из `ytm_to`:
  `offer` → «доходность к оферте <offer>», `maturity` → «доходность к погашению».
- `coupon_pct` называть только «купонная ставка» (или «купон»), никогда не «доходность».
- Есть оферта (`offer` / `offer_date`) — указывай её дату.
- `ytm_check: "mismatch"` — приводи оба числа: наша `ytm_pct` и MOEX `moex_yield_pct`,
  с оговоркой «расхождение из-за методики расчёта»; ни одно не подавай как бесспорное.
  `no_moex_data` — MOEX-доходности нет (нет сделок), достаточно `ytm_pct`.
- Пустой `ytm_pct` (см. `ytm_reason`, напр. флоатер) не заменяй купоном.
- Контракт строки (P1.5, `contract_version: 1`): `rating_status` — рейтинг подтверждён
  только при `confirmed`; `proxy_only`/`missing`/`stale` — так и говори, `risk_level` рейтингом
  не называй. Непустой `blockers[]` (`YTM_MISSING`, `PRICE_STALE`) — строку в рекомендацию
  не бери, назови код; `warnings[]` — оговори. Группы ключей — блок `row_contract`.
- Пример (Эмитент В 001Р-01, RU000A10XYC1): «купон 19%, доходность к оферте 23.11.2026 ≈13.7%».

## When to Use

- «Какие лучшие бумаги купить сейчас», «подбери кандидатов на замену спящей позиции».
- Сценарий ротации (swap) в `finbonds-rebalance-advisor`: утилизация кэша от купонов/погашений или продажа неэффективных бумаг.
- Поиск бумаг с ежемесячным купоном (12 раз в год) для максимизации регулярного потока.
- Подбор надежных флоатеров с максимальной текущей ставкой и спредом к RUONIA/КС.

Don't use for: подачи заявок или торговых поручений (проект не подключен к торговле); расчета чистой прибыли с учетом налогов/брокерских комиссий/НКД (этих данных нет в БД).

## Prerequisites

1. **Гейт свежести данных (v2):** перед запуском обязательно проверить свежесть через `finbonds-data-refresh` (psql --csv) или команду на хосте:
   `main.py check-db --freshness --json` (поле `gate_status` — канонический статус гейта v2, согласован с SQL-точкой)
   Статус `WARN_ZOMBIE_ROWS` — данные свежие (`is_fresh=true`), работаем дальше; зомби-строки (погашенные бумаги, цена 0 от брокера) не блокируют скрининг. Если JSON невалиден или `is_fresh=false` (`STALE_*`/`CRITICAL_*`) — сначала обновить данные (`main.py run-update`), не строить торговых рекомендаций на устаревших ценах.
2. Сам SQL-скрипт жестко отсекает цены старше 24 часов (`market_price_updated_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'`).
3. В SOP ребалансировки отдельный вызов гейта не нужен: `rebalance-report`
   (скилл `finbonds-rebalance-advisor`) несёт тот же контракт гейта в блоке
   `meta` (`gate_status`, `is_fresh`, `criticals[]`, `warnings[]` — общая
   точка `classify_freshness`, 005.1).

## Правило ISIN (анти-P-D)

**ISIN кандидата не изобретать и не писать по памяти** — только копировать из
вывода инструмента (CLI/JSON/SQL). Скринер всегда отдаёт связку
«name + issuer + ISIN» в одной строке; перед переносом ISIN в сценарий или
рекомендацию сверяй пару «имя ↔ ISIN» с выводом строка-в-строку. Серии одного
эмитента различаются одной буквой (кейс 2026-09-29: Эмитент А 001Р-02
`RU000A10XYA1` vs Эмитент А 001Р-04 `RU000A10XYA2`; Эмитент Б 002Р-03
`RU000A10XYB1` vs Эмитент Б 002Р-02 `RU000A10XYB2`) — по памяти был записан
неверный ISIN; контрольная сверка с каталогом — крайняя линия обороны,
основная защита — копирование ISIN только из вывода инструмента.

## How to Run

**Правило широкого окна погашения (002.3, обновлено 005.3):** для запросов
«максимизировать доход / лучшая прибыль» первый прогон скрининга делай
**без `--max-maturity`**: после 002.3/P9.5 у `calculate-ytm` и тулкита
верхней границы срока нет по умолчанию (ловушка-дефолт `2030-12-31`
устранена; SQL-вариант `buy_candidates.sql` срок не режет). До исправления
дефолт отсекал Сэтл 002P-04 (погаш. 2030, YTM 24.0%) и РЕСО БО-П-23
(2034, 23.9%) — первый план агента снижал доходность. Явно широкое окно
(`--max-maturity 2036-12-31`) допустимо, но не обязательно; сужение под
предпочтения пользователя — отдельный осознанный второй прогон.

⚠️ **Ограничение срока = ограничение доходности:** топ-фиксы риск-1 на длине
2030+ дают на 5–7 п.п. больше коротких бумаг. Узкое окно погашения — самый
дешёвый способ незаметно ухудшить план.

### Вариант 1: Прямой SQL через psql (быстрый срез)

```bash
# Топ-30 фиксов с низким риском (risk <= 1), без ОФЗ
"${FIN_BONDS_PSQL:-psql}" -v max_risk=1 -v coupon_freq=0 -v coupon_type=FIX -v ofz_mode=exclude -v limit=30 -f "$FIN_BONDS_SKILLS_DIR/finbonds-buy-candidates/scripts/buy_candidates.sql"

# То же, но с докупаемыми позициями портфеля (бейдж held (N%)) — обязательный режим для ребалансировки
"${FIN_BONDS_PSQL:-psql}" -v max_risk=1 -v coupon_freq=0 -v coupon_type=FIX -v ofz_mode=exclude -v include_held=true -v limit=500 -f "$FIN_BONDS_SKILLS_DIR/finbonds-buy-candidates/scripts/buy_candidates.sql"

# Режим ежемесячного купона (12 выплат в год)
"${FIN_BONDS_PSQL:-psql}" -v max_risk=1 -v coupon_freq=12 -v coupon_type=FIX -v ofz_mode=exclude -v limit=30 -f "$FIN_BONDS_SKILLS_DIR/finbonds-buy-candidates/scripts/buy_candidates.sql"

# Режим флоатеров (топ по расчетной ставке и спреду)
"${FIN_BONDS_PSQL:-psql}" -v max_risk=1 -v coupon_freq=0 -v coupon_type=FLOAT -v ofz_mode=all -v limit=20 -f "$FIN_BONDS_SKILLS_DIR/finbonds-buy-candidates/scripts/buy_candidates.sql"
```

Параметры `psql`:
- `max_risk`: `1` (по умолчанию, низкий риск) или `2`.
- `coupon_freq`: `0` (все), `12` (ежемесячные), `4` (квартальные), `2` (полугодовые).
- `coupon_type`: `'ALL'`, `'FIX'`, `'FLOAT'`.
- `ofz_mode`: `'all'`, `'only'` (только ОФЗ), `'exclude'` (без суверенных: ОФЗ и евро-РФ — `companies.entity_type='sovereign'`; эквивалент CLI `--exclude-sovereign`).
- `limit`: лимит строк (по умолчанию 30). При `include_held=true` поднимать до 500: бумаги без доходности уходят в хвост сортировки (`DESC NULLS LAST`) и при дефолтном лимите не попадают в выдачу вместе с докупаемыми позициями.
- `include_held`: `false` (по умолчанию — позиции портфеля исключены, совместимость прежнего вывода; в конце строки две новые колонки `held_share_pct`, `held_badge` — в этом режиме всегда NULL) или `true` (докупаемые позиции портфеля остаются в выдаче с бейджем `held (доля N%)`; доля агрегируется по ISIN через `v_portfolio_positions_valuation`, задвоения для ISIN на двух счетах нет). ⚠️ `-v include_held=true` относится только к psql-запуску скрипта: для CLI `analyze-buy-candidates` / `calculate-ytm` используется флаг `--include-held` (argparse не примет `-v`).

### Вариант 2: CLI приложения на хосте

```bash
# Топ корпоративных бумаг с ежемесячным купоном
ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py analyze-buy-candidates top --monthly --exclude-ofz --max-risk 1 --limit 20'

# То же с докупаемыми позициями портфеля (бейдж held (N%))
ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py analyze-buy-candidates top --monthly --exclude-ofz --max-risk 1 --limit 20 --include-held'

# Топ флоатеров
ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py analyze-buy-candidates floaters --max-risk 1 --limit 15'

# Скрининг calculate-ytm с докупаемыми позициями (режим buy);
# окно не режем: --max-maturity в первом прогоне не нужен (дефолт открыт)
ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py calculate-ytm --mode buy --max-risk 1 --include-held --limit 50'

# Машинный JSON для агента + частотный фильтр «квартальные и реже» +
# без суверенных (005.3): stdout — чистый JSON, логи — в stderr
ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py calculate-ytm --mode buy --format json --coupon-freq-max 4 --exclude-sovereign --max-risk 1 --include-held --limit 50'
```

CLI-эквиваленты того же контракта: `analyze-buy-candidates top|floaters|compare --include-held` и `calculate-ytm --mode buy --include-held`. По умолчанию (без флага) поведение прежнее — позиции портфеля исключены.

Частотные фильтры (005.3/P-C): `--coupon-freq-max N` (4 = квартальные и реже)
или `--coupon-freq-in 2,4` (только указанные частоты); взаимоисключающие,
бумаги с неизвестной частотой (NULL) при активном фильтре не проходят. В
тулките `rebalance-report` те же фильтры называются `--freq-max`/`--freq-in`.

«Не ОФЗ» → `--exclude-sovereign` (005.3/P-G): флаг исключает суверенных
эмитентов — ОФЗ и евробонды РФ (`companies.entity_type='sovereign'`, фолбэк —
имя «ОФЗ…»). Не вычёркивать ОФЗ из топа вручную; в `buy_candidates.sql` то же
даёт `ofz_mode=exclude`.

### Вариант 3: скринер в тулките rebalance-report (JSON, 005.1)

Для SOP ребалансировки скринер встроен в `rebalance-report` — блок `screener`
(`fixed[]`/`floater[]`/`issuers[]` + воронка отсечений `excluded`) с теми же
конвенциями фильтров (`--freq-max`/`--freq-in`, `--exclude-sovereign`,
`--include-held`, `--include-ku`, `--max-risk`, `--max-listlevel`,
`--max-ytm`, `--min-maturity`/`--max-maturity`, `--screener-limit`):

`ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py rebalance-report --format json --freq-max 4 --exclude-sovereign --include-held --max-risk 1'`

Полный SOP — скилл `finbonds-rebalance-advisor` (скринер приходит в одном
JSON вместе со свежестью, портфелем, концентрациями и погашениями).

### `analyze-buy-candidates compare` и `compare-external` (issue #5)

- `analyze-buy-candidates compare` — **справочная разница YTM** между бумагами;
  вердикта «покупать/не покупать» нет и быть не должно. Флоатеры исключены
  (их `ytm_pct` пуст). Не превращай разницу в рекомендацию.
- `compare-external --file <json|csv> [--format json|table] [--min-credit-rating A+] [--max-source-skew-days 3]` —
  безопасное сравнение внешней подборки с каталогом и портфелем (advisory,
  заявок не создаёт):
  - внешний список — **только по полному ISIN** (fuzzy-match по имени нет);
    ISIN копируй из вывода, не по памяти;
  - строка `decision.eligibility` ∈ `comparable | not_comparable | blocked`;
    **никогда не подавай внешнюю строку как рекомендацию к покупке**, даже
    `comparable` — это лишь «сопоставимо»;
  - `blocked` и `not_comparable` строки обязательно показывай с кодами из
    `blockers[]`/`warnings[]` (напр. `PRICE_STALE`, `YTM_NOT_COMPARABLE`,
    `RATING_MISSING`, `ISSUER_LIMIT`, `DATA_GATE_BLOCKED`); не скрывай и не
    смягчай;
  - `credit.rating_status` `proxy_only` / `missing` (и `stale`, `issuer_only`,
    `conflicting`) — это НЕ подтверждённый рейтинг; `risk_level`/`list_level`
    рейтингом не считать;
  - флоатер: `ytm_pct = null` (`ytm_reason=floating_coupon`) — не заменять
    купоном и не сравнивать с FIX; свой рейтинг `rankings.float`;
  - `context_fingerprint` в ответе — привязка к `PlanningContext`; строки с
    `plan_input_allowed=false` в сценарий не передавать.

## Financial Model & Cashflow Increment

При ротации (продажа бумаги $A$ на сумму $V$ руб. $\rightarrow$ покупка кандидата $B$):

1. **Текущий средний месячный купонный поток позиции $A$:**
   $$\text{Income}_A = \frac{\text{nominal}_A \cdot \text{coupon\_rate}_A}{100 \cdot 12} \cdot \text{quantity}_A$$
2. **Прогнозный средний месячный поток от покупки $B$ на капитал $V$:**
   $$\text{Quantity}_B = \left\lfloor \frac{V}{\text{market\_price}_B} \right\rfloor$$
   $$\text{Income}_B = \frac{\text{nominal}_B \cdot \text{coupon\_rate}_B}{100 \cdot 12} \cdot \text{Quantity}_B$$
3. **Инкремент денежного потока (Profit Delta):**
   - **В месяц:** $\Delta \text{Income}_{\text{month}} = \text{Income}_B - \text{Income}_A$
   - **За 3 месяца (среднее):** $\Delta \text{Income}_{3m} = \Delta \text{Income}_{\text{month}} \times 3$
   - *Примечание:* расчет валовой (до НКД, комиссий брокера и НДФЛ).

4. **Проверка лимита концентрации на эмитента:**
   Перед рекомендацией ротации обязательно рассчитать долю эмитента после сделки:
   $$\text{issuer\_after} = \frac{\text{issuer\_before} - \text{sell\_value\_if\_same} + \text{buy\_value}}{\text{total\_portfolio\_value}}$$
   Если $\text{issuer\_after} > \text{limit}$ (например, 15%) — сделка отклоняется по риску концентрации. Если `company_id IS NULL`, бумага исключается из рекомендаций до ручной привязки. В тулките (`rebalance-report --scenario`) доли и лимит по сумме всех ног считает тулкит (`scenario.issuer_limits`) — ручная арифметика не нужна (анти-P6); формула выше — для понимания модели, не для счёта.

## Pitfalls & Risk Rules

- **ISIN не изобретать (анти-P-D):** правило и ловушка «серии одной буквой» —
  в разделе «Правило ISIN» выше; ISIN только копированием из вывода
  инструмента, со сверкой «имя ↔ ISIN».
- **«Не ОФЗ» → `--exclude-sovereign` / `ofz_mode=exclude` (P-G):** ОФЗ
  (262xx, 290xx) и Russia-евробонды присутствуют в топах штатно — не
  вычёркивать вручную, фильтровать флагом.
- **Докупки портфеля (002.2):** лучшая докупка может уже лежать в портфеле с запасом до лимита концентрации (кейс ГТЛК БО 002P-04: доля 3.3% при лимите 15%, купон 25% фикс — была невидима дефолтному скринеру). Для ребалансировки всегда прогонять скрининг с `include_held=true` / `--include-held`; бейдж `held (N%)` — текущая доля бумаги в портфеле «сейчас», сверять её с лимитом эмитента перед рекомендацией докупки.
- **Рейтинговый фильтр (002.6):** `rating_history` наполняет `update-ratings` (адаптер `credit_rating`, ночной `run_update.sh`) **только по бумагам портфеля**; `'N/A'` и коды вне шкалы `rating_scale` не пишутся. Для кандидатов **вне портфеля** рейтинга в БД нет — базовый кредитный фильтр `risk_level <= 1` (Т-Банк), а `rating` из CTE `latest_ratings` — справочное поле (для них будет NULL). Рейтинги по всему каталогу — отдельное решение (дорогой внешний API), на него не рассчитывать.
- **Разделение FIX и FLOAT:** текущая ставка флоатера рассчитывается из КС/RUONIA и спреда, но не гарантирована на весь срок. Не маркировать текущую купонную доходность как доходность к погашению (YTM).
- **Ликвидность выпуска:** отбираются только выпуски объемом от 500 млн руб. (`issue_size * nominal >= 500000000`), доступные к торгам (`is_trade_available IS TRUE`) и без вечных бумаг (`perpetual_flag IS NOT TRUE`).
- **Свежесть цен:** цены за 1 бумагу в рублях должны быть не старше 24 часов.
