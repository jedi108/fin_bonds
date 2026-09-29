# Команда: `export-portfolio`

## Описание
Команда для экспорта данных портфеля облигаций из локальной базы данных PostgreSQL в различные форматы: Markdown (`md`), Excel (`xlsx`), CSV (`csv`), вывод в консоль (`console`) и Google Таблицы (`gsheets`).

Формат `md` формирует готовый отчет с позициями, итогами, честной YTM из каталога и тремя метками свежести данных, не требуя сетевых обращений к брокерскому API.

## Синтаксис
```bash
python3 main.py export-portfolio --format <формат> [опции]
```

## Параметры

| Параметр | Тип | Обязательный | Описание |
|----------|-----|--------------|----------|
| `--format` | string | Да | Формат вывода: `md`, `xlsx`, `csv`, `console`, `gsheets` |
| `--output` | string | Нет | Путь к выходному файлу (поддерживается для формата `md`; по умолчанию `_output_/portfolio_export_%Y%m%d_%H%M%S.md`) |
| `--with-isin` | flag | Нет | Включить колонку ISIN в вывод Markdown (по умолчанию выключено для защиты приватности) |

> [!NOTE]
> Параметры `--output` и `--with-isin` действуют при `--format md`. Существующие форматы `csv` и `xlsx` автоматически сохраняют результат в каталог `_output_/` с меткой времени.

## Форматы экспорта

### 1. Markdown (`--format md`)
Канонический формат для отчетов, взаимодействия с LLM и внешними сервисами.
- **Штамп свежести данных**: включает три scoped-метки (только для ISIN текущего портфеля):
  - «Позиции актуальны на» (`MAX(portfolio_positions.updated_at)`)
  - «Каталожные рыночные данные актуальны на» (`MAX(bonds_catalog.market_price_updated_at)`)
  - «YTM рассчитана на» (`MAX(bonds_catalog.ytm_updated_at)`)
- **Таблица**: `| Название | Цена | Стоимость, ₽ | Доля, % | YTM, % |`
  - При указании флага `--with-isin` первой колонкой добавляется `ISIN`: `| ISIN | Название | ...`
  - Доли `Доля, %` рассчитываются от общей стоимости портфеля (`100 * value / total_value`)
  - Честная YTM берется из `bonds_catalog.ytm` (брокерский `yield_to_maturity` в рублях игнорируется)
  - Итоговая строка: количество позиций, общая стоимость, 100% доля
- **Автономность**: работает полностью локально без сетевых вызовов к TBank API.

### 2. Консоль (`--format console`)
Вывод классифицированных по категориям (ОФЗ, Корп. облигации, Фонды, Акции, Прочее) таблиц прямо в терминал.

### 3. Excel (`--format xlsx`)
Сохранение в файл `_output_/portfolio_export_%Y%m%d_%H%M%S.xlsx` с отдельными листами по категориям активов.

### 4. CSV (`--format csv`)
Сохранение всех позиций с указанием категории в единый CSV-файл `_output_/portfolio_export_%Y%m%d_%H%M%S.csv`.

### 5. Google Sheets (`--format gsheets`)
Синхронизация с Google Таблицей, заданной в `config.yaml` (`google_sheet_name`), через сервисный аккаунт (`gcreds.json`).

## Примеры использования

### Экспорт в Markdown
```bash
# Базовый экспорт в каталог _output_/ (без ISIN)
python3 main.py export-portfolio --format md

# Экспорт в конкретный файл с включением ISIN
python3 main.py export-portfolio --format md --output _output_/my_portfolio.md --with-isin
```

### Просмотр в консоли
```bash
python3 main.py export-portfolio --format console
```

### Экспорт в Excel и CSV
```bash
# Экспорт в XLSX
python3 main.py export-portfolio --format xlsx

# Экспорт в CSV
python3 main.py export-portfolio --format csv
```

## Устранение неполадок

### Проблема: Пустой экспорт
```bash
# Проверить наличие позиций в базе данных PostgreSQL
python3 main.py check-db --freshness

# Синхронизировать портфель из брокера
python3 main.py sync-portfolio
```
