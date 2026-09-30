# Контракт Google Sheet для ChatGPT (CONTROL + SCHEMA + PORTFOLIO + CANDIDATES)

Публичный контракт таблицы, в которую команда `sync-chatgpt-portfolio`
(`src/use_cases/sync_chatgpt_portfolio.py`) публикует снапшот текущего
портфеля и кандидатного контекста для ревью ребалансировки внешним агентом
(ChatGPT). Документ не содержит данных портфеля — только формат.

Рабочие листы (schema_version = 4): `CONTROL`, `SCHEMA`, `PORTFOLIO`,
`CANDIDATES`. Других листов нет.

Runtime source-of-truth для потребителя — сам лист `SCHEMA` в таблице: он
генерируется кодом из versioned definitions рядом с headers
(`build_schema_rows()` в `src/use_cases/sync_chatgpt_portfolio.py`) и
записывается при каждом sync. Этот документ — зеркало того же контракта.

## Protocol чтения

1. Читай `CONTROL` (пары `key`/`value`) первым.
2. Данные (`PORTFOLIO`, `CANDIDATES`) можно использовать **только при**
   `CONTROL.sync_status = READY`. Значение `WRITING` означает, что запись
   идёт — данные частичные.
3. Перед интерпретацией `PORTFOLIO` или `CANDIDATES` прочитай `SCHEMA`
   (`CONTROL.contract_sheet = SCHEMA`) — он описывает все колонки и единицы.
4. Сверь число строк данных `PORTFOLIO` (без заголовка) с
   `CONTROL.positions_count` и число строк данных `CANDIDATES` с
   `CONTROL.candidates_count` — publisher делает те же проверки при записи.
5. `CONTROL.snapshot_id` (`YYYYMMDDThhmmssZ`, UTC) и `generated_at`
   идентифицируют момент снапшота; `portfolio_updated_at` /
   `market_updated_at` — свежесть исходных данных в БД.
6. Инвестиционный капитал = `portfolio_value_rub + cash_available_rub`
   (`investable_total_rub`). Пустые `cash_available_rub` /
   `investable_total_rub` / `cash_updated_at` означают «cash неизвестен»
   (sync ещё не выполнялся или последний sync не удался) — это НЕ ноль:
   не считать свободные деньги нулём и не предполагать их отсутствие.

## CONTROL — ключи (schema_version = 4)

| Ключ | Смысл |
|---|---|
| `sync_status` | `WRITING` до записи, `READY` после успешной записи обеих таблиц данных и проверок числа строк (commit-marker) |
| `schema_version` | Версия контракта таблицы (см. ниже) |
| `snapshot_id` | Идентификатор снапшота (`YYYYMMDDThhmmssZ`) |
| `generated_at` | ISO-время генерации |
| `gate_status` | Результат freshness-гейта (`OK`/`WARN`/`BLOCKED`); экспорт блокируется при нехороших данных |
| `is_fresh` | Всегда `true` — не прошедшие гейт экспорты не публикуются |
| `positions_count` | Число строк данных в `PORTFOLIO` |
| `portfolio_value_rub` | Стоимость облигационных позиций (сумма `PORTFOLIO.value_rub`); **не включает** cash |
| `cash_available_rub` | Свободный доступный RUB по настроенным брокерским счетам из канонического снапшота (пишет только `sync-portfolio`). Пустая ячейка — cash неизвестен (sync ещё не выполнялся или последний sync не удался); неизвестность **не равна** нулю |
| `investable_total_rub` | `portfolio_value_rub + cash_available_rub` — облигации плюс свободный cash (инвестиционный капитал); пусто, если cash неизвестен |
| `cash_updated_at` | Время последнего успешного cash sync; пусто, если cash неизвестен. Устаревшее значение — cash мог устареть (неудачный sync не затирает снапшот) |
| `portfolio_updated_at` | Время последнего обновления позиций портфеля |
| `market_updated_at` | Время последнего обновления рыночных цен (по timestamp записи в БД — см. «Ограничение свежести») |
| `warnings` | JSON-список предупреждений гейта или пусто |
| `candidates_count` | Число строк данных в `CANDIDATES` |
| `candidate_meta` | JSON metadata кандидатного набора из канонического скринера, без пересчёта: `filters`, `candidates_total`, `fixed_matched`, `floater_matched`, `excluded` |
| `contract_sheet` | Имя листа с описанием всех полей: `SCHEMA` |
| `consumer_rule` | Короткий машинный контракт потребителя (совпадает с «Protocol чтения» выше + правило «пустые cash-поля = неизвестность, не ноль») |

Cash-поля (`cash_available_rub` / `investable_total_rub` / `cash_updated_at`)
принадлежат тому же snapshot'у, что и позиции: они публикуются в том же
атомарном протоколе и читаются только при `sync_status = READY`.

## SCHEMA — самодокументация контракта

Лист с колонками `sheet` / `field` / `type_or_unit` / `description`: по строке
на каждое поле `CONTROL`, `PORTFOLIO` и `CANDIDATES`. Единицы указаны явно:
`RUB`, `percent`, `percent_per_year`, `years`, `count`, `ISO date`
(а также `ISO datetime`, `boolean`, `string`, `JSON`).

- Генерируется кодом и записывается при каждом sync сразу после
  `WRITING`-маркера (в рамках версии definitions статичны, пишутся поверх).
- Семантика пустых значений: пустая ячейка значения — осознанный NULL из
  canonical-модели, причина лежит в соседней колонке `*_reason` (см. ниже).

## PORTFOLIO — колонки (schema_version = 4)

| Колонка | Единицы / формат | Смысл |
|---|---|---|
| `isin` | строка | Идентификатор выпуска; одна строка на ISIN |
| `name` | строка | Название бумаги |
| `ticker` | строка | Тикер |
| `issuer` | строка | Эмитент (компания из справочника; фолбэк — название бумаги) |
| `quantity` | шт | Количество бумаг в позиции |
| `price_rub` | рубли за бумагу | Цена позиции (средневзвешенная по сделкам или рыночная) |
| `value_rub` | рубли | Оценка позиции |
| `valuation_source` | строка | Какая ветка canonical valuation дала `value_rub` (см. ниже) |
| `valuation_warning` | строка или пусто | Предупреждение для неоднозначных веток оценки; пусто для остальных (см. ниже) |
| `share_pct` | % (доля × 100) | Доля позиции в портфеле |
| `issuer_pct` | % (доля × 100) | Доля эмитента в портфеле |
| `ytm_pct` | % годовых | Доходность к погашению; пусто, если не рассчитана — см. `ytm_reason` |
| `ytm_reason` | строка или пусто | Причина пустого `ytm_pct` (см. ниже); пусто, если YTM рассчитана |
| `coupon_pct` | % годовых | Ставка купона |
| `coupon_frequency` | раз в год | Частота купонов |
| `coupon_type` | `FIX` / `FLOAT` | Тип купона |
| `maturity_date` | ISO-дата | Погашение |
| `offer_date` | ISO-дата или пусто | Оферта |
| `amortization_flag` | TRUE/FALSE | Есть амортизация номинала |
| `credit_rating` | строка или пусто | Последний кредитный рейтинг |
| `risk_level` | целое или пусто | Уровень риска (Tinkoff) |
| `list_level` | целое или пусто | Уровень листинга MOEX |
| `duration` | годы | Дюрация Маколея; пусто — см. `duration_reason` |
| `modified_duration` | годы | Модифицированная дюрация; пусто — см. `duration_reason` |
| `duration_reason` | строка или пусто | Причина пустых `duration`/`modified_duration` |
| `liquidity_loss_pct` | % (0–100) | Оценка потерь при выходе из позиции по рынку (взвешенная по позиции); имя унаследовано, в БД колонка называется `liquidity_loss_ratio`, но хранит проценты |

## CANDIDATES — колонки (schema_version = 4)

Кандидаты buy-выборки канонического скринера rebalance-report:
`screener.fixed[]` и `screener.floater[]` идут в один набор на одном листе,
тип различается `candidate_type`. Экспортер не делает дополнительных
финансовых расчётов — все значения переносятся из канонического отчёта.
Максимум строк определяется canonical screener limit
(`candidate_meta.filters`); перед записью лист очищается полностью, поэтому
shrink не оставляет хвостов предыдущего снапшота.

| Колонка | Единицы / формат | Смысл |
|---|---|---|
| `candidate_type` | `FIX` / `FLOAT` | Группа кандидата (фикс или флоатер) |
| `rank` | целое, с 1 | Ранг внутри `candidate_type`; порядок сохраняет ranking канонического скринера |
| `isin` | строка | Идентификатор выпуска |
| `name` | строка | Название бумаги |
| `ticker` | строка | Тикер |
| `issuer` | строка | Эмитент |
| `price_rub` | рубли за бумагу | Свежая рыночная цена из buy-выборки (018: требуется реальная свежая `market_price`, fallback на nominal убран) |
| `ytm_pct` | % годовых или пусто | Доходность к погашению; пусто — см. `ytm_reason` |
| `ytm_reason` | строка или пусто | Причина пустого `ytm_pct` (например `floating_coupon`); пусто, если YTM рассчитана |
| `coupon_pct` | % годовых или пусто | Ставка купона |
| `coupon_frequency` | раз в год | Частота купонов |
| `maturity_date` | ISO-дата | Погашение |
| `offer_date` | ISO-дата или пусто | Оферта |
| `amortization_flag` | TRUE/FALSE | Есть амортизация номинала |
| `credit_rating` | строка или пусто | Последний canonical кредитный рейтинг кандидата (latest `rating_history` по ISIN, перенесён из строки канонического скринера); пусто — строки рейтинга нет |
| `risk_level` | целое | Уровень риска (Tinkoff); отдельная шкала, **не** заменяет `credit_rating` |
| `list_level` | целое | Уровень листинга MOEX |
| `ku` | TRUE/FALSE | Бумага доступна только квалифицированным инвесторам |
| `issuer_pct_current` | % (доля × 100) | Текущая доля эмитента в портфеле **до** гипотетической покупки (сам кандидат в ней не учтён) |
| `held_badge` | строка или пусто | Непустой, если кандидат уже держится в портфеле (контекст `include_held=True`); формат `held (N%)` |

Кандидаты — не портфель: у них нет qty/value/duration/liquidity (покрытие вне
портфеля неполное, этих полей нет в каноническом скринере); рейтинг с 025
есть — canonical `credit_rating` из строки скринера (023).

## Семантика пустых значений и причин NULL

Пустая ячейка значения (`ytm_pct`, `duration`, `modified_duration`) — это
осознанный NULL из canonical-модели: причина лежит в соседней колонке
`*_reason`. Экспортер **не вычисляет** метрики и причины сам — он переносит
их из БД (`bonds_catalog.ytm_null_reason`, `bonds_catalog.duration_null_reason`)
и enrichment-движка (`src/services/cashflow.py`).

Канонические причины (одинаковые для YTM и дюрации, если применимо):

| Причина | Значение |
|---|---|
| `perpetual` | Вечная облигация — погашения нет |
| `floating_coupon` | Флоатер: без известного графика купонов YTM/дюрация не считаются |
| `amortization_schedule_missing` | Нет локального графика амортизации |
| `coupon_frequency_missing` | Неизвестна частота купонов |
| `maturity_missing_or_past` | Дата погашения отсутствует или в прошлом |
| `market_price_missing_or_nonpositive` | Нет рыночной цены или цена ≤ 0 |
| `nominal_missing_or_nonpositive` | Номинал отсутствует или ≤ 0 |
| `coupon_rate_missing_or_negative` | Ставка купона отсутствует или отрицательная |
| `solver_no_convergence` | Решатель не сошёлся |

Особые случаи:

- `not_applicable` в `ytm_reason` — расчёт YTM для бумаги ещё не запускался
  и причина не сохранена (временный fallback до общего backfill).
- Пустая `ytm_reason` при пустом `ytm_pct` — не ожидается при штатной работе;
  трактовать как «причина неизвестна».
- Пустая `duration_reason` при пустых `duration`/`modified_duration` —
  расчёт дюрации для бумаги ещё не запускался (в БД `duration_updated_at`
  IS NULL и причина не сохранена).

## Происхождение оценки (valuation_source / valuation_warning)

`valuation_source` переносится из БД (view `v_portfolio_positions_valuation`)
тем же CASE, который вычисляет `position_value_rub` — это не восстановление
постфактум из чисел. Каноническая цепочка оценки позиции и соответствующие
значения:

| `valuation_source` | Ветка оценки | `price_rub` / `value_rub` |
|---|---|---|
| `broker_value` | `current_value` от брокера | обычный случай: `price_rub` согласован с `value_rub / quantity` |
| `broker_price` | `quantity × current_price` | обычный случай |
| `market_price` | `quantity × market_price` каталога | обычный случай |
| `nominal_fallback` | `quantity × nominal` (только непогашенная бумага) | `price_rub` может быть пустым или 0 и **не обязан** равняться `value_rub / quantity` — см. `valuation_warning` |
| `zero` | оценки нет | `value_rub = 0` |

Правила для потребителя:

- `valuation_warning` непустая **только** для `nominal_fallback` и `zero`;
  для остальных веток ячейка пустая. Тексты зафиксированы, машиночитаемы:
  - `nominal_fallback` →
    `value_rub = quantity * nominal; price_rub may be empty/0 and differ from value_rub/quantity`
  - `zero` → `no valuation available; value_rub = 0`
- `valuation_warning` не дублирует другие колонки — читай его как флаг
  «`price_rub` не выводится из `value_rub` обычным делением».
- Одна строка `PORTFOLIO` агрегирует позиции одного ISIN на нескольких
  счетах/у брокеров: `value_rub`/`quantity` — суммы, а `valuation_source` —
  источник позиции с наибольшим `position_value_rub` внутри ISIN.
- `price_rub` — цена на бумагу: средневзвешенная по счетам `current_price`
  (SUM(quantity × current_price) / SUM(quantity)), при отсутствии котировок —
  `market_price` каталога. Округлённый брокерский `current_value` может давать
  расхождения копеек с `value_rub / quantity` — они не являются ошибкой.

## Ограничение свежести рыночных цен

`market_updated_at` и freshness-гейт считают свежесть рыночной цены по
timestamp записи в БД (`market_price_updated_at`): timestamp сделки/источника
котировки не хранится. «Свежая цена» означает «недавно записана в БД», а не
«недавно торговалась».

## История версий

- **1** — первичный формат (позиции + CONTROL-гейт).
- **2** — один релиз, две порции изменений:
  - добавлены `ytm_reason` (после `ytm_pct`) и `duration_reason`
    (после `modified_duration`): причины осознанных NULL рядом со значениями;
  - добавлены `valuation_source` (после `value_rub`) и `valuation_warning`
    (после `valuation_source`): происхождение canonical valuation.
- **3** — кандидатный контекст и самодокументация:
  - добавлен лист `SCHEMA` (все поля CONTROL/PORTFOLIO/CANDIDATES с единицами,
    генерируется кодом рядом с headers);
  - добавлен лист `CANDIDATES` (кандидаты buy-выборки канонического скринера
    из программного API 019, FIX+FLOAT одним набором, rank с 1 внутри типа);
  - в `CONTROL` добавлены `candidates_count`, `candidate_meta`,
    `contract_sheet`, `consumer_rule`;
  - атомарный протокол записи расширен на обе таблицы данных:
    WRITING → SCHEMA → PORTFOLIO → CANDIDATES → verify обеих → READY.
- **4** — cash контекст и рейтинг кандидатов (025):
  - в `CONTROL` добавлены `cash_available_rub`, `investable_total_rub`,
    `cash_updated_at` — cash канонического снапшота (022) в том же snapshot;
    пустая ячейка = неизвестность, не ноль; `portfolio_value_rub` уточнён:
    это стоимость облигационных позиций, без cash;
  - в `CANDIDATES` добавлена колонка `credit_rating` — последний canonical
    рейтинг из строки канонического скринера (023); `risk_level` явно помечен
    как отдельная шкала Tinkoff, не замена рейтинга.
