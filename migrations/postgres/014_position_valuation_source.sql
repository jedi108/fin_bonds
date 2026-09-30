-- Migration 014: происхождение оценки позиции (задача 008).
--
-- Внешний потребитель снапшота (ChatGPT Sheet) не видит, какая ветка canonical
-- valuation дала position_value_rub (кейс: price_rub = 0 при value_rub > 0 —
-- это легитимный broker_value, а не ошибка). Добавляем в view колонку
-- valuation_source: CASE точно зеркалит ветки COALESCE в position_value_rub
-- (010). Арифметика оценки не меняется.
--
-- Номинальный фолбэк требует непогашенную бумагу (тот же guard, что в 010)
-- и (quantity * nominal) IS NOT NULL: если quantity или nominal NULL,
-- COALESCE падает в финальный 0 — источник 'zero', а не 'nominal_fallback'.
--
-- ВАЖНО: valuation_source добавлен В КОНЕЦ списка колонок — CREATE OR REPLACE
-- VIEW в PostgreSQL дописывает только в конец (см. 010). Первые 21 колонок
-- обязаны совпадать с 010 по имени, типу и порядку.
CREATE OR REPLACE VIEW v_portfolio_positions_valuation AS
SELECT
    p.isin,
    p.broker_name,
    p.account_id,
    p.ticker,
    p.name,
    p.quantity,
    p.average_price,
    p.current_price,
    c.nominal,
    c.market_price,
    ROUND(COALESCE(
        NULLIF(p.current_value, 0),
        NULLIF(p.quantity * p.current_price, 0),
        NULLIF(p.quantity * c.market_price, 0),
        CASE WHEN COALESCE(c.maturity_date, CURRENT_DATE) >= CURRENT_DATE
             THEN p.quantity * c.nominal END,
        0
    )::numeric, 2) AS position_value_rub,
    p.liquidity_loss_ratio,
    c.duration_modified,
    c.ytm,
    c.risk_level,
    c.list_level,
    COALESCE(c.coupon_rate_percent, mc_floater.metric_value::numeric) AS coupon_rate_percent,
    c.coupon_quantity_per_year,
    co.id AS company_id,
    COALESCE(co.name, c.name, p.name) AS company_name,
    p.status AS position_status,
    CASE
        WHEN NULLIF(p.current_value, 0) IS NOT NULL THEN 'broker_value'
        WHEN NULLIF(p.quantity * p.current_price, 0) IS NOT NULL THEN 'broker_price'
        WHEN NULLIF(p.quantity * c.market_price, 0) IS NOT NULL THEN 'market_price'
        WHEN COALESCE(c.maturity_date, CURRENT_DATE) >= CURRENT_DATE
             AND (p.quantity * c.nominal) IS NOT NULL THEN 'nominal_fallback'
        ELSE 'zero'
    END AS valuation_source
FROM portfolio_positions p
LEFT JOIN bonds_catalog c ON c.isin = p.isin
LEFT JOIN companies co ON co.id = c.company_id
LEFT JOIN (
    SELECT DISTINCT ON (isin) isin, metric_value
    FROM monitoring_checks
    WHERE metric_name = 'floater_coupon_calculator'
    ORDER BY isin, check_date DESC
) mc_floater ON mc_floater.isin = p.isin;
