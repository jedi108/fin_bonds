-- 002_add_market_prices.sql
-- Рыночные цены в каталоге облигаций.
-- Аналог миграции из storage.py (_add_column_if_not_exists для market_price*).

ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS market_price DOUBLE PRECISION;
ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS market_price_source TEXT;
ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS market_price_updated_at TEXT;
