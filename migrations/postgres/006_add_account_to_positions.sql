-- 006: измерение "счёт" для позиций портфеля (T-Bank: несколько account_id).
-- account_id: идентификатор счёта у брокера ('' для Excel-импорта и старых строк).
-- Новый уникальный ключ (isin, broker_name, account_id) — позиции одного ISIN
-- на разных счетах хранятся построчно; агрегация — SUM/GROUP BY в запросах.

ALTER TABLE portfolio_positions ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT '';

-- Дроп старого 2-колоночного UNIQUE(isin, broker_name) — по составу колонок,
-- а не по имени (детерминированно, независимо от истории применения 001).
-- Порядок колонок в констрейнте не важен (@> + ровно 2 колонки), и дропаются
-- ВСЕ совпавшие констрейнты (FOR-цикл, а не только первый).
DO $$
DECLARE c text;
BEGIN
    FOR c IN
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_attribute a1 ON a1.attrelid = con.conrelid AND a1.attname = 'isin'
        JOIN pg_attribute a2 ON a2.attrelid = con.conrelid AND a2.attname = 'broker_name'
        WHERE con.conrelid = 'portfolio_positions'::regclass
          AND con.contype = 'u'
          AND array_length(con.conkey, 1) = 2
          AND con.conkey @> ARRAY[a1.attnum, a2.attnum]
    LOOP
        EXECUTE format('ALTER TABLE portfolio_positions DROP CONSTRAINT %I', c);
    END LOOP;
END $$;

ALTER TABLE portfolio_positions ADD CONSTRAINT portfolio_positions_uniq_isin_broker_account UNIQUE (isin, broker_name, account_id);
