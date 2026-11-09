# SQL запросы для анализа YTM облигаций

Этот документ содержит примеры SQL запросов для анализа доходности облигаций в портфеле и поиска облигаций для покупки.

## Анализ облигаций в портфеле (по средней цене покупки)

### 1. Все облигации в портфеле с расчетом реальной доходности

```sql
SELECT 
    pp.isin,
    bc.ticker,
    bc.name,
    bc.nominal,
    bc.coupon_rate_percent,
    bc.coupon_quantity_per_year,
    bc.maturity_date,
    bc.risk_level,
    bc.list_level,
    bc.amortization_flag,
    pp.average_price,
    pp.current_price,
    pp.quantity,
    CASE 
        WHEN pp.average_price IS NOT NULL AND pp.average_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.average_price * 100
        ELSE NULL 
    END as real_yield_percent,
    CASE 
        WHEN pp.current_price IS NOT NULL AND pp.average_price IS NOT NULL 
        THEN ((pp.current_price - pp.average_price) / pp.average_price) * 100
        ELSE NULL 
    END as price_change_percent
FROM portfolio_positions pp
JOIN bonds_catalog bc ON pp.isin = bc.isin
WHERE bc.currency = 'rub'
ORDER BY real_yield_percent DESC NULLS LAST;
```

### 2. Облигации без амортизации с ежемесячными купонами

```sql
SELECT 
    pp.isin,
    bc.ticker,
    bc.name,
    bc.coupon_rate_percent,
    bc.maturity_date,
    pp.average_price,
    pp.current_price,
    CASE 
        WHEN pp.average_price IS NOT NULL AND pp.average_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.average_price * 100
        ELSE NULL 
    END as real_yield_percent
FROM portfolio_positions pp
JOIN bonds_catalog bc ON pp.isin = bc.isin
WHERE bc.currency = 'rub'
    AND bc.amortization_flag = 0
    AND bc.coupon_quantity_per_year = 12
    AND bc.risk_level <= 2
ORDER BY real_yield_percent DESC NULLS LAST;
```

### 3. Облигации с доходностью выше 20%

```sql
SELECT 
    pp.isin,
    bc.ticker,
    bc.name,
    bc.coupon_rate_percent,
    bc.maturity_date,
    pp.average_price,
    CASE 
        WHEN pp.average_price IS NOT NULL AND pp.average_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.average_price * 100
        ELSE NULL 
    END as real_yield_percent
FROM portfolio_positions pp
JOIN bonds_catalog bc ON pp.isin = bc.isin
WHERE bc.currency = 'rub'
    AND real_yield_percent > 20
ORDER BY real_yield_percent DESC;
```

### 4. Облигации с фиксированным купоном

```sql
SELECT 
    pp.isin,
    bc.ticker,
    bc.name,
    bc.coupon_rate_percent,
    bc.maturity_date,
    pp.average_price,
    CASE 
        WHEN pp.average_price IS NOT NULL AND pp.average_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.average_price * 100
        ELSE NULL 
    END as real_yield_percent
FROM portfolio_positions pp
JOIN bonds_catalog bc ON pp.isin = bc.isin
WHERE bc.currency = 'rub'
    AND bc.floating_coupon_flag = 0
ORDER BY real_yield_percent DESC NULLS LAST;
```

### 5. Облигации с плавающим купоном

```sql
SELECT 
    pp.isin,
    bc.ticker,
    bc.name,
    bc.coupon_rate_percent,
    bc.maturity_date,
    pp.average_price,
    CASE 
        WHEN pp.average_price IS NOT NULL AND pp.average_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.average_price * 100
        ELSE NULL 
    END as real_yield_percent
FROM portfolio_positions pp
JOIN bonds_catalog bc ON pp.isin = bc.isin
WHERE bc.currency = 'rub'
    AND bc.floating_coupon_flag = 1
ORDER BY real_yield_percent DESC NULLS LAST;
```

## Анализ облигаций для покупки (по текущей цене)

### 1. Все доступные облигации с текущими ценами

```sql
SELECT 
    bc.isin,
    bc.ticker,
    bc.name,
    bc.nominal,
    bc.coupon_rate_percent,
    bc.coupon_quantity_per_year,
    bc.maturity_date,
    bc.risk_level,
    bc.list_level,
    bc.amortization_flag,
    pp.current_price,
    CASE 
        WHEN pp.current_price IS NOT NULL AND pp.current_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.current_price * 100
        ELSE NULL 
    END as real_yield_percent
FROM bonds_catalog bc
LEFT JOIN portfolio_positions pp ON bc.isin = pp.isin
WHERE bc.currency = 'rub'
    AND (bc.is_trade_available = 1 OR bc.is_trade_available IS NULL)
    AND bc.perpetual_flag = 0
    AND pp.isin IS NOT NULL
ORDER BY real_yield_percent DESC NULLS LAST;
```

### 2. Лучшие облигации для покупки (низкий риск, высокий доход)

```sql
SELECT 
    bc.isin,
    bc.ticker,
    bc.name,
    bc.coupon_rate_percent,
    bc.maturity_date,
    bc.risk_level,
    pp.current_price,
    CASE 
        WHEN pp.current_price IS NOT NULL AND pp.current_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.current_price * 100
        ELSE NULL 
    END as real_yield_percent
FROM bonds_catalog bc
LEFT JOIN portfolio_positions pp ON bc.isin = pp.isin
WHERE bc.currency = 'rub'
    AND (bc.is_trade_available = 1 OR bc.is_trade_available IS NULL)
    AND bc.perpetual_flag = 0
    AND bc.risk_level <= 2
    AND bc.list_level <= 1
    AND bc.amortization_flag = 0
    AND bc.coupon_quantity_per_year = 12
    AND pp.isin IS NOT NULL
ORDER BY real_yield_percent DESC NULLS LAST
LIMIT 10;
```

### 3. Облигации с погашением в ближайшие 2 года

```sql
SELECT 
    bc.isin,
    bc.ticker,
    bc.name,
    bc.coupon_rate_percent,
    bc.maturity_date,
    pp.current_price,
    CASE 
        WHEN pp.current_price IS NOT NULL AND pp.current_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.current_price * 100
        ELSE NULL 
    END as real_yield_percent
FROM bonds_catalog bc
LEFT JOIN portfolio_positions pp ON bc.isin = pp.isin
WHERE bc.currency = 'rub'
    AND (bc.is_trade_available = 1 OR bc.is_trade_available IS NULL)
    AND bc.perpetual_flag = 0
    AND bc.maturity_date <= '2026-12-31'
    AND pp.isin IS NOT NULL
ORDER BY real_yield_percent DESC NULLS LAST;
```

### 4. Облигации с фиксированным купоном для покупки

```sql
SELECT 
    bc.isin,
    bc.ticker,
    bc.name,
    bc.coupon_rate_percent,
    bc.maturity_date,
    pp.current_price,
    CASE 
        WHEN pp.current_price IS NOT NULL AND pp.current_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.current_price * 100
        ELSE NULL 
    END as real_yield_percent
FROM bonds_catalog bc
LEFT JOIN portfolio_positions pp ON bc.isin = pp.isin
WHERE bc.currency = 'rub'
    AND (bc.is_trade_available = 1 OR bc.is_trade_available IS NULL)
    AND bc.perpetual_flag = 0
    AND bc.floating_coupon_flag = 0
    AND pp.isin IS NOT NULL
ORDER BY real_yield_percent DESC NULLS LAST;
```

### 5. Облигации с плавающим купоном для покупки

```sql
SELECT 
    bc.isin,
    bc.ticker,
    bc.name,
    bc.coupon_rate_percent,
    bc.maturity_date,
    pp.current_price,
    CASE 
        WHEN pp.current_price IS NOT NULL AND pp.current_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.current_price * 100
        ELSE NULL 
    END as real_yield_percent
FROM bonds_catalog bc
LEFT JOIN portfolio_positions pp ON bc.isin = pp.isin
WHERE bc.currency = 'rub'
    AND (bc.is_trade_available = 1 OR bc.is_trade_available IS NULL)
    AND bc.perpetual_flag = 0
    AND bc.floating_coupon_flag = 1
    AND pp.isin IS NOT NULL
ORDER BY real_yield_percent DESC NULLS LAST;
```

## Полезные запросы для анализа

### 1. Статистика по портфелю

```sql
SELECT 
    COUNT(*) as total_bonds,
    AVG(CASE 
        WHEN pp.average_price IS NOT NULL AND pp.average_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.average_price * 100
        ELSE NULL 
    END) as avg_yield,
    MIN(CASE 
        WHEN pp.average_price IS NOT NULL AND pp.average_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.average_price * 100
        ELSE NULL 
    END) as min_yield,
    MAX(CASE 
        WHEN pp.average_price IS NOT NULL AND pp.average_price > 0 
        THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.average_price * 100
        ELSE NULL 
    END) as max_yield
FROM portfolio_positions pp
JOIN bonds_catalog bc ON pp.isin = bc.isin
WHERE bc.currency = 'rub';
```

### 2. Облигации с наибольшим изменением цены

```sql
SELECT 
    pp.isin,
    bc.ticker,
    bc.name,
    pp.average_price,
    pp.current_price,
    CASE 
        WHEN pp.current_price IS NOT NULL AND pp.average_price IS NOT NULL 
        THEN ((pp.current_price - pp.average_price) / pp.average_price) * 100
        ELSE NULL 
    END as price_change_percent
FROM portfolio_positions pp
JOIN bonds_catalog bc ON pp.isin = bc.isin
WHERE bc.currency = 'rub'
ORDER BY price_change_percent DESC NULLS LAST;
```

## Формула расчета реальной доходности

Реальная доходность рассчитывается по формуле:

```
real_yield = (coupon_rate_percent * nominal / 100) / price * 100
```

Где:
- `coupon_rate_percent` - купонная ставка в процентах
- `nominal` - номинальная стоимость облигации (обычно 1000 рублей)
- `price` - цена покупки (средняя цена для портфеля или текущая цена для покупки)

Эта формула показывает, какую доходность в процентах в год вы получите по купонам, если купите облигацию по указанной цене.

## Новые возможности

### Колонка "Тип купона"
В команде `calculate-ytm` добавлена колонка "Тип купона", которая показывает:
- **"Фиксированный"** - для облигаций с постоянной купонной ставкой
- **"Плавающий"** - для флоатеров (ставка = RUONIA + спред)

Это помогает быстро понять, какой тип купона у облигации и как рассчитывается доходность.

### Колонка "Купон/мес"
Показывает **общую сумму купонных выплат в месяц по всем бумагам в строке**:
- Для режима `portfolio` - учитывает количество бумаг в позиции
- Для режима `buy` - рассчитывается для 1 бумаги

**Формула:** `(Купонная ставка × Номинал) / Количество выплат в год × Количество бумаг`

## Различия между режимами анализа

### Режим "Портфель" (по средней цене покупки)
В этом режиме доходность рассчитывается относительно **вашей средней цены покупки** (`pp.average_price`):

```sql
-- Доходность от средней цены покупки
CASE 
    WHEN pp.average_price IS NOT NULL AND pp.average_price > 0 
    THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.average_price * 100
    ELSE NULL 
END as real_yield_percent
```

**Что показывает:** Вашу реальную доходность по уже купленным облигациям. Помогает понять, насколько выгодно вы купили облигацию.

### Режим "Покупка" (по текущей рыночной цене)
В этом режиме доходность рассчитывается относительно **текущей рыночной цены** (`pp.current_price`):

```sql
-- Доходность от текущей рыночной цены
CASE 
    WHEN pp.current_price IS NOT NULL AND pp.current_price > 0 
    THEN (bc.coupon_rate_percent * bc.nominal / 100) / pp.current_price * 100
    ELSE NULL 
END as real_yield_percent
```

**Что показывает:** Потенциальную доходность, если купить облигацию сейчас по текущей цене. Помогает понять, стоит ли покупать облигацию в данный момент.

### Пример различий
Возьмем облигацию с купоном 25.5% и номиналом 1000₽:

- **Если вы купили по 1045.40₽:**
  - Доходность = (25.5% × 1000₽) / 1045.40₽ × 100% = **24.39%**

- **Если сейчас купить по 1184.00₽:**
  - Доходность = (25.5% × 1000₽) / 1184.00₽ × 100% = **21.54%**

**Вывод:** Вы купили выгоднее, чем если бы покупали сейчас!

## Связанные документы

Для анализа ежемесячной прибыли по облигациям (включая купонный доход и прибыль от изменения цены) см.:
- [SQL примеры для ежемесячной прибыли](monthly_profit_sql_examples.md)
