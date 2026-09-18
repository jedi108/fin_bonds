-- SQLite
SELECT * FROM portfolio_positions;

SELECT * from bonds_catalog ;
;



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
    
    --,
    
    --  -- Процентная доходность (годовая)
    ROUND(
        ((COALESCE(pp.coupon_rate_percent, mc_floater.metric_value, bc.coupon_rate_percent) * bc.nominal / 100) / pp.current_price) * 100, 
        2
    ) as price_yield_percent

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

LEFT JOIN (
    SELECT loss_ratio, timestamp, isin, ROW_NUMBER() OVER (PARTITION BY isin ORDER BY timestamp DESC) AS rn
    FROM liquidity_history
) lh ON bc.isin = lh.isin and rn = 1

WHERE bc.currency = 'rub'
   -- AND pp.current_price > 0
    --AND pp.quantity > 0
    AND bc.floating_coupon_flag = 0
    AND bc.amortization_flag = 0
    AND bc.coupon_quantity_per_year = 12
    --AND bc.risk_level =1
    --AND bc.list_level =2
ORDER BY 
    price_yield_percent DESC
    -- bc.coupon_rate_percent DESC

;


Select bc.coupon_rate_percent, pp.coupon_rate_percent  
from bonds_catalog bc 
left JOIN portfolio_positions pp ON bc.isin = pp.isin
where bc.coupon_rate_percent != pp.coupon_rate_percent or pp.coupon_rate_percent is null;
;



SELECT isin, metric_value, * 
FROM monitoring_checks  
WHERE metric_name = 'floater_coupon_calculator'
AND metric_value is NOT NULL;


SELECT coupon_rate_percent FROM bonds_catalog WHERE isin = 'RU000A10ASC6';
