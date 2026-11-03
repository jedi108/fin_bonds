-- FinBonds portfolio balance and allocation summary (read-only).
-- Usage: /opt/data/profiles/fin_bonds/bin/pg.sh -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-portfolio-summary/scripts/portfolio_summary.sql

-- 1) Portfolio breakdown by broker and account
SELECT 
    p.broker_name,
    COALESCE(NULLIF(p.account_id, ''), 'основной') AS account_id,
    COUNT(*) AS positions_count,
    ROUND(SUM(p.quantity * p.average_price)::numeric, 0) AS invested_rub,
    ROUND(SUM(p.current_value)::numeric, 0) AS current_value_rub,
    ROUND(SUM(p.current_value - (p.quantity * p.average_price))::numeric, 0) AS unrealized_pnl_rub,
    ROUND(100.0 * SUM(p.current_value)::numeric / 
          NULLIF((SELECT SUM(current_value) FROM portfolio_positions), 0)::numeric, 1) AS share_pct
FROM portfolio_positions p
GROUP BY p.broker_name, p.account_id
ORDER BY current_value_rub DESC;

-- 2) Total portfolio summary
SELECT 
    COUNT(*) AS total_positions,
    ROUND(SUM(quantity * average_price)::numeric, 0) AS total_invested_rub,
    ROUND(SUM(current_value)::numeric, 0) AS total_current_value_rub,
    ROUND(SUM(current_value - (quantity * average_price))::numeric, 0) AS total_pnl_rub,
    ROUND(100.0 * SUM(current_value - (quantity * average_price))::numeric / 
          NULLIF(SUM(quantity * average_price)::numeric, 0), 2) AS total_pnl_pct
FROM portfolio_positions;

-- 3) Freshness timestamp
SELECT 
    MAX(updated_at) AS last_positions_sync
FROM portfolio_positions;
