---
name: finbonds-risk-assessment
description: "Риск-профиль портфеля: концентрации, дюрация, ликвидность."
version: 0.1.1
author: jedi (jedi108), Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [FinBonds, Risk, Portfolio, SQL]
    related_skills: [finbonds-ops, finbonds-data-refresh, finbonds-rebalance-advisor]
---

# FinBonds Risk Assessment Skill

Сводный риск-профиль портфеля из PostgreSQL: концентрация по эмитентам,
распределение по уровням риска, корзины до погашения (прокси дюрации),
потери при выходе. Чистое чтение — работает прямо в контейнере через
`scripts/risk_snapshot.sql`.

## When to Use

- Пользователь просит «риск-отчёт по портфелю», «где концентрации»,
  «какая доля высокорискованных» и т.п.
- Вход для `finbonds-rebalance-advisor` — лимиты применяются к этому снапшоту.
- Периодическая (еженедельная) фиксация риск-профиля для сравнения в динамике.

Don't use for: событий по конкретным бумагам (`finbonds-risk-monitoring`);
подбора кандидатов на покупку (`analyze-buy-candidates`, см.
`finbonds-rebalance-advisor`).

## Prerequisites

- Контракт путей — см. `finbonds-ops`; скрипт лежит в этом скилле:
  `scripts/risk_snapshot.sql` относительно каталога скилла (в контейнере:
  `/opt/data/profiles/fin_bonds/skills/finance/finbonds-risk-assessment/scripts/risk_snapshot.sql`).
- Перед отчётом — свежесть данных по `finbonds-data-refresh` (пороги там же).

## How to Run

`terminal(command="/opt/data/profiles/fin_bonds/bin/pg.sh -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-risk-assessment/scripts/risk_snapshot.sql")`

## Procedure

1. Проверь свежесть (последний блок скрипта вернёт метки; старше порогов —
   сначала `finbonds-data-refresh`). Критерий: метки в пределах порогов.
2. Прогони скрипт целиком. Критерий: 5 секций результата получены.
3. Интерпретируй с порогами тревоги:
   - концентрация на одного эмитента > 15% — medium; > 25% — high;
   - доля позиций с unknown `risk_level` > 10% — medium (каталог неполон);
   - средневзвешенная модифицированная дюрация вне целевого коридора лимитов (по умолчанию 1.0–2.5 года) — процентный риск при росте ставок (если > max) или реинвест-риск при снижении ставок (если < min);
   - покрытие дюрацией `coverage_pct` < 50% — недостаточный охват (высокая доля флоатеров/амортизаций/непосчитанных бумаг);
   - freshness: `positions_with_stale_duration > 0` — запустить пересчёт дюрации (`calculate-duration --recalculate`);
   - top `liquidity_loss_ratio` > 2% — отметить позиции в отчёте.
   Критерий: каждое нарушение порога названо с числом из снапшота.
4. Ответ пользователю: короткий вердикт (статус: ок / внимания / тревога),
   затем таблица нарушений, затем полные секции снапшота.

## Опционально: долговая нагрузка эмитентов

Если пользователь спрашивает про долг эмитентов — `main.py company-debt-report`
(скилл `finbonds-company-debt`). Оговорки: `issued_bond_volume_rub` — оценка
объёма выпусков (RUB, текущий номинал, без погашенных), не долг и не леверидж;
`net_debt_to_ebitda` — только если есть в выводе; нет/устарел снимок =
«неизвестно», сказать явно. Пороги и вердикт снапшота выше не меняются.

## Pitfalls

- Эмитент группируется через справочник `companies` (`bonds_catalog.company_id`);
  позиции без привязки попадают в группу `(без компании)`.
- Дюрация Маколея и модифицированная дюрация рассчитываются каноническим cashflow-решателем по формуле дисконтированных потоков. Флоатеры, вечные и амортизируемые бумаги исключаются из средневзвешенной дюрации с явным указанием причин (`floating_coupon`, `perpetual`, `amortization_schedule_missing`).
- `liquidity_loss_ratio` может быть NULL у новых позиций — это «не посчитано»,
  а не «нет потерь»; фиксируй долю NULL.
- Сравнение снапшотов между датами — по значению, не по rowid (позиции
  перезаписываются).

## Verification

- Сумма `pct` по эмитентам ≈ 100 (расхождение > 0.5 п.п. — данные битые,
  сообщи, не молчи).
- Каждое нарушение порога в ответе сопровождается ISIN или эмитентом.
