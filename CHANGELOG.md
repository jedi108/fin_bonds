# Changelog

Все заметные изменения проекта. Стиль — по коммитам: `feat(scope): ... (task NNN)`.

## 2026-10-01

- feat(tbank): strict configured-account preflight (029-T01) — при заданном
  `TBANK_ACCOUNT_IDS` ВСЕ configured account IDs обязаны присутствовать среди
  счетов токена: missing one-of-many -> ValueError (с перечнем отсутствующих
  ID) ДО GetPortfolio/GetPositions и ДО записи в БД; sync-portfolio
  прерывается без clear/add TBank positions и без публикации частичного
  positions/cash snapshot (позиции и cash отсутствующего счёта остаются в
  последнем успешном snapshot). Пустой `TBANK_ACCOUNT_IDS` — все счета
  токена (прежнее поведение). Тесты: tests/test_tbank_account_preflight.py.
