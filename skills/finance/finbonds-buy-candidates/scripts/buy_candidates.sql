-- FinBonds buy candidates screener for LLM / Hermes Agent (read-only).
-- Finds top bond candidates outside current portfolio with low risk and fresh prices.
-- By default (include_held=false) portfolio ISINs are excluded; with include_held=true
-- held bonds stay in the output with a "held (share N%)" badge (task 002.2).
-- Usage: /opt/data/profiles/fin_bonds/bin/pg.sh -v max_risk=1 -v coupon_freq=12 -v coupon_type=FIX -v ofz_mode=exclude -v limit=30 -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-buy-candidates/scripts/buy_candidates.sql
-- Usage (rebalance SOP, with held bonds): add -v include_held=true

\if :{?max_risk}
\else
  \set max_risk 1
\endif

\if :{?limit}
\else
  \set limit 30
\endif

\if :{?coupon_freq}
\else
  \set coupon_freq 0
\endif

\if :{?coupon_type}
\else
  \set coupon_type 'ALL'
\endif

\if :{?ofz_mode}
\else
  \set ofz_mode 'all'
\endif

\if :{?include_held}
\else
  \set include_held 'false'
\endif

WITH latest_ratings AS (
    SELECT DISTINCT ON (isin) isin, rating_code, rating_date
    FROM rating_history
    ORDER BY isin, rating_date DESC, id DESC
),
latest_floater AS (
    SELECT DISTINCT ON (isin) isin, metric_value::numeric AS calculated_rate
    FROM monitoring_checks
    WHERE metric_name = 'floater_coupon_calculator'
    ORDER BY isin, check_date DESC
),
-- Доля бумаги в портфеле «сейчас» (бейдж held (N%), задача 002.2).
-- Агрегат по ISIN через view v_portfolio_positions_valuation обязателен:
-- прямой LEFT JOIN portfolio_positions задвоил бы строки для ISIN на двух
-- счетах (кейс — эмитент Д 5 шт + 2 шт).
held AS (
    SELECT v.isin,
           SUM(v.quantity) AS held_qty,
           ROUND(100.0 * SUM(v.position_value_rub)
                 / NULLIF((SELECT SUM(position_value_rub) FROM v_portfolio_positions_valuation), 0), 2) AS held_share_pct
    FROM v_portfolio_positions_valuation v
    GROUP BY v.isin
),
candidates AS (
    SELECT
        bc.isin,
        bc.ticker,
        bc.name,
        bc.company_id,
        co.name AS company,
        co.entity_type,
        CASE
            WHEN bc.floating_coupon_flag IS TRUE THEN 'FLOAT'
            ELSE 'FIX'
        END AS coupon_type,
        ROUND(COALESCE(
            CASE WHEN bc.floating_coupon_flag IS TRUE THEN lf.calculated_rate ELSE bc.coupon_rate_percent END,
            bc.coupon_rate_percent,
            lf.calculated_rate
        )::numeric, 2) AS coupon_rate,
        bc.coupon_quantity_per_year AS coupon_freq,
        ROUND((
            COALESCE(
                CASE WHEN bc.floating_coupon_flag IS TRUE THEN lf.calculated_rate ELSE bc.coupon_rate_percent END,
                bc.coupon_rate_percent,
                lf.calculated_rate
            ) * bc.nominal / 100
        ) / NULLIF(bc.coupon_quantity_per_year, 0), 2) AS coupon_payment_per_bond,
        ROUND((
            COALESCE(
                CASE WHEN bc.floating_coupon_flag IS TRUE THEN lf.calculated_rate ELSE bc.coupon_rate_percent END,
                bc.coupon_rate_percent,
                lf.calculated_rate
            ) * bc.nominal / 100
        ) / 12.0, 2) AS average_monthly_coupon_per_bond,
        ROUND(bc.market_price::numeric, 2) AS market_price,
        bc.market_price_updated_at,
        ROUND((
            (COALESCE(
                CASE WHEN bc.floating_coupon_flag IS TRUE THEN lf.calculated_rate ELSE bc.coupon_rate_percent END,
                bc.coupon_rate_percent,
                lf.calculated_rate
            ) * bc.nominal / 100) / bc.market_price
        ) * 100.0, 2) AS current_coupon_yield,
        bc.maturity_date,
        bc.offer_date,
        bc.risk_level,
        r.rating_code AS rating,
        r.rating_date,
        bc.floating_coupon_flag,
        bc.coupon_spread,
        h.held_share_pct,
        CASE WHEN h.isin IS NOT NULL THEN 'held (' || h.held_share_pct || '%)' END AS held_badge
    FROM bonds_catalog bc
    LEFT JOIN held h ON h.isin = bc.isin
    JOIN companies co ON co.id = bc.company_id
    LEFT JOIN latest_ratings r ON r.isin = bc.isin
    LEFT JOIN latest_floater lf ON lf.isin = bc.isin
    WHERE bc.currency = 'rub'
      AND bc.is_trade_available IS TRUE
      AND bc.perpetual_flag IS NOT TRUE
      AND (UPPER(:'include_held') = 'TRUE' OR h.isin IS NULL)
      AND bc.company_id IS NOT NULL
      AND bc.is_for_qualified_investors IS NOT TRUE
      AND bc.market_price IS NOT NULL
      AND bc.market_price > 0
      AND bc.market_price_updated_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
      AND bc.issue_size * bc.nominal >= 500000000
      AND (bc.maturity_date IS NULL OR bc.maturity_date > CURRENT_DATE)
      AND bc.risk_level <= NULLIF(:'max_risk', '')::integer
      AND (
          NULLIF(:'coupon_freq', '')::integer IS NULL
          OR NULLIF(:'coupon_freq', '')::integer = 0
          OR bc.coupon_quantity_per_year = NULLIF(:'coupon_freq', '')::integer
      )
      AND (
          UPPER(:'coupon_type') = 'ALL'
          OR (UPPER(:'coupon_type') = 'FIX' AND bc.floating_coupon_flag IS NOT TRUE)
          OR (UPPER(:'coupon_type') = 'FLOAT' AND bc.floating_coupon_flag IS TRUE)
      )
      AND (
          LOWER(:'ofz_mode') = 'all'
          OR (LOWER(:'ofz_mode') IN ('only', 'ofz_only') AND co.entity_type = 'sovereign')
          OR (LOWER(:'ofz_mode') IN ('exclude', 'no_ofz') AND co.entity_type != 'sovereign')
      )
      -- For floaters, require calculated rate and coupon_spread
      AND (
          bc.floating_coupon_flag IS NOT TRUE
          OR (lf.calculated_rate IS NOT NULL AND bc.coupon_spread IS NOT NULL)
      )
      -- For fix, require coupon_rate_percent
      AND (
          bc.floating_coupon_flag IS TRUE
          OR bc.coupon_rate_percent IS NOT NULL
      )
)
SELECT
    isin,
    ticker,
    name,
    company_id,
    company,
    entity_type,
    coupon_type,
    coupon_rate,
    coupon_freq,
    coupon_payment_per_bond,
    average_monthly_coupon_per_bond,
    market_price,
    market_price_updated_at,
    current_coupon_yield,
    maturity_date,
    offer_date,
    risk_level,
    rating,
    rating_date,
    held_share_pct,
    held_badge
FROM candidates
ORDER BY
    CASE WHEN UPPER(:'coupon_type') = 'FLOAT' THEN coupon_rate ELSE current_coupon_yield END DESC NULLS LAST,
    coupon_spread DESC NULLS LAST,
    isin
LIMIT NULLIF(:'limit', '')::integer;
