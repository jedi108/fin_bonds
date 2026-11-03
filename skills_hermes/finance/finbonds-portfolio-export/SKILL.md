---
name: finbonds-portfolio-export
description: "Выгрузка портфеля в MD/CSV для отчетов и ребалансировки."
version: 0.2.1
author: jedi (jedi108), Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [FinBonds, Export, Markdown, CSV, Rebalance, Reporting]
    related_skills: [finbonds-ops, finbonds-data-refresh, finbonds-rebalance-advisor, finbonds-event-calendar]
---

# FinBonds Portfolio Export Skill

Выгрузка позиций портфеля (базовый список либо расширенный датасет для LLM-ребалансировки)
в Markdown или CSV. Отправка наружу — только после явного подтверждения пользователем
адресата в текущем диалоге.

## Доходность ≠ купон (issue #3, обязательно)

- «Доходность» — **только** `ytm_pct`, и всегда с горизонтом из `ytm_to`:
  `offer` → «доходность к оферте <offer>», `maturity` → «доходность к погашению».
- `coupon_pct` называть только «купонная ставка» (или «купон»), никогда не «доходность».
- Есть оферта (`offer` / `offer_date`) — указывай её дату.
- `ytm_check: "mismatch"` — приводи оба числа: наша `ytm_pct` и MOEX `moex_yield_pct`,
  с оговоркой «расхождение из-за методики расчёта»; ни одно не подавай как бесспорное.
  `no_moex_data` — MOEX-доходности нет (нет сделок), достаточно `ytm_pct`.
- Пустой `ytm_pct` (см. `ytm_reason`, напр. флоатер) не заменяй купоном.
- Пример (Эмитент В 001Р-01, RU000A10XYC1): «купон 19%, доходность к оферте 23.11.2026 ≈13.7%».

## When to Use

- Пользователь просит «список портфеля в md», «выгрузи портфель в <сервис>»,
  «отчёт по позициям с ценами и доходностью».
- Подготовка данных для ребалансировки: «срез портфеля для LLM», «подготовь
  отчет для ребалансировки», «выгрузи позиции с рейтингами и офертами в CSV/MD».
- Регулярный отчёт по расписанию (совместно с cronjob Hermes).

Don't use for: расчета целевых лимитов сделок (`finbonds-rebalance-advisor`);
агрегированного календаря денежного потока (`finbonds-event-calendar`);
выгрузки каталога всех облигаций (это `export-bonds`, см. `finbonds-ops`).

## Prerequisites

- Контракт путей и хоста — см. `finbonds-ops`.
- Свежесть позиций — см. `finbonds-data-refresh` (портфель синхронизирован
  не позже 3 дней назад).
- Скрипт ребалансировки лежит в этом скилле: `scripts/rebalance_export.sql`
  (в контейнере: `/opt/data/profiles/fin_bonds/skills/finance/finbonds-portfolio-export/scripts/rebalance_export.sql`).
- Адресат выгрузки назван пользователем. Если адресат — публичный ресурс
  (web, публичный репозиторий, сторонний SaaS), напомни о приватности
  состава портфеля и получи явное согласие до отправки.

## How to Run

1. **Базовый список позиций (Markdown-отчет со штампом свежести и честной YTM)**:

`terminal(command="cd ${FIN_BONDS_ROOT} && venv/bin/python main.py export-portfolio --format md")`

Файл сохраняется в `${FIN_BONDS_ROOT}/_output_/`. Для явного пути файла или включения ISIN:
`terminal(command="cd ${FIN_BONDS_ROOT} && venv/bin/python main.py export-portfolio --format md --output /tmp/portfolio.md --with-isin")`

2. **Полный срез для ребалансировки / LLM** (ISIN, эмитент, YTM, купон, оферта, рейтинг, риск, ликвидность выхода):

`terminal(command="/opt/data/profiles/fin_bonds/bin/pg.sh -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-portfolio-export/scripts/rebalance_export.sql")`

3. **Экспорт в CSV** (для Code Interpreter, Python REPL или внешних моделей):

`terminal(command="/opt/data/profiles/fin_bonds/bin/pg.sh --csv -f /opt/data/profiles/fin_bonds/skills/finance/finbonds-portfolio-export/scripts/rebalance_export.sql")`

## Procedure

1. Проверь свежесть позиций (`finbonds-data-refresh`, шаги 1–2).
2. Выбери режим выгрузки:
   - **Обычный отчёт**: выполни `venv/bin/python main.py export-portfolio --format md`.
   - **Срез для ребалансировки/LLM**: выполни `scripts/rebalance_export.sql`.
     Для качественной ребалансировки также сними ближайший денежный поток
     (купоны/погашения на 30 дней) через `finbonds-event-calendar` и приложи к промпту.
3. Собери MD или CSV: заголовок с датой и меткой свежести данных, таблица позиций
   (total в последней строке), источник данных.
4. Запиши файл через `write_file` и покажи пользователю содержимое.
   Критерий: пользователь видел отчёт до отправки во внешние системы.
5. Отправка адресату — только после явного подтверждения адресата пользователем.

## Pitfalls

- Конфиденциальность: состав портфеля не отправлять никуда без подтверждения
  адресата в текущем диалоге — даже если адресат встречался раньше.
- При передаче в публичные LLM по запросу пользователя скрывай реальные объёмы
  денег (`value_rub`, `quantity`), оставляя только доли `share_pct`.
- В PostgreSQL числовые колонки типизированы; при необходимости используй ROUND.
- `_output_/` внутри контейнера read-only — пишут только команды на хосте;
  свой MD пиши в рабочую область Hermes (`write_file`), а не в маунт.

## Verification

- В итоговом MD/CSV: каждая позиция имеет название, цену и стоимость; сумма
  долей `share_pct` сходится к ~100% (расхождение ≤ 0.5%).
- Для ребалансировочного отчета присутствуют столбцы `coupon_type`, `offer_date`,
  `risk_level` и `exit_loss_pct`.
- Отправка подтверждена пользователем и результат доставки доложен.
