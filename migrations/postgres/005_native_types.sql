-- 005_native_types.sql
-- Перевод схемы 001-004 с «совместимых с SQLite» типов на нативные типы
-- PostgreSQL: даты TEXT -> DATE, метки времени TEXT -> TIMESTAMPTZ,
-- деньги/проценты DOUBLE PRECISION -> NUMERIC, булевы флаги INTEGER 0/1 -> BOOLEAN.
-- Уже применённые базы (версии 001-004 зафиксированы в schema_migrations)
-- получают новые типы именно этой миграцией; на чистой базе файлы применяются
-- по порядку 001 -> 005.
--
-- Замечания:
--   * NULLIF(col, '') защищает ::date / ::timestamptz от пустых строк;
--     NULL приводится молча.
--   * NOT NULL, DEFAULT (в т.ч. DEFAULT CURRENT_TIMESTAMP — валиден и для
--     TIMESTAMPTZ) при ALTER TYPE сохраняются автоматически.
--   * UNIQUE-ограничения (calculated_coupons(isin, check_date),
--     monitoring_checks(isin, check_date, metric_name)) и индексы по
--     конвертируемым колонкам PG перестраивает вместе с колонкой.
--   * Не трогаются: bonds_catalog.company_id, числовые не-флаги
--     (coupon_quantity_per_year, list_level, risk_level, rating_score,
--     issue_size, liquidity_history.depth_level), триггер set_updated_at.

-- bonds_catalog: деньги/проценты, даты, метки времени, флаги
ALTER TABLE bonds_catalog
    ALTER COLUMN nominal TYPE NUMERIC,
    ALTER COLUMN maturity_date TYPE DATE USING NULLIF(maturity_date, '')::date,
    ALTER COLUMN offer_date TYPE DATE USING NULLIF(offer_date, '')::date,
    ALTER COLUMN coupon_rate_percent TYPE NUMERIC,
    ALTER COLUMN coupon_spread TYPE NUMERIC,
    ALTER COLUMN is_trade_available TYPE BOOLEAN USING is_trade_available::boolean,
    ALTER COLUMN is_for_qualified_investors TYPE BOOLEAN USING is_for_qualified_investors::boolean,
    ALTER COLUMN created_at TYPE TIMESTAMPTZ USING NULLIF(created_at, '')::timestamptz,
    ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING NULLIF(updated_at, '')::timestamptz,
    ALTER COLUMN floating_coupon_flag TYPE BOOLEAN USING floating_coupon_flag::boolean,
    ALTER COLUMN perpetual_flag TYPE BOOLEAN USING perpetual_flag::boolean,
    ALTER COLUMN amortization_flag TYPE BOOLEAN USING amortization_flag::boolean,
    ALTER COLUMN market_price TYPE NUMERIC,
    ALTER COLUMN market_price_updated_at TYPE TIMESTAMPTZ USING NULLIF(market_price_updated_at, '')::timestamptz;

-- rating_history: дата рейтинга
ALTER TABLE rating_history
    ALTER COLUMN rating_date TYPE DATE USING NULLIF(rating_date, '')::date;

-- risk_history: дата уровня риска
ALTER TABLE risk_history
    ALTER COLUMN risk_date TYPE DATE USING NULLIF(risk_date, '')::date;

-- listlevel_history: дата проверки уровня листинга
ALTER TABLE listlevel_history
    ALTER COLUMN check_date TYPE DATE USING NULLIF(check_date, '')::date;

-- calculated_coupons: дата проверки и ставка флоатера
ALTER TABLE calculated_coupons
    ALTER COLUMN check_date TYPE DATE USING NULLIF(check_date, '')::date,
    ALTER COLUMN calculated_rate TYPE NUMERIC;

-- portfolio_positions: денежные колонки и метки времени
ALTER TABLE portfolio_positions
    ALTER COLUMN quantity TYPE NUMERIC,
    ALTER COLUMN average_price TYPE NUMERIC,
    ALTER COLUMN current_price TYPE NUMERIC,
    ALTER COLUMN yield_to_maturity TYPE NUMERIC,
    ALTER COLUMN portfolio_percent TYPE NUMERIC,
    ALTER COLUMN current_value TYPE NUMERIC,
    ALTER COLUMN created_at TYPE TIMESTAMPTZ USING NULLIF(created_at, '')::timestamptz,
    ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING NULLIF(updated_at, '')::timestamptz,
    ALTER COLUMN liquidity_loss_ratio TYPE NUMERIC,
    ALTER COLUMN coupon_rate_percent TYPE NUMERIC;

-- liquidity_history: метка времени и показатели ликвидности
ALTER TABLE liquidity_history
    ALTER COLUMN "timestamp" TYPE TIMESTAMPTZ USING NULLIF("timestamp", '')::timestamptz,
    ALTER COLUMN base_volume TYPE NUMERIC,
    ALTER COLUMN market_exit_value TYPE NUMERIC,
    ALTER COLUMN loss_ratio TYPE NUMERIC;

-- monitoring_checks: дата проверки
ALTER TABLE monitoring_checks
    ALTER COLUMN check_date TYPE DATE USING NULLIF(check_date, '')::date;

-- companies: метки времени (миграция 004)
ALTER TABLE companies
    ALTER COLUMN created_at TYPE TIMESTAMPTZ USING NULLIF(created_at, '')::timestamptz,
    ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING NULLIF(updated_at, '')::timestamptz;
