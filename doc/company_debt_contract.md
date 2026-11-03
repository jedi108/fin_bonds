# Долговая нагрузка эмитентов: контракт данных и JSON (issue #4)

Документ для авторов скиллов (Hermes): на что можно опираться в выводе
`company-debt-report` и в блоке `debt_overlay` команды `rebalance-report`.
Всё advisory/read-only: ордеров нет, валидность плана от этих данных не зависит.

## Две разные величины

| Поле | Что это | Чем НЕ является |
|------|---------|-----------------|
| `issued_bond_volume_rub` (в отчёте) | `SUM(issue_size * nominal)` по **RUB**-выпускам каталога компании (не-RUB выпуск входит в `issues_count`, но не в объём → `issued_volume_partial`; погашенные, `maturity_date < CURRENT_DATE`, не учитываются вовсе); `nominal` — **текущий** номинал от брокера (после амортизации) | не остаток долга, не леверидж |
| снимок `company_debt_snapshots` | долг из отчётности: `total/bond/bank/lease_debt_rub`, `cash_rub`, `net_debt_rub`, `revenue_rub`, `ebitda_rub`, `outstanding_bond_debt_rub` | — |

Скилл не должен выдавать `issued_bond_volume_rub` за долговую нагрузку и не должен
выводить леверидж из купона, YTM или числа выпусков.

## Хранилище

Таблица `company_debt_snapshots` (миграция 017). Ключ версии снимка —
`(company_id, report_date, source)`: повторный импорт с тем же ключом **перезаписывает**
снимок целиком (last write wins, `retrieved_at` обновляется); другой `source` или
другая `report_date` — отдельная строка, история сохраняется. «Последний» снимок
компании — по `report_date`, затем `retrieved_at`. `report_date` в будущем отклоняется
(`bad_report_date`).
Все суммы в рублях; отсутствующая метрика = `NULL` (не 0). `net_debt_to_ebitda` не
хранится — считается в отчёте при известных `net_debt` и `ebitda > 0`.
`net_debt` берётся заявленный (`net_debt_source: "reported"`), иначе
`total_debt - cash` только если оба известны (`"derived"`), иначе `null`.

Идентичность компаний — существующий слой `company_overrides` (config.yaml) +
`link-companies`; таблицы алиасов нет. Старые строки `companies` не удаляются.

## `import-company-debt --file <csv|json>`

Колонки/ключи: `company_inn`, `company_name`, **`source`**, **`report_date`** (YYYY-MM-DD),
**`source_ref`**, `total_debt_rub`, `bond_debt_rub`, `bank_debt_rub`, `lease_debt_rub`,
`cash_rub`, `net_debt_rub`, `revenue_rub`, `ebitda_rub`, `issued_bond_volume_rub`,
`outstanding_bond_debt_rub`, `coverage_status` (`full|partial|unknown`),
`quality_status` (`verified|unverified|estimated`), `notes`.
Числа: десятичный разделитель `.` или `,`; значения с запятой и тремя цифрами после неё (`250,000`) и со смесью `,`/`.` отклоняются как неоднозначные (`bad_<поле>`).
JSON: список объектов или `{"snapshots": [...]}`. Компания: ИНН, затем точное имя
(без регистра; имя с другим ИНН не сопоставляется); несопоставленные/неоднозначные строки не вставляются. Печатает в stdout:

```json
{"rows": 3, "inserted": 1, "updated": 1,
 "unmatched": [{"row": 3, "company_inn": "...", "company_name": "...", "reason": "not_found|ambiguous"}],
 "invalid": [{"row": 2, "company_inn": null, "company_name": null, "errors": ["missing_source_ref", "bad_cash_rub"]}]}
```

## `company-debt-report [--format json|table] [--stale-days N]`

По умолчанию `--stale-days 180`. В отчёт входят только компании типа `company`
с выпусками в каталоге. JSON (stdout), `schema_version: 1`:

```text
meta:          as_of, stale_days, currency="RUB", issued_volume_note, advisory=true
summary:       companies_total, freshness{fresh,stale,missing}, issued_bond_volume_total_rub
concentration: basis="issued_bond_volume_rub", companies_with_volume, top1_pct, top3_pct, top5_pct, hhi
companies[]:   rank (по объёму; null если объём неизвестен), company_id, name, inn,
               issues_count, issues_with_volume_count,
               issued_bond_volume_rub (null если неизвестен), issued_volume_share_pct,
               portfolio_value_rub, portfolio_share_pct,
               freshness{status: fresh|stale|missing, age_days, stale_days},
               coverage_status, quality_status (null, если снимка нет),
               net_debt_rub, net_debt_source, net_debt_to_ebitda,
               latest_snapshot (все поля снимка + provenance: source, source_ref, report_date, retrieved_at) | null,
               unknowns[]
```

`unknowns` (явные «не знаем»): `issued_volume_unknown`, `issued_volume_partial`,
`no_debt_snapshot`, `debt_snapshot_stale`, `net_debt_unknown`, `ebitda_unknown`,
`ebitda_non_positive`, `outstanding_bond_debt_unknown`, `coverage_not_full`,
`quality_not_verified`. Скилл обязан показать их пользователю и не заполнять пробелы догадками.

## `rebalance-report --debt-overlay`

Флаг выключен по умолчанию; без него ответ побайтно прежний. С флагом добавляется
верхнеуровневый ключ `debt_overlay`; остальные блоки (включая `scenario.valid/feasible`,
ограничения, лимит эмитента, freshness-гейт) не меняются. `schema_version` отчёта прежний.

```text
debt_overlay: advisory=true, affects_plan_validity=false, as_of, stale_days, issued_volume_note,
  coverage: portfolio_issuers, freshness{fresh,stale,missing},
            portfolio_share_with_fresh_debt_data_pct, portfolio_share_unlinked_or_non_company_pct
  issuers[]: company_id, name, portfolio_share_pct, issues_count, issued_bond_volume_rub,
             issued_volume_share_pct, freshness, coverage_status, quality_status,
             net_debt_rub, net_debt_to_ebitda, unknowns[]     (сортировка по доле в портфеле)
```

Overlay описывает текущий портфель, а не состояние «после сценария».
