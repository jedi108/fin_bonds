-- Migration 009: Canonical portfolio positions valuation view
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
        p.quantity * c.nominal,
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
    COALESCE(co.name, c.name, p.name) AS company_name
FROM portfolio_positions p
LEFT JOIN bonds_catalog c ON c.isin = p.isin
LEFT JOIN companies co ON co.id = c.company_id
LEFT JOIN (
    SELECT DISTINCT ON (isin) isin, metric_value
    FROM monitoring_checks
    WHERE metric_name = 'floater_coupon_calculator'
    ORDER BY isin, check_date DESC
) mc_floater ON mc_floater.isin = p.isin;
