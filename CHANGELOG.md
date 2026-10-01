# Changelog

Все заметные изменения проекта. Стиль — по коммитам: `feat(scope): ... (task NNN)`.

## 2026-10-01

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
