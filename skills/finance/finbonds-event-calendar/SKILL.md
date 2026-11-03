---
name: finbonds-event-calendar
description: "Календарь купонов, оферт, амортизаций и погашений портфеля."
version: 0.1.2
author: jedi (jedi108)
license: MIT
platforms: [linux, macos]
metadata:
  tags: "FinBonds, Calendar, Cashflow, Offers"
  related_skills: "finbonds-ops, finbonds-risk-monitoring, finbonds-rebalance-advisor"
---

# FinBonds Event Calendar Skill

Будущие события по позициям портфеля: купоны, оферты, амортизации, погашения
и связанный денежный поток. Источники — `calculated_coupons`,
`bonds_catalog.offer_date` / `maturity_date`, команда
`calculate-monthly-profit`.

## When to Use

- «Что предстоит по портфелю», «когда ближайшие купоны/оферты/погашения».
- Планирование кэша: сколько и когда придёт денег, что нужно реинвестировать.
- Вход для ребаланса: погашения/оферты = естественные точки пересборки
  (`finbonds-rebalance-advisor`).

Don't use for: классификации событий как рисков (`finbonds-risk-monitoring`);
сводного риск-профиля (`finbonds-risk-assessment`).

## Prerequisites

- Контракт путей и хоста — см. `finbonds-ops`.
- `calculated_coupons` заполняется пайплайном обновления; если пусто — сначала
  `finbonds-data-refresh`.

## How to Run

Ближайшие события:

`"${FIN_BONDS_PSQL:-psql}" -c "SELECT p.isin, p.name, c.offer_date, c.maturity_date FROM portfolio_positions p JOIN bonds_catalog c ON c.isin=p.isin WHERE c.offer_date BETWEEN CURRENT_DATE AND (CURRENT_DATE + INTERVAL '60 days') OR c.maturity_date BETWEEN CURRENT_DATE AND (CURRENT_DATE + INTERVAL '60 days') ORDER BY COALESCE(c.offer_date, c.maturity_date);"`

Денежный поток по месяцам (на хосте):

`ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py calculate-monthly-profit'`

## Procedure

1. Сними окно событий (30–60 дней) SQL-запросом выше. Критерий: каждая позиция
   с событием в окне видна с типом события (оферта/погашение).
2. Досчитай купоны: `calculate-monthly-profit` на хосте. Критерий: месяцы с
   ненулевым притоком определены.
3. Пометь риск-контекст каждого события:
   - put-оферта — сумма вернётся, потребуется реинвестирование; сопоставь с
     корзиной `<1y` из `finbonds-risk-assessment`;
   - погашение — финальный номинал + купон;
   - амортизация — частичный возврат номинала (позиция остаётся).
4. Ответ: таблица «дата — ISIN — событие — ожидаемая сумма» и итог по месяцам.
   Отдельно перечисли события, требующие решения (оферта без плана
   реинвестирования).

## Pitfalls

- `offer_date` у флоатеров — регулярные оферты, не «событие риска»; не
  дублируй их как эксцессы.
- `perpetual_flag = 1` — даты погашения нет, ориентируйся только на оферты.
- Суммы притоков из `calculate-monthly-profit` — оценка (номинал по
  `quantity`), сверяй крупные с пользователем перед ребаланс-планом.

## Verification

- Каждая строка календаря имеет дату, ISIN и тип события.
- Месячные итоги не «теряют» события: их сумма за окно >= количества событий
  из шага 1.
