-- 003_add_portfolio_liquidity.sql
-- Показатели ликвидности и купона в позициях портфеля.
-- Аналог миграции из storage.py (_add_column_if_not_exists для portfolio_positions).

ALTER TABLE portfolio_positions ADD COLUMN IF NOT EXISTS liquidity_loss_ratio DOUBLE PRECISION;
ALTER TABLE portfolio_positions ADD COLUMN IF NOT EXISTS coupon_rate_percent DOUBLE PRECISION;
