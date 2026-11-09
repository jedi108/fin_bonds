---
name: finbonds-chatgpt-portfolio-export
description: "Обновляет контекст для ChatGPT в Google Sheets (v4)."
version: 0.4.1
author: jedi (jedi108)
license: MIT
platforms: [linux, macos]
metadata:
  tags: "FinBonds, ChatGPT, GoogleSheets, Export, Rebalance"
  related_skills: "finbonds-ops, finbonds-data-refresh, finbonds-portfolio-summary, finbonds-rebalance-advisor"
---

# FinBonds ChatGPT Portfolio Export Skill

Публикует одной командой полный контекст для ревью ребалансировки в заранее
настроенную приватную Google Sheet, которую пользователь затем открывает из
ChatGPT: `CONTROL` + `SCHEMA` + `PORTFOLIO` + `CANDIDATES`. Это отдельный
transport-skill: он не заменяет статистику, risk assessment или rebalance и
не создаёт второго аналитического контура — кандидаты и аналитика приходят
только из канонического `rebalance-report` внутри команды.

Два режима (029-T15):

- **Direct export** (backward-compatible) — полный контекст без
  candidate-фильтров: канонический скринер работает на canonical defaults.
- **Filtered follow-up export** — транспорт уже нормализованного
  candidate-filter context последнего comparative rebalance run: та же
  команда с candidate-filter флагами. После специализированной
  ребалансировки default export делать нельзя — он теряет filter context
  и даёт ChatGPT другой набор кандидатов.

## When to Use

- «Выгрузи портфель для ChatGPT», «Обнови таблицу для ChatGPT»,
  «Синхронизируй портфель с ChatGPT» — direct export.
- Follow-up после/в рамках ребалансировки `finbonds-rebalance-advisor`:
  «Обнови контекст для ChatGPT под эту ребалансировку», «Выгрузи этих
  кандидатов ChatGPT», «Подготовь кандидатов для ChatGPT с такими же
  условиями», «Выгрузи кандидатов ChatGPT под эту ребалансировку» —
  filtered export с ТЕМ ЖЕ normalized candidate-filter set.

Don't use for: ВЫПОЛНЕНИЯ анализа или ребалансировки вместо
`finbonds-rebalance-advisor` (этот скилл — только транспорт, не второй
аналитический контур), обычного просмотра статистики в агенте
(`finbonds-portfolio-summary`), анализа риска (`finbonds-risk-assessment`).
Формулировка «выгрузи кандидатов ChatGPT под эту ребалансировку» — это
transport follow-up, а не выполнение ребалансировки: routing rules её
разрешают и блокировка старым «Don't use for rebalance» не применяется.
Но «предложи/сделай ребалансировку» — всегда `finbonds-rebalance-advisor`.

## Prerequisites

- Контракт путей и классов команд — `finbonds-ops`.
- На хосте в `.env` настроены:
  - `CHATGPT_SHEETS_SPREADSHEET_ID`;
  - `GOOGLE_SERVICE_ACCOUNT_JSON_B64`.
- Целевая Google Sheet расшарена service-account email с правом Editor.
- Destination фиксирован конфигурацией. Не принимать от пользователя
  произвольный Spreadsheet ID/URL и не подменять его в команде.

## Shared natural-language semantics фильтров (029-T15)

Canonical owner правил natural-language semantics —
`finbonds-rebalance-advisor` (раздел «Semantic mapping фильтров»).
Export skill переиспользует ТЕ ЖЕ правила frequency / rating / sovereign;
если правила дублируются здесь — только с regression examples, semantics
расходиться не должны:

- EXACT range «4-12 выплат в год» и SYNONYM safe range «от ежемесячных
  до квартальных» (и обратные формулировки) →
  `--freq-min 4 --freq-max 12`; exact set «только ежемесячные или
  квартальные» → `--freq-in 4,12` — set НЕ превращать в диапазон 4..12;
- явный порог «AA- и выше» / «не ниже AA-» → `--min-credit-rating AA-`
  (любой явно названный порог пишется как назван); «высокий рейтинг»,
  «надёжные бумаги» — AMBIGUOUS: порог не придумывать;
- «без суверенных» / «суверенные исключить» → `--exclude-sovereign`;
  «не ОФЗ» / «без ОФЗ» → `--exclude-sovereign` с обязательной оговоркой
  расширения «ОФЗ/суверенные выпуски РФ: исключены»; «государственные
  не брать» / «только корпоративные» молча на флаг НЕ подменять.

AMBIGUOUS-формулировки скрытым строгим фильтром не переводятся — так же,
как в rebalance-advisor.

Обычный follow-up пере-нормализацию не выполняет вовсе: фильтры берутся
из уже нормализованного candidate-filter context задачи ребалансировки
(раздел ниже). Semantic mapping нужен только когда пользователь задаёт
условия в самом export-запросе.

## Shared execution filter context (029-T15)

Для follow-up «выгрузи кандидатов под эту ребалансировку» недостаточно
трёх semantic полей: exporter обязан перенести ВЕСЬ уже нормализованный
canonical candidate-filter context последнего comparative rebalance run:

    freq_min
    freq_max
    freq_in
    min_credit_rating
    exclude_sovereign
    include_ku
    max_risk
    max_listlevel
    max_ytm
    min_maturity
    max_maturity
    screener_limit

Правила:

- **include_held=true на обеих сторонах.** rebalance-advisor передаёт
  `--include-held`; exporter имеет product-default include_held=true и
  флагов `--include-held`/`--exclude-held` не имеет. Отдельный false
  override exporter не добавлять и не эмулировать.
- **Canonical values не теряются и не интерпретируются заново.** Если
  max_risk/max_listlevel/max_ytm/min_maturity/max_maturity/include_ku не
  пришли из natural language, а взяты из canonical defaults (1 / 2 / 35)
  или уже нормализованного execution context — export всё равно
  переносит фактически применённые effective values (например,
  `--max-risk 1 --max-listlevel 2 --max-ytm 35`), а не подставляет свои.
  `screener_limit` — фактически использованный финальный лимит
  comparative run (включая разрешённое политикой расширение, например
  analysis default 30 политики FULL_REBALANCE), а не exporter default 10.
- **scope и sales_allowed — не поля exporter.** Они принадлежат
  rebalance intent и являются обязанностью `finbonds-rebalance-advisor`.
  Export skill НЕ создаёт для них CLI-полей и НЕ пытается заново
  классифицировать режим ребалансировки.
- **Final-target-only constraints НЕ переносить.** `max-position-value`
  нормализуется в rebalance-advisor, но exporter его НЕ принимает (это
  final target constraint, а не candidate filter); то же — issuer
  concentration и budget. В команде export их не передавать.

## How to Run

Обе команды обновляют весь ChatGPT-контекст — все четыре листа — одним
вызовом.

1. **Direct export** (backward-compatible, без candidate-фильтров):

`ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py sync-chatgpt-portfolio'`

2. **Filtered follow-up export** (parameterized comparative export —
   normalized candidate-filter context ребалансировки; значения — из
   execution context задачи, полный список флагов — раздел «Shared
   execution filter context»):

`ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py sync-chatgpt-portfolio --freq-min 4 --freq-max 12 --min-credit-rating AA- --exclude-sovereign --screener-limit 30'`

Команда сама проверяет freshness gate, строит кандидатов из канонического
отчёта `rebalance-report` с переданным filter context и публикует
самодокументацию `SCHEMA`. Отдельный SQL/psql, ручной расчёт кандидатов
и правки листов руками не нужны.

## Procedure

1. Определи режим: direct export (запрос без привязки к ребалансировке)
   или filtered follow-up (есть normalized candidate-filter context
   текущей задачи ребалансировки). Follow-up после специализированной
   ребалансировки через default export НЕ выполнять — теряется filter
   context.
2. Для filtered follow-up сформируй CLI args из уже нормализованного
   candidate-filter context (раздел «Shared execution filter context»):
   перенеси все 12 поддерживаемых полей с их effective values; ничего не
   пере-нормализуй заново; `max-position-value` (и другие final-target
   constraints) НЕ передавай; поля scope/sales_allowed не создавай.
3. Выполни ОДНУ команду `sync-chatgpt-portfolio` (раздел How to Run) —
   это единственное действие. Post-filtering после команды НЕ делать:
   состав CANDIDATES определяется только каноническим скринером.
4. Если команда завершилась успешно — передай пользователю ответ по
   разделу Verification, подставив значения из stdout команды (включая
   cash и фактически применённые фильтры).
5. Если freshness gate не пройден — не обходи защиту. Сообщи, что сначала
   нужно обновить данные через `finbonds-data-refresh`.
6. Если отсутствует Google-конфигурация или доступ к Sheet — сообщи
   конкретную ошибку конфигурации. Не проси пользователя присылать
   JSON-ключ в чат.

## Stdout и provenance фильтров (029-T15)

Python stdout несёт cash и фактически применённые фильтры — canonical
serialization того, что реально применил скринер:

```text
✅ Контекст для ChatGPT обновлён
Snapshot: 20260930T103000Z
Позиции: N
Кандидаты: M
Freshness: OK
Свободные деньги: 123456.78 ₽
Cash snapshot: 2026-10-01T10:30:00+00:00
Candidate filters: {"exclude_sovereign": true, "freq_max": 12, "freq_min": 4, "screener_limit": 30, ...}
```

(`Свободные деньги: неизвестно` / `Cash snapshot: unknown` — cash
неизвестен; это не ноль.)

Правила:

- **Post-run proof — только canonical stdout/report.** Фактически
  применённые фильтры читай из `Candidate filters:` stdout (он же
  `candidate_meta.filters` в Sheet = `report["screener"]["filters"]`).
  Не реконструируй применённые значения из памяти или только из
  исходного prompt.
- **Provenance строится ДО запуска.** user constraints (явные условия
  пользователя) / skill policy overrides (например analysis
  `screener-limit=30` политики FULL_REBALANCE) / canonical defaults —
  зафиксируй этот context до команды и после запуска сверь effective
  values с ним. Provenance НЕ угадывается из значений: canonical filters
  block не хранит provenance, и присутствие значения в effective filters
  не делает его пользовательским constraint. Если provenance значения
  неизвестен — показывай нейтрально «Фактически применено».
- Serialized filters можно форматировать в читаемый список:

```text
Фильтры:
- частота: 4-12 раз/год
- рейтинг: AA- и выше
- ОФЗ/суверенные РФ: исключены
- held: включены
- лимит кандидатов: 30 на тип
```

## Output contract

Google Sheet (schema v4) содержит ровно четыре вкладки:

- `CONTROL` — `schema_version`, `snapshot_id`, freshness, `positions_count`,
  `candidates_count`, `candidate_meta`, `contract_sheet`, `consumer_rule`,
  стоимость облигационных позиций (`portfolio_value_rub`) и cash контекст
  канонического снапшота: `cash_available_rub`, `investable_total_rub`,
  `cash_updated_at`. Пустые cash-поля — «cash неизвестен» (sync ещё не
  выполнялся или последний sync не удался), это НЕ ноль.
  `candidate_meta.filters` — canonical filters применённого скринер-прогона
  (post-run proof состава кандидатов; при direct export — canonical
  defaults скринера).
  `sync_status=READY` — commit-marker: данные читаемы
  только при READY; `WRITING` — незавершённый sync, такой snapshot нельзя
  считать готовым.
- `SCHEMA` — самодокументация контракта: все поля и единицы CONTROL /
  PORTFOLIO / CANDIDATES. Новый ChatGPT-чат читает его вместо памяти
  прошлой беседы.
- `PORTFOLIO` — одна строка на активный ISIN с ценой, стоимостью, весом
  позиции и эмитента, YTM, купоном, рейтингом, риском, сроком, дюрацией и
  ликвидностью.
- `CANDIDATES` — кандидаты buy-выборки канонического скринера
  `rebalance-report` (FIX и FLOAT одним набором, rank внутри типа) с
  canonical `credit_rating`, текущей долей эмитента и бейджем уже держимых
  бумаг. При filtered follow-up выборка строится с перенесённым
  candidate-filter context; post-filtering строк exporter'ом/агентом не
  выполняется.

## Safety

- Skill не делает SQL и не обращается к PostgreSQL напрямую: единственный
  вызов — canonical Python command `sync-chatgpt-portfolio`.
- Skill не считает и не фильтрует кандидатов сам — состав CANDIDATES
  определяется только каноническим `rebalance-report` внутри команды;
  post-filtering после команды запрещён.
- Skill не нормализует заново уже нормализованный canonical filter
  context и не угадывает применённые фильтры из памяти/prompt — proof
  только из canonical stdout / `candidate_meta.filters`.
- Не передавать `--max-position-value` и не создавать CLI-поля
  scope/sales_allowed — они не принадлежат exporter.
- Skill не пишет в PostgreSQL и не совершает сделок.
- Google Sheet — заранее разрешённый пользователем destination, поэтому
  повторное подтверждение перед каждым sync не требуется.
- Не логировать и не показывать `GOOGLE_SERVICE_ACCOUNT_JSON_B64`.
- Не отправлять портфель в другие таблицы или внешние сервисы.

## Verification

Успешный вызов возвращает пользователю сообщение по шаблону ниже
(значения — только из вывода команды). В ответе — счётчики, статусы, cash
и фактически применённые фильтры (читаемым списком или canonical
serialization); содержимое строк PORTFOLIO/CANDIDATES не выводится.
Старый контракт «ровно такое сообщение» без cash/filters больше не
действует: строки «Свободные деньги» / «Cash snapshot» / «Candidate
filters» обязательны, когда команда их вернула (029-T15):

```text
✅ Контекст для ChatGPT обновлён
Snapshot: 20260930T103000Z
Позиции: N
Кандидаты: M
Freshness: OK
Свободные деньги: 123456.78 ₽
Cash snapshot: 2026-10-01T10:30:00+00:00
Candidate filters: {...}

Фильтры:
- частота: 4-12 раз/год
- рейтинг: AA- и выше
- ОФЗ/суверенные РФ: исключены
- held: включены
- лимит кандидатов: 30 на тип
```

После успешного вызова `CONTROL.sync_status` должен быть `READY`,
`CONTROL.positions_count` — совпадать с числом строк данных `PORTFOLIO`,
`CONTROL.candidates_count` — с числом строк данных `CANDIDATES`, а
`CONTROL.contract_sheet` — равняться `SCHEMA`. При filtered follow-up
`candidate_meta.filters` должен подтверждать перенесённый context (то же
значение, что в `Candidate filters:` stdout); расхождение stdout и
`candidate_meta.filters` невозможно — это один и тот же canonical
serialization, но при расхождении с ожидаемым execution context задачи
export не считать выполненным под ту ребалансировку: повторить с
корректным context.
