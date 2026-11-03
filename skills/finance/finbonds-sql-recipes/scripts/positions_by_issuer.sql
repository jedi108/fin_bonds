-- FinBonds portfolio positions by issuer (read-only) — рецепт «позиции
-- портфеля по эмитенту» (скилл finbonds-sql-recipes, задача 014).
-- Источник оценки — канонический view v_portfolio_positions_valuation
-- (position_value_rub, company_id/company_name, position_status,
-- valuation_source). Фильтр position_status ОБЯЗАТЕЛЕН (миграция 010):
-- без него в оценку попадают зомби-позиции matured/closed; значение
-- status='all' снимает фильтр (только аудит, не для оценки портфеля).
-- Параметры:
--   q      - обязательный, эмитент (companies.name/full_name) или бумага
--            каталога (имя/тикер/ISIN) - подстрока для ILIKE '%...%';
--   status - необязательный, default 'active'; 'all' - без фильтра (аудит).
-- Латиницу можно передавать инлайном: "${FIN_BONDS_PSQL:-psql}" --csv -v q='RU000A10XYD1' -f <этот файл>
-- Кириллические значения q - только через файл-обёртку (см. issuer_lookup.sql):
--   \set q 'Глобал'
--   \ir <абсолютный путь: $FIN_BONDS_SKILLS_DIR/finbonds-sql-recipes/scripts/positions_by_issuer.sql>
-- Вывод: блок 1 - сводка по найденным эмитентам (живая оценка, доля
-- портфеля), блок 2 - позиции (брокер/счёт/статус/valuation_source).
-- Строка CREATE VIEW в stdout - служебный тег psql, блоки различаются по
-- CSV-заголовкам.

\if :{?q} \else \echo 'Error: q is required (issuer/bond/ISIN substring)' \q \endif

\if :{?status}
\else
  \set status 'active'
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

-- Блок 1: найденные эмитенты в портфеле (живые позиции, доля портфеля).
SELECT v.company_id,
       MAX(v.company_name) AS company,
       COUNT(*) FILTER (WHERE v.position_status = 'active') AS active_positions,
       ROUND(SUM(v.position_value_rub) FILTER (WHERE v.position_status = 'active')::numeric, 2) AS active_value_rub,
       ROUND(100.0 * SUM(v.position_value_rub) FILTER (WHERE v.position_status = 'active')
             / NULLIF((SELECT SUM(position_value_rub) FROM v_portfolio_positions_valuation
                       WHERE position_status = 'active'), 0)::numeric, 2) AS portfolio_share_pct
FROM v_portfolio_positions_valuation v
WHERE v.company_id IN (SELECT id FROM v_matched_issuers)
GROUP BY v.company_id
ORDER BY active_value_rub DESC;

-- Блок 2: позиции найденных эмитентов (статус - :'status', default active).
SELECT v.isin,
       v.ticker,
       v.name,
       v.broker_name,
       COALESCE(NULLIF(v.account_id, ''), '-') AS account_id,
       v.quantity,
       v.position_value_rub,
       v.position_status,
       v.valuation_source
FROM v_portfolio_positions_valuation v
WHERE v.company_id IN (SELECT id FROM v_matched_issuers)
  AND (UPPER(:'status') = 'ALL' OR v.position_status = :'status')
ORDER BY v.position_value_rub DESC;
