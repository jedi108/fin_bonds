"""
Тесты canonical cash snapshot (задача 022, миграция 015, schema v2 отчёта).

Покрывают чек-лист задачи:
- RUB money position читается; blocked RUB уменьшают available;
- foreign currency не конвертируются и не входят в cash_available_rub;
- несколько configured accounts суммируются; unconfigured не учитывается;
- API failure не превращается в cash=0 и не стирает прошлый снапшот;
- cash хранится в PostgreSQL (portfolio_cash_balances);
- rebalance-report читает cash из PostgreSQL (не live API): unknown -> NULL,
  investable_total_rub корректен, total_value backward-compatible.

Сетевые вызовы TBank эмулируются подменой src.tbank.api_client.Client
(контракт OperationsService GetPositions: money[]/blocked[] из MoneyValue).
Все данные — синтетические на тестовой БД (POSTGRES_DSN_TEST).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.tbank.api_client as api_client_module
from src.storage import PortfolioStorage
from src.tbank.api_client import TbankApiClient
from src.use_cases.rebalance_report import RebalanceReportUseCase, SCHEMA_VERSION
from src.use_cases.sync_portfolio import SyncPortfolioUseCase


_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fakes: TBank Client (OperationsService GetPositions) и фабрика use-case'а
# ---------------------------------------------------------------------------

class _FakeMoney:
    def __init__(self, currency: str, units: int, nano: int = 0):
        self.currency = currency
        self.units = units
        self.nano = nano


class _FakePositionsResponse:
    def __init__(self, money, blocked):
        self.money = money
        self.blocked = blocked


class _FakeAccount:
    def __init__(self, account_id: str, name: str):
        self.id = account_id
        self.name = name


class _FakeOperations:
    def __init__(self, responses):
        self._responses = responses

    def get_positions(self, account_id: str):
        if account_id not in self._responses:
            raise AssertionError(f"Неожиданный запрос get_positions: {account_id}")
        response = self._responses[account_id]
        if isinstance(response, Exception):
            raise response
        return response


class _FakeUsersService:
    def __init__(self, accounts):
        self._accounts = accounts

    def get_accounts(self):
        return SimpleNamespace(accounts=list(self._accounts))


class _FakeClient:
    """Подмена tinkoff.invest.Client в api_client (context manager)."""

    accounts: list = []
    responses: dict = {}

    def __init__(self, token: str):
        assert token, "токен обязателен"

    def __enter__(self):
        self.users = _FakeUsersService(_FakeClient.accounts)
        self.operations = _FakeOperations(dict(_FakeClient.responses))
        return self

    def __exit__(self, *exc):
        return False


class _FakeTbankClient(TbankApiClient):
    """TbankApiClient с подменённым сетевым Client (unit-режим)."""

    def __init__(self, account_ids=None):
        super().__init__(token='fake-token', account_ids=account_ids or [])


@pytest.fixture
def fake_tbank(monkeypatch):
    """Подменяет Client в api_client и сбрасывает состояние фейка."""
    monkeypatch.setattr(api_client_module, 'Client', _FakeClient)
    _FakeClient.accounts = [
        _FakeAccount('acc-1', 'Основной'),
        _FakeAccount('acc-2', 'ИИС'),
        _FakeAccount('acc-3', 'Вне списка'),
    ]
    _FakeClient.responses = {
        'acc-1': _FakePositionsResponse(
            money=[_FakeMoney('rub', 100, 500000000), _FakeMoney('usd', 10)],
            blocked=[_FakeMoney('rub', 20)],
        ),
        'acc-2': _FakePositionsResponse(
            money=[_FakeMoney('rub', 250)],
            blocked=[],
        ),
        'acc-3': _FakePositionsResponse(
            money=[_FakeMoney('rub', 999)],
            blocked=[],
        ),
    }
    return _FakeClient


class _FakeFactory:
    """Минимальная фабрика для SyncPortfolioUseCase (без сети)."""

    def __init__(self, db: PortfolioStorage, tbank):
        self._db = db
        self._tbank = tbank

    def get_db_connection(self) -> PortfolioStorage:
        return self._db

    def get_tbank_api_client(self):
        return self._tbank

    def get_alor_api_client(self):
        return None


def _make_sync_args():
    return SimpleNamespace(source='tbank', excel_path=None, upsert=False)


# ---------------------------------------------------------------------------
# TBank API client: чтение money positions
# ---------------------------------------------------------------------------

class TestGetMoneyPositions:
    def test_rub_position_read_and_blocked_reduces_available(self, fake_tbank):
        """RUB money position читается; blocked RUB не считаются доступными."""
        client = _FakeTbankClient(account_ids=['acc-1'])
        balances = client.get_money_positions()
        rub = [b for b in balances if b['currency'] == 'rub']
        assert len(rub) == 1
        row = rub[0]
        assert row['broker_name'] == 'TBank'
        assert row['account_id'] == 'acc-1'
        assert row['money'] == Decimal('100.5')
        assert row['blocked'] == Decimal('20')
        assert row['available'] == Decimal('80.5')  # 100.5 - 20

    def test_foreign_currency_not_converted(self, fake_tbank):
        """Foreign currency хранится как есть, конвертации нет."""
        client = _FakeTbankClient(account_ids=['acc-1'])
        balances = client.get_money_positions()
        usd = [b for b in balances if b['currency'] == 'usd']
        assert len(usd) == 1
        assert usd[0]['money'] == Decimal('10')
        assert usd[0]['available'] == Decimal('10')

    def test_only_configured_accounts_fetched(self, fake_tbank):
        """Unconfigured account не учитывается (не запрашивается вовсе)."""
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-2'])
        balances = client.get_money_positions()
        assert {b['account_id'] for b in balances} == {'acc-1', 'acc-2'}
        assert all(b['account_id'] != 'acc-3' for b in balances)

    def test_no_account_ids_fetches_all_token_accounts(self, fake_tbank):
        """Пустой configured set — все счета токена (совместимость с позициями)."""
        client = _FakeTbankClient(account_ids=[])
        balances = client.get_money_positions()
        assert {b['account_id'] for b in balances} == {'acc-1', 'acc-2', 'acc-3'}

    def test_api_error_raises_not_zero(self, fake_tbank):
        """API error пробрасывается наверх — не превращается в cash=0."""
        _FakeClient.responses = {
            'acc-1': RuntimeError('UNAVAILABLE: operations service down'),
        }
        client = _FakeTbankClient(account_ids=['acc-1'])
        with pytest.raises(RuntimeError):
            client.get_money_positions()

    def test_none_of_configured_accounts_found_fails_fast(self, fake_tbank):
        client = _FakeTbankClient(account_ids=['acc-unknown'])
        with pytest.raises(ValueError):
            client.get_money_positions()


# ---------------------------------------------------------------------------
# Storage: PostgreSQL canonical cash snapshot
# ---------------------------------------------------------------------------

def _balance(account_id, currency='rub', money='100', blocked='0', available=None):
    return {
        'broker_name': 'TBank',
        'account_id': account_id,
        'currency': currency,
        'money': Decimal(money),
        'blocked': Decimal(blocked),
        'available': Decimal(available) if available is not None else Decimal(money) - Decimal(blocked),
    }


class TestCashStorage:
    def test_cash_persisted_in_postgres_and_read_back(self, db):
        """cash хранится в PostgreSQL и читается canonical reader'ом."""
        written = db.upsert_cash_balances([_balance('acc-1', money='1234.56')])
        assert written == 1
        result = db.get_available_cash_rub()
        assert result['cash_known'] is True
        assert result['cash_available_rub'] == Decimal('1234.56')
        assert result['cash_updated_at'] is not None

    def test_idempotent_upsert(self, db):
        db.upsert_cash_balances([_balance('acc-1', money='100')])
        db.upsert_cash_balances([_balance('acc-1', money='500')])
        result = db.get_available_cash_rub()
        assert result['cash_available_rub'] == Decimal('500')
        with db.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM portfolio_cash_balances")
            assert cur.fetchone()[0] == 1

    def test_blocked_rub_reduces_available_in_storage(self, db):
        db.upsert_cash_balances([_balance('acc-1', money='100.5', blocked='20')])
        assert db.get_available_cash_rub()['cash_available_rub'] == Decimal('80.5')

    def test_foreign_currency_not_in_rub_total(self, db):
        """Валютные строки хранятся, но в cash_available_rub не входят."""
        db.upsert_cash_balances([
            _balance('acc-1', currency='rub', money='100'),
            _balance('acc-1', currency='usd', money='1000'),
            _balance('acc-1', currency='eur', money='500'),
        ])
        result = db.get_available_cash_rub()
        assert result['cash_available_rub'] == Decimal('100')

    def test_multiple_configured_accounts_summed(self, db):
        """Несколько configured accounts суммируются."""
        db.upsert_cash_balances([
            _balance('acc-1', money='100.5'),
            _balance('acc-2', money='250'),
        ])
        assert db.get_available_cash_rub()['cash_available_rub'] == Decimal('350.5')

    def test_real_zero_rub_distinct_from_unknown(self, db):
        """Реальный 0 RUB (строка) != unknown (строк нет)."""
        unknown = db.get_available_cash_rub()
        assert unknown == {
            'cash_available_rub': None, 'cash_updated_at': None, 'cash_known': False,
        }
        db.upsert_cash_balances([_balance('acc-1', money='0')])
        known_zero = db.get_available_cash_rub()
        assert known_zero['cash_known'] is True
        assert known_zero['cash_available_rub'] == Decimal('0')

    def test_delete_accounts_not_in_removes_unconfigured(self, db):
        """Смена configured set: выбывший счёт перестаёт попадать в агрегат."""
        db.upsert_cash_balances([
            _balance('acc-1', money='100'),
            _balance('acc-old', money='900'),
        ])
        deleted = db.delete_cash_accounts_not_in('TBank', {'acc-1'})
        assert deleted == 1
        assert db.get_available_cash_rub()['cash_available_rub'] == Decimal('100')

    def test_upsert_empty_list_writes_nothing(self, db):
        assert db.upsert_cash_balances([]) == 0
        assert db.get_available_cash_rub()['cash_known'] is False


# ---------------------------------------------------------------------------
# sync-portfolio: cash тем же запуском, failure виден
# ---------------------------------------------------------------------------

class TestSyncPortfolioCash:
    def test_sync_updates_cash_same_run(self, db, fake_tbank):
        """Один sync-portfolio обновляет cash (позиции пусты — cash всё равно пишется)."""
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-2'])
        client.get_portfolio_positions = lambda: []
        use_case = SyncPortfolioUseCase(_FakeFactory(db, client))
        use_case.execute(_make_sync_args())
        result = db.get_available_cash_rub()
        assert result['cash_known'] is True
        # acc-1: 100.5 - 20 = 80.5; acc-2: 250
        assert result['cash_available_rub'] == Decimal('330.5')

    def test_api_failure_keeps_previous_cash_not_zero(self, db, fake_tbank):
        """API failure != 0 RUB: прошлый снапшот остаётся, updated_at не меняется."""
        db.upsert_cash_balances([_balance('acc-1', money='777')])
        before = db.get_available_cash_rub()

        client = _FakeTbankClient(account_ids=['acc-1'])
        client.get_portfolio_positions = lambda: []
        client.get_money_positions = lambda: (_ for _ in ()).throw(
            RuntimeError('UNAVAILABLE: operations service down'))
        use_case = SyncPortfolioUseCase(_FakeFactory(db, client))
        use_case.execute(_make_sync_args())

        after = db.get_available_cash_rub()
        assert after['cash_known'] is True
        assert after['cash_available_rub'] == Decimal('777')
        assert after['cash_updated_at'] == before['cash_updated_at']

    def test_successful_sync_replaces_stale_snapshot(self, db, fake_tbank):
        """Успешный sync заменяет устаревший снапшот (idempotent upsert)."""
        db.upsert_cash_balances([_balance('acc-stale', money='999')])

        client = _FakeTbankClient(account_ids=['acc-1'])
        client.get_portfolio_positions = lambda: []
        use_case = SyncPortfolioUseCase(_FakeFactory(db, client))
        use_case.execute(_make_sync_args())

        result = db.get_available_cash_rub()
        assert result['cash_available_rub'] == Decimal('80.5')
        # выбывший счёт удалён из агрегата
        with db.conn.cursor() as cur:
            cur.execute(
                "SELECT account_id FROM portfolio_cash_balances WHERE account_id = 'acc-stale'"
            )
            assert cur.fetchone() is None


# ---------------------------------------------------------------------------
# rebalance-report v2: cash из PostgreSQL
# ---------------------------------------------------------------------------

def _insert_position(db, isin: str, quantity: float, price: float):
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO portfolio_positions (isin, ticker, name, quantity, average_price, "
            "current_price, current_value, broker_name, account_id, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (isin, 'TICK', 'Позиция', quantity, price, price, quantity * price,
             'TBank', 'acc-1', _NOW),
        )
    db.conn.commit()


def _report_args():
    return RebalanceReportUseCase.default_args()


class TestRebalanceReportCash:
    def test_schema_version_bumped(self):
        assert SCHEMA_VERSION == 2

    def test_unknown_cash_gives_null(self, db):
        """unknown cash: null, а не 0 (не трактовать NULL как 0)."""
        _insert_position(db, 'RU000A0TEST1', 10, 1000)
        report = RebalanceReportUseCase(db=db).build_report(_report_args())
        portfolio = report['portfolio']
        assert portfolio['cash_available_rub'] is None
        assert portfolio['investable_total_rub'] is None
        assert portfolio['cash_updated_at'] is None
        # total_value backward-compatible
        assert portfolio['total_value'] == 10000.0
        assert portfolio['securities_value_rub'] == portfolio['total_value']

    def test_cash_read_from_postgres_not_live_api(self, db):
        """rebalance-report берёт cash из PostgreSQL и считает investable total."""
        _insert_position(db, 'RU000A0TEST1', 10, 1000)
        db.upsert_cash_balances([
            _balance('acc-1', money='100.5', blocked='20'),
            _balance('acc-2', money='250'),
            _balance('acc-1', currency='usd', money='1000'),
        ])
        report = RebalanceReportUseCase(db=db).build_report(_report_args())
        portfolio = report['portfolio']
        assert portfolio['cash_available_rub'] == 330.5  # 80.5 + 250, без usd
        assert portfolio['investable_total_rub'] == 10330.5
        assert portfolio['cash_updated_at'] is not None
        # семантика total_value не изменилась: только облигации
        assert portfolio['total_value'] == 10000.0
        assert portfolio['securities_value_rub'] == 10000.0
        # доли и концентрации по-прежнему от securities (cash не входит)
        risk_mix = report['concentrations']['risk_mix']
        assert sum(item['value'] for item in risk_mix) == 10000.0
        assert sum(item['pct'] for item in risk_mix) == 100.0

    def test_real_zero_cash_is_not_null(self, db):
        """Реальный 0 RUB в отчёте — 0 (known), а не null (unknown)."""
        _insert_position(db, 'RU000A0TEST1', 10, 1000)
        db.upsert_cash_balances([_balance('acc-1', money='0')])
        portfolio = RebalanceReportUseCase(db=db).build_report(_report_args())['portfolio']
        assert portfolio['cash_available_rub'] == 0.0
        assert portfolio['investable_total_rub'] == 10000.0
        assert portfolio['cash_updated_at'] is not None


# ---------------------------------------------------------------------------
# Стартовые строки cash для отчёта «после sync»: stale updated_at
# ---------------------------------------------------------------------------

class TestCashFreshness:
    def test_stale_cash_updated_at_visible(self, db):
        """Метка последнего успешного sync отдаётся как есть: старый cash виден."""
        stale = (_NOW - timedelta(hours=48)).replace(microsecond=0)
        with db.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_cash_balances "
                "(broker_name, account_id, currency, money, blocked, available, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                ('TBank', 'acc-1', 'rub', 100, 0, 100, stale),
            )
        db.conn.commit()
        portfolio = RebalanceReportUseCase(db=db).build_report(_report_args())['portfolio']
        assert portfolio['cash_updated_at'] is not None
        # ISO-строка с таймзоной; момент времени — ровно метка последнего sync
        assert datetime.fromisoformat(portfolio['cash_updated_at']) == stale
