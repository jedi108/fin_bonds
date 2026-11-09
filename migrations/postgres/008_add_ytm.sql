-- 008_add_ytm.sql
-- Каноническая доходность к погашению (YTM) в каталоге облигаций.

ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS ytm NUMERIC;
ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS ytm_null_reason TEXT;
ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS ytm_updated_at TIMESTAMPTZ;
