# Конвенции единиц измерения (fin_bonds)

Канонический справочник единиц полей `bonds_catalog` и вывода CLI. Задача 005.4
(P-E) устранила тихую ловушку «доля vs проценты» в `bonds_catalog.ytm` миграцией
`013_ytm_percent_units.sql`: единицы унифицированы в процентах, конвенция
закреплена документно (этот файл), комментарием на колонке (`COMMENT ON
COLUMN bonds_catalog.ytm`) и в докстрингах кода.

## bonds_catalog.ytm — проценты годовых

**Канон: `bonds_catalog.ytm` хранится в ПРОЦЕНТАХ годовых (24.01 = 24.01%).**

- До миграции 013 колонка хранила долю единицы (0.24 = 24%) — любой
  потребитель, смешавший SQL-колонку с выводом CLI, молча ошибался в 100 раз.
- Писатель колонки один — `PortfolioStorage.update_bonds_derived_metrics`
  (вызывается из `add_bonds_to_catalog`, `save_market_price`,
  `update_market_prices`, команды `update-bonds
  --recalculate-derived-metrics`). Движок
  `src/services/cashflow.py:calculate_bond_cashflow_metrics` внутри считает
  в ДОЛЯХ (внутренний контракт решателя) — писатель умножает на 100 на
  границе записи.
- Читатели (`calculate-ytm`, `rebalance-report` через
  `CalculateYtmUseCase._enrich_bonds_with_ytm`, `export-portfolio --format md`)
  берут значение как есть, без конвертации.

## Сопутствующие поля каталога

| Поле / значение | Единицы | Пример |
|---|---|---|
| `bonds_catalog.ytm` | проценты годовых | `24.01` = 24.01% |
| `bonds_catalog.ytm_null_reason` | строка-причина осознанного NULL | `floating_coupon` |
| `bonds_catalog.coupon_rate_percent` | проценты годовых | `12.5` = 12.5% |
| `bonds_catalog.market_price` | рубли за бумагу (абсолютная clean-цена) | `950.0` при номинале 1000 ₽ |
| `portfolio_positions.average_price` / `current_price` | рубли за бумагу | `980.0` |

## Вывод CLI и JSON тулкита

- Всё, что печатает CLI, — **проценты** (ключи с суффиксом `_pct`:
  `ytm_pct`, `min_ytm_pct`, `max_ytm_pct`); флаги `--min-ytm`/`--max-ytm`
  задаются тоже в процентах.
- Каноническая YTM в тулките (`rebalance-report`, `calculate-ytm
  --format json`) считается ТОЛЬКО расчётным движком
  (`CalculateYtmUseCase._enrich_bonds_with_ytm`) в одном формате —
  проценты; двусмысленность единиц не покидает пределы кода.
- Бейдж «⚠️ ВДО/Дистресс» — YTM выше порога `--max-ytm` (по умолчанию 35%).

## История

- До 013 (задача 005.4): `bonds_catalog.ytm` — доля (0.24 = 24%),
  CLI — проценты; ловушка задокументирована в роадмапе 005 (P-E) и
  постмортемах 001/002 как «молчание» при смешении источников.
- Конвенция дублируется в ops-скилле `finbonds-ops` (задача 005.5) — см.
  роадмап 005.

## Связанные документы

- [SQL примеры для анализа YTM](ytm_sql_examples.md)
- Миграция: `migrations/postgres/013_ytm_percent_units.sql`
