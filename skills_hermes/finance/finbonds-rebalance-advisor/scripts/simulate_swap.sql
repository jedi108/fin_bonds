-- FinBonds Swap Simulator & Financial Increment Calculator
-- Usage: /opt/data/profiles/fin_bonds/bin/pg.sh -v sell_isin='RU000A...' -v sell_qty=50 -v buy_isin='RU000B...' -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-rebalance-advisor/scripts/simulate_swap.sql
-- Двухногая покупка (опционально, 002.4/P6.3): вторая нога — buy_isin_2 с ЯВНОЙ
-- суммой buy_amount_2 (руб.); нога 1 покупает на остаток выручки. Итоговый
-- портфель и лимиты цепочки свопов всё равно проверяет scenario_summary.sql.
-- Usage 2: /opt/data/profiles/fin_bonds/bin/pg.sh -v sell_isin='RU000A...' -v sell_qty=50 \
--              -v buy_isin='RU000B...' -v buy_isin_2='RU000C...' -v buy_amount_2=100000 -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-rebalance-advisor/scripts/simulate_swap.sql

\if :{?sell_isin} \else \echo 'Error: sell_isin is required' \q \endif
\if :{?sell_qty}  \else \echo 'Error: sell_qty is required'  \q \endif
\if :{?buy_isin}  \else \echo 'Error: buy_isin is required'  \q \endif
\if :{?buy_isin_2}
    \if :{?buy_amount_2} \else \echo 'Error: buy_amount_2 is required when buy_isin_2 is set' \q \endif
\else
    \set buy_isin_2 ''
    \set buy_amount_2 0
\endif

WITH portfolio_total AS (
    SELECT COALESCE(SUM(position_value_rub), 0) AS total_val
    FROM v_portfolio_positions_valuation
),
sell_asset AS (
    SELECT
        v.isin, v.ticker, v.name, v.company_name, v.company_id,
        COALESCE(NULLIF(v.current_price, 0), v.market_price, v.nominal, 0) AS effective_sell_price,
        v.nominal,
        COALESCE(v.coupon_rate_percent, 0) AS coupon_rate_percent,
        (:sell_qty * COALESCE(NULLIF(v.current_price, 0), v.market_price, v.nominal, 0)) AS sell_amount_rub,
        ROUND(((:sell_qty * v.nominal * COALESCE(v.coupon_rate_percent, 0)) / 1200.0)::numeric, 2) AS monthly_income_sell
    FROM v_portfolio_positions_valuation v
    WHERE v.isin = :'sell_isin'
    LIMIT 1
),
buy_asset AS (
    SELECT
        b.leg, b.isin, b.ticker, b.name, b.company_name, b.company_id,
        b.effective_buy_price, b.nominal, b.coupon_rate_percent, b.risk_level, b.list_level,
        FLOOR(b.budget_rub / NULLIF(b.effective_buy_price, 0))::int AS buy_qty,
        (FLOOR(b.budget_rub / NULLIF(b.effective_buy_price, 0)) * b.effective_buy_price) AS buy_amount_rub,
        ROUND((FLOOR(b.budget_rub / NULLIF(b.effective_buy_price, 0)) * b.nominal * b.coupon_rate_percent / 1200.0)::numeric, 2) AS monthly_income_buy
    FROM (
        SELECT 1 AS leg, c.isin, c.ticker, c.name, COALESCE(co.name, c.name) AS company_name, c.company_id,
               COALESCE(NULLIF(c.market_price, 0), c.nominal, 1000) AS effective_buy_price,
               c.nominal,
               COALESCE(c.coupon_rate_percent, mc_floater.metric_value::numeric, 0) AS coupon_rate_percent,
               c.risk_level, c.list_level,
               CASE WHEN :'buy_isin_2' <> ''
                    THEN GREATEST((SELECT sell_amount_rub FROM sell_asset) - :'buy_amount_2'::numeric, 0)
                    ELSE (SELECT sell_amount_rub FROM sell_asset) END AS budget_rub
        FROM bonds_catalog c
        LEFT JOIN companies co ON co.id = c.company_id
        LEFT JOIN (
            SELECT DISTINCT ON (isin) isin, metric_value
            FROM monitoring_checks
            WHERE metric_name = 'floater_coupon_calculator'
            ORDER BY isin, check_date DESC
        ) mc_floater ON mc_floater.isin = c.isin
        WHERE c.isin = :'buy_isin'
        UNION ALL
        SELECT 2 AS leg, c.isin, c.ticker, c.name, COALESCE(co.name, c.name) AS company_name, c.company_id,
               COALESCE(NULLIF(c.market_price, 0), c.nominal, 1000) AS effective_buy_price,
               c.nominal,
               COALESCE(c.coupon_rate_percent, mc_floater.metric_value::numeric, 0) AS coupon_rate_percent,
               c.risk_level, c.list_level,
               :'buy_amount_2'::numeric AS budget_rub
        FROM bonds_catalog c
        LEFT JOIN companies co ON co.id = c.company_id
        LEFT JOIN (
            SELECT DISTINCT ON (isin) isin, metric_value
            FROM monitoring_checks
            WHERE metric_name = 'floater_coupon_calculator'
            ORDER BY isin, check_date DESC
        ) mc_floater ON mc_floater.isin = c.isin
        WHERE :'buy_isin_2' <> '' AND c.isin = :'buy_isin_2'
    ) b
),
issuer_check AS (
    SELECT
        b.leg,
        b.company_name,
        b.company_id,
        ROUND((
            (COALESCE(SUM(v.position_value_rub), 0)
             - CASE WHEN b.company_id IS NOT NULL AND b.company_id = (SELECT company_id FROM sell_asset) THEN (SELECT sell_amount_rub FROM sell_asset) ELSE 0 END
             + CASE WHEN b.company_id IS NULL THEN b.buy_amount_rub
                    ELSE COALESCE((SELECT SUM(b2.buy_amount_rub) FROM buy_asset b2 WHERE b2.company_id = b.company_id), 0) END
             ) * 100.0 / NULLIF((SELECT total_val FROM portfolio_total), 0)
        )::numeric, 2) AS post_trade_issuer_pct
    FROM buy_asset b
    LEFT JOIN v_portfolio_positions_valuation v ON (b.company_id IS NOT NULL AND v.company_id = b.company_id)
    GROUP BY b.leg, b.company_name, b.company_id, b.buy_amount_rub
)
SELECT
    s.isin AS sell_isin,
    s.name AS sell_name,
    :sell_qty AS sell_qty,
    s.sell_amount_rub,
    s.monthly_income_sell,
    b.isin AS buy_isin,
    b.name AS buy_name,
    b.buy_qty,
    b.buy_amount_rub,
    (s.sell_amount_rub - b.buy_amount_rub) AS cash_remainder_rub,
    b.monthly_income_buy,
    (b.monthly_income_buy - s.monthly_income_sell) AS delta_monthly_rub,
    ((b.monthly_income_buy - s.monthly_income_sell) * 3) AS delta_quarterly_rub,
    ic.company_name AS buy_company,
    ic.post_trade_issuer_pct,
    CASE
        WHEN ic.company_id IS NULL THEN 'BLOCKED: NO_COMPANY_LINK'
        WHEN ic.post_trade_issuer_pct > 15.0 THEN 'LIMIT_VIOLATION (>15%)'
        ELSE 'PASS'
    END AS limit_status
FROM sell_asset s, buy_asset b, issuer_check ic
WHERE ic.leg = b.leg
ORDER BY b.leg;
