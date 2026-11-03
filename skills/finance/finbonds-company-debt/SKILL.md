---
name: finbonds-company-debt
description: "Долговая нагрузка эмитентов: снимки долга, отчёт."
version: 0.1.1
author: jedi (jedi108)
license: MIT
platforms: [linux, macos]
metadata:
  tags: "FinBonds, Debt, Issuer, Risk, Advisory"
  related_skills: "finbonds-ops, finbonds-risk-assessment, finbonds-rebalance-advisor, finbonds-sql-recipes"
---

# FinBonds Company Debt Skill

Долговая нагрузка эмитентов (fin_bonds issue #4): снимки долга из отчётности
(`company_debt_snapshots`) и отчёт `company-debt-report`. Только чтение и
advisory: заявок нет, валидность плана ребалансировки от этих данных не зависит.
Контракт полей — `doc/company_debt_contract.md` основного репозитория.

## When to Use

- «Какая долговая нагрузка у эмитента X», «у кого в портфеле нет данных по долгу».
- Дополнение к `finbonds-risk-assessment` (концентрации) и опциональный блок
  `--debt-overlay` в `rebalance-report` (`finbonds-rebalance-advisor`).

Don't use for: событий по бумагам (`finbonds-risk-monitoring`); рейтингов
(`finbonds-ratings`); ордеров и сигналов на продажу — не предназначен.

## Prerequisites

- Контракт путей и запуска CLI на хосте — `finbonds-ops`.
- `import-company-debt` пишет в БД — это не часть скилла; запускать только по
  явной просьбе пользователя с готовым файлом (csv/json). Скилл сам данные
  отчётности не добывает и не придумывает.

## How to Run

Отчёт (JSON по умолчанию для агента, stdout — чистый JSON):

`ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py company-debt-report --format json --stale-days 180'`

Для человека — `--format table`. Импорт снимков (только по просьбе, пишет в БД):
`main.py import-company-debt --file <csv|json>` — вывод: `rows/inserted/updated`,
`unmatched[]` (`not_found|ambiguous`), `invalid[]` (коды ошибок). Несопоставленные
и невалидные строки НЕ вставляются — покажи их пользователю.

## Procedure

1. Получи JSON `company-debt-report`. Критерий: `schema_version: 1`, `meta.advisory=true`.
2. Покажи покрытие: `summary.freshness{fresh,stale,missing}` — сколько
   компаний вообще без свежих данных. Критерий: число `missing` названо.
3. По компании читай: `freshness.status`, `net_debt_rub` + `net_debt_source`
   (`reported|derived`), `net_debt_to_ebitda`, `coverage_status`,
   `quality_status`, provenance снимка (`source`, `source_ref`, `report_date`).
4. Каждый элемент `unknowns[]` перечисли пользователю дословно-по-смыслу
   (`no_debt_snapshot`, `debt_snapshot_stale`, `net_debt_unknown`,
   `ebitda_unknown`, `ebitda_non_positive`, `issued_volume_partial`, ...).
   Критерий: нет пробела, закрытого догадкой.

## Rules (обязательно)

- `issued_bond_volume_rub` = размер выпуска × **текущий** номинал, только
  RUB-выпуски, погашенные исключены. Это НЕ текущий долг, НЕ остаток облигаций
  и НЕ леверидж. Называй «оценка объёма размещённых выпусков (RUB, по текущему
  номиналу)». Реальный остаток — только `outstanding_bond_debt_rub` из снимка.
- `net_debt_to_ebitda` называй только если он есть в выводе (нужны net_debt и
  EBITDA > 0). Самому не считать и не оценивать.
- Нет данных или снимок устарел (`stale`/`missing`) = **неизвестно**; так и
  скажи явно. Не писать «долг низкий/нормальный» без снимка.
- Леверидж не выводить из купона, YTM, рейтинга или числа выпусков.
- Долговой overlay никогда не меняет валидность плана и не является триггером
  продажи; ордеров нет. Любой вывод — «информация к решению человека».
- Качество: `quality_status != verified` или `coverage_status != full` —
  оговорить в ответе.

## Pitfalls

- Не-RUB выпуск входит в `issues_count`, но не в объём (`issued_volume_partial`).
- Повторный импорт с тем же `(company_id, report_date, source)` перезаписывает
  снимок; «последний» снимок — по `report_date`, затем `retrieved_at`.
- В отчёт входят только компании типа `company` с выпусками в каталоге;
  суверенные эмитенты отсутствуют.

## Verification

- `summary.freshness` fresh+stale+missing = `companies_total`.
- Каждое названное число в ответе есть в JSON; для каждого `unknowns[]`
  сказано, что именно неизвестно.

## References

- `references/debt-model.md` — таблица «какой показатель для какого вопроса», правило слияния компаний (только по ИНН/verified override) и этапы debt-overlay.
