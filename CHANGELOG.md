# Changelog

Все заметные изменения проекта. Стиль — по коммитам: `feat(scope): ... (task NNN)`.

## 2026-10-01

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
