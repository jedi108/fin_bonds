---
name: finbonds-risk-monitoring
description: "Ежедневный контроль событий риска по портфелю облигаций."
version: 0.1.2
author: jedi (jedi108)
license: MIT
platforms: [linux, macos]
metadata:
  tags: "FinBonds, Risk, Monitoring, Alerts"
  related_skills: "finbonds-ops, finbonds-data-refresh, finbonds-event-calendar"
---

# FinBonds Risk Monitoring Skill

Прогоняет мониторинговые проверки fin_bonds (`check-changes`, `check-offers`,
`export-monitoring`) и классифицирует события по порогам риска. Это замена
risk engine, пока он не реализован в самом проекте (задача 4.3);
после появления движка скилл переключается на его вывод.

## When to Use

- Ежедневный / по запросу обход портфеля на события риска.
- Пользователь спрашивает «что изменилось по портфелю», «были даунгрейды»,
  «какие оферты приближаются».
- После обновления данных — сверка, не появилось ли новых событий.

Don't use for: сводного риск-профиля портфеля (это
`finbonds-risk-assessment`); подсчёта будущих купонов
(`finbonds-event-calendar`).

## Prerequisites

- Контракт путей и хоста — см. `finbonds-ops`; свежесть данных — см.
  `finbonds-data-refresh` (прогнать его при сомнении).
- Исторические таблицы в БД: `risk_history`, `monitoring_checks`,
  `rating_history`, `listlevel_history`.

## How to Run

На хосте:

`ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py check-changes && venv/bin/python main.py check-offers'`

Быстрая сверка истории:

`"${FIN_BONDS_PSQL:-psql}" -c "SELECT * FROM risk_history ORDER BY id DESC LIMIT 20;"`

## Procedure

1. Убедись в свежести данных (`finbonds-data-refresh`, шаги 1–2).
2. Прогони `check-changes` (рейтинги, купоны, листинг) и `check-offers`
   (оферты флоатеров). Критерий: обе команды завершились, вывод собран.
3. Классифицируй каждое событие по порогам:
   - **critical** — понижение купона у бумаги с фиксированным купоном;
     downgrade рейтинга ниже инвестиционного порога.
   - **high** — любой downgrade рейтинга; оферта в течение 7 дней; понижение
     `list_level` с удалением из торгов.
   - **medium** — изменение купона флоатера (ожидаемое, привязка к ключевой
     ставке); оферта в течение 30 дней; снижение ликвидности.
   - **low** — информационные события без влияния на позицию.
   Критерий: у каждого события проставлен уровень и одно предложение обоснования.
4. Для подтверждения деталей используй `rating_history` / `listlevel_history`
   по ISIN события.
5. Итог пользователю: список событий по убыванию уровня; только уровни
   high/critical требуют реакции в этом же ответе (предложение действия, не
   исполнение).

## Pitfalls

- `check-changes` пишет результат сравнения в БД — это «мутирующая» команда
  по карте `finbonds-ops`, но безопасная (история, не портфель); на хосте.
- Изменение купона флоатера — почти всегда НЕ риск (пересчёт по ключевой
  ставке); false positives здесь — главный источник шума.
- Пустой вывод `check-changes` = изменений нет; это нормальный результат.

## Verification

- Вывод обоих прогонов сохранён в ответе; каждое событие классифицировано.
- Уровни high/critical подтверждены строкой из `risk_history` или
  `rating_history` (ISIN, старое → новое значение).
