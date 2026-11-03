-- 017_company_debt_snapshots.sql
-- issue #4: снимки долговой нагрузки компании по отчётной дате и источнику.
-- Версионирование: (company_id, report_date, source) уникален, повторный импорт
-- идемпотентен. Все суммы в рублях; отсутствующая метрика = NULL (не 0).
-- net_debt_to_ebitda в таблице НЕ хранится — считается в отчёте, и только
-- при наличии net_debt и ebitda. issued_bond_volume_rub — объём размещения ×
-- номинал (оценка), outstanding_bond_debt_rub — реальный остаток облигационного
-- долга из отчётности; первое не равно второму и не выдаётся за леверидж.

CREATE TABLE IF NOT EXISTS company_debt_snapshots (
    id BIGSERIAL PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies(id),
    report_date DATE NOT NULL,
    source TEXT NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_debt_rub NUMERIC,
    bond_debt_rub NUMERIC,
    bank_debt_rub NUMERIC,
    lease_debt_rub NUMERIC,
    cash_rub NUMERIC,
    net_debt_rub NUMERIC,
    revenue_rub NUMERIC,
    ebitda_rub NUMERIC,
    issued_bond_volume_rub NUMERIC,
    outstanding_bond_debt_rub NUMERIC,
    coverage_status TEXT NOT NULL DEFAULT 'unknown',   -- full | partial | unknown
    quality_status TEXT NOT NULL DEFAULT 'unverified', -- verified | unverified | estimated
    source_ref TEXT,
    notes TEXT,
    UNIQUE (company_id, report_date, source)
);
