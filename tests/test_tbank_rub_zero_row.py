"""
Тесты complete RUB row invariant (задача 029-T02, Track A).

Кейс (спека 029, раздел 5.2 «Complete RUB row invariant»; тесты 33.3.2 Case D):

    account A вернул RUB=120, получил новый updated_at;
    account B не вернул RUB (реально 0);
    старая RUB row B (500) остаётся в БД;
    MAX(updated_at) продвигается за счёт A;
    cash_available_rub ошибочно = 120 + 500 = 620 вместо 120.

Инвариант: после УСПЕШНОГО полного get_money_positions() для каждого
выбранного TBank account в результирующем snapshot существует актуальная
RUB row этого sync:

- успешный GetPositions без RUB ни в money, ни в blocked трактуется как
  money=0, blocked=0, available=0 — синтезируется zero RUB row;
- отсутствие RUB в успешном ответе ≠ API failure: exception любого
  account прерывает get_money_positions() до DB write, zero не
  синтезируется, прошлый cash остаётся как failed snapshot.

Сетевые вызовы TBank эмулируются подменой src.tbank.api_client.Client.
DB-регрессии (Case D) — синтетика на тестовой БД (POSTGRES_DSN_TEST);
гарантии агрегата продублированы in-memory тестами sync-потока (без БД).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.tbank.api_client as api_client_module
from src.storage import PortfolioStorage
from src.tbank.api_client import TbankApiClient
from src.use_cases.sync_portfolio import SyncPortfolioUseCase


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
    """Подменяет Client в api_client и сбрасывает состояние фейка.

    Кейс Case D: A (acc-1) возвращает RUB, B (acc-2) — успешный ответ
    вообще без RUB (фактический баланс обнулился).
    """
    monkeypatch.setattr(api_client_module, 'Client', _FakeClient)
    _FakeClient.accounts = [
        _FakeAccount('acc-1', 'Основной'),
        _FakeAccount('acc-2', 'ИИС'),
    ]
    _FakeClient.responses = {
        'acc-1': _FakePositionsResponse(
            money=[_FakeMoney('rub', 120)],
            blocked=[],
        ),
        'acc-2': _FakePositionsResponse(
            money=[],
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


def _make_sync_args():
    return SimpleNamespace(source='tbank', excel_path=None, upsert=False)


# ---------------------------------------------------------------------------
# Unit: synthetic zero RUB row в conversion layer (_money_positions_to_balances)
# ---------------------------------------------------------------------------

class TestSyntheticZeroRubRowConversion:
    def test_no_rub_in_response_gets_zero_rub_row(self, fake_tbank):
        """Успешный ответ без RUB -> synthetic zero RUB row (0/0/0)."""
        _FakeClient.responses = {
            'acc-1': _FakePositionsResponse(
                money=[_FakeMoney('usd', 10)],
                blocked=[],
            ),
        }
        client = _FakeTbankClient(account_ids=['acc-1'])
        balances = client.get_money_positions()
        rub_rows = [b for b in balances if b['currency'] == 'rub']
        assert len(rub_rows) == 1, "инвариант: ровно одна актуальная RUB row"
        row = rub_rows[0]
        assert row['broker_name'] == 'TBank'
        assert row['account_id'] == 'acc-1'
        assert row['money'] == Decimal('0')
        assert row['blocked'] == Decimal('0')
        assert row['available'] == Decimal('0')
        # валютная строка сохранена как была
        usd = [b for b in balances if b['currency'] == 'usd']
        assert len(usd) == 1 and usd[0]['money'] == Decimal('10')

    def test_fully_empty_successful_response_gets_zero_rub_row(self, fake_tbank):
        """Успешный пустой money/blocked — тоже «RUB отсутствует», не ошибка."""
        client = _FakeTbankClient(account_ids=['acc-2'])
        balances = client.get_money_positions()
        assert [b['currency'] for b in balances] == ['rub']
        row = balances[0]
        assert row['money'] == Decimal('0')
        assert row['blocked'] == Decimal('0')
        assert row['available'] == Decimal('0')

    def test_rub_present_no_duplicate_row(self, fake_tbank):
        """RUB присутствует в ответе — synthetic row не добавляется."""
        _FakeClient.responses = {
            'acc-1': _FakePositionsResponse(
                money=[_FakeMoney('rub', 100, 500000000)],
                blocked=[_FakeMoney('rub', 20)],
            ),
        }
        client = _FakeTbankClient(account_ids=['acc-1'])
        balances = client.get_money_positions()
        rub_rows = [b for b in balances if b['currency'] == 'rub']
        assert len(rub_rows) == 1
        assert rub_rows[0]['money'] == Decimal('100.5')
        assert rub_rows[0]['available'] == Decimal('80.5')

    def test_rub_in_blocked_only_no_duplicate_row(self, fake_tbank):
        """RUB только в blocked — это «RUB присутствует», дубликата нет."""
        _FakeClient.responses = {
            'acc-1': _FakePositionsResponse(
                money=[],
                blocked=[_FakeMoney('rub', 5)],
            ),
        }
        client = _FakeTbankClient(account_ids=['acc-1'])
        balances = client.get_money_positions()
        rub_rows = [b for b in balances if b['currency'] == 'rub']
        assert len(rub_rows) == 1
        assert rub_rows[0]['money'] == Decimal('0')
        assert rub_rows[0]['blocked'] == Decimal('5')
        assert rub_rows[0]['available'] == Decimal('-5')

    def test_pure_conversion_function_direct(self):
        """Conversion layer напрямую: без RUB возвращает zero RUB row счёта."""
        balances = TbankApiClient._money_positions_to_balances(
            account_id='acc-x', money=[], blocked=[])
        assert balances == [{
            'broker_name': 'TBank',
            'account_id': 'acc-x',
            'currency': 'rub',
            'money': Decimal('0'),
            'blocked': Decimal('0'),
            'available': Decimal('0'),
        }]


# ---------------------------------------------------------------------------
# Unit: отсутствие RUB в успешном ответе ≠ API failure (разные code paths)
# ---------------------------------------------------------------------------

class TestMissingRubDistinctFromApiFailure:
    def test_missing_rub_returns_rows_not_exception(self, fake_tbank):
        """Успешный ответ B без RUB -> строки возвращаются, исключения нет."""
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-2'])
        balances = client.get_money_positions()
        rub_rows = [b for b in balances if b['currency'] == 'rub']
        assert {b['account_id'] for b in rub_rows} == {'acc-1', 'acc-2'}
        assert {b['available'] for b in rub_rows} == {Decimal('120'), Decimal('0')}

    def test_api_failure_raises_no_zero_row(self, fake_tbank):
        """API failure счёта — exception наверх, zero RUB не синтезируется."""
        _FakeClient.responses = {
            'acc-1': _FakePositionsResponse(money=[_FakeMoney('rub', 120)], blocked=[]),
            'acc-2': RuntimeError('UNAVAILABLE: operations service down'),
        }
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-2'])
        with pytest.raises(RuntimeError):
            client.get_money_positions()


# ---------------------------------------------------------------------------
# In-memory sync-регрессии Case D (без БД): агрегат = fresh RUB без stale
# ---------------------------------------------------------------------------

def _balance(account_id, currency='rub', money='100', blocked='0'):
    return {
        'broker_name': 'TBank',
        'account_id': account_id,
        'currency': currency,
        'money': Decimal(money),
        'blocked': Decimal(blocked),
        'available': Decimal(money) - Decimal(blocked),
    }


class _FakeStorage:
    """In-memory замена PortfolioStorage: состояние + лог мутирующих вызовов."""

    def __init__(self):
        self.positions = []
        self.cash = {}  # (account_id, currency) -> balance row
        self.calls = []

    def get_portfolio_positions(self):
        return list(self.positions)

    def clear_portfolio_positions(self):
        self.calls.append('clear_all')
        self.positions = []

    def clear_portfolio_positions_by_broker(self, broker):
        self.calls.append(('clear_broker', broker))
        self.positions = [p for p in self.positions if p.broker_name != broker]

    def add_portfolio_positions(self, positions):
        self.calls.append(('add', [p.isin for p in positions]))
        self.positions.extend(positions)

    def upsert_cash_balances(self, balances):
        self.calls.append(('upsert_cash', [b['account_id'] for b in balances]))
        for b in balances:
            self.cash[(b['account_id'], b['currency'])] = b
        return len(balances)

    def delete_cash_accounts_not_in(self, broker, account_ids):
        self.calls.append(('delete_cash_not_in', broker, set(account_ids)))
        self.cash = {k: v for k, v in self.cash.items() if k[0] in set(account_ids)}

    def replace_cash_balances_snapshot(self, broker, balances, selected_account_ids):
        # 029-T03: sync публикует cash атомарным методом (upsert + prune).
        self.calls.append(
            ('replace_cash_snapshot', broker, set(selected_account_ids)))
        self.upsert_cash_balances(balances)
        self.delete_cash_accounts_not_in(broker, set(selected_account_ids))
        return len(balances)

    def mark_closed_positions(self, keep_keys=None, brokers=None):
        return []

    def mark_matured_positions(self):
        return []


class TestCaseDSyncInMemory:
    def _seed_stale_snapshot(self, storage):
        """Прошлый успешный sync (T1): A RUB=100, B RUB=500."""
        storage.upsert_cash_balances([
            _balance('acc-1', money='100'),
            _balance('acc-2', money='500'),
        ])
        storage.calls = []

    def _run_sync(self, storage):
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-2'])
        client.get_portfolio_positions = lambda: []
        use_case = SyncPortfolioUseCase(_FakeFactory(storage, client))
        use_case.execute(_make_sync_args())

    def test_case_d_aggregate_120_not_620(self, fake_tbank):
        """Case D: A RUB=120, B без RUB -> агрегат 120, а не 120+500=620."""
        storage = _FakeStorage()
        self._seed_stale_snapshot(storage)
        self._run_sync(storage)

        rub_rows = [b for (_, ccy), b in storage.cash.items() if ccy == 'rub']
        total = sum((b['available'] for b in rub_rows), Decimal(0))
        assert total == Decimal('120'), (
            f"stale RUB row B обязана быть перезаписана synthetic zero, "
            f"агрегат = {total}"
        )
        assert storage.cash[('acc-2', 'rub')]['available'] == Decimal('0')

    def test_case_d_both_rows_written_by_new_sync(self, fake_tbank):
        """Обе RUB rows (A и B) записаны одним новым sync (upsert того же запуска)."""
        storage = _FakeStorage()
        self._seed_stale_snapshot(storage)
        self._run_sync(storage)

        upserts = [call for call in storage.calls if call[0] == 'upsert_cash']
        assert upserts, "успешный sync обязан записать новый cash snapshot"
        written_ids = set()
        for _, ids in upserts:
            written_ids.update(ids)
        assert written_ids == {'acc-1', 'acc-2'}, (
            "synthetic zero row B пишется тем же sync, что и fresh row A"
        )
        # B-строка в хранилище — объект именно этого sync (zero row)
        assert storage.cash[('acc-2', 'rub')]['money'] == Decimal('0')

    def test_exception_on_any_account_publishes_nothing(self, fake_tbank):
        """Exception на любом account -> новый DB cash snapshot не публикуется.

        Контракт: exception прерывает get_money_positions() до DB write
        (zero не синтезируется); на уровне sync не-preflight API error по
        семантике 022 превращается в «cash не обновлён» — прошлый успешный
        snapshot остаётся как failed snapshot.
        """
        storage = _FakeStorage()
        self._seed_stale_snapshot(storage)

        _FakeClient.responses = {
            'acc-1': _FakePositionsResponse(money=[_FakeMoney('rub', 120)], blocked=[]),
            'acc-2': RuntimeError('UNAVAILABLE: operations service down'),
        }
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-2'])
        client.get_portfolio_positions = lambda: []
        use_case = SyncPortfolioUseCase(_FakeFactory(storage, client))
        use_case.execute(_make_sync_args())

        cash_writes = [
            call for call in storage.calls
            if call[0] in ('upsert_cash', 'delete_cash_not_in',
                           'replace_cash_snapshot')
        ]
        assert cash_writes == [], (
            f"failed snapshot не должен публиковать cash, было: {cash_writes}"
        )
        # последний успешный snapshot не тронут
        assert storage.cash[('acc-2', 'rub')]['available'] == Decimal('500')


# ---------------------------------------------------------------------------
# DB-регрессии Case D (требуют POSTGRES_DSN_TEST, как остальные тесты хранилища)
# ---------------------------------------------------------------------------

_T1 = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0)


def _seed_stale_cash(db, t1):
    """Прошлый успешный sync (T1): A RUB=100, B RUB=500, updated_at=T1."""
    db.upsert_cash_balances([
        _balance('acc-1', money='100'),
        _balance('acc-2', money='500'),
    ])
    with db.conn.cursor() as cur:
        cur.execute(
            "UPDATE portfolio_cash_balances SET updated_at = %s", (t1,)
        )
    db.conn.commit()


def _cash_rows(db):
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT account_id, available, updated_at FROM portfolio_cash_balances "
            "WHERE lower(currency) = 'rub'"
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


class TestCaseDDatabase:
    def test_case_d_aggregate_120_and_fresh_rows(self, db, fake_tbank):
        """Case D на БД: агрегат 120 (не 620), обе RUB rows нового sync.

        before: A RUB=100, B RUB=500, updated_at=T1;
        успешный snapshot: A RUB=120, B без RUB (synthetic zero);
        после sync: cash_available_rub = 120, cash_updated_at > T1,
        updated_at обеих RUB rows > T1 (актуальные, того же sync).
        """
        _seed_stale_cash(db, _T1)

        client = _FakeTbankClient(account_ids=['acc-1', 'acc-2'])
        client.get_portfolio_positions = lambda: []
        use_case = SyncPortfolioUseCase(_FakeFactory(db, client))
        use_case.execute(_make_sync_args())

        result = db.get_available_cash_rub()
        assert result['cash_known'] is True
        assert result['cash_available_rub'] == Decimal('120'), (
            "stale RUB row B (500) не должна складываться с fresh A (120)"
        )
        assert result['cash_updated_at'] > _T1

        rows = _cash_rows(db)
        assert set(rows) == {'acc-1', 'acc-2'}
        assert rows['acc-1'][0] == Decimal('120')
        assert rows['acc-2'][0] == Decimal('0'), (
            "synthetic zero RUB row B обязана быть в DB snapshot"
        )
        assert rows['acc-1'][1] > _T1
        assert rows['acc-2'][1] > _T1, (
            "synthetic zero row B получает updated_at того же (нового) sync"
        )
        # 029-T03: publish атомарен — обе RUB rows с одним transaction timestamp
        assert rows['acc-1'][1] == rows['acc-2'][1], (
            "все RUB rows успешного snapshot обязаны иметь один timestamp транзакции"
        )
