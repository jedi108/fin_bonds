-- 004_add_companies.sql
-- Справочник компаний-эмитентов (юрлиц) и связь «одна компания — много облигаций»:
-- bonds_catalog.company_id -> companies.id.
-- Аналог миграции из storage.py (_create_tables для companies).
-- Суррогатный PK id — точка входа для будущих связей (news, tags и т.п.).

CREATE TABLE IF NOT EXISTS companies (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    full_name TEXT,
    inn TEXT UNIQUE,
    entity_type TEXT NOT NULL DEFAULT 'company',  -- company | individual | region | sovereign
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER update_companies_updated_at
BEFORE UPDATE ON companies
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS company_id BIGINT REFERENCES companies(id);

CREATE INDEX IF NOT EXISTS idx_bonds_catalog_company_id ON bonds_catalog(company_id);
