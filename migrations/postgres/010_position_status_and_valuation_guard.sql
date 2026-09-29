-- Migration 010: жизненный цикл позиции (status) и guard номинального фолбэка.
-- Задача 002.1 (P1): зомби-позиции (погашенные бумаги, нулевая оценка от брокера)
-- давали фантомную стоимость через nominal-фолбэк view и ложный
-- CRITICAL_ZERO_VALUATION в гейте свежести.
--
-- status: active — позиция у брокера и бумага не погашена;
--         matured — бумага погашена (maturity_date < CURRENT_DATE);
--         closed — позиция закрыта (нулевое количество / отсутствует у брокера).

ALTER TABLE portfolio_positions ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';

-- Значения статуса ограничены доменом active|matured|closed (002.1).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'portfolio_positions_status_check'
          AND conrelid = 'portfolio_positions'::regclass
    ) THEN
        ALTER TABLE portfolio_positions
            ADD CONSTRAINT portfolio_positions_status_check
            CHECK (status IN ('active', 'matured', 'closed'));
    END IF;
END $$;

-- Погашенные бумаги из каталога помечаем как matured (idempotent):
-- кейс 2026-09-29 — МОНОП 1P02 (RU000A10AA02), погашена 2025-12-04,
-- 35 шт всё ещё в позициях с нулевой оценкой.
UPDATE portfolio_positions pp
SET status = 'matured'
FROM bonds_catalog c
WHERE c.isin = pp.isin
  AND c.maturity_date < CURRENT_DATE
  AND pp.status = 'active';

-- Позиции с нулевым/NULL количеством закрываем (idempotent).
UPDATE portfolio_positions
SET status = 'closed'
WHERE COALESCE(quantity, 0) <= 0
  AND status = 'active';

-- Nominal-фолбэк оценки не применяется к погашенным бумагам: нулевая оценка
-- погашенной позиции остаётся нулём (фантом вида «35 шт * 1000 номинал» больше
-- не возникает). Дата погашения неизвестна (NULL/нет в каталоге) — фолбэк
-- сохраняется, чтобы не обнулять легитимные позиции вне каталога.
--
-- ВАЖНО: position_status добавлен В КОНЕЦ списка колонок — CREATE OR REPLACE
-- VIEW в PostgreSQL не может вставить колонку в середину существующего view
-- (009): «cannot change name/name of view column». Первые 20 колонок обязаны
-- совпадать с 009 по имени, типу и порядку.
CREATE OR REPLACE VIEW v_portfolio_positions_valuation AS
SELECT
    p.isin,
    p.broker_name,
    p.account_id,
    p.ticker,
    p.name,
    p.quantity,
    p.average_price,
    p.current_price,
    c.nominal,
    c.market_price,
    ROUND(COALESCE(
        NULLIF(p.current_value, 0),
        NULLIF(p.quantity * p.current_price, 0),
        NULLIF(p.quantity * c.market_price, 0),
        CASE WHEN COALESCE(c.maturity_date, CURRENT_DATE) >= CURRENT_DATE
             THEN p.quantity * c.nominal END,
        0
    )::numeric, 2) AS position_value_rub,
    p.liquidity_loss_ratio,
    c.duration_modified,
    c.ytm,
    c.risk_level,
    c.list_level,
    COALESCE(c.coupon_rate_percent, mc_floater.metric_value::numeric) AS coupon_rate_percent,
    c.coupon_quantity_per_year,
    co.id AS company_id,
    COALESCE(co.name, c.name, p.name) AS company_name,
    p.status AS position_status
FROM portfolio_positions p
LEFT JOIN bonds_catalog c ON c.isin = p.isin
LEFT JOIN companies co ON co.id = c.company_id
LEFT JOIN (
    SELECT DISTINCT ON (isin) isin, metric_value
    FROM monitoring_checks
    WHERE metric_name = 'floater_coupon_calculator'
    ORDER BY isin, check_date DESC
) mc_floater ON mc_floater.isin = p.isin;
