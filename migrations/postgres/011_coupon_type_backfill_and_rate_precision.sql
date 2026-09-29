-- Migration 011: качество данных каталога (задача 002.5, P4 + P9.4).
--
-- P4: bonds_catalog.coupon_type пуст (все строки NULL): каталог пишется через
-- _update_from_tbank без этого поля (coupon_rate_percent тоже приходит позже,
-- из MOEX), а Bond.from_tinkoff_api с русскими значениями
-- ('Постоянный'/'Переменный') нигде не вызывается. Ad-hoc SQL с фильтром
-- coupon_type='FLOAT' молча возвращал пустоту/0.0%, тогда как скрининги
-- (buy_candidates.sql, rebalance_export.sql) флоат вычисляют через
-- floating_coupon_flag — расхождение источников в одном проекте.
-- Домен значения — 'FIX'/'FLOAT', синхронно с флагом (те же буквы, что
-- используют параметр coupon_type в buy_candidates.sql и CASE в выгрузках).
--
-- P9.4: мусорная точность coupon_rate_percent после перехода на NUMERIC
-- (миграция 005): неограниченный Decimal из формулы MOEX
-- (25.00250000000000000000000001, 28 значащих цифр) и длинный repr
-- float-пересчёта actual/365 (0.10027472527472528, 17 цифр) сохранялись в
-- numeric-колонку литералом. Каноническая точность купона — 4 знака после
-- запятой; каналы записи нормализуются кодом
-- (src/utils.py:round_coupon_rate), здесь — разовая чистка накопленного.

-- P4: backfill купонного типа из флага флоатера (идемпотентно: только NULL).
UPDATE bonds_catalog
SET coupon_type = CASE WHEN floating_coupon_flag THEN 'FLOAT' ELSE 'FIX' END
WHERE coupon_type IS NULL;

-- P9.4: нормализация накопленной точности до 4 знаков (идемпотентно:
-- повторный запуск уже нормализованные значения не меняет).
UPDATE bonds_catalog
SET coupon_rate_percent = ROUND(coupon_rate_percent, 4)
WHERE coupon_rate_percent IS NOT NULL
  AND coupon_rate_percent <> ROUND(coupon_rate_percent, 4);
