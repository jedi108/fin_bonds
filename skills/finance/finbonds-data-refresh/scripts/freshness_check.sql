-- FinBonds instant in-container freshness gate (v2: зомби-строки = WARN, не CRITICAL).
-- Usage: /opt/data/profiles/fin_bonds/bin/pg.sh --csv -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-data-refresh/scripts/freshness_check.sql
--   (--csv даёт строку заголовков — фикс P9.3; прежний -t -A -F'|' тоже допустим)
-- Контракт статусов:
--   OK                        — данные свежие, zero-строк нет
--   WARN_ZOMBIE_ROWS          — данные свежие, есть zero-строки, но подозрительных
--                               (нулевая оценка на НЕпогашенной бумаге) не больше 2;
--                               is_fresh=true, полный список виновников в
--                               zero_rows_isins; ребалансировке не мешают, вручную
--                               не разбирать
--   CRITICAL_ZERO_VALUATION   — >2 zero-строк на непогашенных бумагах (массовый
--                               сбой синка, класс бага из 001); is_fresh=false.
--                               Порог по непогашенным, а не по всем строкам:
--                               кейс 2026-09-29 — 3 zero-строки (погашенная
--                               эмитент Г 1 + эмитент Д 2), из них подозрительных 2
--                               → WARN, а не повторный ложный CRITICAL
--   CRITICAL_NO_PRICES / STALE_PRICES / STALE_PORTFOLIO / EMPTY_PORTFOLIO — как в v1
WITH metrics AS (
    SELECT
        COUNT(*) AS total_catalog,
        COUNT(*) FILTER (WHERE is_trade_available IS TRUE) AS tradeable_bonds,
        COUNT(*) FILTER (WHERE market_price IS NOT NULL) AS bonds_with_price,
        COUNT(*) FILTER (
            WHERE is_trade_available IS TRUE
              AND (market_price IS NULL OR market_price_updated_at IS NULL
                   OR market_price_updated_at < NOW() - INTERVAL '24 hours')
        ) AS tradeable_stale_or_missing_prices,
        MAX(market_price_updated_at) AS max_market_price_time,
        (SELECT COUNT(*) FROM portfolio_positions) AS portfolio_rows,
        -- Все zero-строки (для отчёта и условия WARN)
        (SELECT COUNT(*) FROM portfolio_positions WHERE current_value IS NULL OR current_value <= 0) AS portfolio_zero_rows,
        -- Подозрительные: zero-строка на непогашенной бумаге (брокер занулил живую
        -- позицию — класс бага из 001). ISIN нет в каталоге или дата погашения
        -- неизвестна → считаем подозрительной (COALESCE → CURRENT_DATE)
        (SELECT COUNT(*) FROM portfolio_positions pp
          WHERE (pp.current_value IS NULL OR pp.current_value <= 0)
            AND COALESCE((SELECT c.maturity_date FROM bonds_catalog c
                          WHERE c.isin = pp.isin), CURRENT_DATE) >= CURRENT_DATE
        ) AS zero_rows_suspicious,
        (SELECT string_agg(DISTINCT isin, ';' ORDER BY isin)
           FROM portfolio_positions WHERE current_value IS NULL OR current_value <= 0) AS zero_rows_isins,
        (SELECT ROUND(SUM(COALESCE(current_value, quantity * current_price, 0))::numeric, 0) FROM portfolio_positions) AS portfolio_total_val,
        (SELECT MAX(updated_at) FROM portfolio_positions) AS last_sync_time
    FROM bonds_catalog
)
SELECT
    CASE
        WHEN portfolio_rows = 0 THEN 'EMPTY_PORTFOLIO'
        WHEN zero_rows_suspicious > 2 THEN 'CRITICAL_ZERO_VALUATION'
        WHEN tradeable_bonds = 0 OR bonds_with_price = 0 THEN 'CRITICAL_NO_PRICES'
        WHEN tradeable_stale_or_missing_prices > (tradeable_bonds * 0.2) THEN 'STALE_PRICES'
        WHEN last_sync_time IS NULL OR last_sync_time < NOW() - INTERVAL '3 days' THEN 'STALE_PORTFOLIO'
        WHEN portfolio_zero_rows > 0 THEN 'WARN_ZOMBIE_ROWS'
        ELSE 'OK'
    END AS status,
    CASE
        WHEN portfolio_rows > 0
          AND zero_rows_suspicious <= 2
          AND tradeable_bonds > 0
          AND bonds_with_price > 0
          AND tradeable_stale_or_missing_prices <= (tradeable_bonds * 0.2)
          AND last_sync_time IS NOT NULL
          AND last_sync_time >= NOW() - INTERVAL '3 days'
        THEN 'true'
        ELSE 'false'
    END AS is_fresh,
    portfolio_rows,
    portfolio_total_val,
    portfolio_zero_rows,
    COALESCE(zero_rows_isins, '') AS zero_rows_isins,
    bonds_with_price,
    tradeable_stale_or_missing_prices,
    COALESCE(TO_CHAR(max_market_price_time, 'YYYY-MM-DD HH24:MI:SS'), 'NULL') AS market_time,
    COALESCE(TO_CHAR(last_sync_time, 'YYYY-MM-DD HH24:MI:SS'), 'NULL') AS sync_time
FROM metrics;
