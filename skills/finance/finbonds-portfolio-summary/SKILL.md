---
name: finbonds-portfolio-summary
description: "Сводка по счетам и портфелям: вложено, оценка и PnL."
version: 0.1.2
author: jedi (jedi108)
license: MIT
platforms: [linux, macos]
metadata:
  tags: "FinBonds, Portfolio, Balances, Summary"
  related_skills: "finbonds-ops, finbonds-data-refresh, finbonds-portfolio-export"
---

# FinBonds Portfolio Summary Skill

Сводка по портфелям и счетам из PostgreSQL: сколько денег вложено всего,
текущая рыночная оценка, нереализованный PnL и распределение по брокерам
и счетам. Чистое чтение — работает через psql.

## When to Use

- Пользователь спрашивает: «Сколько денег вложено всего?», «Сколько в каком
  портфеле/счёте?», «Какая оценка портфелей и доходность (PnL)?».
- Верхнеуровневый обзор распределения капитала без простыни отдельных бумаг.
- Быстрая сверка балансов перед ребалансировкой (`finbonds-rebalance-advisor`).

Don't use for: детального списка бумаг с ценами и YTM (`finbonds-portfolio-export`);
риск-профиля по эмитентам и дюрации (`finbonds-risk-assessment`).

## Prerequisites

- Контракт путей и хоста — см. `finbonds-ops`.
- Свежесть позиций — см. `finbonds-data-refresh` (портфель синхронизирован
  не позже 3 дней назад).
- Скрипт лежит в этом скилле: `scripts/portfolio_summary.sql` (`"$FIN_BONDS_SKILLS_DIR/finbonds-portfolio-summary/scripts/portfolio_summary.sql"`).

## How to Run

Вызов через файл скрипта (read-only):

`"${FIN_BONDS_PSQL:-psql}" -f "$FIN_BONDS_SKILLS_DIR/finbonds-portfolio-summary/scripts/portfolio_summary.sql"`

Или прямой SQL-запрос сводки:

`"${FIN_BONDS_PSQL:-psql}" -c "SELECT broker_name, COALESCE(NULLIF(account_id, ''), 'основной') AS account_id, COUNT(*) AS positions_count, ROUND(SUM(quantity * average_price)::numeric, 0) AS invested_rub, ROUND(SUM(current_value)::numeric, 0) AS current_value_rub, ROUND(SUM(current_value - (quantity * average_price))::numeric, 0) AS pnl_rub, ROUND(100.0 * SUM(current_value)::numeric / NULLIF((SELECT SUM(current_value) FROM portfolio_positions), 0)::numeric, 1) AS share_pct FROM portfolio_positions GROUP BY broker_name, account_id ORDER BY current_value_rub DESC;"`

## Procedure

1. Проверь свежесть данных (`finbonds-data-refresh` или последняя секция скрипта).
   Критерий: `last_positions_sync` в пределах порога.
2. Выполни скрипт `scripts/portfolio_summary.sql`. Критерий: получены обе секции
   (сводка по счетам и общий итог).
3. Сформируй ответ:
   - Общий итог: всего позиций, вложено (₽), текущая стоимость (₽),
     нереализованный PnL (₽ и %).
   - Таблица по брокерам и счетам:
     `| Брокер | Счёт | Позиций | Вложено, ₽ | Оценка, ₽ | PnL, ₽ | Доля, % |`
4. Если `account_id` пустой — укажи «основной» (или Excel-импорт).
5. Если у части позиций `average_price IS NULL`, сделай оговорку, что сумма
   вложений рассчитана по известным ценам покупки.

## Pitfalls

- Вложенные средства (`invested_rub`) рассчитываются как `SUM(quantity * average_price)`.
  Если средняя цена не заполнена брокером, сумма вложений может быть занижена.
- Текущая стоимость (`current_value_rub`) отражает рыночную оценку на момент
  последней синхронизации/обновления цен.
- Приватность: суммы балансов и идентификаторы счетов не передавать во внешние
  публичные сервисы без явного согласия пользователя.

## Verification

- Сумма долей `share_pct` по всем портфелям сходится к 100% (расхождение ≤ 0.5%).
- Суммы колонок `invested_rub` и `current_value_rub` сходятся со строкой общего итога.
