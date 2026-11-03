-- FinBonds Scenario Summary — целевой портфель после набора сделок (P6).
-- Все доли, концентрации, поток и дюрация считаются в БД (нулевой ментальный счёт).
-- Usage (2 файла в ОДНОЙ psql-сессии):
--   1) write_file /tmp/planned_trades.sql:
--        CREATE TEMP TABLE planned(isin text, qty_delta numeric);
--        INSERT INTO planned VALUES ('RU000A10XXXX', -530), ('RU000A10YYYY', 532);
--      qty_delta: продажа — отрицательное, покупка — положительное, в штуках.
--   2) /opt/data/profiles/fin_bonds/bin/pg.sh -f /tmp/planned_trades.sql \
--        -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-rebalance-advisor/scripts/scenario_summary.sql
--   CSV целевого портфеля: те же файлы + флаг --csv (psql печатает подряд и
--   служебные теги CREATE TABLE / INSERT n / CREATE VIEW, и result set'ы;
--   целевой портфель — блок 2, т.е. третий CSV-заголовок после блоков 0–1).
-- Лимит эмитента 15% — как в simulate_swap.sql. Знаменатель долей — стоимость
-- портфеля ПОСЛЕ сделок (кэш-остаток не учитывается). Оценка «после» — по
-- текущей позиционной цене (value/qty), для новых покупок — каталог market_price.
-- Погашенные (maturity_date < CURRENT_DATE): цена и купон = 0, из целевого
-- портфеля (блоки 2–3) исключаются — иначе nominal-фолбэк view и
-- coupon_rate_percent дают фантом (кейс 2026-09-29: Эмитент Г 001Р-01 —
-- N ₽ и M ₽/мес в «целевом портфеле»; P1, артефакт A2 не должен
-- противоречить собственному guard из catalog).
CREATE TEMP TABLE IF NOT EXISTS planned(isin text, qty_delta numeric);

CREATE OR REPLACE TEMP VIEW v_scenario AS
WITH floater AS (
    SELECT DISTINCT ON (isin) isin, metric_value::numeric AS calc_rate
    FROM monitoring_checks
    WHERE metric_name = 'floater_coupon_calculator'
    ORDER BY isin, check_date DESC
),
held AS (  -- текущее состояние по ISIN, все счета/брокеры агрегированы
    SELECT v.isin,
           SUM(v.quantity) AS qty,
           SUM(v.position_value_rub) AS value_now,
           MAX(v.company_id) AS company_id,
           MAX(v.company_name) AS company_name,
           MAX(v.nominal) AS nominal,
           MAX(v.name) AS held_name
    FROM v_portfolio_positions_valuation v
    GROUP BY v.isin
),
catalog AS (
    SELECT c.isin, c.ticker, c.name, c.nominal, c.company_id, c.maturity_date,
           COALESCE(co.name, c.name) AS company_name,
           -- P1: для погашенных номинальный фолбэк цены не даём
           CASE WHEN c.maturity_date < CURRENT_DATE THEN 0
                ELSE COALESCE(NULLIF(c.market_price, 0), c.nominal, 0) END AS eff_price,
           -- P1: погашенные не дают купонного потока (иначе фантомный ₽/мес
           -- из coupon_rate_percent — та же ловушка, что и nominal-фолбэк цены;
           -- c.maturity_date IS NULL → ELSE, NULL-дату не зажигаем)
           CASE WHEN c.maturity_date < CURRENT_DATE THEN 0
                ELSE COALESCE(c.coupon_rate_percent, f.calc_rate, 0) END AS coupon_rate,
           c.duration_modified, c.risk_level, c.list_level
    FROM bonds_catalog c
    LEFT JOIN companies co ON co.id = c.company_id
    LEFT JOIN floater f ON f.isin = c.isin
),
universe AS (
    SELECT isin FROM held
    UNION
    SELECT isin FROM planned
),
base AS (
    SELECT u.isin,
           COALESCE(h.qty, 0) AS qty_before,
           COALESCE(t.qty_delta, 0) AS qty_delta,
           -- P1: сначала запрет для погашенных — иначе ветка h.value_now/qty
           -- обходит guard из catalog и возвращает nominal-фолбэк view
           -- (Эмитент Г 001Р-01: N ₽); затем оценка «сейчас» из view,
           -- затем каталог
           CASE WHEN c.maturity_date IS NOT NULL AND c.maturity_date < CURRENT_DATE THEN 0
                WHEN COALESCE(h.qty, 0) > 0 AND COALESCE(h.value_now, 0) > 0
                THEN h.value_now / h.qty
                ELSE COALESCE(c.eff_price, 0) END AS eff_price,
           COALESCE(c.maturity_date < CURRENT_DATE, FALSE) AS is_matured,
           COALESCE(h.nominal, c.nominal) AS nominal,
           COALESCE(h.company_id, c.company_id) AS company_id,
           COALESCE(h.company_name, c.company_name, u.isin) AS company_name,
           COALESCE(c.coupon_rate, 0) AS coupon_rate,
           c.duration_modified, c.risk_level, c.list_level,
           COALESCE(c.name, h.held_name) AS display_name,
           (h.isin IS NULL AND c.isin IS NULL) AS unknown_isin
    FROM universe u
    LEFT JOIN held h ON h.isin = u.isin
    LEFT JOIN catalog c ON c.isin = u.isin
    LEFT JOIN (SELECT isin, SUM(qty_delta) AS qty_delta FROM planned GROUP BY isin) t ON t.isin = u.isin
)
SELECT b.*,
       GREATEST(b.qty_before + b.qty_delta, 0) AS qty_after,  -- продажа сверх позиции = 0 (ошибку ловит блок 0)
       ROUND((GREATEST(b.qty_before + b.qty_delta, 0) * b.eff_price)::numeric, 2) AS value_after,
       ROUND((b.qty_before * b.eff_price)::numeric, 2) AS value_before,
       ROUND(((GREATEST(b.qty_before + b.qty_delta, 0) * b.nominal * b.coupon_rate) / 1200.0)::numeric, 2) AS coupon_month_after,
       ROUND(((b.qty_before * b.nominal * b.coupon_rate) / 1200.0)::numeric, 2) AS coupon_month_before
FROM base b;

-- Блок 0: ошибки сценария (штатно — 0 строк; блоки 1–3 валидны только при пустом блоке 0)
SELECT error FROM (
    SELECT 'UNKNOWN_ISIN: ' || isin AS error FROM v_scenario WHERE unknown_isin
    UNION ALL
    SELECT 'NEGATIVE_QTY_AFTER: ' || isin || ' (' || (qty_before + qty_delta) || ')' FROM v_scenario
        WHERE NOT unknown_isin AND (qty_before + qty_delta) < 0
    UNION ALL
    SELECT 'NO_COMPANY_LINK: ' || isin FROM v_scenario WHERE NOT unknown_isin AND qty_after > 0 AND company_id IS NULL
) e ORDER BY error;

-- Блок 1: сводка «сейчас → после» (купон ₽/мес, дюрация)
SELECT
    ROUND(SUM(value_before)::numeric, 0) AS portfolio_now,
    ROUND(SUM(value_after)::numeric, 0) AS portfolio_after,
    ROUND(SUM(coupon_month_before)::numeric, 0) AS coupon_month_now,
    ROUND(SUM(coupon_month_after)::numeric, 0) AS coupon_month_after,
    ROUND((SUM(coupon_month_after) - SUM(coupon_month_before))::numeric, 0) AS delta_month,
    ROUND((SUM(coupon_month_after) - SUM(coupon_month_before))::numeric * 3, 0) AS delta_quarter,
    ROUND((SUM(CASE WHEN duration_modified IS NOT NULL THEN value_before * duration_modified ELSE 0 END)
           / NULLIF(SUM(CASE WHEN duration_modified IS NOT NULL THEN value_before ELSE 0 END), 0))::numeric, 2) AS duration_now,
    ROUND((SUM(CASE WHEN duration_modified IS NOT NULL THEN value_after * duration_modified ELSE 0 END)
           / NULLIF(SUM(CASE WHEN duration_modified IS NOT NULL THEN value_after ELSE 0 END), 0))::numeric, 2) AS duration_after
FROM v_scenario;

-- Блок 2: целевой портфель (CSV через psql --csv)
SELECT isin, display_name, company_name, qty_after AS qty, value_after,
       ROUND(100.0 * value_after / NULLIF(SUM(value_after) OVER (), 0), 2) AS share_pct,
       coupon_month_after, risk_level, list_level
FROM v_scenario
WHERE qty_after > 0 AND NOT unknown_isin AND NOT is_matured
ORDER BY value_after DESC;

-- Блок 3: концентрации эмитентов после всех сделок (лимит по сумме всех ног)
SELECT company_name,
       ROUND(SUM(value_after)::numeric, 0) AS value_after,
       ROUND(100.0 * SUM(value_after) / NULLIF(SUM(SUM(value_after)) OVER (), 0), 2) AS issuer_pct,
       CASE
           WHEN company_id IS NULL THEN 'BLOCKED: NO_COMPANY_LINK'
           WHEN 100.0 * SUM(value_after) / NULLIF(SUM(SUM(value_after)) OVER (), 0) > 15.0
               THEN 'LIMIT_VIOLATION (>15%)'
           ELSE 'PASS'
       END AS limit_status
FROM v_scenario
WHERE qty_after > 0 AND NOT unknown_isin AND NOT is_matured
GROUP BY company_id, company_name
ORDER BY value_after DESC;
