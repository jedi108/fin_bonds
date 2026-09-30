# Контракт Google Sheet для ChatGPT (PORTFOLIO + CONTROL)

Публичный контракт листа, в который команда `sync-chatgpt-portfolio`
(`src/use_cases/sync_chatgpt_portfolio.py`) публикует снапшот текущего
портфеля для чтения внешним агентом (ChatGPT). Документ не содержит данных
портфеля — только формат.

## Protocol чтения

1. Читай `CONTROL` (пары `key`/`value`).
2. `PORTFOLIO` можно использовать **только при** `CONTROL.sync_status = READY`.
   Значение `WRITING` означает, что запись идёт — данные частичные.
3. Сверь число строк данных `PORTFOLIO` (без заголовка) с
   `CONTROL.positions_count` — publisher делает ту же проверку при записи.
4. `CONTROL.snapshot_id` (`YYYYMMDDThhmmssZ`, UTC) и `generated_at`
   идентифицируют момент снапшота; `portfolio_updated_at` / `market_updated_at`
   — свежесть исходных данных в БД.

## CONTROL — ключи

| Ключ | Смысл |
|---|---|
| `sync_status` | `WRITING` до записи, `READY` после успешной записи и проверки (commit-marker) |
| `schema_version` | Версия контракта листа (см. ниже) |
| `snapshot_id` | Идентификатор снапшота (`YYYYMMDDThhmmssZ`) |
| `generated_at` | ISO-время генерации |
| `gate_status` | Результат freshness-гейта (`OK`/`WARN`/`BLOCKED`); экспорт блокируется при нехороших данных |
| `is_fresh` | Всегда `true` — не прошедшие гейт экспорты не публикуются |
| `positions_count` | Число строк данных в `PORTFOLIO` |
| `portfolio_value_rub` | Суммарная оценка портфеля, рубли |
| `portfolio_updated_at` | Время последнего обновления позиций портфеля |
| `market_updated_at` | Время последнего обновления рыночных цен |
| `warnings` | JSON-список предупреждений гейта или пусто |

## PORTFOLIO — колонки (schema_version = 2)

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

## История версий

- **1** — первичный формат (позиции + CONTROL-гейт).
- **2** — один релиз, две порции изменений:
  - добавлены `ytm_reason` (после `ytm_pct`) и `duration_reason`
    (после `modified_duration`): причины осознанных NULL рядом со значениями;
  - добавлены `valuation_source` (после `value_rub`) и `valuation_warning`
    (после `valuation_source`): происхождение canonical valuation.
