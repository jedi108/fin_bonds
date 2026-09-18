# Команда: `export-portfolio`

## Описание
Команда для экспорта данных портфеля облигаций в различные форматы. Поддерживает экспорт в CSV, Excel (XLSX), консоль и другие форматы с возможностью фильтрации и настройки полей.

## Синтаксис
```bash
python3 main.py export-portfolio [опции]
```

## Параметры

### Основные параметры

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|--------------|----------|
| `--format` | string | `console` | Формат экспорта |
| `--output` | string | - | Путь к выходному файлу |
| `--fields` | string | - | Список полей для экспорта |
| `--filter-risk` | integer | - | Максимальный уровень риска |
| `--filter-maturity` | string | - | Минимальная дата погашения |

### Детальное описание параметров

#### `--format`
Формат экспорта данных портфеля.

**Доступные значения:**
- `console` - Вывод в консоль (по умолчанию)
- `csv` - CSV файл
- `xlsx` - Excel файл (XLSX)
- `json` - JSON файл

**Примеры:**
```bash
# Вывод в консоль
python3 main.py export-portfolio --format console

# Экспорт в CSV
python3 main.py export-portfolio --format csv --output portfolio.csv

# Экспорт в Excel
python3 main.py export-portfolio --format xlsx --output portfolio.xlsx
```

#### `--output`
Путь к выходному файлу для сохранения данных.

**Используется с:** `--format csv`, `--format xlsx`, `--format json`
**Примеры:**
```bash
# Экспорт в CSV файл
python3 main.py export-portfolio --format csv --output data/portfolio.csv

# Экспорт в Excel файл
python3 main.py export-portfolio --format xlsx --output reports/portfolio.xlsx
```

#### `--fields`
Список полей для включения в экспорт.

**Доступные поля:**
- `isin` - Международный идентификатор
- `ticker` - Биржевой тикер
- `name` - Название облигации
- `quantity` - Количество облигаций
- `average_price` - Средняя цена покупки
- `current_price` - Текущая цена
- `coupon_rate` - Ставка купона
- `yield_to_maturity` - Доходность к погашению
- `risk_level` - Уровень риска
- `maturity_date` - Дата погашения
- `currency` - Валюта

**Примеры:**
```bash
# Экспорт основных полей
python3 main.py export-portfolio --fields "isin,name,quantity,current_price,yield_to_maturity"

# Экспорт всех полей
python3 main.py export-portfolio --fields "isin,ticker,name,quantity,average_price,current_price,coupon_rate,yield_to_maturity,risk_level,maturity_date,currency"
```

#### `--filter-risk`
Максимальный уровень риска для фильтрации облигаций.

**Диапазон:** 1-5
**Примеры:**
```bash
# Только надежные облигации
python3 main.py export-portfolio --filter-risk 2

# Включая средний риск
python3 main.py export-portfolio --filter-risk 3
```

#### `--filter-maturity`
Минимальная дата погашения для фильтрации облигаций.

**Формат:** YYYY-MM-DD
**Примеры:**
```bash
# Облигации с погашением после 2025 года
python3 main.py export-portfolio --filter-maturity 2025-01-01

# Облигации с погашением после 2027 года
python3 main.py export-portfolio --filter-maturity 2027-01-01
```

## Примеры использования

### 1. Экспорт в консоль
```bash
python3 main.py export-portfolio --format console
```
**Вывод:**
```
========================================================================================================================
📊 Экспорт портфеля облигаций
========================================================================================================================
ISIN         | Название                  | Количество | Цена покупки | Текущая цена | Доходность % | Риск | Погашение
------------------------------------------------------------------------------------------------------------------------
RU000A1084K3 | ВТБ-1-5                  | 100        | 1,000        | 1,050        | 16.8         | 2    | 2027-05-15
RU000A105FZ9 | Сбербанк-001Р-02         | 50         | 950          | 980          | 8.2          | 1    | 2025-12-20
```

### 2. Экспорт в CSV
```bash
python3 main.py export-portfolio --format csv --output portfolio.csv
```
**Результат:** Создается файл `portfolio.csv` с данными портфеля

### 3. Экспорт в Excel с фильтрацией
```bash
python3 main.py export-portfolio --format xlsx --output safe_bonds.xlsx --filter-risk 2 --filter-maturity 2025-01-01
```
**Результат:** Создается файл `safe_bonds.xlsx` только с надежными облигациями

### 4. Экспорт выбранных полей
```bash
python3 main.py export-portfolio --format csv --output summary.csv --fields "isin,name,quantity,yield_to_maturity"
```
**Результат:** Создается файл `summary.csv` с основными полями

## Структура данных

### Доступные поля для экспорта

| Поле | Тип | Описание | Источник |
|------|-----|----------|----------|
| `isin` | TEXT | Международный идентификатор | `portfolio_positions` |
| `ticker` | TEXT | Биржевой тикер | `bonds_catalog` |
| `name` | TEXT | Название облигации | `bonds_catalog` |
| `quantity` | REAL | Количество облигаций | `portfolio_positions` |
| `average_price` | REAL | Средняя цена покупки | `portfolio_positions` |
| `current_price` | REAL | Текущая цена | `portfolio_positions` |
| `coupon_rate` | REAL | Ставка купона | `bonds_catalog` |
| `yield_to_maturity` | REAL | Доходность к погашению | `portfolio_positions` |
| `risk_level` | INTEGER | Уровень риска | `bonds_catalog` |
| `maturity_date` | TEXT | Дата погашения | `bonds_catalog` |
| `currency` | TEXT | Валюта | `portfolio_positions` |

### Форматы экспорта

#### CSV формат
- Разделитель: запятая
- Кодировка: UTF-8
- Заголовки: включены

#### Excel формат (XLSX)
- Один лист с данными
- Автоматическая ширина колонок
- Форматирование чисел

#### JSON формат
- Массив объектов
- Вложенная структура
- Поддержка Unicode

## Алгоритм работы

### 1. Получение данных портфеля
1. Загружает позиции из `portfolio_positions`
2. Объединяет с данными из `bonds_catalog`
3. Вычисляет дополнительные поля

### 2. Фильтрация данных
1. Применяет фильтр по уровню риска
2. Применяет фильтр по дате погашения
3. Валидирует данные

### 3. Выбор полей
1. Определяет поля для экспорта
2. Проверяет доступность полей
3. Формирует структуру данных

### 4. Экспорт
1. Форматирует данные согласно формату
2. Сохраняет в файл или выводит в консоль
3. Проверяет результат экспорта

## Ограничения и особенности

### Требования к данным
- Портфель должен быть синхронизирован
- Каталог облигаций должен быть обновлен
- Данные должны быть валидными

### Ограничения форматов
- CSV: ограничения на специальные символы
- Excel: зависимость от библиотеки openpyxl
- JSON: ограничения на размер файла

### Производительность
- Время выполнения: <5 секунд
- Память: зависит от размера портфеля
- Диск: зависит от формата и размера данных

## Интеграция с другими командами

### Зависимости
- `sync-portfolio` - для актуальных данных портфеля
- `update-bonds` - для данных об облигациях

### Использование результатов
Экспортированные данные используются для:
- Анализа в Excel
- Отчетности
- Интеграции с другими системами
- Резервного копирования

## Мониторинг и логирование

### Уровни логирования
- `INFO` - Основные этапы экспорта
- `DEBUG` - Детальная информация о данных
- `WARNING` - Проблемы с отдельными записями
- `ERROR` - Критические ошибки

### Ключевые метрики
- Количество экспортированных записей
- Размер выходного файла
- Время выполнения экспорта
- Количество отфильтрованных записей

## Устранение неполадок

### Проблема: Пустой экспорт
```bash
# Проверить наличие данных в портфеле
sqlite3 ratings.db "SELECT COUNT(*) FROM portfolio_positions;"

# Синхронизировать портфель
python3 main.py sync-portfolio
```

### Проблема: Ошибка записи файла
```bash
# Проверить права доступа к папке
ls -la /path/to/output/directory

# Проверить свободное место на диске
df -h
```

### Проблема: Некорректные данные
```bash
# Проверить данные портфеля
sqlite3 ratings.db "SELECT * FROM portfolio_positions LIMIT 5;"

# Проверить данные каталога
sqlite3 ratings.db "SELECT * FROM bonds_catalog LIMIT 5;"
```

### Проблема: Ошибка формата
```bash
# Проверить доступность библиотек
pip list | grep openpyxl

# Установить недостающие библиотеки
pip install openpyxl
```

## Рекомендации по использованию

### Регулярный экспорт
```bash
# Еженедельный экспорт портфеля
python3 main.py export-portfolio --format xlsx --output reports/weekly_portfolio.xlsx

# Ежемесячный экспорт с фильтрацией
python3 main.py export-portfolio --format csv --output reports/monthly_safe_bonds.csv --filter-risk 2
```

### Автоматизация
```bash
# Добавить в crontab для еженедельного экспорта
0 18 * * 5 cd /path/to/portfolio && python3 main.py export-portfolio --format xlsx --output reports/weekly_portfolio.xlsx
```

### Резервное копирование
```bash
# Полный экспорт для резервного копирования
python3 main.py export-portfolio --format csv --output backup/portfolio_$(date +%Y%m%d).csv

# Экспорт только основных полей
python3 main.py export-portfolio --format csv --output backup/summary_$(date +%Y%m%d).csv --fields "isin,name,quantity,yield_to_maturity"
```

### Анализ в Excel
```bash
# Экспорт для анализа в Excel
python3 main.py export-portfolio --format xlsx --output analysis/portfolio_analysis.xlsx --fields "isin,name,quantity,average_price,current_price,yield_to_maturity,risk_level,maturity_date"
```

### Интеграция с другими системами
```bash
# Экспорт в JSON для API
python3 main.py export-portfolio --format json --output api/portfolio.json

# Экспорт в CSV для импорта в другие системы
python3 main.py export-portfolio --format csv --output integration/portfolio.csv
```
