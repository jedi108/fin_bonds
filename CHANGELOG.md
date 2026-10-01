# Changelog

Все заметные изменения проекта. Стиль — по коммитам: `feat(scope): ... (task NNN)`.

## 2026-10-01

- feat(planning): domain intents — typed DTO + normalization (030.5) — новый
  `src/services/planning_intents.py`: граница «LLM/CLI приносит предметные
  intents, код нормализует и валидирует» (правило 030 №7). Минимальные
  intents: `sell_all(ISIN)` (итоговая позиция = 0) и
  `target_value(ISIN, target_value_rub, priority)` — ВЕРХНЯЯ граница желаемой
  итоговой стоимости позиции после плана: fitter (030.7) возьмёт максимальный
  допустимый final qty с `final_position_value <= target_value_rub` (точного
  равенства нет — целые бумаги + hard limits; requested/fitted возвращает
  planner, поэтому отдельный `reduce_to_value` для MVP не нужен). DTO frozen:
  `SellAllIntent`, `TargetValueIntent` (деньги — Decimal; int/str/float
  канонизируются на границе, bool/NaN/Inf/неположительные отклоняются),
  `PortfolioConstraints` (freq_min/freq_max/freq_in/min_credit_rating/
  exclude_sovereign/max_position_value_rub — пассивный planning-DTO,
  canonical валидатор один, движок 030.2: `to_scenario_constraints()` —
  relay, Decimal→float только на границе float-домена движка) и
  `CandidateFilters` (max_risk/max_list_level/max_ytm_pct/include_ku/
  min_maturity/max_maturity — контракт policy eligibility покупки для 030.6;
  дефолтов у полей нет намеренно). Один high-level target на ISIN:
  `normalize_intents()` отвергает повторный target того же вида
  (`DUPLICATE_TARGET`) и разных видов (`CONFLICTING_INTENT`) — last-write-wins
  запрещён; диагностики собираются все сразу, при любой из них intents пусты
  (частичное применение запрещено); `INVALID_INTENT` — прочие проблемы формы
  (неизвестный type, пустой/нестроковый isin, неположительные/нечисловые
  деньги, отсутствующий/нецелый priority). Priority — явное обязательное поле
  (меньше = выше allocation priority, ранг; молчаливого default нет; CLI-маппинг
  порядка `--target-value` — 030.8); normalized intents эхо-возвращаются 1:1 в
  порядке входа, `allocation_order()` отдаёт target_value-intents по рангу для
  fitter'а (sell_all в allocation не участвует). Canonical источники без
  дублирования: defaults candidate-фильтров — `default_candidate_filters()`
  из `RebalanceReportUseCase.default_args()` (тот же parser, что CLI отчёта;
  единственный источник — `_SCREENER_FILTER_SPECS`, 029-T06/030.1), лимит
  концентрации эмитента — реэкспорт canonical `ISSUER_CONCENTRATION_LIMIT_PCT`
  движка (030.2), нового policy flag и второй hardcoded копии нет.
  Нормализация не придумывает продажи (P0.3): результат — 1:1 эхо входа;
  auto-fit права (уменьшение BUY, explicit sell_all/target_value) — зона 030.7.
  Legacy `rebalance-report --scenario` не тронут (низкоуровневая агрегация
  нескольких qty_delta осталась). Тесты: tests/test_planning_intents.py (46,
  без БД): immutability DTO, duplicate/conflicting без last-write-wins,
  эхо с priority + стабильный allocation order, identity defaults с
  canonical report args, issuer limit = объект движка, Decimal-канонизация
  денег, полный список диагностик за один вызов, relay в ScenarioConstraints.
  Полный набор: 579 collected / 336 passed / 243 skipped / 0 failed.

- feat(planning): ephemeral context store — core-owned context handle (030.4) —
  новый `src/services/context_store.py` (`EphemeralContextStore`): workflow
  «analysis-snapshot → sandbox Python → plan --context-id» опирается на
  core-owned immutable артефакт, а не на данные, вернувшиеся из sandbox
  (обычный SHA/fingerprint не защищает от «код изменил и данные, и сам hash»;
  авторитет — только артефакт, созданный самим core). Хранение двухуровневое:
  process-local dict (сравнение в одном процессе возвращает тот же
  Python-объект) + ephemeral JSON-артефакт на host
  (`data/planning_contexts/`: файлы 0600, каталог 0700, запись write-once
  через O_EXCL+link — частичное обновление context после создания
  невозможно; persistent snapshot DB не создаётся). `context_id` —
  secrets.token_hex(16), строго opaque: формат 32 hex валидируется, любое
  отклонение (включая path traversal) — CONTEXT_NOT_FOUND; в filesystem path
  id не попадает — имя файла sha256(scope:id). Scoping: артефакт привязан к
  scope инстанса приложения (sha256 корня проекта + DSN источника данных;
  сам DSN/tokens в артефакт не попадают) — чужой profile с известным id
  получает ровно тот же CONTEXT_NOT_FOUND (существование чужого context не
  раскрывается, файл чужого scope даже не разрешается по имени). Порядок
  проверок при загрузке: формат id → файл → envelope (store schema version)
  → scope → версии → TTL → fingerprint; БД store не читает вовсе —
  `plan --context-id` не перечитывает и не подмешивает свежие market/cash/
  portfolio rows (неизменность legacy-путей покрыта существующими тестами).
  Ошибки machine-readable (`.code`): CONTEXT_NOT_FOUND (неизвестен/удалён/
  невалиден/чужой scope), CONTEXT_STALE (истёк TTL — отдельная ось свежести,
  не data_gate и не per-instrument eligibility: stale-срез внутри TTL
  загружается с исходным gate и ценами, TTL eligibility reasons не
  переписывает; истёкший артефакт удаляется), CONTEXT_VERSION_MISMATCH
  (context_schema_version / derived calculation_policy_version / формат
  envelope несовместимы — старый context не пересчитывается новой политикой
  молча), CONTEXT_FINGERPRINT_MISMATCH (corruption core-owned артефакта:
  fingerprint пересчитывается при загрузке; консистентную подмену
  «данные+hash» отсекает граница владения файлом, а не hash). Versioned
  payload: типизированная сериализация расчётного payload (Decimal/date/
  datetime — теги, float — нативный JSON round-trip) восстанавливает
  PlanningContext без потери типов; `calculation_policy_version` — не
  «магическая строка»: `derive_calculation_policy_version()` — digest
  канонических policy-входов (canonical issuer limit 15% движка 030.2, raw
  universe policy 030.3, schema/policy версии, rating-scale семантика
  config.yaml) — изменение любого входа делает сохранённые context'ы
  несовместимыми (fail-loud), regression-тест дополнительно проверяет
  покрытие каждого входа. TTL по умолчанию 6h (bounded; cleanup истёкших и
  нечитаемых артефактов — при каждом save и явно `cleanup()`). CLI/factory
  проводка — 030.8/030.10, transport/lifecycle для LLM — 030.11 (не
  предугадываются). Тесты: tests/test_context_store.py (51, синтетические
  данные, артефакты в tmp_path): round-trip с доменными типами и равным
  fingerprint, in-process same object, чужой scope = NOT_FOUND с тем же
  ответом, sandbox-правки копии payload не меняют авторитет, corruption
  (данные/fingerprint/обрезка/форма envelope) → FINGERPRINT_MISMATCH,
  unknown/expired → NOT_FOUND/STALE, TTL отдельно от data gate, cleanup,
  schema/policy/envelope версии → VERSION_MISMATCH, derived policy digest
  покрывает каждый вход, загрузка без единого чтения БД, path traversal
  отвергается, права 0600/0700, secrets в артефакте отсутствуют, write-once.

- feat(planning): PlanningContext — builder + fingerprint (030.3) — новый
  `src/services/planning_context.py`: immutable срез данных один раз на
  plan/compare-run поверх canonical engine 030.2. Согласованное чтение —
  read-only `REPEATABLE READ` транзакция (`PortfolioStorage
  .snapshot_transaction`; READ COMMITTED гарантией не является); единый
  frozen `as_of` — транзакционный timestamp (`get_transaction_timestamp`:
  CURRENT_TIMESTAMP/CURRENT_DATE зафиксированы на старте транзакции, так что
  SQL-правила buy-path — maturity-границы и окно 24h market-price freshness —
  считаются на том же as_of; wall-clock Python в сборке не участвует).
  Скрытый второй clock устранён на plan/compare path: временный YTM fallback
  (`_enrich_bonds_with_ytm` → cashflow-решатель) получает `as_of_date`
  (новые опциональные `valuation_date`/`as_of_date` в calculate_ytm/engine —
  без аргумента прежнее wall-clock поведение legacy report не изменено);
  `get_db_freshness` получил опциональный `now` (окно 24h от as_of);
  `RebalanceEngine` — `planning_price()` (та же математика
  `_make_scenario_state`, копий нет) и `as_of_date` в `evaluate_scenario`.
  Контекст несёт: held/universe rows (canonical raw universe ДО
  variant-specific screening — та же широкая buy-выборка, что у legacy
  screener, `_SCREENER_*` константы переиспользованы), cash snapshot (unknown
  ≠ 0), effective prices {price, basis} на каждый ISIN (= eligibility,
  зафиксированная в контексте; replay не пересчитывает её по wall-clock),
  canonical data gate `DataGateState` (тот же `classify_freshness` — второй
  набор thresholds не создан; feasible и data gate — разные оси), metadata
  (generated_at/as_of/positions_updated_at/cash_updated_at/
  context_schema_version/calculation_policy_version). `context_fingerprint` —
  детерминированный sha256 канонической сериализации исходных calculation
  data + as_of + версий (Decimal → decimal string, datetime → ISO/UTC,
  коллекции строк сортированы по ISIN; generated_at, сам fingerprint,
  intents/constraints варианта исключены). context_id/TTL/store — 030.4,
  eligibility-гейт — 030.6, intents — 030.5 (не предугадываются). Новые
  тесты tests/test_planning_context.py (28, без тестовой БД): frozen as_of,
  все чтения внутри snapshot-транзакции, один as_of в SQL/Python-правилах,
  stale DB → stale gate, feasible=true не маскирует stale gate, replay не
  меняет eligibility, fingerprint (presentation order / market data /
  as_of / intents), Decimal/datetime canonicalization, raw universe для
  двух вариантов, immutability. `make test`: 482 collected / 239 passed /
  243 skipped / 0 failed (baseline 030.2: 454/211/243/0 — legacy числа
  сохранены, +28 новых).

- refactor(rebalance): canonical calculation/validation engine извлечён из
  RebalanceReportUseCase (030.2) — новый `src/services/rebalance_engine.py::
  RebalanceEngine`, единственный расчётчик целевого портфеля (правило 030:
  второго калькулятора нет): `evaluate_scenario(trades, constraints)` одним
  вызовом возвращает valid/feasible/errors/trades/summary/budget_check/
  constraint_checks/freq_mix_before|after/issuer_limits/positions_after —
  Python API без shell-парсинга для планировщика (030.7) и adapter'ов
  (030.8/030.11). Ограничения — плоский `ScenarioConstraints` (без argparse/
  intents в engine); чтение scenario.json осталось в use case (CLI-контракт).
  Planning price (030.2): для КАЖДОГО ISIN вселенной движок отдаёт цену
  расчёта и её basis (`planning_prices`: held_valuation — каноническая оценка
  view на штуку; market_price; nominal_fallback — legacy фолбэк теперь ЯВЕН;
  matured_zero — P1-guard) — фундамент правила «одна цена на ISIN внутри
  plan». В JSON legacy `rebalance-report --scenario` planning_prices не
  публикуется — контракт команды не изменён (проверено diff-прогоном
  сценария и ядра отчёта до/после на одной тестовой БД: вывод идентичен
  байт-в-байт; регресс 209 rebalance/export тестов зелёный). В engine
  переехали: scenario-математика и валидатор формальных ограничений (024),
  общая арифметика `issuer_shares`/`value_mix`/`bond_to_json`, YTM-enrichment
  (единый путь через CalculateYtmUseCase), контрактные константы
  SCENARIO_*/CONSTRAINT_*/BOND_ROW_KEYS и `ISSUER_CONCENTRATION_LIMIT_PCT`
  (15.0; из rebalance_report реэкспортируются — старые импорты работают).
  Новые тесты tests/test_rebalance_engine.py (11): price+basis на каждый ISIN
  (включая nominal_fallback и matured), одна цена calculator==validator
  (POSITION_VALUE_LIMIT.actual == qty_after * planning_price, консистентность
  trades/positions/лимитов), полный результат одним вызовом, блок scenario
  == результат engine, ключи legacy JSON в прежнем порядке.

- docs(preflight): зафиксированы фактические якоря post-029 для project 030
  (030.1) — код не менялся (подзадача read-only): baseline `make test` зелёный
  (443 collected / 211 passed / 232 skipped / 0 failed); подтверждены CLI
  (argparse в `main.py` + `UseCaseFactory.get_use_case_map`), canonical
  `RebalanceReportUseCase` (`SCHEMA_VERSION=2`, scenario-контракт: valid /
  feasible / errors / summary / budget_check / issuer_limits / constraint_checks
  / positions_after), `ISSUER_CONCENTRATION_LIMIT_PCT = 15.0`, freshness gate
  `classify_freshness` → `meta.data_freshness.{gate_status,is_fresh,criticals,
  warnings}`, NUMERIC-миграции (005), отсутствие canonical
  `lot_size`/`quantity_step` в `bonds_catalog`, structural gates buy-path
  (`get_bonds_yield_table(mode='buy')`: RUB, is_trade_available true-or-null,
  не perpetual, maturity >= CURRENT_DATE, свежая market_price > 0 в окне 24h,
  coupon COALESCE, широкие risk/listlevel/ytm), permissive
  `get_scenario_universe_rows`, valuation view `v_portfolio_positions_valuation`
  (valuation_source: broker_value/broker_price/market_price/nominal_fallback/zero),
  eff_price (matured=0 / held=value_rub/qty / buy=market_price→nominal fallback),
  единственный источник defaults 12 screener-фильтров `_SCREENER_FILTER_SPECS`
  (029-T06), скрытый второй clock `calculate_bond_cashflow_metrics(
  valuation_date=date.today())` и `CURRENT_DATE`/`CURRENT_TIMESTAMP` в buy SQL.
  Детали — в файле задачи 030.1 (workspace); расхождений имён/путей с ожиданиями
  030 нет.
- test(export): equivalence-тесты rebalance-report vs sync-chatgpt-portfolio
  на одном snapshot (029-T09) — tests/test_report_export_equivalence.py
  прогоняет оба вызова с каноническим filter set задачи 029 (--freq-min 4
  --freq-max 12 --min-credit-rating AA- --exclude-sovereign --screener-limit 30;
  у report ещё --include-held, у sync include_held=true — product-default
  exporter) по одной тестовой БД без data refresh: CANDIDATES совпадают с
  `report["screener"]` по набору ISIN и порядку/rank отдельно FIX и FLOAT,
  `candidate_meta.filters` == `report["screener"]["filters"]`, каждый столбец
  CANDIDATES равен полю canonical screener-строки и count 1:1 — exporter не
  делает post-filter transformations. Тест изолирован от DB-time границ:
  ytm + ytm_updated_at сеются явно (fallback с valuation_date=date.today()
  не участвует), цены заведомо свежие с запасом до 24h cutoff, maturity
  заведомо в будущем (CURRENT_DATE bound не участвует). Отдельный
  regression-тест фиксирует caveat: `ytm_updated_at IS NULL` считает YTM
  на лету с date.today() — «same DB rows» без same valuation date не
  гарантирует идентичный ranking.
- feat(export): один canonical cash read + candidate metadata + stdout
  контракт (029-T08) — cash-поля CONTROL (`cash_available_rub` /
  `cash_updated_at`) копируются из `report["portfolio"]` того же
  build_report run: второго `db.get_available_cash_rub()` после
  report_builder в exporter больше нет (убрано окно рассинхронизации
  CANDIDATES/CONTROL между cash snapshot'ами). Арифметика Sheet сохранена:
  `portfolio_value_rub` — сумма transport PORTFOLIO rows,
  `investable_total_rub` = portfolio_value_rub + опубликованный cash
  (blind copy `report["portfolio"]["investable_total_rub"]` не делается —
  transport schema не унифицируется с `report["portfolio"]`). `candidate_meta
  .filters` — только `report["screener"]["filters"]` (все 13 canonical
  полей, без ручной реконструкции; provenance sync не угадывает). Stdout
  успешного sync расширен до контракта для skill (T15): Свободные деньги
  (значение ₽ | неизвестно), Cash snapshot (`cash_updated_at` | unknown),
  Candidate filters (canonical serialization `report["screener"]["filters"]`).
  Тесты: cash CONTROL из `report["portfolio"]` + guard на отсутствие второго
  cash read, candidate_meta.filters == canonical output (13 полей), stdout
  known/unknown cash + canonical filters serialization.
- feat(export): data flow args execute → candidate_report_args (029-T07) —
  CLI args sync-chatgpt-portfolio больше не теряются до candidate report:
  `execute(args)` передаёт весь распарсенный Namespace в
  `_build_payload(candidate_overrides=...)`, тот же Namespace идёт в
  `candidate_report_args(overrides)` (backward-compatible Optional-сигнатура:
  no-args вызов сохраняет прежний контракт
  `vars(candidate_report_args()) == vars(RebalanceReportUseCase.default_args())
  + include_held=True`). Итоговые args = canonical default_args() +
  include_held=True + только реально присутствующие candidate-filter attrs
  (по факту атрибута, без materialized defaults) → canonical validation →
  `RebalanceReportUseCase.build_report`; exporter rows сам не фильтрует,
  CANDIDATES строится только из `report["screener"]`, READY protocol не
  меняется. Тесты: execute-level forwarding (spy builder получает ровно
  `candidate_report_args(overrides)`, no-args — прежние `candidate_report_args()`)
  и сквозной прогон CLI-фильтра --min-credit-rating до canonical build_report
  через execute (CANDIDATES + CONTROL candidate_meta).
- feat(cli): shared screener filter arguments helper (029-T06) — 12
  candidate/screener фильтров rebalance-report (--freq-min/--freq-max/
  --freq-in/--min-credit-rating/--exclude-sovereign/--include-ku/--max-risk/
  --max-listlevel/--max-ytm/--min-maturity/--max-maturity/--screener-limit)
  вынесены в переиспользуемый public helper
  `add_screener_filter_arguments(parser, *, defaults=True)`: единственный
  источник parsing semantics и canonical defaults (таблица определений +
  выводимый из неё `SCREENER_FILTER_DESTS`; ручных копий default
  max-risk/max-listlevel/max-ytm/screener-limit больше нет). CLI
  rebalance-report не изменился (setup_parser использует helper с
  defaults=True, default_args() продолжает materialize defaults тем же
  parser contract). sync-chatgpt-portfolio теперь принимает все 12
  candidate-filter args через тот же helper с defaults=False
  (argparse.SUPPRESS): в sync Namespace materialize второго набора defaults
  нет, explicit override переносится в канонические args кандидатного
  отчёта по факту присутствия атрибута (`candidate_report_args(overrides)`,
  hasattr), forwarded фильтры проходят canonical validation build_report.
  include_held=true остаётся product-default экспорта; флагов
  --include-held/--exclude-held у exporter нет; scenario/csv/
  max-position-value/redemptions-специфика остаётся у rebalance-report.
  Тесты: tests/test_sync_chatgpt_portfolio.py (TestScreenerCliArgsForwarding,
  TestScreenerCliArgsDriftGuard).
- feat(storage): atomic cash snapshot publish (029-T03) — upsert актуальных
  cash-строк и prune счетов вне configured/selected set выполняются в ОДНОЙ
  DB transaction: новый `PortfolioStorage.replace_cash_balances_snapshot()`
  (BEGIN -> upsert всех balances -> prune rows вне selected_account_ids ->
  COMMIT, при любой ошибке ROLLBACK). Прежняя пара `upsert_cash_balances` +
  `delete_cash_accounts_not_in` коммитилась раздельно: падение prune после
  успешного upsert оставляло в БД часть нового snapshot с продвинутым
  `cash_updated_at`, хотя sync возвращал failure (mixed snapshot, ошибочно
  принимаемый skill за свежий). SQL-тела обоих операций вынесены в общие
  helper'ы — публичная семантика отдельных методов не изменилась;
  `_sync_tbank_cash()` публикует cash только атомарным методом. PostgreSQL
  `now()` стабилен внутри транзакции: все строки успешного snapshot получают
  один transaction timestamp; вместе с synthetic zero RUB row (029-T02)
  продвижение агрегированного `cash_updated_at` становится proof полного
  TBank RUB snapshot (current-cash guard задачи T14). Тесты:
  tests/test_cash_atomic_publish.py (unit fake-connection + DB-регрессии
  rollback/один timestamp), tests/test_tbank_rub_zero_row.py (Case D поверх
  атомарного publish), tests/test_tbank_account_preflight.py.
- feat(report): canonical validation внутри программного build_report
  (029-T05) — programmatic consumers (экспортёр ChatGPT) больше не обходят
  `_validate_args`: validation выполняется в начале `build_report()`, а
  двойной вызов из `execute()` убран (проверка чистая — повторный вызов
  идемпотентен). Невалидная комбинация фильтров (freq-in + freq-min/max,
  freq-min > freq-max, unknown min-credit-rating, screener-limit < 1,
  max-ytm <= 0) отклоняется той же ошибкой, что и CLI. Переходные явные
  вызовы `_validate_args` из 029-T04 в sync-chatgpt-portfolio убраны —
  validation в exporter не дублируется. Тесты: tests/test_rebalance_report.py
  (TestBuildReportValidatesArgs), tests/test_sync_chatgpt_portfolio.py.
- feat(tbank): complete RUB row invariant — synthetic zero row (029-T02) —
  успешный `GetPositions(account_id)` без RUB ни в money, ни в blocked
  трактуется как money=0, blocked=0, available=0: в
  `TbankApiClient._money_positions_to_balances()` синтезируется zero RUB row,
  чтобы после успешного `get_money_positions()` для каждого выбранного счёта
  существовала актуальная RUB row этого sync (иначе агрегат складывал fresh
  строки с устаревшей RUB row прошлого sync: 120+500=620 вместо 120).
  API failure отличается от отсутствия RUB: exception любого счёта прерывает
  `get_money_positions()` до записи в БД, zero не синтезируется, прошлый cash
  остаётся как failed snapshot. Тесты: tests/test_tbank_rub_zero_row.py.
- feat(export): sync-chatgpt-portfolio — factory-backed canonical report
  builder (029-T04) — create(factory) инжектит factory-created
  RebalanceReportUseCase.build_report: rating_scale из конфига и canonical
  construction переиспользуются (config.yaml в exporter повторно не читается,
  второй rating scale не создаётся), db — кешированный factory connection,
  оба use case работают с одним PortfolioStorage. Builder прогоняет candidate
  args через canonical _validate_args: unknown min-credit-rating даёт ту же
  ошибку, что CLI rebalance-report; прямой constructor без factory и без
  injected report_builder при strict min-credit-rating fail-fast вместо
  silent skip rating filter. Тесты: tests/test_sync_chatgpt_portfolio.py.
- feat(tbank): strict configured-account preflight (029-T01) — при заданном
  `TBANK_ACCOUNT_IDS` ВСЕ configured account IDs обязаны присутствовать среди
  счетов токена: missing one-of-many -> ValueError (с перечнем отсутствующих
  ID) ДО GetPortfolio/GetPositions и ДО записи в БД; sync-portfolio
  прерывается без clear/add TBank positions и без публикации частичного
  positions/cash snapshot (позиции и cash отсутствующего счёта остаются в
  последнем успешном snapshot). Пустой `TBANK_ACCOUNT_IDS` — все счета
  токена (прежнее поведение). Тесты: tests/test_tbank_account_preflight.py.
