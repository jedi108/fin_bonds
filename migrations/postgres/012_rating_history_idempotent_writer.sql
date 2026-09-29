-- Migration 012: идемпотентный писатель rating_history (задача 002.6, вариант 1).
--
-- update-ratings (адаптер credit_rating) отныне дублирует найденный рейтинг
-- в rating_history вместе с monitoring_checks (согласовано с задачей 003,
-- п. 4.2: их критерий — «SELECT count(*) FROM rating_history > 0 и растёт
-- после апдейтов»). Ночной пайплайн (run_update.sh, шаг 4) вызывает
-- update-ratings ежедневно, плюс возможны повторные ручные прогоны
-- (в т.ч. update-ratings --isin) в течение суток: без ограничения они
-- плодили бы дубликаты (isin, дата, код), а потребители читают «последнюю
-- по дате» строку через ORDER BY rating_date DESC, id DESC — дубли делают
-- выбор недетерминированным и раздувают таблицу.
--
-- Схема таблицы не меняется: только уникальный индекс, под который
-- INSERT ... ON CONFLICT DO NOTHING в storage.add_rating опирается.
-- rating_code nullable: строки с NULL (schema allows; писатели пишут NOT NULL)
-- не конфликтуют — семантика ON CONFLICT для них не меняется.
--
-- На момент задачи rating_history пуста (писал только seed_data), поэтому
-- построение индекса конфликтов не имеет; IF NOT EXISTS делает миграцию
-- повторно безопасной.

CREATE UNIQUE INDEX IF NOT EXISTS uq_rating_history_isin_date_code
    ON rating_history (isin, rating_date, rating_code);
