-- FinBonds portfolio rebalance export for LLM / external advisor (read-only).
-- Output contains all crucial columns for allocation, duration, credit risk and exit loss analysis.
-- Usage (table/markdown): "${FIN_BONDS_PSQL:-psql}" -f "$FIN_BONDS_SKILLS_DIR/finbonds-portfolio-export/scripts/rebalance_export.sql"
-- Usage (CSV):            "${FIN_BONDS_PSQL:-psql}" --csv -f "$FIN_BONDS_SKILLS_DIR/finbonds-portfolio-export/scripts/rebalance_export.sql" > rebalance.csv

WITH latest_ratings AS (
    SELECT DISTINCT ON (isin) isin, rating_code, rating_score
    FROM rating_history
    ORDER BY isin, rating_date DESC
),
portfolio_calc AS (
    SELECT
        p.isin,
        COALESCE(c.name, p.name) AS name,
        COALESCE(co.name, c.name, p.name) AS company,
        p.quantity,
        COALESCE(p.current_price, 0) AS price,
        COALESCE(p.current_value, p.quantity * p.current_price, 0) AS value_rub,
        -- p.yield_to_maturity хранит expected_yield в ₽ от брокера, не YTM%;
        -- временно выставляем NULL до появления честной YTM в bonds_catalog
        NULL::numeric AS ytm,
        COALESCE(c.coupon_rate_percent, p.coupon_rate_percent) AS coupon_rate,
        CASE
            WHEN c.floating_coupon_flag IS TRUE THEN 'FLOAT'
            ELSE 'FIX'
        END AS coupon_type,
        c.coupon_quantity_per_year AS coupon_freq,
        c.maturity_date,
        c.offer_date,
        COALESCE(c.amortization_flag, FALSE) AS is_amort,
        r.rating_code AS rating,
        COALESCE(c.risk_level, -1) AS risk_level,
        p.liquidity_loss_ratio,
        p.broker_name,
        COALESCE(NULLIF(p.account_id, ''), 'main') AS account
    FROM portfolio_positions p
    LEFT JOIN bonds_catalog c ON c.isin = p.isin
    LEFT JOIN companies co ON co.id = c.company_id
    LEFT JOIN latest_ratings r ON r.isin = p.isin
),
total_val AS (
    SELECT NULLIF(SUM(value_rub), 0) AS total_portfolio_value FROM portfolio_calc
)
SELECT
    pc.isin,
    pc.name,
    pc.company,
    pc.quantity,
    ROUND(pc.price::numeric, 2) AS price,
    ROUND(pc.value_rub::numeric, 0) AS value_rub,
    ROUND((100.0 * pc.value_rub / t.total_portfolio_value)::numeric, 2) AS share_pct,
    ROUND(pc.ytm::numeric, 2) AS ytm,
    ROUND(pc.coupon_rate::numeric, 2) AS coupon_rate,
    pc.coupon_type,
    pc.coupon_freq,
    pc.maturity_date,
    pc.offer_date,
    pc.is_amort,
    pc.rating,
    pc.risk_level,
    ROUND(pc.liquidity_loss_ratio::numeric, 2) AS exit_loss_pct,
    pc.broker_name,
    pc.account
FROM portfolio_calc pc
CROSS JOIN total_val t
ORDER BY pc.value_rub DESC;
