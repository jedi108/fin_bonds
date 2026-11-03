-- FinBonds issuer/bond lookup (read-only) — рецепт «найти эмитента/бумагу»
-- (скилл finbonds-sql-recipes, задача 014).
-- Ищет по ISIN/тикеру/имени бумаги и по имени/полному имени эмитента
-- (companies через bonds_catalog.company_id).
-- Параметры:
--   q     - обязательный, подстрока для ILIKE '%...%' (имя/тикер/ISIN);
--   limit - необязательный, максимум строк на блок (default 30).
-- Латиницу можно передавать инлайном: pg.sh --csv -v q='RU000A10XYD1' -f <этот файл>
-- Кириллические значения q - только через файл-обёртку (inline -c с кириллицей
-- запрещён, см. SKILL.md «Анти-паттерны»): в /tmp/q_issuer.sql написать
--   \set q 'Система'
--   \ir /opt/data/profiles/fin_bonds/skills/finance/finbonds-sql-recipes/scripts/issuer_lookup.sql
-- и выполнить: pg.sh --csv -f /tmp/q_issuer.sql
-- Вывод: блок 1 - найденные бумаги (с эмитентом), блок 2 - найденные эмитенты
-- (companies) со счётчиками бумаг каталога. Строка CREATE VIEW в stdout -
-- служебный тег psql, блоки различаются по CSV-заголовкам.

\if :{?q} \else \echo 'Error: q is required (name/ticker/ISIN substring)' \q \endif

\if :{?limit}
\else
  \set limit 30
\endif

CREATE OR REPLACE TEMP VIEW v_matched_issuers AS
SELECT id FROM companies
WHERE name ILIKE '%' || :'q' || '%' OR full_name ILIKE '%' || :'q' || '%'
UNION
SELECT company_id FROM bonds_catalog
WHERE company_id IS NOT NULL
  AND (name ILIKE '%' || :'q' || '%'
       OR ticker ILIKE '%' || :'q' || '%'
       OR isin ILIKE '%' || :'q' || '%');

-- Блок 1: бумаги, подходящие под запрос (в т.ч. по эмитенту).
SELECT bc.isin,
       bc.ticker,
       bc.name AS bond_name,
       bc.company_id,
       COALESCE(co.name, '') AS company,
       COALESCE(co.full_name, '') AS company_full_name,
       COALESCE(co.entity_type, '') AS entity_type,
       bc.maturity_date,
       bc.risk_level,
       bc.list_level,
       bc.is_trade_available
FROM bonds_catalog bc
LEFT JOIN companies co ON co.id = bc.company_id
WHERE bc.isin ILIKE '%' || :'q' || '%'
   OR bc.ticker ILIKE '%' || :'q' || '%'
   OR bc.name ILIKE '%' || :'q' || '%'
   OR co.name ILIKE '%' || :'q' || '%'
   OR co.full_name ILIKE '%' || :'q' || '%'
ORDER BY bc.isin
LIMIT NULLIF(:'limit', '')::integer;

-- Блок 2: эмитенты, подходящие под запрос (напрямую или через их бумаги).
SELECT c.id AS company_id,
       c.name AS company,
       COALESCE(c.full_name, '') AS full_name,
       c.entity_type,
       COUNT(bc.isin) AS bonds_in_catalog,
       COUNT(bc.isin) FILTER (WHERE bc.maturity_date >= CURRENT_DATE) AS live_bonds
FROM companies c
LEFT JOIN bonds_catalog bc ON bc.company_id = c.id
WHERE c.id IN (SELECT id FROM v_matched_issuers)
GROUP BY c.id, c.name, c.full_name, c.entity_type
ORDER BY live_bonds DESC, bonds_in_catalog DESC, c.name
LIMIT NULLIF(:'limit', '')::integer;
