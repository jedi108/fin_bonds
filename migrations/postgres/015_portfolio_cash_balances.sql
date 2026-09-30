-- 015_portfolio_cash_balances.sql
-- Canonical cash snapshot (задача 022 roadmap ChatGPT v3).
--
-- Свободные деньги становятся частью того же canonical snapshot, что и
-- позиции: TBank API -> sync-portfolio -> PostgreSQL -> rebalance-report.
-- Прямые вызовы broker API из rebalance-report/Hermes/ChatGPT запрещены.
--
-- Ключ: broker_name + account_id + currency. Идемпотентный upsert пишет
-- только sync-portfolio при УСПЕШНОМ ответе API; ошибка API строки не
-- трогает — последний успешный cash остаётся в БД (не «стирается»).
--
-- Различение «реальный 0 RUB» и «unknown»: строка с available=0 — реальный
-- ноль; отсутствие строк — cash ещё не синхронизировался (unknown, reader
-- возвращает NULL). Валютные строки хранятся как есть, НЕ конвертируются и
-- в cash_available_rub не входят (P1.5 — только RUB).
--
-- Минимальная подсистема: отдельной cash БД, истории снапшотов и конвертации
-- валют нет (anti-scope roadmap).

CREATE TABLE IF NOT EXISTS portfolio_cash_balances (
    broker_name TEXT NOT NULL,
    account_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    money NUMERIC NOT NULL,
    blocked NUMERIC NOT NULL DEFAULT 0,
    available NUMERIC NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (broker_name, account_id, currency)
);

-- Дополнительные индексы не нужны: строк на брокера — единицы, reader
-- get_available_cash_rub() агрегирует по currency поверх полного скана.
