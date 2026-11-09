-- 007_add_duration.sql
-- Дюрация Маколея и модифицированная дюрация в каталоге облигаций.

ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS duration_macaulay NUMERIC;
ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS duration_modified NUMERIC;
ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS duration_null_reason TEXT;
ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS duration_updated_at TIMESTAMPTZ;
