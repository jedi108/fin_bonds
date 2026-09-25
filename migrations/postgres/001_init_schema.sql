-- 001_init_schema.sql
-- Начальная схема fin_bonds для PostgreSQL.
-- Перенос SQLite-схемы из src/monitoring/storage.py::_create_tables().
--
-- ВАЖНО: типы подобраны совместимо с текущим SQLite-кодом приложения:
--   * даты хранятся как TEXT в формате 'YYYY-MM-MM' (как в SQLite);
--   * булевы флаги — INTEGER 0/1;
--   * REAL -> DOUBLE PRECISION.
-- Это позволяет переносить storage.py на PostgreSQL с минимальными изменениями.

-- Основной каталог облигаций
CREATE TABLE IF NOT EXISTS bonds_catalog (
    isin TEXT PRIMARY KEY NOT NULL,
    figi TEXT,
    ticker TEXT,
    name TEXT,
    currency TEXT,
    nominal DOUBLE PRECISION,
    maturity_date TEXT,
    offer_date TEXT,
    coupon_quantity_per_year INTEGER,
    coupon_rate_percent DOUBLE PRECISION,
    coupon_type TEXT,
    list_level INTEGER,
    risk_level INTEGER,
    coupon_spread DOUBLE PRECISION,
    is_trade_available INTEGER,
    is_for_qualified_investors INTEGER,
    issue_size BIGINT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    floating_coupon_flag INTEGER,
    perpetual_flag INTEGER,
    amortization_flag INTEGER
);

-- История рейтингов
CREATE TABLE IF NOT EXISTS rating_history (
    id BIGSERIAL PRIMARY KEY,
    isin TEXT NOT NULL,
    rating_date TEXT NOT NULL,
    rating_code TEXT,
    rating_score INTEGER,
    FOREIGN KEY (isin) REFERENCES bonds_catalog(isin)
);

-- История уровней риска от Тинькофф
CREATE TABLE IF NOT EXISTS risk_history (
    id BIGSERIAL PRIMARY KEY,
    isin TEXT NOT NULL,
    risk_date TEXT NOT NULL,
    risk_level INTEGER,
    FOREIGN KEY (isin) REFERENCES bonds_catalog(isin)
);

-- История уровней листинга на Мосбирже
CREATE TABLE IF NOT EXISTS listlevel_history (
    id BIGSERIAL PRIMARY KEY,
    isin TEXT NOT NULL,
    check_date TEXT NOT NULL,
    list_level INTEGER,
    FOREIGN KEY (isin) REFERENCES bonds_catalog(isin)
);

-- Рассчитанные значения купонов (ставка флоатера на дату)
CREATE TABLE IF NOT EXISTS calculated_coupons (
    id BIGSERIAL PRIMARY KEY,
    isin TEXT NOT NULL,
    check_date TEXT NOT NULL,
    calculated_rate DOUBLE PRECISION NOT NULL,
    UNIQUE(isin, check_date)
);

-- Текущие позиции в портфеле
CREATE TABLE IF NOT EXISTS portfolio_positions (
    id BIGSERIAL PRIMARY KEY,
    isin TEXT NOT NULL,
    ticker TEXT,
    name TEXT,
    quantity DOUBLE PRECISION,
    average_price DOUBLE PRECISION,
    current_price DOUBLE PRECISION,
    currency TEXT,
    yield_to_maturity DOUBLE PRECISION,
    portfolio_percent DOUBLE PRECISION,
    current_value DOUBLE PRECISION,
    broker_name TEXT NOT NULL,
    instrument_type TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(isin, broker_name)
);

-- История анализа ликвидности
CREATE TABLE IF NOT EXISTS liquidity_history (
    id BIGSERIAL PRIMARY KEY,
    isin TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    base_volume DOUBLE PRECISION NOT NULL,
    market_exit_value DOUBLE PRECISION NOT NULL,
    loss_ratio DOUBLE PRECISION NOT NULL,
    depth_level INTEGER NOT NULL,
    data_source TEXT NOT NULL,
    FOREIGN KEY (isin) REFERENCES bonds_catalog(isin)
);

-- Результаты мониторинга
CREATE TABLE IF NOT EXISTS monitoring_checks (
    id BIGSERIAL PRIMARY KEY,
    isin TEXT NOT NULL,
    check_date TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value TEXT,
    UNIQUE(isin, check_date, metric_name)
);

-- Автоматическое обновление portfolio_positions.updated_at при UPDATE
-- (аналог SQLite-триггера update_positions_updated_at;
-- в PostgreSQL BEFORE-триггер не порождает рекурсию)
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS update_positions_updated_at ON portfolio_positions;
CREATE TRIGGER update_positions_updated_at
BEFORE UPDATE ON portfolio_positions
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
