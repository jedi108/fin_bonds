-- FinBonds portfolio risk snapshot (read-only).
-- Usage: /opt/data/profiles/fin_bonds/bin/pg.sh -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-risk-assessment/scripts/risk_snapshot.sql
-- Все оценки позиций берутся из канонического view v_portfolio_positions_valuation
-- (position_value_rub в рублях за позицию); деления на итоговую стоимость
-- защищены NULLIF(SUM(position_value_rub), 0).

-- 1) Issuer concentration (company link resolved in the view via companies).
SELECT COALESCE(co.name, '(без компании)') AS issuer,
       COUNT(*) AS positions,
       ROUND(SUM(v.position_value_rub)::numeric, 0) AS value,
       ROUND(100.0 * SUM(v.position_value_rub)::numeric /
             NULLIF((SELECT SUM(position_value_rub) FROM v_portfolio_positions_valuation), 0)::numeric, 1) AS pct
FROM v_portfolio_positions_valuation v
LEFT JOIN companies co ON co.id = v.company_id
GROUP BY issuer
ORDER BY pct DESC;

-- 2) Risk-level distribution (bonds_catalog.risk_level; NULL = unknown).
SELECT COALESCE(v.risk_level, -1) AS risk_level,
       COUNT(*) AS positions,
       ROUND(100.0 * SUM(v.position_value_rub)::numeric /
             NULLIF((SELECT SUM(position_value_rub) FROM v_portfolio_positions_valuation), 0)::numeric, 1) AS pct
FROM v_portfolio_positions_valuation v
GROUP BY v.risk_level
ORDER BY risk_level;

-- 3a) Portfolio weighted modified duration & coverage.
SELECT
    ROUND(
        SUM(CASE WHEN v.duration_modified IS NOT NULL THEN v.position_value_rub * v.duration_modified ELSE 0 END)::numeric /
        NULLIF(SUM(CASE WHEN v.duration_modified IS NOT NULL THEN v.position_value_rub ELSE 0 END), 0)::numeric,
        2
    ) AS weighted_duration_modified,
    COUNT(CASE WHEN v.duration_modified IS NOT NULL THEN 1 END) AS covered_positions,
    COUNT(*) AS total_positions,
    ROUND(SUM(CASE WHEN v.duration_modified IS NOT NULL THEN v.position_value_rub ELSE 0 END)::numeric, 0) AS covered_value,
    ROUND(SUM(v.position_value_rub)::numeric, 0) AS total_value,
    ROUND(100.0 * SUM(CASE WHEN v.duration_modified IS NOT NULL THEN v.position_value_rub ELSE 0 END)::numeric /
          NULLIF(SUM(v.position_value_rub), 0)::numeric, 1) AS coverage_pct
FROM v_portfolio_positions_valuation v;

-- 3b) Positions without duration and exclusion reasons.
SELECT COALESCE(c.duration_null_reason, 'not_calculated') AS reason,
       COUNT(*) AS positions,
       ROUND(SUM(v.position_value_rub)::numeric, 0) AS value,
       ROUND(100.0 * SUM(v.position_value_rub)::numeric /
             NULLIF((SELECT SUM(position_value_rub) FROM v_portfolio_positions_valuation), 0)::numeric, 1) AS pct
FROM v_portfolio_positions_valuation v
LEFT JOIN bonds_catalog c ON c.isin = v.isin
WHERE c.duration_modified IS NULL
GROUP BY reason
ORDER BY value DESC;

-- 4) Liquidity loss (loss on exit; NULL = not calculated yet).
SELECT v.isin, v.name,
       v.liquidity_loss_ratio,
       ROUND(v.position_value_rub::numeric, 0) AS value
FROM v_portfolio_positions_valuation v
WHERE v.liquidity_loss_ratio IS NOT NULL
ORDER BY v.liquidity_loss_ratio DESC
LIMIT 10;

-- 5) Data freshness guard (see finbonds-data-refresh thresholds).
SELECT MAX(market_price_updated_at) AS last_market_price,
       (SELECT MAX(updated_at) FROM portfolio_positions) AS last_positions_sync,
       (
           SELECT COUNT(*)
           FROM portfolio_positions p
           LEFT JOIN bonds_catalog c ON c.isin = p.isin
           WHERE c.duration_updated_at IS NULL
              OR (c.market_price_updated_at IS NOT NULL AND c.duration_updated_at < c.market_price_updated_at)
       ) AS positions_with_stale_duration
FROM bonds_catalog;
