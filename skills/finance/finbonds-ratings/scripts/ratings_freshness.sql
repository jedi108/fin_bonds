-- Гейт свежести кредитных рейтингов (скилл finbonds-ratings, задача 002.9).
-- Одна строка CSV (запуск: "${FIN_BONDS_PSQL:-psql}" --csv -f <этот файл>):
--   rating_history_rows — всего строк в rating_history;
--   max_rating_date     — дата последнего записанного рейтинга;
--   na_tail             — живые бумаги каталога с последней проверкой
--                         credit_rating = 'N/A';
--   never_checked       — живые бумаги каталога без проверок вовсе;
--   alive_catalog       — все живые бумаги каталога (maturity_date >= сегодня).
-- Правило «стоит ли обновлять» и выбор скоупа — см. SKILL.md (Procedure 1-3).
WITH last_check AS (
    SELECT DISTINCT ON (mc.isin)
           mc.isin, mc.metric_value
    FROM monitoring_checks mc
    WHERE mc.metric_name = 'credit_rating'
    ORDER BY mc.isin, mc.check_date DESC
)
SELECT
    (SELECT count(*) FROM rating_history)                        AS rating_history_rows,
    (SELECT max(rating_date) FROM rating_history)                AS max_rating_date,
    count(*) FILTER (WHERE lc.metric_value = 'N/A')              AS na_tail,
    count(*) FILTER (WHERE lc.isin IS NULL)                      AS never_checked,
    count(*)                                                     AS alive_catalog
FROM bonds_catalog bc
LEFT JOIN last_check lc ON lc.isin = bc.isin
WHERE bc.maturity_date >= CURRENT_DATE;
