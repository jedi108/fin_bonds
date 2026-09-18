# SQL Примеры: Расчет ежемесячной прибыли по облигациям

## Глоссарий терминов

### Основные понятия:
- **Купонный доход** - регулярные выплаты по облигации (обычно раз в квартал или полгода)
- **Номинал** - номинальная стоимость облигации (обычно 1000 рублей)
- **Купонная ставка** - процент от номинала, выплачиваемый в качестве купона
- **Количество выплат в год** - сколько раз в год выплачивается купон (обычно 2 или 4)
- **Текущая цена** - рыночная цена облигации на данный момент
- **Средняя цена** - средняя цена покупки облигации в портфеле
- **Доходность к погашению (YTM)** - годовая доходность, учитывающая купоны и изменение цены

### Типы доходов:
- **monthly_coupon_income_per_bond** - купонный доход за месяц на 1 облигацию (в рублях)
- **total_monthly_coupon_income** - общий купонный доход за месяц по всем облигациям в позиции
- **price_change_profit** - прибыль/убыток от изменения рыночной цены
- **total_monthly_profit** - общая ежемесячная прибыль (купоны + изменение цены)

## Основные компоненты ежемесячной прибыли

### 1. Купонный доход (основной источник)
```sql
-- Ежемесячный купонный доход на 1 облигацию = (Купонная ставка × Номинал) / Количество выплат в год
monthly_coupon_income_per_bond = (coupon_rate_percent * nominal / 100) / coupon_quantity_per_year

-- Общий купонный доход за месяц = Доход на 1 облигацию × Количество облигаций
total_monthly_coupon_income = monthly_coupon_income_per_bond * quantity
```

### 2. Прибыль от изменения цены
```sql
-- Ежемесячная прибыль от изменения цены = (Текущая цена - Средняя цена) × Количество
price_change_profit = (current_price - average_price) * quantity
```

### 3. Общая ежемесячная прибыль
```sql
-- Общая ежемесячная прибыль = Купонный доход + Прибыль от изменения цены
total_monthly_profit = total_monthly_coupon_income + price_change_profit
```

### 4. Доходность к погашению (YTM)
```sql
-- Годовая доходность к погашению = (Годовой купонный доход / Текущая цена) × 100%
annual_yield_percent = ((coupon_rate_percent * nominal / 100) / current_price) * 100

-- Месячная доходность = Годовая доходность / 12
monthly_yield_percent = annual_yield_percent / 12
```

## SQL Запросы

### 1. Ежемесячная прибыль по каждой бумаге (детальный расчет)

```sql
SELECT 
    bc.isin,
    bc.ticker,
    bc.name,
    bc.nominal,
    COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) as coupon_rate_percent,
    bc.coupon_quantity_per_year,
    pp.average_price,
    pp.current_price,
    pp.quantity,
    
    -- Общий купонный доход за месяц по всем облигациям в позиции (в рублях)
    -- Формула: (Ставка купона × Номинал) / Количество выплат в год × Количество облигаций
    ROUND(
        (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity, 
        2
    ) as monthly_coupon_income,
    
    -- Прибыль от изменения цены
    ROUND(
        (pp.current_price - pp.average_price) * pp.quantity, 
        2
    ) as price_change_profit,
    
    -- Общая ежемесячная прибыль
    ROUND(
        ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
        ((pp.current_price - pp.average_price) * pp.quantity), 
        2
    ) as total_monthly_profit,
    
    -- Процентная доходность (годовая)
    ROUND(
        ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / pp.average_price) * 100, 
        2
    ) as annual_yield_percent,
    
    -- Процентная доходность (месячная)
    ROUND(
        ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / pp.average_price / 12) * 100, 
        2
    ) as monthly_yield_percent
    
FROM bonds_catalog bc
JOIN portfolio_positions pp ON bc.isin = pp.isin
LEFT JOIN (
    SELECT isin, metric_value
    FROM monitoring_checks 
    WHERE metric_name = 'floater_coupon_calculator' 
    AND check_date = (
        SELECT MAX(check_date) 
        FROM monitoring_checks 
        WHERE isin = monitoring_checks.isin 
        AND metric_name = 'floater_coupon_calculator'
    )
) mc_floater ON bc.isin = mc_floater.isin
WHERE bc.currency = 'rub'
    AND pp.current_price > 0
    AND pp.quantity > 0
ORDER BY total_monthly_profit DESC;
```

### 2. Суммарная ежемесячная прибыль по всем бумагам

```sql
SELECT 
    'ИТОГО' as description,
    COUNT(DISTINCT bc.isin) as bonds_count,
    SUM(pp.quantity) as total_quantity,
    
    -- Общий купонный доход за месяц
    ROUND(
        SUM((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity), 
        2
    ) as total_monthly_coupon_income,
    
    -- Общая прибыль от изменения цены
    ROUND(
        SUM((pp.current_price - pp.average_price) * pp.quantity), 
        2
    ) as total_price_change_profit,
    
    -- Общая ежемесячная прибыль
    ROUND(
        SUM(((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
            ((pp.current_price - pp.average_price) * pp.quantity)), 
        2
    ) as total_monthly_profit,
    
    -- Средняя годовая доходность портфеля
    ROUND(
        AVG((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / pp.average_price * 100), 
        2
    ) as avg_annual_yield_percent
    
FROM bonds_catalog bc
JOIN portfolio_positions pp ON bc.isin = pp.isin
LEFT JOIN (
    SELECT isin, metric_value
    FROM monitoring_checks 
    WHERE metric_name = 'floater_coupon_calculator' 
    AND check_date = (
        SELECT MAX(check_date) 
        FROM monitoring_checks 
        WHERE isin = monitoring_checks.isin 
        AND metric_name = 'floater_coupon_calculator'
    )
) mc_floater ON bc.isin = mc_floater.isin
WHERE bc.currency = 'rub'
    AND pp.current_price > 0
    AND pp.quantity > 0;
```

### 3. Ежемесячная прибыль по брокерам

```sql
SELECT 
    pp.broker_name,
    COUNT(DISTINCT bc.isin) as bonds_count,
    
    -- Купонный доход за месяц
    ROUND(
        SUM((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity), 
        2
    ) as monthly_coupon_income,
    
    -- Прибыль от изменения цены
    ROUND(
        SUM((pp.current_price - pp.average_price) * pp.quantity), 
        2
    ) as price_change_profit,
    
    -- Общая ежемесячная прибыль
    ROUND(
        SUM(((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
            ((pp.current_price - pp.average_price) * pp.quantity)), 
        2
    ) as total_monthly_profit
    
FROM bonds_catalog bc
JOIN portfolio_positions pp ON bc.isin = pp.isin
LEFT JOIN (
    SELECT isin, metric_value
    FROM monitoring_checks 
    WHERE metric_name = 'floater_coupon_calculator' 
    AND check_date = (
        SELECT MAX(check_date) 
        FROM monitoring_checks 
        WHERE isin = monitoring_checks.isin 
        AND metric_name = 'floater_coupon_calculator'
    )
) mc_floater ON bc.isin = mc_floater.isin
WHERE bc.currency = 'rub'
    AND pp.current_price > 0
    AND pp.quantity > 0
GROUP BY pp.broker_name
ORDER BY total_monthly_profit DESC;
```

### 4. Ежемесячная прибыль по уровням риска

```sql
SELECT 
    bc.risk_level,
    COUNT(DISTINCT bc.isin) as bonds_count,
    
    -- Купонный доход за месяц
    ROUND(
        SUM((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity), 
        2
    ) as monthly_coupon_income,
    
    -- Прибыль от изменения цены
    ROUND(
        SUM((pp.current_price - pp.average_price) * pp.quantity), 
        2
    ) as price_change_profit,
    
    -- Общая ежемесячная прибыль
    ROUND(
        SUM(((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
            ((pp.current_price - pp.average_price) * pp.quantity)), 
        2
    ) as total_monthly_profit,
    
    -- Средняя годовая доходность
    ROUND(
        AVG((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / pp.average_price * 100), 
        2
    ) as avg_annual_yield_percent
    
FROM bonds_catalog bc
JOIN portfolio_positions pp ON bc.isin = pp.isin
LEFT JOIN (
    SELECT isin, metric_value
    FROM monitoring_checks 
    WHERE metric_name = 'floater_coupon_calculator' 
    AND check_date = (
        SELECT MAX(check_date) 
        FROM monitoring_checks 
        WHERE isin = monitoring_checks.isin 
        AND metric_name = 'floater_coupon_calculator'
    )
) mc_floater ON bc.isin = mc_floater.isin
WHERE bc.currency = 'rub'
    AND pp.current_price > 0
    AND pp.quantity > 0
GROUP BY bc.risk_level
ORDER BY bc.risk_level;
```

### 5. Топ-10 облигаций по ежемесячной прибыли

```sql
SELECT 
    bc.isin,
    bc.ticker,
    bc.name,
    COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) as coupon_rate_percent,
    bc.risk_level,
    pp.quantity,
    
    -- Ежемесячная прибыль
    ROUND(
        ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
        ((pp.current_price - pp.average_price) * pp.quantity), 
        2
    ) as monthly_profit,
    
    -- Купонная часть
    ROUND(
        (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity, 
        2
    ) as coupon_part,
    
    -- Прибыль от цены
    ROUND(
        (pp.current_price - pp.average_price) * pp.quantity, 
        2
    ) as price_part
    
FROM bonds_catalog bc
JOIN portfolio_positions pp ON bc.isin = pp.isin
LEFT JOIN (
    SELECT isin, metric_value
    FROM monitoring_checks 
    WHERE metric_name = 'floater_coupon_calculator' 
    AND check_date = (
        SELECT MAX(check_date) 
        FROM monitoring_checks 
        WHERE isin = monitoring_checks.isin 
        AND metric_name = 'floater_coupon_calculator'
    )
) mc_floater ON bc.isin = mc_floater.isin
WHERE bc.currency = 'rub'
    AND pp.current_price > 0
    AND pp.quantity > 0
ORDER BY monthly_profit DESC
LIMIT 10;
```

### 6. Ежемесячная прибыль с учетом амортизации

```sql
SELECT 
    bc.isin,
    bc.name,
    bc.amortization_flag,
    pp.quantity,
    
    -- Купонный доход
    ROUND(
        (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity, 
        2
    ) as monthly_coupon_income,
    
    -- Прибыль от изменения цены
    ROUND(
        (pp.current_price - pp.average_price) * pp.quantity, 
        2
    ) as price_change_profit,
    
    -- Амортизация (если есть)
    CASE 
        WHEN bc.amortization_flag = 1 THEN 
            ROUND((bc.nominal - pp.current_price) * pp.quantity / 12, 2)  -- Примерная месячная амортизация
        ELSE 0 
    END as monthly_amortization,
    
    -- Общая прибыль
    ROUND(
        ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
        ((pp.current_price - pp.average_price) * pp.quantity) +
        CASE 
            WHEN bc.amortization_flag = 1 THEN (bc.nominal - pp.current_price) * pp.quantity / 12
            ELSE 0 
        END, 
        2
    ) as total_monthly_profit
    
FROM bonds_catalog bc
JOIN portfolio_positions pp ON bc.isin = pp.isin
LEFT JOIN (
    SELECT isin, metric_value
    FROM monitoring_checks 
    WHERE metric_name = 'floater_coupon_calculator' 
    AND check_date = (
        SELECT MAX(check_date) 
        FROM monitoring_checks 
        WHERE isin = monitoring_checks.isin 
        AND metric_name = 'floater_coupon_calculator'
    )
) mc_floater ON bc.isin = mc_floater.isin
WHERE bc.currency = 'rub'
    AND pp.current_price > 0
    AND pp.quantity > 0
ORDER BY total_monthly_profit DESC;
```

## Анализ бумаг для покупки (не в портфеле)

### 7. Потенциальная ежемесячная прибыль по бумагам для покупки

```sql
SELECT 
    bc.isin,
    bc.ticker,
    bc.name,
    bc.nominal,
    COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) as coupon_rate_percent,
    bc.coupon_quantity_per_year,
    bc.risk_level,
    bc.list_level,
    bc.maturity_date,
    bc.amortization_flag,
    bc.floating_coupon_flag,
    
    -- Текущая рыночная цена (если есть в каталоге)
    COALESCE(pp.current_price, 0) as current_price,
    
    -- Купонный доход за месяц на 1 облигацию (в рублях)
    -- Формула: (Ставка купона × Номинал) / Количество выплат в год
    -- Примечание: Это доход на 1 облигацию, для расчета общего дохода нужно умножить на количество
    ROUND(
        (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year, 
        2
    ) as monthly_coupon_income_per_bond,
    
    -- Годовая доходность к погашению (если есть цена)
    -- Формула: (Годовой купонный доход / Текущая цена) × 100%
    -- Используем рыночную цену из каталога или цену из портфеля
    CASE 
        WHEN COALESCE(bc.market_price, pp.current_price, 0) > 0 THEN
            ROUND(
                ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / COALESCE(bc.market_price, pp.current_price)) * 100, 
                2
            )
        ELSE NULL 
    END as annual_yield_percent,
    
    -- До погашения (дней)
    ROUND(
        (julianday(bc.maturity_date) - julianday('now')), 
        0
    ) as days_to_maturity
    
FROM bonds_catalog bc
LEFT JOIN portfolio_positions pp ON bc.isin = pp.isin
LEFT JOIN (
    SELECT isin, metric_value
    FROM monitoring_checks 
    WHERE metric_name = 'floater_coupon_calculator' 
    AND check_date = (
        SELECT MAX(check_date) 
        FROM monitoring_checks 
        WHERE isin = monitoring_checks.isin 
        AND metric_name = 'floater_coupon_calculator'
    )
) mc_floater ON bc.isin = mc_floater.isin
WHERE bc.currency = 'rub'
    AND (bc.is_trade_available = 1 OR bc.is_trade_available IS NULL)
    AND bc.perpetual_flag = 0
    AND pp.isin IS NULL  -- Нет в портфеле
    AND COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) IS NOT NULL  -- Есть купонная ставка
ORDER BY annual_yield_percent DESC NULLS LAST
LIMIT 50;
```

### 8. Топ бумаг для покупки по доходности (без учета цены)

```sql
SELECT 
    bc.isin,
    bc.ticker,
    bc.name,
    COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) as coupon_rate_percent,
    bc.risk_level,
    bc.list_level,
    bc.maturity_date,
    
    -- Купонный доход за месяц (на 1 облигацию)
    ROUND(
        (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year, 
        2
    ) as monthly_coupon_income_per_bond,
    
    -- До погашения (месяцев)
    ROUND(
        (julianday(bc.maturity_date) - julianday('now')) / 30, 
        1
    ) as months_to_maturity,
    
    -- Тип облигации
    CASE 
        WHEN bc.floating_coupon_flag = 1 THEN 'Флоатер'
        WHEN bc.amortization_flag = 1 THEN 'Амортизируемая'
        ELSE 'Обычная'
    END as bond_type
    
FROM bonds_catalog bc
LEFT JOIN portfolio_positions pp ON bc.isin = pp.isin
LEFT JOIN (
    SELECT isin, metric_value
    FROM monitoring_checks 
    WHERE metric_name = 'floater_coupon_calculator' 
    AND check_date = (
        SELECT MAX(check_date) 
        FROM monitoring_checks 
        WHERE isin = monitoring_checks.isin 
        AND metric_name = 'floater_coupon_calculator'
    )
) mc_floater ON bc.isin = mc_floater.isin
WHERE bc.currency = 'rub'
    AND (bc.is_trade_available = 1 OR bc.is_trade_available IS NULL)
    AND bc.perpetual_flag = 0
    AND pp.isin IS NULL  -- Нет в портфеле
    AND COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) IS NOT NULL
    AND bc.risk_level <= 2  -- Только низкорисковые
    AND (julianday(bc.maturity_date) - julianday('now')) > 30  -- До погашения больше месяца
ORDER BY monthly_coupon_income_per_bond DESC
LIMIT 30;
```

### 9. Анализ флоатеров для покупки

```sql
SELECT 
    bc.isin,
    bc.ticker,
    bc.name,
    mc_floater.metric_value as calculated_coupon_rate,
    bc.coupon_spread,
    bc.risk_level,
    bc.maturity_date,
    
    -- Купонный доход за месяц (на 1 облигацию)
    ROUND(
        (mc_floater.metric_value * bc.nominal / 100) / bc.coupon_quantity_per_year, 
        2
    ) as monthly_coupon_income_per_bond,
    
    -- До погашения (месяцев)
    ROUND(
        (julianday(bc.maturity_date) - julianday('now')) / 30, 
        1
    ) as months_to_maturity
    
FROM bonds_catalog bc
LEFT JOIN portfolio_positions pp ON bc.isin = pp.isin
LEFT JOIN (
    SELECT isin, metric_value
    FROM monitoring_checks 
    WHERE metric_name = 'floater_coupon_calculator' 
    AND check_date = (
        SELECT MAX(check_date) 
        FROM monitoring_checks 
        WHERE isin = monitoring_checks.isin 
        AND metric_name = 'floater_coupon_calculator'
    )
) mc_floater ON bc.isin = mc_floater.isin
WHERE bc.currency = 'rub'
    AND (bc.is_trade_available = 1 OR bc.is_trade_available IS NULL)
    AND bc.perpetual_flag = 0
    AND pp.isin IS NULL  -- Нет в портфеле
    AND bc.floating_coupon_flag = 1  -- Только флоатеры
    AND mc_floater.metric_value IS NOT NULL  -- Есть рассчитанная ставка
    AND bc.coupon_spread IS NOT NULL  -- Установлен спред
ORDER BY monthly_coupon_income_per_bond DESC
LIMIT 20;
```

### 10. Сравнение с текущим портфелем

```sql
WITH portfolio_avg_yield AS (
    SELECT 
        AVG((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / pp.average_price * 100) as avg_portfolio_yield
    FROM bonds_catalog bc
    JOIN portfolio_positions pp ON bc.isin = pp.isin
    LEFT JOIN (
        SELECT isin, metric_value
        FROM monitoring_checks 
        WHERE metric_name = 'floater_coupon_calculator' 
        AND check_date = (
            SELECT MAX(check_date) 
            FROM monitoring_checks 
            WHERE isin = monitoring_checks.isin 
            AND metric_name = 'floater_coupon_calculator'
        )
    ) mc_floater ON bc.isin = mc_floater.isin
    WHERE bc.currency = 'rub'
        AND pp.current_price > 0
        AND pp.quantity > 0
)
SELECT 
    bc.isin,
    bc.ticker,
    bc.name,
    COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) as coupon_rate_percent,
    bc.risk_level,
    
    -- Потенциальная годовая доходность (если купить по номиналу)
    ROUND(
        (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.nominal * 100, 
        2
    ) as potential_yield_percent,
    
    -- Сравнение с портфелем
    ROUND(
        ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.nominal * 100) - pavg.avg_portfolio_yield, 
        2
    ) as yield_vs_portfolio,
    
    -- Рекомендация
    CASE 
        WHEN ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.nominal * 100) > pavg.avg_portfolio_yield + 2 THEN 'ПОКУПАТЬ'
        WHEN ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.nominal * 100) > pavg.avg_portfolio_yield THEN 'РАССМОТРЕТЬ'
        ELSE 'НЕ РЕКОМЕНДУЕТСЯ'
    END as recommendation
    
FROM bonds_catalog bc
LEFT JOIN portfolio_positions pp ON bc.isin = pp.isin
LEFT JOIN (
    SELECT isin, metric_value
    FROM monitoring_checks 
    WHERE metric_name = 'floater_coupon_calculator' 
    AND check_date = (
        SELECT MAX(check_date) 
        FROM monitoring_checks 
        WHERE isin = monitoring_checks.isin 
        AND metric_name = 'floater_coupon_calculator'
    )
) mc_floater ON bc.isin = mc_floater.isin
CROSS JOIN portfolio_avg_yield pavg
WHERE bc.currency = 'rub'
    AND (bc.is_trade_available = 1 OR bc.is_trade_available IS NULL)
    AND bc.perpetual_flag = 0
    AND pp.isin IS NULL  -- Нет в портфеле
    AND COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) IS NOT NULL
    AND bc.risk_level <= 2  -- Только низкорисковые
ORDER BY yield_vs_portfolio DESC
LIMIT 25;
```

## Важные замечания

### Ограничения расчета:

1. **Купонные выплаты** - расчет основан на номинальной ставке, реальные выплаты могут отличаться
2. **Амортизация** - упрощенный расчет, реальная амортизация зависит от графика
3. **Изменение цены** - это нереализованная прибыль/убыток
4. **Налоги** - не учитываются в расчете
5. **Рыночные цены** - доступны только для облигаций в портфеле

### Критические проблемы:

#### 1. Отсутствие рыночных цен для бумаг не в портфеле
**Проблема:** В запросах для анализа бумаг для покупки используется `current_price` только из таблицы `portfolio_positions`. Для бумаг не в портфеле это поле равно NULL, что делает невозможным расчет реальной доходности к погашению.

**Последствия:**
- Невозможно рассчитать годовую доходность для бумаг не в портфеле
- Сортировка по доходности работает некорректно (NULL значения)
- Пользователь не может принимать обоснованные решения о покупке

**Решение:** ✅ **РЕАЛИЗОВАНО** - Интеграция рыночных цен через TBank API. Добавлены поля `market_price`, `market_price_source`, `market_price_updated_at` в таблицу `bonds_catalog`. Используйте команду:
```bash
# Обновить каталог и цены
python3 main.py update-bonds --update-prices

# Обновить только цены
python3 main.py update-bonds --prices-only
```

#### 2. Неясность в расчетах доходов
**Проблема:** В разных запросах используются похожие названия полей, но с разным смыслом:
- `monthly_coupon_income_per_bond` - доход на 1 облигацию
- `monthly_coupon_income` - общий доход по позиции
- `total_monthly_coupon_income` - общий доход по позиции

**Решение:** Унифицировать названия и добавить четкие комментарии (выполнено в этом документе).

### Рекомендации:

1. **Купонный доход** - это основной стабильный источник дохода
2. **Прибыль от цены** - нестабильная составляющая, зависит от рыночных условий
3. **Амортизация** - дополнительный доход для амортизируемых облигаций
4. **Диверсификация** - важно иметь облигации с разными датами погашения

### Использование:

- Используйте эти запросы для анализа текущей доходности портфеля
- Сравнивайте доходность по разным брокерам и уровням риска
- Отслеживайте изменения ежемесячной прибыли во времени
- Планируйте реинвестирование купонных выплат
- **Для анализа бумаг для покупки** - учитывайте ограничения с рыночными ценами

### ⚠️ Важно: Избежание проблем с NULL значениями

**Проблема:** У флоатеров (облигаций с плавающим купоном) поле `coupon_rate_percent` в таблице `bonds_catalog` равно NULL, что приводит к неправильным расчетам.

**Решение:** Все SQL запросы в этом документе используют `COALESCE(bc.coupon_rate_percent, mc_floater.metric_value)`, что означает:
- Для обычных облигаций используется `bc.coupon_rate_percent`
- Для флоатеров используется рассчитанная ставка из `monitoring_checks`

**Для новых облигаций:**
1. **При добавлении флоатера в портфель** - обязательно установите спред командой:
   ```bash
   python3 main.py set-spread --isin ISIN --spread VALUE
   ```
2. **Запустите обновление рейтингов** для расчета купонных ставок:
   ```bash
   python3 main.py update-ratings
   ```
3. **Используйте исправленные SQL запросы** из этого документа

**Проверка:** Если у облигации `coupon_rate_percent = NULL` и `floating_coupon_flag = 1`, значит это флоатер и нужно установить спред.

## Использование CLI команд

### Анализ бумаг для покупки

Для удобного анализа бумаг, которых нет в портфеле, используйте команду:

```bash
# Топ бумаг по доходности
python3 main.py analyze-buy-candidates top --limit 10 --min-risk 2

# Флоатеры для покупки
python3 main.py analyze-buy-candidates floaters --limit 15

# Сравнение с портфелем
python3 main.py analyze-buy-candidates compare --limit 20 --min-risk 2
```

Эта команда использует те же SQL запросы, что приведены выше в разделах 7-10.

## Связанные документы

Для анализа доходности к погашению (YTM) облигаций см.:
- [SQL примеры для YTM](ytm_sql_examples.md)
