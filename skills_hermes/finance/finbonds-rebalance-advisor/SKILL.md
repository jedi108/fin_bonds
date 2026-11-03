---
name: finbonds-rebalance-advisor
description: "SOP ребалансировки: adapter report/plan/compare/snapshot."
version: 0.14.1
author: jedi (jedi108), Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [FinBonds, Rebalance, Advisory, Domain Engine]
    related_skills: [finbonds-rebalance-tool, finbonds-ops, finbonds-data-refresh, finbonds-buy-candidates, finbonds-risk-assessment, finbonds-event-calendar, finbonds-chatgpt-portfolio-export]
---

# FinBonds Rebalance Advisor Skill

SOP ребалансировки проекта fin_bonds (030): **LLM интерпретирует и выбирает —
детерминированный расчёт делает доменный engine core**. Standard path — stable
adapter `finbonds-rebalance-tool` (030.11): `report` → `plan` (baseline: 2
domain-вызова, 0 ad-hoc кода), альтернативы — `compare` (A/B на одном
PlanningContext), нестандартная read-only аналитика — `analysis-snapshot` +
controlled sandbox fallback (разделы «Capability ladder» и «Sandbox fallback»).
Все доменные команды ребалансировки (`rebalance-report` / `rebalance-plan` /
`rebalance-compare` / `rebalance-analysis-snapshot`) агент вызывает ТОЛЬКО через
adapter — прямой ssh-вызов этих команд, heredoc/scp/sed и ad-hoc Python в
standard path запрещены (раздел «Anti-rules»). Вся арифметика (qty, деньги,
бюджет, netting, лимиты эмитента, купонный поток, валидация, сравнение) — в
core; SQL, psql и `POSTGRES_DSN` агенту не нужны. Только совет (advisory only):
сделки исполняет человек, проект fin_bonds не подключён к торговле.

Разделение ответственности, на которое опирается SOP:

- **LLM/Hermes**: интерпретация пользовательских ограничений (нормализация
  intent, semantic mapping фильтров); выбор финансовой гипотезы; выбор ISIN
  только из canonical candidate output; явный выбор приоритета между целями
  покупки; решение, какую позицию продать/сократить; запрос альтернативного
  сценария; объяснение trade-off.
- **Детерминированный код (core)**: monetary arithmetic; final target qty;
  перевод target value → qty; netting; cash/budget; position value; issuer
  concentration; coupon cashflow; frequency/rating/sovereign constraints;
  final validation; сравнение сценариев; compact projections.

## Доходность ≠ купон (issue #3, обязательно)

- «Доходность» — **только** `ytm_pct`, и всегда с горизонтом из `ytm_to`:
  `offer` → «доходность к оферте <offer>», `maturity` → «доходность к погашению».
- `coupon_pct` называть только «купонная ставка» (или «купон»), никогда не «доходность».
- Есть оферта (`offer` / `offer_date`) — указывай её дату.
- `ytm_check: "mismatch"` — приводи оба числа: наша `ytm_pct` и MOEX `moex_yield_pct`,
  с оговоркой «расхождение из-за методики расчёта»; ни одно не подавай как бесспорное.
  `no_moex_data` — MOEX-доходности нет (нет сделок), достаточно `ytm_pct`.
- Пустой `ytm_pct` (см. `ytm_reason`, напр. флоатер) не заменяй купоном.
- Контракт строки (P1.5, `contract_version: 1`): `rating_status` — рейтинг подтверждён
  только при `confirmed`; `proxy_only`/`missing`/`stale` — так и говори, `risk_level` рейтингом
  не называй. Непустой `blockers[]` (`YTM_MISSING`, `PRICE_STALE`) — строку в рекомендацию
  не бери, назови код; `warnings[]` — оговори. Группы ключей — блок `row_contract`.
- Пример (Эмитент В 001Р-01, RU000A10XYC1): «купон 19%, доходность к оферте 23.11.2026 ≈13.7%».

## When to Use

- «Предложи ребалансировку», «проверь портфель по лимитам», «куда разместить
  пришедший купон/погашение».
- После события high/critical из `finbonds-risk-monitoring` — пересборка
  пострадавшей доли.
- После оферты/погашения из `finbonds-event-calendar` — план реинвестирования
  (пул — `redemptions_6m` отчёта).
- Ротация спящих/низкодоходных бумаг — тот же SOP: продажи-аутсайдеры +
  докупки, эффект считает engine.
- «Можно ли увеличить доход / сравни варианты» — `compare` (A/B на одном
  контексте), интерпретация delta — на LLM.

Don't use for: исполнения сделок или подачи заявок — никогда; мониторинга
событий без пересборки (`finbonds-risk-monitoring`); глубокого риск-профиля
вне ребалансировки (`finbonds-risk-assessment`); ad-hoc SQL
(`finbonds-sql-recipes`); обновления данных (`finbonds-data-refresh`).

## Нормализация intent: scope / sales_allowed / require_current_cash (029-T10)

Запрос пользователя нормализуется ДО ШАГА 1 в три НЕЗАВИСИМЫХ признака.
Нормализация — только на уровне скилла: эти поля НЕ добавляются в Python,
PostgreSQL или Google Sheet, и не смешиваются в один enum.

    scope                = FULL_PORTFOLIO | CASH_DEPLOYMENT
    sales_allowed        = true | false
    require_current_cash = true | false   (guard/refresh SOP — 029-T14)

Shorthand: FULL_REBALANCE = FULL_PORTFOLIO + sales_allowed=true. Запрета
продаж как самостоятельного mode («NO_SELL») нет: «без продаж» — это
sales_allowed=false, независимый от scope.

Приоритет semantic rules:

    явная инструкция пользователя > однозначный синоним > default-семантика scope

### Default-семантика по формулировкам

- **«Ребалансировка» → FULL_REBALANCE** (default именно для запроса
  «ребалансировка»). Фразы «предложи ребалансировку», «ребалансируй
  портфель», «пересобери портфель», «сбалансируй портфель», «оптимизируй
  портфель», «приведи портфель к условиям», «что продать и купить» — при
  отсутствии явного запрета продаж: `scope=FULL_PORTFOLIO,
  sales_allowed=true`.
- **«Свободные деньги» → CASH_DEPLOYMENT**. Фразы «куда вложить свободные
  деньги», «распредели свободный кэш», «куда вложить 500к», «реинвестируй
  купоны», «реинвестируй погашение», «докупи на свободные деньги» — по
  умолчанию: `scope=CASH_DEPLOYMENT, sales_allowed=false`.
- **«Текущие/актуальные свободные деньги» → `require_current_cash=true`**.
  Фразы «с учётом текущих свободных денег», «на свободные деньги сейчас»,
  «используй актуальный кэш на брокерских счетах», «сколько денег сейчас
  можно вложить» требуют current-cash guard (029-T14); CASH_DEPLOYMENT с
  «сейчас» («куда вложить свободные деньги сейчас») — тоже `true`. Без
  такой формулировки — `false`: canonical snapshot по обычному freshness
  SOP.
- **Явный запрет продаж**: «без продаж», «ничего не продавай», «только
  покупки», «существующие позиции не трогать», «старые бумаги оставить» —
  задают `sales_allowed=false` и сами по себе НЕ меняют scope.
- **Смешанные формулировки** (приоритет явной инструкции над default):
  - «Сделай ребалансировку, но ничего не продавай» → `scope=FULL_PORTFOLIO,
    sales_allowed=false`. Это противоречивый target-state request: если
    существующие позиции нарушают constraints и без продаж их исправить
    невозможно — покажи blocker, а не молча переключайся в CASH_DEPLOYMENT.
  - «Размести свободный кэш, но можешь продать плохие позиции» → НЕ
    автоматически FULL_REBALANCE: сохраняется `scope=CASH_DEPLOYMENT,
    sales_allowed=true`. Продажи — вспомогательное действие в рамках
    указанной цели; scope до полной санации не расширяется без явной
    команды. «Плохие позиции» неоднозначно — уточни либо не вводи скрытые
    критерии.

### Семантика FULL_REBALANCE

- Все explicit final-target constraints, которые canonical validator умеет
  проверять (`--freq-min/--freq-max/--freq-in/--min-credit-rating/
  --exclude-sovereign/--max-position-value` + встроенный лимит эмитента
  15%), относятся ко всему `positions_after` — их проверяет движок
  (constraint_checks).
- Candidate policy buy-фильтры (max-risk, max-listlevel, max-ytm, KU,
  maturity) — eligibility НОВЫХ/увеличиваемых позиций (030.6), а не
  обязанность переписать весь портфель: untouched holdings под них не
  подпадают. С 031.1 политика выражается ФЛАГАМИ plan
  (`--max-risk/--max-listlevel/--max-ytm/--include-ku/--min-maturity/
  --max-maturity`); без флагов core берёт canonical defaults
  (default_candidate_filters — те же, что у скринера отчёта). Политику,
  использованную при выборе целей в отчёте, повтори явно во `plan` —
  не подбирай её повторными report-прогонами (promote-gap 030.15
  закрыт).
- Существующая позиция НЕ получает grandfathering. Решение «кого продать» —
  на LLM (правило 030 №7), выражается явным intent: categorical violation
  (рейтинг ниже min; rating missing при заданном min; частота вне
  диапазона; частота missing; суверенные при `--exclude-sovereign`) —
  частичное сокращение НЕ исправляет нарушение → `--sell-all ISIN`;
  количественные (max_position_value, issuer concentration) —
  `--target-value ISIN=RUB` до допустимого уровня либо `--sell-all`. Точные
  qty считает fitter.
- Продажи без intent не придумываются: план трогает только указанное.
- Capital pool = current cash + estimated proceeds of sales — netting и
  бюджет считает движок.
- Turnover minimization («не продавать без необходимости») — только
  secondary optimization ПОСЛЕ выполнения constraints. Приоритет:
  explicit canonical final-target constraints → budget → остальные
  canonical feasibility checks (включая issuer limits) → deployment
  completeness (раздел ниже) → и только после этого — минимизация
  turnover/числа сделок. Существующую допустимую позицию предпочтительно
  оставить; позицию, нарушающую canonical final-target constraint, нельзя
  оставить в FULL_REBALANCE ради уменьшения turnover; candidate-only
  mismatch (риск/листинг/YTM/maturity/KU выше дефолтов политики) продажу
  сам по себе не требует.
- Завершённый FULL_REBALANCE: `data_gate.is_fresh==true` И `valid==true`,
  `feasible==true`, `budget_check.known==true`, `budget_check.passed==true`,
  `violations==[]` И deployment completeness (residual cash объяснён либо
  размещение продолжено; формально feasible all-cash — не завершение).
  Cash unknown или feasible target получить нельзя — blocker.
- План с `feasible=false` НЕ выдавать за готовый допустимый target.

### CASH_DEPLOYMENT: acceptance contract

Legacy violations могут сохраниться. Canonical validator проверяет весь
`positions_after` и НЕ классифицирует violation как inherited/new, поэтому
global `feasible=false` НЕ является автоматическим failure для
CASH_DEPLOYMENT. Скилл НЕ выдумывает inherited/new machine classification и
НЕ требует `violations==[]` для всего portfolio в этом scope.

План корректно провалидирован, если:

    valid == true
    budget_check.known == true
    budget_check.passed == true

и дополнительно:

- `sales_allowed=false` → sell-ноги отсутствуют (план без sell intents их
  и не создаёт);
- каждая buy-нога прошла canonical buy-путь движка: eligibility
  (structural gate + policy фильтры) проверяет core, ineligible BUY
  возвращается `BUY_NOT_ELIGIBLE` с reasons и ногой НЕ становится —
  ручная проверка «ISIN в скринере отчёта» не заменяет и не дублирует её,
  но выбор цели всё равно только из canonical output (правило ISIN ниже);
- для каждого bought/top-up ISIN его `positions_after` проходит
  `max_position_value`, если constraint задан (проверено движком);
- issuer limit после сделки проходит для каждого эмитента, в который
  увеличивается позиция (проверено движком);
- `require_current_cash=true` → current-cash guard (029-T14) пройден.

Global `feasible` может оставаться false из-за unrelated legacy
investment violations. Явно разделяй в ответе: «новые операции прошли
scope-specific checks» и «полный target portfolio всё ещё имеет legacy
investment violations». Послабление НЕ относится к structural validity:

- `valid=false` из-за `UNKNOWN_ISIN` / `NEGATIVE_QTY_AFTER` /
  `NO_COMPANY_LINK` — hard blocker и для CASH_DEPLOYMENT;
- unrelated legacy позиция с `NO_COMPANY_LINK` НЕ «игнорируется как legacy
  investment violation» — сначала исправить canonical data/link либо план.

Не утверждать:

- что CASH_DEPLOYMENT plan провалидирован при `valid=false`;
- что `feasible=false` всегда допустим в CASH_DEPLOYMENT (допустим только
  случай `valid=true`, когда false обусловлен legacy investment
  violations, а scope-specific checks новых операций пройдены);
- что такой план «полностью соответствует всем ограничениям портфеля» —
  это только корректный CASH_DEPLOYMENT plan.

Если `sales_allowed=true` в CASH_DEPLOYMENT: sell intents допустимы только
в рамках явно разрешённой пользователем вспомогательной цели. Это НЕ
превращает режим в полную санацию всех legacy violations. Отдельный
inherited/new violation engine не создаётся.

### Interpretation block — перед выполнением

Перед выполнением покажи краткую интерпретацию — пользователь сразу видит
semantic interpretation. Шаблоны (строки-ограничения — только фактически
нормализованные из запроса; чего пользователь не задавал, не утверждать):

FULL_REBALANCE:

    Понял задачу так:

    Режим: полная ребалансировка
    - существующие позиции можно продавать/сокращать
    - выплаты: 4-12 раз в год
    - рейтинг: AA- и выше
    - ОФЗ/суверенные выпуски РФ: исключены
    - максимум на выпуск: 150 000 ₽
    - held позиции участвуют в анализе

FULL_PORTFOLIO + sales_allowed=false:

    Режим: весь портфель под заданные условия, без продаж
    - существующие позиции не продаю
    - explicit constraints всё равно относятся ко всему target portfolio
    - если без продаж выполнить условия невозможно - покажу blocker

CASH_DEPLOYMENT:

    Режим: размещение свободных денег
    - scope не расширяется до полной санации legacy positions
    - продажи по умолчанию запрещены; если пользователь явно разрешил их,
      это меняет sales_allowed, но не scope

### Regression examples нормализации (сверяй перед ответом)

- «предложи ребалансировку» → `scope=FULL_PORTFOLIO, sales_allowed=true`;
- «ребалансируй портфель» / «пересобери портфель под эти ограничения» /
  «оптимизируй портфель» → то же;
- «ребалансируй без продаж» → FULL_PORTFOLIO, `sales_allowed=false`;
- «ничего не продавай» / «только докупки» / «существующие позиции не
  трогать» → `sales_allowed=false` без скрытой смены scope;
- «куда вложить свободные 500к» / «реинвестируй купоны» → CASH_DEPLOYMENT,
  `sales_allowed=false`, без инициации полной санации legacy positions;
- «Размести свободный кэш, но можешь продать плохие позиции» →
  CASH_DEPLOYMENT, `sales_allowed=true`, НЕ автоматически FULL_REBALANCE;
- «Сделай ребалансировку, но ничего не продавай» при неисправимом без продаж
  нарушении — blocker, не молча CASH_DEPLOYMENT;
- «с учётом текущих свободных денег» / «на свободные деньги сейчас» /
  «используй актуальный кэш на брокерских счетах» →
  `require_current_cash=true` — перед финальным планом current-cash guard
  (раздел «Current-cash guard», 029-T14).

## Semantic mapping фильтров: frequency / rating / sovereign / max-position + confidence (029-T11)

Поверх intent (раздел выше) формулировки фильтров из запроса маппятся в
canonical CLI-параметры adapter'а. Mapping — только на уровне скилла
(Python-парсеров не создавать); он независим от `scope` / `sales_allowed`:
одни и те же фразы → одни и те же флаги в любом режиме. Этот скилл —
canonical owner правил; `finbonds-chatgpt-portfolio-export` переиспользует
те же frequency/rating/sovereign правила (интеграция — 029-T15), semantics
не должны расходиться.

Каждая формулировка классифицируется по confidence ДО вызова adapter'а:

    EXACT     — конкретное значение названо прямо -> применять автоматически
    SYNONYM   — однозначно соответствует canonical параметру -> применять автоматически
    AMBIGUOUS — несколько разумных интерпретаций -> скрытый strict parameter НЕ генерировать

Правила AMBIGUOUS: в interactive flow — уточни у пользователя; если flow без
уточнений — спорный strict filter не ставится, и в ответе явно сказано, что
параметр не был численно определён. Оценочные формулировки никогда не
превращаются в числа.

### Coupon frequency

- EXACT range: «4-12 выплат в год», «от 4 до 12 выплат в год» →
  `--freq-min 4 --freq-max 12`.
- SYNONYM, safe range (все → `--freq-min 4 --freq-max 12`, т.е.
  freq_min=4, freq_max=12): «от ежемесячных до квартальных», «от квартальных
  до ежемесячных», «выплаты от месяца до квартала», «не реже квартала и не
  чаще месяца», «выплаты раз в месяц - раз в квартал».
- SYNONYM, exact set (в report → `--freq-in 4,12`) — именно две допустимые
  частоты: «только ежемесячные или квартальные», «только месячные и
  квартальные», «купоны ежемесячные либо квартальные». Set НЕ превращать в
  диапазон 4..12: диапазон допускает промежуточные частоты (например
  6 выплат/год), set — нет; подмена меняет множество допустимых бумаг.
  ВАЖНО (031.2): set-частота — теперь и constraint целевого портфеля:
  `--freq-in` есть у `plan` (и зеркала `--a-freq-in`/`--b-freq-in` у
  compare), взаимоисключим с `--freq-min/--freq-max` (конфликт ловит
  core). Set-запрос пользователя не подменять диапазоном ни в report,
  ни в plan.
- AMBIGUOUS: «максимум раз в квартал» / «квартальные максимум» без
  дополнительного контекста может означать разное (freq ≤ 4; только
  квартальные; диапазон от ежемесячных до квартальных). Скрыто в 4..12 НЕ
  переводить и вообще не ставить строгий частотный фильтр молча: уточни
  либо (flow без уточнений) не ставь фильтр и скажи явно, что частотный
  параметр не был численно определён.

### Credit rating

- EXACT/SYNONYM, явный порог — пишется как назван: «рейтинг не ниже AA-»,
  «AA- и выше», «минимум AA-», «от AA-» → `--min-credit-rating AA-`
  (аналогично любым другим явно названным порогам; флаг есть и у report,
  и у plan — final-target constraint).
- AMBIGUOUS — порог не придумывать: «высокий рейтинг», «хороший рейтинг»,
  «надёжные бумаги», «качественные эмитенты», «без мусора», «не ВДО».
  Фильтр не ставить, смотреть `credit_rating` в строках (правило 023:
  Hermes не придумывает AA-).

### Sovereign

- SYNONYM, safe (все → `--exclude-sovereign`): «без суверенных»,
  «суверенные исключить», «без государственных облигаций РФ».
- Compatibility mapping — допустимо маппить, но с ОБЯЗАТЕЛЬНОЙ user-visible
  оговоркой расширения: «не ОФЗ», «без ОФЗ», «ОФЗ исключить» →
  `--exclude-sovereign` + в ответе строка «ОФЗ/суверенные выпуски РФ:
  исключены». Flag исключает не только ОФЗ, но и другие sovereign rows
  (например евро-РФ) — расширение смысла нельзя скрывать (оговорка уже
  входит в interpretation block).
- НЕ safe synonyms — автоматически на `--exclude-sovereign` НЕ маппить:
  «государственные не брать» (может означать запрет бумаг госкомпаний),
  «только корпоративные облигации» (задаёт corporate-only множество).
  Если пользователь требует именно corporate-only, а canonical filter этого
  не выражает — Hermes не подменяет его `--exclude-sovereign` молча:
  уточни либо явно оговори фактическую семантику применённого фильтра.

### Max position value

- EXACT/SYNONYM (число как названо → `--max-position-value 150000`):
  «не более 150тр на бумагу», «не больше 150 тысяч на бумагу», «максимум
  150к на выпуск», «до 150 тыс на ISIN», «позиция максимум 150 тысяч»,
  «не более 150000 на одну бумагу».
- Numeric aliases (однозначные обозначения → число в рублях):
  `150к`, `150k`, `150 тыс`, `150тр`, `150 000`, `150000` → 150000 RUB.
- Флаг есть и у report, и у plan: fitter pre-size'ит покупки под лимит,
  validator проверяет каждую позицию `positions_after`
  (`POSITION_VALUE_LIMIT`); вручную по строкам не пересчитывать. В
  `finbonds-chatgpt-portfolio-export` НЕ передавать (029-T15).

### Regression examples mapping (тест 30) — сверяй перед ответом

- «от ежемесячных до квартальных» / «от квартальных до ежемесячных» /
  «выплаты от месяца до квартала» / «не реже квартала и не чаще месяца» /
  «выплаты раз в месяц - раз в квартал» → freq_min=4, freq_max=12;
- «только ежемесячные или квартальные» / «только месячные и квартальные» /
  «купоны ежемесячные либо квартальные» → report `--freq-in 4,12`; на
  plan-пути (final-target constraint) — `plan --freq-in 4,12` (031.2);
  set НЕ превращается в диапазон 4..12;
- «AA- и выше» / «не ниже AA-» / «минимум AA-» / «от AA-» →
  min_credit_rating=AA-;
- «без суверенных» / «суверенные исключить» / «без государственных
  облигаций РФ» → exclude_sovereign=true;
- «не ОФЗ» / «без ОФЗ» / «ОФЗ исключить» → exclude_sovereign=true +
  обязательная оговорка «ОФЗ/суверенные выпуски РФ: исключены»;
- «не больше 150к на бумагу» / «до 150 тыс на выпуск» / «максимум 150тр на
  ISIN» / «позиция не выше 150000» → max_position_value=150000.

### Ambiguity safety (тест 31) — скрытых численных параметров не генерировать

- «высокий рейтинг» / «надёжные бумаги» / «хорошее качество» / «не
  мусорные» → БЕЗ автоматического rating threshold;
- «подлиннее» / «не короткие» → БЕЗ автоматического maturity фильтра
  (окно реинвеста `--min-maturity` — только явный горизонт);
- «ликвидные» / «нормальная ликвидность» → БЕЗ численного liquidity
  фильтра;
- никакие скрытые численные параметры не генерируются из AMBIGUOUS
  формулировок: строгий фильтр не ставится, в ответе явно сказано, что
  параметр не был численно определён.

## PlanningContext и --context-id (030.3/030.4)

Каждый `plan`/`compare` считается на frozen PlanningContext: один
согласованный срез данных (REPEATABLE READ), frozen `as_of`, canonical
data gate, детерминированный `context_fingerprint`. Без `--context-id` —
свежая сборка; с `--context-id` (opaque id из `analysis-snapshot`) —
core-owned контекст из EphemeralContextStore (TTL, версия, fingerprint
проверяются core; отказы `CONTEXT_*` — machine-readable JSON, не
traceback).

- `compare` считает ОБА варианта на ОДНОМ контексте (общий `--context-id`
  или одна свежая сборка) — delta не может смешать эффект стратегии с
  изменением рыночных данных между вызовами.
- Два `plan`-вызова в разное время без `--context-id` = разные
  `context_fingerprint` — их результаты напрямую сравнивать нельзя
  (сравнение — `compare`).
- Workflow snapshot → sandbox → план: `plan --context-id <id из snapshot>`
  считает по тому же frozen срезу, БД не перечитывается.
- `feasible` (финансовая ось) и `data_gate` (ось свежести) — разные поля,
  друг в друга не конвертируются.

## SOP: обязательный порядок шагов (standard path)

Порядок не менять. Все числа — из JSON adapter'а; ручная арифметика по
долям/потокам/qty запрещена (анти-P6, правило 030 №7). Baseline обычной
ребалансировки — 2 domain-вызова (`report` → `plan`); для FULL_REBALANCE
разрешены дополнительные итерации `plan` (разумный предел — около 3 правок
intents; исчерпание — остановка с blockers, не бесконечный цикл).

0. **ШАГ 0 (Нормализация intent + semantic mapping фильтров):** нормализуй
   запрос в `scope` / `sales_allowed` / `require_current_cash` по разделу
   «Нормализация intent» (приоритет: явная инструкция > однозначный
   синоним > default-семантика scope) и классифицируй формулировки
   фильтров (частота / рейтинг / суверенные / лимит выпуска) по разделу
   «Semantic mapping фильтров» (EXACT/SYNONYM → применяются автоматически;
   AMBIGUOUS → строгий параметр скрыто не ставится). Покажи
   interpretation block — и только потом вызывай adapter. Смешанные
   формулировки разрешай по явной инструкции пользователя;
   противоречивый target-state request («ребалансировка без продаж» при
   неисправимом без продаж нарушении) — blocker, а не молчаливая смена
   scope.
1. **ШАГ 1 (Отчёт-ядро, data freshness gate):** вызови `report` через
   adapter с нормализованными фильтрами (`--include-held` в
   ребалансировке обязателен; полный флаг-набор — в
   `finbonds-rebalance-tool`). Один вызов даёт свежесть + портфель +
   концентрации + скринер + погашения. Проверь `meta`:
   `is_fresh == true` (статусы `OK`/`WARNING`, в т.ч. `gate_status`
   `WARN_ZOMBIE_ROWS` — зомби-строки погашенных бумаг не мешают и не
   разбираются) → продолжай; `is_fresh == false` (`STALE_*`/`CRITICAL_*`)
   → обновление данных по `finbonds-data-refresh` и повтор ШАГА 1.
   В НАЧАЛЕ ответа приведи капитал читаемым блоком из полей `portfolio`
   (числа — только из JSON, не из памяти):
   - cash известен:
     ```text
     Облигации: <securities_value_rub> ₽
     Свободные деньги: <cash_available_rub> ₽
     Инвестиционный капитал: <investable_total_rub> ₽
     ```
   - `cash_available_rub = null`:
     ```text
     Облигации: <securities_value_rub> ₽
     Свободные деньги: неизвестно
     ```
     (инвестиционный капитал в этом случае не называй — он не определён).
2. **ШАГ 2 (LLM выбирает target intents + priority):** интерпретируй
   отчёт и сформируй intents — это решение LLM (правило 030 №7):
   - продажи: `concentrations.issuers.violations` (превышения лимита 15%
     «сейчас»), нарушители final-target constraints (категориальные →
     `--sell-all`, количественные → `--target-value` до допустимого
     уровня), ротация/риск-события по явной цели пользователя. Продажи без
     intent не придумываются (CASH_DEPLOYMENT — только явно разрешённые
     вспомогательные; «притоки вместо продаж» — см. Pitfalls).
   - покупки: цели ТОЛЬКО из canonical output этого вызова —
     `screener.fixed[]`/`screener.floater[]`, `screener.issuers[]`
     (агрегат «эмитент → серии» для выбора между одноимёнными выпусками),
     `redemptions_6m` (пул реинвеста). ISIN никогда по памяти (правило
     ниже). Каждая цель — `--target-value ISIN=RUB`: верхняя граница
     итоговой стоимости позиции; фактический qty/урезание по бюджету и
     лимитам считает fitter.
   - candidate-policy: если цели выбирались из отчёта с более широкой
     политикой покупки (например `report --max-risk 3 --include-ku`),
     та же политика ОБЯЗАНА поехать во `plan` флагами candidate-policy
     (`--max-risk/--max-listlevel/--max-ytm/--include-ku/--min-maturity/
     --max-maturity`, передаются в ШАГЕ 3). Политика выражается
     аргументами plan, а НЕ подбором report-фильтров (promote-gap 030.15
     закрыт, 031.1): без явных флагов plan считается по canonical
     defaults (max-risk 1, KU исключены) и выбранные по более широкой
     политике цели получат `BUY_NOT_ELIGIBLE`.
   - priority: ПОРЯДОК повторений `--target-value` = allocation priority
     (первый — самый приоритетный); при нехватке бюджета/лимита fitter
     режет менее приоритетные (более поздние) покупки первыми. Приоритет
     между целями — явное решение LLM, объяснимое в ответе.
   - `require_current_cash=true` → current-cash guard (раздел ниже) ДО
     финального плана; бюджет — только по post-refresh отчёту.
   - `cash_available_rub = null` = unknown, не 0 (см. JSON-контракт);
     отдельно не запрашивать broker API за деньгами (канонический поток —
     sync-portfolio → PostgreSQL → отчёт/план).
3. **ШАГ 3 (План):** вызови `plan` через adapter: intents + все
   нормализованные final-target constraints
   (`--freq-min/--freq-max/--freq-in/--min-credit-rating/--exclude-sovereign/
   --max-position-value`; `--freq-in` взаимоисключим с
   `--freq-min/--freq-max` — конфликт ловит core) + выбранная в ШАГЕ 2
   candidate-policy (`--max-risk/--max-listlevel/--max-ytm/--include-ku/
   --min-maturity/--max-maturity`). Сначала прочитай `diagnostics[]`:
   - intents: `INVALID_TARGET_VALUE`, `INVALID_INTENT`,
     `DUPLICATE_TARGET`, `CONFLICTING_INTENT` → исправь intents (не числа
     в JSON — их нет), повтори;
   - context: `CONTEXT_NOT_FOUND`/`CONTEXT_STALE`/`CONTEXT_VERSION_MISMATCH`/
     `CONTEXT_FINGERPRINT_MISMATCH` → context истёк/не тот: пересобери
     snapshot или план без `--context-id`; `CONTEXT_NOT_FOUND` для
     произвольной строки без handle — корректный machine-readable отказ,
     не ошибка вызова (handle'ы минтит `analysis-snapshot`, раздел
     «PlanningContext и --context-id»);
   - plan: `UNKNOWN_ISIN` (ISIN не из canonical output), `BUY_NOT_ELIGIBLE`
     с structured `reasons[]` (eligibility gate 030.6: ineligible BUY
     ногой НЕ становится — выбери ДРУГУЮ цель из canonical output, не
     «дожимай» ineligible бумагу и не обходи gate), `PRICE_UNAVAILABLE`
     (цены расчёта нет — sizing невозможен), `CONTEXT_PRICE_MISMATCH` →
     учитывай и повтори с исправленными intents;
   - структурные ошибки движка (`NEGATIVE_QTY_AFTER`, `NO_COMPANY_LINK`) →
     исправь intents/данные, повтори.
   Затем вердикты: `valid`, `feasible`, `price_consistent`, `violations[]`
   (constraint_checks), `budget_check`, `summary`, `targets` (requested vs
   fitted), `trades`, `positions_after`. Правка после violations — это
   изменение INTENTS (другая цель из canonical output, другой приоритет,
   добавление продажи нарушителя), а НЕ ручной подбор qty: sizing/netting/
   бюджет/лимиты пересчитывает fitter на каждом прогоне. Компактные
   проекции — `--view summary/targets/trades/positions-after/violations`.
4. **ШАГ 4 (Альтернатива — только если нужна):** `compare` через adapter
   строит A/B на одном PlanningContext: варианты отличаются только
   префиксом `--a-*`/`--b-*`, общий `--context-id` (или одна свежая
   сборка). Готовые plan results с разными `context_fingerprint`
   сравнивать нельзя (core вернёт `CONTEXT_MISMATCH`, `comparable=false`).
   LLM интерпретирует `delta` (вариант − baseline, считает core) и
   canonical блоки `variants[]`, объясняет trade-off и выбирает
   рекомендацию.
5. **ШАГ 5 (Гейт перед рекомендацией):** проверь ОБЕ оси ОТДЕЛЬНО:
   - data_gate / `meta.is_fresh` (свежесть): stale/critical → обновление
     данных и повтор; `feasible=true` при stale/critical data ≠ «можно
     рекомендовать»;
   - canonical validation: `valid==true`, `feasible==true`,
     `violations==[]`, `budget_check.known==true`,
     `budget_check.passed==true`. Каждое violation из `violations[]`
     (type/isin/actual/limit) либо устранено новой итерацией intents, либо
     явно перечислено в ответе (для CASH_DEPLOYMENT допустимое исключение —
     только по acceptance contract этого scope). `feasible == null` —
     осуществимость не определена (структурные ошибки или unknown cash):
     скажи об этом явно, не трактуй как «всё ок» и не как «отказ»;
     `budget_check.passed == false` — `INSUFFICIENT_CASH_ESTIMATE`.
   Для FULL_REBALANCE — deployment completeness (раздел ниже): feasible
   all-cash прогон не завершает полную ребалансировку.
6. **ШАГ 6 (Explain):** итоговый ответ — таблица отклонений → действия
   (ISIN, effect — числа из `targets`/`trades`/`summary`), блок бюджета
   после плана (числа из `summary`; при residual cash — объективная
   причина):
   ```text
   Покупки: <purchases_value> ₽
   Продажи: <sales_value> ₽
   Свободные деньги после плана: ~<cash_after_estimate> ₽
   ```
   (~ — оценка qty × planning price, без НКД и комиссий settlement'а —
   сказать явно). При `require_current_cash=true` числа — только из
   post-refresh отчёта/плана, новый `cash_updated_at` показан в ответе
   (тест 33.3.2). Объясни trade-off выбранного варианта (если был
   `compare`). Пометка «требует подтверждения человеком». Никаких
   исполняемых команд к бирже/брокеру — только текст. Валидация встроена
   в `plan` и всегда проверяется до рекомендации — «объяснять» невалидный
   план как готовый target запрещено.

## Долговой overlay (`--debt-overlay`, опционально, issue #4)

`rebalance-report --debt-overlay` добавляет верхнеуровневый блок `debt_overlay`
(`advisory=true`, `affects_plan_validity=false`). По умолчанию флаг выключен —
включай только если пользователь просит про долговую нагрузку. Блок не меняет
`scenario.valid/feasible`, лимиты и гейт; не является триггером продажи и
описывает текущий портфель, а не состояние после сценария. Правила чтения
(`issued_bond_volume_rub` ≠ долг, `unknowns[]`, неизвестно = неизвестно) —
скилл `finbonds-company-debt`.

## Anti-rules standard workflow (обязательные)

1. Если domain tool умеет операцию — не воспроизводить её Python'ом.
2. Не считать qty (ни «сумма ÷ price», ни подбор после violation) —
   sizing/target value → qty делает fitter.
3. Не мутировать scenario JSON — сценарных файлов в standard path больше
   нет; intents передаются флагами adapter'а.
4. Не использовать sed/heredoc (в т.ч. `cat > file <<EOF` для JSON на
   хосте) — adapter принимает только whitelist-флаги, файлов не требует.
5. Не считать issuer share / cashflow / budget вручную — движок.
6. Не применять pandas напрямую к данным портфеля в standard path.
7. Unsupported **read-only analysis** → сначала controlled escape hatch
   (раздел «Sandbox fallback»), если безопасно.
8. `UNSUPPORTED_INTENT` (вердикт уровня скилла: «запрошенное действие
   не выполняю») — только когда действие нельзя безопасно выразить ни
   domain API, ни read-only fallback, либо требуется unsupported
   mutation/action. НЕ возвращать его из-за отсутствия заранее
   написанного projection для новой аналитической идеи.
9. Escape hatch не имеет права подменять canonical planner/validator
   (границы — раздел «Sandbox fallback»).

## Capability ladder (порядок сверху вниз, не прыгать в Python)

Каждый следующий уровень — только если предыдущий не отвечает на вопрос
задачи. Спускаться по лестнице, а не стартовать с Python:

1. **Typed domain operation** — `report` / `plan` / `compare` через
   adapter. Первая попытка для любого ребалансировочного вопроса.
2. **Compact projection / compare** — `--view` (summary/targets/trades/
   positions-after/violations) и `compare` с canonical `delta`: если
   вопрос про «сколько/что изменится», сначала готовые проекции.
3. **Read-only analysis-snapshot** — нестандартный аналитический вопрос:
   `analysis-snapshot` (core-owned frozen контекст + полные данные без
   top-N + capabilities/limitations).
4. **Sandboxed Python/pandas над snapshot** — controlled escape hatch:
   только read-only exploration над artifact'ом snapshot'а (или plan
   result при post-analysis), в границах раздела «Sandbox fallback».
5. **Proposed intents → canonical plan** — результат fallback передаётся
   обратно в canonical путь: `plan --context-id <snapshot context>` с
   proposed intents (НЕ изменённые portfolio/price/cash rows).
6. **Optional custom read-only check над final plan** — проверка
   дополнительного пользовательского критерия (которого нет в canonical
   validator) на `positions_after` того же контекста; результат
   маркируется как custom analysis, не canonical verdict.

Правило «не прыгать»: уровень 4 не используется, пока вопрос не попробован
на уровнях 1–3; уровень 5 — обязательный возврат в canonical путь после
уровня 4 (fallback сам планом не становится).

## Sandbox fallback (controlled escape hatch, 030.12)

Fallback — для unsupported **read-only** аналитики поверх canonical
snapshot. Это не второй planner и не лазейка в данные.

### Где выполняется

Существующий isolated `execute_code` / Python sandbox Hermes: нет prod DSN
и broker credentials; network-disabled режим — если доступен. Писать
Python-файлы на prod host и запускать agent-generated Python через SSH на
host запрещено (см. ниже).

### Запрещено ради fallback (полный список)

- писать Python-файл на prod host;
- запускать agent-generated Python через SSH на host;
- подключаться Python-скриптом напрямую к PostgreSQL (DSN/psql в sandbox
  недоступны и не нужны);
- обращаться к TBank/MOEX/Google API в обход canonical tools;
- отправлять portfolio snapshot / ISIN / позиции на внешние web/API
  endpoints (артефакт приватный — только локальный sandbox);
- читать `.env` / секреты;
- выполнять broker/write actions;
- использовать `subprocess`/shell для обхода этих ограничений.

### Граница input/output

- **Input — только canonical JSON/result/artifact от read-only tools**:
  artifact-файл `analysis-snapshot` (handle `{path, sha256}` из ответа
  adapter'а) или plan result при post-analysis. Fallback считает payload
  read-only input.
- Fallback может создавать derived tables/results/intents.
- Обратно planner передаёт только `context_id` + intents / custom result —
  НЕ изменённые portfolio/price/cash rows.

### МОЖЕТ (read-only exploration, которого нет в domain API)

- groupby / pivot / aggregate;
- ranking / scoring;
- статистика и распределения;
- корреляции;
- custom filtering;
- анализ календаря выплат — только если snapshot реально содержит
  canonical payment dates (иначе только frequency-based метрики с явной
  маркировкой approximation);
- сравнение duration/maturity/rating mix с корректной обработкой
  NULL/reason;
- custom read-only метрики;
- shortlist с учётом `structural_buy_eligible`;
- стратегические target budgets/weights как предложения;
- проверка дополнительного пользовательского критерия, ещё не
  встроенного в canonical validator.

### НЕ МОЖЕТ молча (imputation/approximation policy)

- заполнять NULL средним/нулём;
- превращать `ytm_reason=floating_coupon` в YTM;
- восстанавливать точные даты купонов из frequency;
- считать отсутствующий amortization schedule;
- придумывать settlement/NKD/commission data.

Любая imputation/approximation — явная часть метода анализа (названа в
provenance и в ответе). Неизвестное значение остаётся неизвестным:
NULL + canonical reason (`ytm_reason`, `cash_known=false`,
`ANALYSIS_DATA_UNAVAILABLE`) — данные не синтезируются.

### НЕ МОЖЕТ как authoritative result

- ослаблять/удалять/переинтерпретировать hard constraints пользователя
  (они передаются planner отдельно и проверяются canonical engine);
- final qty;
- canonical trade legs;
- cash_after / budget verdict;
- issuer concentration verdict;
- canonical coupon delta;
- position-value limit;
- canonical `valid`/`feasible`;
- ручная мутация low-level scenario JSON;
- обход `PRICE_UNAVAILABLE` / BUY eligibility (`BUY_NOT_ELIGIBLE` /
  `structural_buy_eligible=false`);
- предложение BUY по `structural_buy_eligible=false` как готового target;
- broker execution.

### Custom hard constraint, которого engine ещё не знает

Допустимый workflow:

1. `analysis-snapshot` → получить `context_id` + artifact (копия payload
   в sandbox);
2. sandbox выбирает/ранжирует targets по пользовательскому критерию;
3. возвращает `context_id` + proposed intents (не snapshot rows);
4. `rebalance-plan --context-id <X>` строит canonical trades;
5. sandbox при необходимости проверяет unsupported custom criterion на
   `positions_after` context X (тот же `context_fingerprint`);
6. LLM объясняет canonical feasibility + ОТДЕЛЬНО custom check.

Правила: custom check использует тот же `context_fingerprint`;
`scenario.feasible=true` означает только canonical constraints;
Python-only hard criterion нельзя молча представлять как часть canonical
`feasible`; в финальном ответе явно различать `canonical_checks` и
`custom_analysis_checks`.

### Provenance fallback (fallback не должен быть невидимым)

При использовании escape hatch сохранить/показать в результате минимум
(поля адаптируемые; смысл — воспроизводимость и явная маркировка):

    analysis_mode: python_fallback
    reason: domain_api_missing_capability   (или другая фактическая причина)
    context_id: <id>
    context_fingerprint: <fingerprint>
    calculation_policy_version: <версия из snapshot>
    purpose: <какой вопрос считаем>
    data_capabilities_used: <коды datasets из snapshot capabilities>
    approximations: <список допущений/imputation; [] если нет>
    canonical_plan_required: true

### Promote repeated gaps

- Единичный новый вопрос → controlled fallback нормален.
- Повторяющийся полезный паттерн → кандидат на typed domain capability
  (отдельная задача; БЕЗ создания новой команды «на каждый
  вопрос» сейчас). Зафиксируй gap в задаче/отчёте сессии: вопрос, какие
  datasets не хватило (`limitations` / `ANALYSIS_DATA_UNAVAILABLE`),
  как считал fallback.
- Не копировать один и тот же ad-hoc скрипт из сессии в сессию как
  «скрытый API».
- `UNSUPPORTED_INTENT` — только когда действие нельзя безопасно выразить
  ни domain API, ни read-only fallback, либо требуется unsupported
  mutation (см. Anti-rules №8). Отсутствие projection для новой
  аналитической идеи — это повод для snapshot+fallback, а не для
  `UNSUPPORTED_INTENT`.

### Приватность snapshot

Snapshot-артефакт (и весь payload) приватный: ISIN/состав портфеля
наружу (web-поиск, внешние API, публичные репозитории) не отправлять и не
публиковать. Sandbox — локальный контур; `sha256` handle'а позволяет
сверить, что считался именно выданный artifact.

## Current-cash guard: refresh SOP при require_current_cash (029-T14)

### cash_known ≠ cash_fresh

Canonical contract сохраняет последний успешный cash snapshot при ошибке
TBank API. Поэтому возможна комбинация: `cash_available_rub != null`,
`budget_check.known == true`, `meta.is_fresh == true` — при старом
`cash_updated_at`: `meta.is_fresh` проверяет portfolio/catalog/market
freshness и возраст cash snapshot НЕ включает. Для формулировок «текущие
свободные деньги», «актуальные свободные деньги», «сколько денег сейчас
можно вложить» одного `budget_check.known == true` недостаточно — нужен
proof нового cash snapshot.

### Refresh SOP (require_current_cash=true)

Перед построением финального плана:

1. Получить initial `report` (ШАГ 1) и запомнить
   `old_cash_updated_at = portfolio.cash_updated_at`.
2. Выполнить canonical TBank sync workflow на хосте (это data-refresh
   команда, не ребалансировочная — прямой вызов допустим):

   ```bash
   ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py sync-portfolio --source=tbank'
   ```

   Для current-cash proof значим только результат cash publish; успешность
   обновления positions тем же вызовом НЕ доказывается — positions/market
   freshness отдельно подтверждает `meta.is_fresh` (ниже).
3. Повторно выполнить `report` с ТЕМ ЖЕ normalized флаг-набором.
4. Проверить в новом report:
   - `cash_available_rub != null`;
   - `cash_updated_at != null`;
   - если old_cash_updated_at был не null — новый `cash_updated_at` СТРОГО
     больше старого;
   - если old был null — после refresh стал non-null.
5. Только новый report использовать как базу intents/плана (Case A,
   тест 33.3.2); финальный ответ показывает новый `cash_updated_at`.

Exit code `sync-portfolio` сам по себе — не proof: при cash API failure
команда может обновить/сохранить positions, оставить СТАРЫЙ cash и лишь
записать WARNING. Поэтому proof — только продвижение агрегированного
`cash_updated_at` (`MAX(portfolio_cash_balances.updated_at)`): T01 (strict
preflight) + T02 (synthetic zero RUB row) + T03 (atomic publish
`replace_cash_balances_snapshot`) закрыли multi-account дыры — продвижение
агрегата = proof нового полного TBank RUB snapshot.

Guard доказывает только CASH snapshot (Case E, тест 33.3.2):
`sync-portfolio --source=tbank` может обновить cash и затем не перезаписать
positions при пустом/ошибочном position fetch. Не заявлять «cash и
positions обновлены одним snapshot» только по продвижению
`cash_updated_at` — positions/market freshness проверяется отдельно через
`meta.is_fresh` / canonical data gate.

### Если cash_updated_at не продвинулся (Case B)

- НЕ называть старый snapshot «текущими свободными деньгами»;
- FULL_REBALANCE / CASH_DEPLOYMENT с `require_current_cash=true` НЕ
  завершать как точный budget-valid target;
- показать blocker «не удалось подтвердить актуальный cash snapshot»;
- НЕ обращаться к broker API отдельным ad-hoc вызовом и НЕ подменять cash
  ручным числом.

Case C: при `cash_available_rub = null` успешный refresh, после которого
cash и `cash_updated_at` стали non-null, снимает unknown — работа
продолжается с новым snapshot.

### Границы исключения

Это осознанное исключение к обычному freshness SOP, обоснованное explicit
intent «текущий cash»: полный `run_update.sh` ради cash refresh не
запускается — достаточно canonical `sync-portfolio --source=tbank`. Если
пользователь НЕ требовал current cash — существующий canonical snapshot
используется по текущему контракту, но заметно старый `cash_updated_at`
показывается/оговаривается в ответе; произвольный TTL для такого случая
не вводится.

## Deployment completeness (029-T13, уровень скилла)

`feasible=true` доказывает прохождение формальных constraints, но НЕ
качество размещения капитала. Тривиальный sell-only/empty-securities план
(«продать всё, ничего не купить») может быть `valid=true`, `feasible=true`,
`violations==[]` — для FULL_REBALANCE это НЕ завершение, если пользователь
явно не попросил уйти в cash или оставить cash reserve (тест 33.5).

Итоговый порядок оптимизации FULL_REBALANCE:

    1. explicit canonical final-target constraints
    2. budget / feasibility / issuer limits
    3. deployment completeness: не оставлять необъяснённый cash, если есть
       допустимая покупочная ёмкость
    4. минимизация turnover/числа сделок

Среди feasible вариантов предпочитай большее разумное размещение
investable capital в допустимые облигации и меньший необъяснённый residual
cash. Это secondary optimization ПОСЛЕ формальных constraints и бюджета,
НО ДО turnover minimization. Новый Python constraint на минимальный
securities allocation НЕ вводится — это completion rule уровня скилла:
если residual cash есть, а feasible capacity осталась — предложи
дополнительные цели из canonical output и повтори `plan`.

Residual cash допустим и должен быть ЯВНО объяснён в ответе — без
произвольного процента «cash должен быть < X%» и без фиксированного
буфера. Объективные причины остатка:

- lot/quantity granularity (fitter округляет к целым бумагам вниз);
- исчерпана допустимая ёмкость по `max_position_value` / issuer limits
  (fitter урезал покупки);
- candidate universe исчерпан (см. Screener limit);
- пользователь явно попросил cash reserve;
- operational settlement buffer под НКД/комиссии/небольшое изменение цены:
  `cash_after_estimate` считается как `qty × price` и НЕ является точным
  settlement amount (бюджет-чек НКД и комиссии не учитывает);
- объективный blocker данных/рынка.

Правила:

- residual cash с обоснованной причиной — НЕ автоматическая ошибка; малый
  разумный остаток может быть лучше формально «полного» размещения;
- НЕ добавлять покупку только ради сведения `cash_after_estimate` к 0 ₽;
- в ответе явно сказать, что `cash_after_estimate` не учитывает НКД и
  комиссии;
- при существенном необъяснённом cash и очевидной дополнительной feasible
  buy capacity — продолжать итерации intents по ШАГАМ 2–3, а не завершать
  работу только потому, что `feasible=true`.

## Screener limit и достаточность candidate universe (029-T12)

Canonical default `--screener-limit 10` (FIX + FLOAT) сохранён, но
FULL_REBALANCE не должен считать top-10 каждого типа достаточным по
определению. Для докупаемой held-бумаги ёмкость ограничена headroom до
`max_position_value`, issuer concentration может уменьшить её ещё сильнее;
фактический fit считает fitter.

Skill-level policy — различай provenance limit: `USER_EXPLICIT` /
`SKILL_POLICY` / `CANONICAL_DEFAULT` (тест 33.1):

- для FULL_REBALANCE использовать analysis default `--screener-limit 30`
  в `report`, если пользователь явно не задал другое; это не investment
  constraint, а техническая ширина candidate universe — skill-selected
  limit показывать отдельно от user constraints;
- если feasible target не строится ИЛИ формально feasible план оставляет
  необъяснённый residual cash при недостаточной опубликованной ёмкости, а
  `fixed_matched`/`floater_matched` показывают непоказанных подходящих
  кандидатов за пределами top-N — расширять finite limit (например
  50/100) автоматически можно ТОЛЬКО когда текущий cap из `SKILL_POLICY`
  или `CANONICAL_DEFAULT`;
- `USER_EXPLICIT` («не больше 30 кандидатов каждого типа») — не превышать
  cap молча: сообщить, что ограничение candidate universe мешает построить
  feasible target; в interactive flow можно предложить расширить — только
  после явного согласия/нового запроса пользователя;
- не объявлять «нет подходящих кандидатов» только потому, что закончился
  автоматически выбранный top-N: каноническая проверка ширины пула —
  `analysis-snapshot` (universe без top-N, `row_count` + политика пула);
- не экспортировать весь market catalog без необходимости.

## Prerequisites

- Контракт среды (хост, каталог приложения, transport) — `finbonds-ops`;
  контракт adapter'а (whitelist, env-переменные, exit codes, artifact) —
  `finbonds-rebalance-tool` (030.11). Доменные команды — только через
  adapter; DSN и psql не нужны: слой хранения core читает `.env` на хосте
  сам (карта DSN — в `finbonds-ops`).
- Лимит эмитента 15% встроен в движок
  (constraint_checks `ISSUER_LIMIT`). Остальные лимиты (дюрация,
  риск-микс, частоты) пользователь держит приватно и даёт в диалоге или
  приватным файлом; форма — `templates/target_allocation.example.yaml` в
  этом скилле. Лимитов нет — сначала согласуй, не выдумывай.
- Отдельный вызов гейта свежести (`finbonds-data-refresh`) для standard
  path не нужен: свежесть встроена в `meta` отчёта и `data_gate`
  plan/compare/snapshot (тот же контракт гейта v2).

## Канонические команды (только через adapter, 030.11)

Скрипт adapter'а:
`/opt/data/profiles/fin_bonds/skills/finance/finbonds-rebalance-tool/scripts/rebalance_adapter.py`
(stdout — machine-readable JSON, stderr — diagnostics, exit code
сохраняется от core; полная карта — `finbonds-rebalance-tool`).

Отчёт-ядро (гейт + портфель + концентрации + скринер + погашения одним
вызовом; `--include-held` в ребалансировке обязателен):

```bash
python3 /opt/data/profiles/fin_bonds/skills/finance/finbonds-rebalance-tool/scripts/rebalance_adapter.py report --freq-max 4 --exclude-sovereign --include-held --min-maturity 2026-12-01 --max-risk 1
```

Диапазон частот + порог рейтинга + лимит выпуска (023/024; порог и лимит —
только явные от пользователя):

```bash
python3 /opt/data/profiles/fin_bonds/skills/finance/finbonds-rebalance-tool/scripts/rebalance_adapter.py report --freq-min 4 --freq-max 12 --min-credit-rating AA- --exclude-sovereign --max-position-value 150000 --screener-limit 30
```

План по intents (порядок `--target-value` = priority; exploratory —
обычно `--view full` или дефолт; компактные проекции — `--view summary|targets|trades|positions-after|violations`):

```bash
python3 /opt/data/profiles/fin_bonds/skills/finance/finbonds-rebalance-tool/scripts/rebalance_adapter.py plan --sell-all RU000A0ZZZ01 --target-value RU000B0YYYY2=350000 --target-value RU000C0XXXX3=200000 --freq-min 4 --freq-max 12 --min-credit-rating AA- --exclude-sovereign --max-position-value 150000
```

Candidate-policy покупки (031.1/031.2): политика, использованная при
выборе целей в отчёте, повторяется флагами plan (set-частота —
`--freq-in`, взаимоисключим с `--freq-min/--freq-max`):

```bash
python3 /opt/data/profiles/fin_bonds/skills/finance/finbonds-rebalance-tool/scripts/rebalance_adapter.py plan --target-value RU000B0YYYY2=350000 --max-risk 3 --include-ku --max-ytm 30 --min-maturity 2026-01-01 --freq-in 4,12
```

A/B альтернативы на одном PlanningContext:

```bash
python3 /opt/data/profiles/fin_bonds/skills/finance/finbonds-rebalance-tool/scripts/rebalance_adapter.py compare --a-target-value RU000B0YYYY2=350000 --b-target-value RU000B0YYYY2=500000 --b-exclude-sovereign
```

Нестандартная аналитика (escape hatch, раздел «Sandbox fallback»):

```bash
python3 /opt/data/profiles/fin_bonds/skills/finance/finbonds-rebalance-tool/scripts/rebalance_adapter.py analysis-snapshot --requires ytm,credit_rating
```

Затем `plan --context-id <id из snapshot>` — canonical план по тому же
frozen срезу.

Current-cash guard — canonical `sync-portfolio --source=tbank` (единственный
прямой ssh-вызов в этом SOP; это data-refresh команда, раздел
«Current-cash guard»):

```bash
ssh ${FIN_BONDS_HOST} 'cd ${FIN_BONDS_APP_DIR:-$HOME/work/bonds} && venv/bin/python main.py sync-portfolio --source=tbank'
```

Соответствие «запрос пользователя → фильтры» (классификация фраз по
confidence — в разделе «Semantic mapping фильтров»; здесь — семантика самих
флагов):

- «выплаты от одного раза в месяц до одного раза в квартал» (= «от квартальных
  до ежемесячных» и другие safe range синонимы) → `--freq-min 4 --freq-max 12`
  (023: диапазон 4..12, а не один `--freq-max 4`; NULL/0-частота не проходит;
  старый вызов «только --freq-max» — прежняя семантика freq ≤ max);
  «максимум раз в квартал» / «квартальные максимум» — AMBIGUOUS: скрыто не
  переводить ни в 4..12, ни в иной строгий частотный фильтр;
  «только квартальные и полугодовые» → `--freq-in 2,4` (report и plan,
  031.2); «ежемесячные» → `--freq-in 12` (`--freq-min`/`--freq-max` и
  `--freq-in` взаимоисключающие — конфликт ловит core; в calculate-ytm те
  же фильтры называются `--coupon-freq-min/--coupon-freq-max/--coupon-freq-in`).
- «рейтинг не ниже AA-» → `--min-credit-rating AA-` (023): строгий порог по
  настоящему рейтингу кандидата (latest `rating_history`); бумаги без рейтинга
  и ниже порога исключаются (`excluded.rating_missing` /
  `excluded.rating_below_min`). Код порога пишется как есть — `AA-`, `AA-.RU`,
  `AAA(RU)` нормализует core. Граница LLM/код: конкретный порог в команду
  кладёт пользователь/диалог — «высокий рейтинг» в `--min-credit-rating` не
  переводить, порог не придумывать; нет явного порога — фильтр не ставить и
  смотреть `credit_rating` в строках. `risk_level` и «risk_level + list_level»
  — НЕ строгий рейтинг: прокси-оговорка допустима только когда рейтинговый
  фильтр не ставился и `credit_rating` пуст.
- «не ОФЗ» → `--exclude-sovereign` (ОФЗ + евро-РФ, P-G) — не вычёркивать
  вручную.
- «итоговая стоимость одного выпуска не более N ₽» → `--max-position-value N`
  (024): лимит по КАЖДОЙ позиции целевого портфеля (включая держимые без
  сделок) проверяет движок — `POSITION_VALUE_LIMIT`; вручную по строкам не
  пересчитывать.
- Докупаемые позиции портфеля → `--include-held` в report (бейдж
  `held (N%)`); без флага скринер их прячет (002.2).
- Бумаги для квалифицированных инвесторов → `--include-ku` (report и
  plan, 031.1; по умолчанию исключены — без флага canonical default).
- Окно реинвеста → `--min-maturity` в report (напр., горизонт
  `redemptions_6m`; в plan — 031.1). `--max-maturity` без нужды не
  ставить: верхней границы нет, сужение окна = ограничение доходности
  (002.3) — осознанный второй прогон.
- Дефолты report: `--max-risk 1`, `--max-listlevel 2`, `--max-ytm 35`
  (аномалии = ВДО/дистресс), `--screener-limit 10`, `--redemptions-months 6`.
  Те же значения — canonical defaults политики покупки plan
  (fitter/eligibility); с 031.1 их можно переопределить флагами
  candidate-policy plan (`--max-risk/--max-listlevel/--max-ytm/
  --include-ku/--min-maturity/--max-maturity`); без флагов — defaults.

## Контракты ответов

### report (`schema_version=2`)

Блоки:

- `meta`: `generated_at`, `catalog_updated_at`, `positions_updated_at`,
  `market_prices_updated_at`, `status` (`OK`/`WARNING`/`CRITICAL`),
  `gate_status`, `is_fresh`, `criticals[]`, `warnings[]`.
- `portfolio`: `total_value`, `securities_value_rub` (= `total_value`),
  `cash_available_rub`, `investable_total_rub`, `cash_updated_at`,
  `positions[]`: `isin, name, ticker, qty, price, value, share_pct,
  ytm_pct, ytm_reason, coupon_pct, freq, coupon_kind (fix|float),
  risk_level, list_level, maturity, offer, amort, ku, issuer, issuer_pct,
  held_badge, credit_rating, contract_version, rating_status, blockers[],
  warnings[]` (P1.5 issue #5, только добавление полей).
  - `share_pct` — доля выпуска, рядом `issuer_pct` — доля эмитента; у
    кандидатов скринера `share_pct = 0`.
  - `credit_rating` — последний рейтинг из `rating_history`; `null` =
    строки рейтинга нет (не «недоступен технически»).
  - `cash_available_rub = null` означает «cash неизвестен» (sync ещё не
    выполнялся), а НЕ 0 ₽: null нельзя считать нулём и нельзя вычитать из
    итогов. `0` — реальный ноль. `cash_updated_at` старая → cash устарел
    (ошибка последнего sync не обнуляла снапшот) — оговори, не выдумывай
    ноль; продвижение метки после canonical sync — proof для current-cash
    guard (029-T14).
- `concentrations`: `issuers` {`limit_pct: 15`, `top[]`, `violations[]`},
  `risk_mix[]`, `freq_mix[]`.
- `screener`: `filters` (эхо применённых), `candidates_total`, `excluded[]`
  (воронка), `fixed_matched`/`floater_matched`, `fixed[]`, `floater[]`
  (строки ядра + `credit_rating`), `issuers[]` (`issuer`, `best_ytm_pct`,
  `series[]`) — агрегат для выбора между сериями одного эмитента.
- `redemptions_6m`: `period_months`, `from`, `to`, `total`, `events[]`
  (`isin, name, ticker, issuer, type (maturity|offer), date, qty, value`).

Единицы: деньги — рубли (`price` — рубли за бумагу, не % от номинала);
`*_pct` — проценты; `qty` — штуки. YTM в процентах годовых, у флоатеров
`ytm_pct = NULL` (`ytm_reason = floating_coupon`). Справочник единиц —
`doc/units_conventions.md` в репозитории fin_bonds.

### plan (envelope + блоки по `--view`)

Envelope (в каждом view): `artifact`, `plan_schema_version`,
`context_fingerprint`, `as_of`, `data_gate` {`status`, `gate_status`,
`is_fresh`, `criticals[]`, `warnings[]`}, `valid`, `feasible` (`true` /
`false` / `null` — не определена), `price_consistent`, `diagnostics[]`
(`scope`: `intents` / `context` / `plan`; коды — canonical, синонимов нет:
`INVALID_TARGET_VALUE`, `INVALID_INTENT`, `DUPLICATE_TARGET`,
`CONFLICTING_INTENT`, `CONTEXT_NOT_FOUND/STALE/VERSION_MISMATCH/
FINGERPRINT_MISMATCH`, `UNKNOWN_ISIN`, `BUY_NOT_ELIGIBLE` (+`reasons[]`),
`PRICE_UNAVAILABLE`, `CONTEXT_PRICE_MISMATCH`, `NEGATIVE_QTY_AFTER`,
`NO_COMPANY_LINK`).

Блоки: `summary` (`securities_before/after`, `cash_before`, `sales_value`,
`purchases_value`, `cash_after_estimate`, `investable_total_before/after`;
поток: `coupon_month_before/after`, `coupon_year_before/after`,
`delta_month`, `delta_year`), `budget_check` (`known`, `passed`; `passed =
null` при unknown cash — неизвестность не маскируется под PASS),
`targets` (requested vs fitted, статус, fit/buy reasons, planning
price/basis), `trades` (canonical ноги), `positions_after` (целевой
портфель), `violations` (constraint_checks: `POSITION_VALUE_LIMIT`,
`COUPON_FREQUENCY_*`, `SOVEREIGN_NOT_ALLOWED`, `CREDIT_RATING_*`,
`ISSUER_LIMIT`, `INSUFFICIENT_CASH_ESTIMATE`). `full` — всё + context/
constraints/filters/intents observability. Деньги planning-слоя —
decimal-строки; блоки движка — его canonical релей.

### compare (030.9)

Envelope: `compare_schema_version`, `context_id` (031.3: opaque id
контекста, на котором считаны оба варианта, — эхо `--context-id`; `null`
при свежей сборке и до построения контекста), `context_fingerprint`,
`as_of`, `data_gate`, `comparable`, `diagnostics[]` (в т.ч.
`CONTEXT_MISMATCH` при разных fingerprint). `delta` — вариант − baseline
по canonical метрикам (decimal-строки; `null` при not comparable);
`variants[]` — canonical блоки plan (summary/budget_check/targets/
trades/positions_after/violations) каждого варианта.

### analysis-snapshot (030.10, через adapter 030.11)

Полный snapshot НЕ попадает в prompt: adapter сохраняет payload в
приватный artifact (`0600`) и печатает metadata/preview envelope:
`context` (context_id/fingerprint/as_of/data_gate/TTL), `analysis`
(статус `--requires`: `OK` или `ANALYSIS_DATA_UNAVAILABLE` + `missing[]`
с canonical reason), `capabilities` (`read_only: true`,
`creates_trades_or_scenarios: false`, `available_datasets`,
`buy_eligibility_scope`), `limitations` (недоступные datasets:
`exact_coupon_schedule`, `amortization_schedule`, `settlement_costs`,
`per_instrument_duration`, ...), counts + `*_preview` (первые 5 строк),
`artifact_file` `{path, sha256, usage}`. Sandbox/python-kernel читает
файл артефакта напрямую; `--requires` проверяет запрошенные dataset-коды
ДО анализа — не хватает данных, ядро не синтезирует.

## Правила (обязательные)

- **ISIN целевой покупки никогда не пишется по памяти (анти-P-D,
  обязательный чекпоинт).** ISIN копируется из canonical output
  (report screener / issuers[] / snapshot universe); перед записью в
  intent сверь пару «имя ↔ ISIN» с выводом строка-в-строку. Ловушка
  (реальный кейс 2026-09-29): 4 ISIN одного семейства, серии одного
  эмитента отличаются одной буквой:
  - `RU000A10XYA1` — Эмитент А 001Р-02 (в портфеле, ежемесячная);
  - `RU000A10XYA2` — Эмитент А 001Р-04 (цель сессии);
  - `RU000A10XYB1` — Эмитент Б 002Р-03 (в портфеле, флоатер);
  - `RU000A10XYB2` — Эмитент Б 002Р-02 (цель сессии).

  По памяти были записаны неверные ISIN (`AHE5`/`B8P3` вместо целей).
  Каноническая защита — движок: ISIN вне canonical output →
  `UNKNOWN_ISIN`; но основная дисциплина — копировать ISIN из вывода, а
  не «изобретать».
- **YTM флоатера валидна только на текущий купон.** У флоатеров
  `ytm_pct = NULL` (`ytm_reason = floating_coupon`), `coupon_pct` —
  текущая расчётная ставка; не показывай её как YTM и не сравнивай
  напрямую с YTM фиксов как «доходность к погашению». В fallback тоже
  (imputation policy).
- **Кредитный рейтинг — из canonical output, не из памяти (023/024).**
  Строгий фильтр включается только флагом `--min-credit-rating`: без него
  бумаги без рейтинга видны с `credit_rating = null`. `risk_level` (1-5,
  Т-Банк) — ОТДЕЛЬНЫЙ фильтр и рейтингом не является: не выдавать его за
  кредитное качество, не выводить из него буквы рейтинга и не считать
  пару «risk_level + list_level» строгим рейтинговым фильтром —
  прокси-оговорка допустима только для бумаг с пустым `credit_rating`,
  когда порог не ставился.
- **Приказы не ставить.** Результат — текстовый план с пометкой «требует
  подтверждения человеком»; никаких заявок и исполняемых команд к бирже.
- **Ноль ручной арифметики.** qty, sizing, netting, деньги, доли
  эмитентов, дельты купонного потока, частотный микс, проверка лимитов,
  сравнение сценариев — только движок; агент интерпретирует готовые числа
  (кейс 002: суммарную долю РЕСО 14.51% по двум ногам агент считал
  вручную — P6). Исключение — controlled sandbox fallback в его границах
  (только read-only аналитика, всегда с provenance).

## Legacy (в standard path не использовать)

- `rebalance-report --scenario` (+ heredoc/scp для scenario JSON) и
  `--csv-out` — прежний standard path (005/024/029): заменён `plan` (030.8)
  и исключены из whitelist adapter'а (030.11). Команда остаётся в core для
  совместимости, скилл её не вызывает; сценарных файлов, sed-правок и
  ручного подбора qty больше нет.
- CSV целевого портфеля: в adapter whitelist `--csv-out` не входит — CSV
  materialization вне standard path. Если пользователю нужен CSV-файл —
  зафиксируй promote gap (кандидат typed capability `plan --csv-out`),
  не воскрешай legacy путь.
- `scripts/simulate_swap.sql` и `scripts/scenario_summary.sql` — SQL
  прежнего SOP (001/002); логика перенесена в движок. Оставлены для
  psql-фолбэка и разбора истории; в новых сессиях не вызывать.

## Pitfalls

- Узкое окно погашения = упущенная доходность (002.3): топ-фиксы риск-1 на
  длине 2030+ дают на 5–7 п.п. больше коротких. Первый прогон — без
  `--max-maturity`; сужение под предпочтения пользователя — отдельный
  второй прогон.
- `held_badge` в скринере — текущая доля ISIN в портфеле: докупка с запасом
  до лимита тоже кандидат, дефолтное исключение портфеля её прячет —
  `--include-held` в report обязателен (002.2, кейс ГТЛК 002P-04).
- Кандидат, проходящий нарушенный лимит, может ломать другой (например,
  добор надёжной бумаги с крупным номиналом ломает концентрацию эмитента) —
  финальный арбитр по концентрациям и бюджету: canonical validation плана
  (`violations[]`), не парная проверка на глаз.
- Не предлагай продажи, когда задача решается притоками (купоны/погашения
  в горизонте `redemptions_6m`), — правило действует только в
  scope=CASH_DEPLOYMENT / reinvestment. В FULL_REBALANCE оно НЕ действует,
  если existing position нарушает canonical final-target constraint
  (нарушитель продаётся: categorical → `--sell-all`, quantitative →
  `--target-value` до допустимого уровня); candidate-only mismatch продажу
  сам по себе не требует.
- Вне торговой сессии MOEX (10:00–18:45 МСК) стаканы заявок пустые:
  отсутствие ликвидности ночью — норма, а не ошибка БД; не перезапускай
  пайплайн и не помечай бумаги неликвидными.
- Лимиты приватны: не вставляй значения из пользовательского файла в
  публичные выводы (web-поиск, репозитории). ISIN/состав портфеля наружу
  не публиковать (включая snapshot-артефакт).
- Два `plan`-вызова в разное время без `--context-id` — разные срезы
  данных: сравнение вручную/в sandbox запрещено, только `compare`.
- Canonical сценарий context-handle (032.1): `analysis-snapshot` минтит
  `context_id` → `plan`/`compare --context-id` считают по тому же frozen
  срезу (`context_fingerprint` совпадает, БД не перечитывается).
  Произвольный id без handle от snapshot'а → `CONTEXT_NOT_FOUND` —
  корректный machine-readable отказ core (JSON с exit 0 через adapter),
  не ошибка вызова; возьми id из ответа `analysis-snapshot` или планируй
  без `--context-id` (свежая сборка).

## Verification

- Standard path — только adapter: ни одного прямого вызова
  `rebalance-report/rebalance-plan/rebalance-compare/rebalance-analysis-snapshot`
  по ssh; ни одного heredoc/scp/sed/scenario-JSON/Python-сниппета в
  standard path; ручного `sum/price → qty`, подбора qty после violation,
  ручного netting — нет (все числа из canonical JSON).
- Intent нормализован до ШАГА 1 в `scope` / `sales_allowed` /
  `require_current_cash` по приоритету «явная инструкция > однозначный
  синоним > default-семантика scope»; interpretation block показан ДО
  выполнения для всех режимов; «NO_SELL» как самостоятельный mode не
  использовался.
- ШАГ 1: `meta.is_fresh == true` (иначе обновление данных и повтор);
  капитал показан блоком из `portfolio` (при `null` — «Свободные деньги:
  неизвестно», не 0).
- Выбор целей (ШАГ 2): каждый ISIN присутствует в canonical output этого
  цикла (report/snapshot); ни одного ISIN по памяти; пары «имя ↔ ISIN»
  сверены; priority `--target-value` отражает явный выбор LLM и объясним.
- ШАГ 3: diagnostics прочитаны; `BUY_NOT_ELIGIBLE` → выбор другой цели, а
  не обход gate; после violations правились intents, а не числа; итераций
  ≤ ~3, исчерпание → blockers.
- Candidate-policy: политика, использованная при выборе целей в отчёте,
  передана во `plan` флагами candidate-policy (а не только
  report-фильтрами; 031.1); set-частота выражена `--freq-in`, а не
  диапазоном (031.2); политика plan-вызова соответствует политике
  report-прогона.
- ШАГ 4 (если compare): оба варианта на одном `context_fingerprint`
  (общий `--context-id` или одна сборка); `comparable == true`;
  `delta` процитирован из ответа, не пересчитан.
- ШАГ 5: data_gate и canonical validation проверены ОТДЕЛЬНО;
  `feasible=true` при stale/critical data не подан как «можно
  рекомендовать»; `feasible == null` оговорён явно (не «ок» и не
  «отказ»); `budget_check.known/passed` проверены; сценарий с
  `feasible == false` не подан как готовый допустимый target.
- FULL_REBALANCE назван завершённым только при `data_gate.is_fresh==true`,
  `valid==true`, `feasible==true`, `budget_check.known==true`,
  `budget_check.passed==true`, `violations==[]` и выполненной deployment
  completeness; feasible all-cash прогон не подан как завершённая полная
  ребалансировка (тест 33.5): покупки продолжены в рамках feasible
  capacity либо указана объективная причина residual cash; explicit
  «оставь N ₽ в cash» учтён как приоритет пользователя.
- Deployment completeness (тест 33.3.3): residual cash с обоснованной
  причиной (settlement buffer / lot granularity / исчерпанная ёмкость /
  явный cash reserve) не трактован как автоматическая ошибка; покупка не
  добавлена только ради сведения `cash_after_estimate` к 0; в ответе явно
  сказано, что `cash_after_estimate` не учитывает НКД и комиссии.
- CASH_DEPLOYMENT-план назван провалидированным только по acceptance
  contract (раздел «CASH_DEPLOYMENT»); `feasible=false` (если есть)
  объяснён legacy investment violations, structural `valid=false` — hard
  blocker.
- Screener-limit политика (тест 33.1): provenance различен
  (USER_EXPLICIT / SKILL_POLICY / CANONICAL_DEFAULT); FULL_REBALANCE
  стартовал с `--screener-limit 30` (если пользователь не задал свой);
  «нет кандидатов» не объявлено из-за исчерпания top-N (для ширины пула —
  snapshot universe); весь market catalog без необходимости не
  экспортировался.
- Current-cash guard (тест 33.3.2): при `require_current_cash=true` —
  initial report зафиксировал `old_cash_updated_at`; выполнен canonical
  `sync-portfolio --source=tbank` на хосте; повторный report с тем же
  флаг-набором; proof — строгое продвижение `cash_updated_at` (Case C:
  null → non-null); бюджет/intents/план — только из post-refresh данных,
  новый `cash_updated_at` показан; exit code sync не принят за proof
  (Case B: неизменная метка — blocker, ad-hoc broker API и ручная
  подмена cash не выполнялись); продвижение метки не подано как «cash и
  positions обновлены одним snapshot» (Case E).
- Fallback (если применялся): вопрос сначала попробован на уровнях 1–3
  лестницы; sandbox без DB/API/secrets/сети/записи; input — только
  canonical artifact/plan result; imputation/approximation явно названа
  (не молча); canonical вердикты (qty/legs/budget/issuer/valid/feasible)
  fallback'ом не подменялись; provenance-блок присутствует
  (`analysis_mode: python_fallback`, reason, context_id, fingerprint,
  approximations, `canonical_plan_required: true`); proposed intents
  возвращены через `plan --context-id`, custom check — на том же
  `context_fingerprint`, в ответе различены `canonical_checks` и
  `custom_analysis_checks`; повторяющийся паттерн зафиксирован как
  promote gap (без создания новой команды).
- `scope` / `sales_allowed` / `require_current_cash` существуют только на
  уровне скилла: в Python-код, PostgreSQL и Google Sheet не добавлялись.
- Приватность: snapshot-артефакт и состав портфеля не отправлены наружу;
  формальные условия запроса переданы флагами (не ручной фильтрацией
  строк); в ответе нет бумаг вне фильтров; «высокий рейтинг»,
  «максимум раз в квартал», «подлиннее», «ликвидные» и другие AMBIGUOUS
  не превращены в скрытые численные параметры; «не ОФЗ» сопровождено
  оговоркой.
- Итоговые рекомендации не содержат: ОФЗ/суверенных (при запросе «не
  ОФЗ»), бумаг риск >1 без явной оговорки, YTM >35%, бумаг не из
  canonical output. В плане нет приказов/заявок.
