-- 016_moex_yield.sql
-- issue #2: внешняя доходность MOEX для сверки с нашим bonds_catalog.ytm.
-- Пишется пакетным MOEX-шагом update-bonds (одним запросом на доску):
-- marketdata.YIELD + marketdata_yields.YIELDDATE/YIELDDATETYPE.
-- Наш ytm под MOEX не подгоняется — расхождение лишь помечается в выводе.

ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS moex_yield NUMERIC;
ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS moex_yield_date DATE;
ALTER TABLE bonds_catalog ADD COLUMN IF NOT EXISTS moex_yield_date_type TEXT;
