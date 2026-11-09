"""
Тесты атомарной публикации cash snapshot (задача 029-T03, Track A).

Кейс (спека 029, раздел 5.2 «Atomic cash snapshot publish»; тесты 27 и
33.3.2 Case D rollback):

    upsert_cash_balances(...)        -> COMMIT
    delete_cash_accounts_not_in(...) -> отдельный COMMIT

Если upsert успешен, а prune затем падает, БД содержит часть нового
snapshot: cash_updated_at продвигается, хотя sync вернул failure — skill
ошибочно принимает mixed snapshot за свежий.

Инвариант: upsert актуальных строк и prune счетов вне selected set
выполняются в ОДНОЙ DB transaction (replace_cash_balances_snapshot),
при любой ошибке ROLLBACK — прошлый успешный snapshot остаётся целиком
(cash_available_rub и cash_updated_at не меняются). PostgreSQL now()
стабилен внутри транзакции: все строки успешного snapshot получают один
transaction timestamp (после T02 это proof полного RUB snapshot для
current-cash guard T14).

Unit-секция проверяет транзакционное поведение на fake-connection (без БД,
выполняется всегда); DB-секция — синтетика на тестовой БД
(POSTGRES_DSN_TEST), как остальные тесты хранилища.
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
# Unit: транзакционное поведение на fake-connection (без БД)
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def execute(self, query, vars=None):
        sql = ' '.join(query.split())
        self._conn.executed.append(sql)
        if self._conn.fail_on and sql.startswith(self._conn.fail_on):
            raise RuntimeError(f'boom: {self._conn.fail_on}')
        self.rowcount = 1 if sql.startswith('DELETE') else 0


class _FakeConn:
    """Fake psycopg2 connection: лог executed SQL + commit/rollback."""

    def __init__(self, fail_on: str = None):
        self.executed = []
        self.tx = []
        self.fail_on = fail_on

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)

    def commit(self):
        self.tx.append('commit')

    def rollback(self):
        self.tx.append('rollback')


def _make_storage(conn) -> PortfolioStorage:
    """PortfolioStorage без __init__ (не нужен DSN/схема): подменяем conn."""
    storage = object.__new__(PortfolioStorage)
    storage.conn = conn
    return storage


def _balance(account_id, currency='rub', money='100', blocked='0'):
    return {
        'broker_name': 'TBank',
        'account_id': account_id,
        'currency': currency,
        'money': Decimal(money),
        'blocked': Decimal(blocked),
        'available': Decimal(money) - Decimal(blocked),
    }


class TestAtomicPublishTransactionUnit:
    def test_upsert_then_prune_one_commit(self):
        """upsert всех строк, затем prune; ровно один COMMIT, без промежуточных."""
        conn = _FakeConn()
        storage = _make_storage(conn)
        written = storage.replace_cash_balances_snapshot(
            'TBank', [_balance('acc-1'), _balance('acc-2')], {'acc-1', 'acc-2'})
        assert written == 2
        kinds = [sql.split()[0] for sql in conn.executed]
        assert kinds == ['INSERT', 'INSERT', 'DELETE'], (
            f"ожидается upsert всех строк и затем prune, было: {kinds}"
        )
        assert conn.tx == ['commit'], (
            f"промежуточный commit между upsert и prune запрещён, было: {conn.tx}"
        )

    def test_error_on_prune_rolls_back(self):
        """Ошибка prune -> ROLLBACK, commit'а нет (upsert не опубликован)."""
        conn = _FakeConn(fail_on='DELETE')
        storage = _make_storage(conn)
        with pytest.raises(RuntimeError):
            storage.replace_cash_balances_snapshot(
                'TBank', [_balance('acc-1')], {'acc-1'})
        assert 'commit' not in conn.tx
        assert conn.tx == ['rollback']

    def test_error_on_upsert_rolls_back(self):
        """Ошибка upsert -> ROLLBACK (транзакция не оставлена открытой)."""
        conn = _FakeConn(fail_on='INSERT')
        storage = _make_storage(conn)
        with pytest.raises(RuntimeError):
            storage.replace_cash_balances_snapshot(
                'TBank', [_balance('acc-1')], {'acc-1'})
        assert conn.tx == ['rollback']

    def test_empty_balances_is_noop(self):
        """Пустой balances — snapshot не публикуется, к БД не обращаемся."""
        conn = _FakeConn()
        storage = _make_storage(conn)
        assert storage.replace_cash_balances_snapshot('TBank', [], {'acc-1'}) == 0
        assert conn.executed == []
        assert conn.tx == []

    def test_empty_selected_ids_skips_prune_only(self):
        """Пустой selected set — prune-часть не выполняется (семантика delete)."""
        conn = _FakeConn()
        storage = _make_storage(conn)
        storage.replace_cash_balances_snapshot('TBank', [_balance('acc-1')], set())
        assert [sql.split()[0] for sql in conn.executed] == ['INSERT']
        assert conn.tx == ['commit']

    def test_legacy_methods_keep_own_transaction(self):
        """Рефактор не изменил публичную семантику upsert/prune по отдельности."""
        conn = _FakeConn()
        storage = _make_storage(conn)
        assert storage.upsert_cash_balances([_balance('acc-1')]) == 1
        assert conn.tx == ['commit']
        conn.tx.clear()
        assert storage.delete_cash_accounts_not_in('TBank', {'acc-1'}) == 1
        assert conn.tx == ['commit']
        conn.tx.clear()
        conn.fail_on = 'DELETE'
        with pytest.raises(RuntimeError):
            storage.delete_cash_accounts_not_in('TBank', {'acc-1'})
        assert conn.tx == ['rollback']


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
    monkeypatch.setattr(api_client_module, 'Client', _FakeClient)
    _FakeClient.accounts = [
        _FakeAccount('acc-1', 'Основной'),
        _FakeAccount('acc-2', 'ИИС'),
    ]
    _FakeClient.responses = {
        'acc-1': _FakePositionsResponse(money=[_FakeMoney('rub', 120)], blocked=[]),
        'acc-2': _FakePositionsResponse(
            money=[_FakeMoney('rub', 100, 500000000)],
            blocked=[_FakeMoney('rub', 20)],
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
# DB-регрессии (требуют POSTGRES_DSN_TEST, как остальные тесты хранилища)
# ---------------------------------------------------------------------------

_T1 = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0)


def _seed_previous_snapshot(db, t1):
    """Прошлый успешный snapshot (T1): acc-1=100, acc-2=500, acc-old=900 RUB."""
    db.upsert_cash_balances([
        _balance('acc-1', money='100'),
        _balance('acc-2', money='500'),
        _balance('acc-old', money='900'),
    ])
    with db.conn.cursor() as cur:
        cur.execute("UPDATE portfolio_cash_balances SET updated_at = %s", (t1,))
    db.conn.commit()


def _cash_rows(db):
    with db.conn.cursor() as cur:
        cur.execute(
            "SELECT account_id, currency, available, updated_at "
            "FROM portfolio_cash_balances ORDER BY account_id, currency"
        )
        return [(row[0], row[1], row[2], row[3]) for row in cur.fetchall()]


class TestAtomicPublishDatabase:
    def test_successful_publish_upsert_and_prune_one_timestamp(self, db):
        """Успешная публикация: upsert + prune одной транзакцией, один timestamp.

        before: acc-1=100, acc-2=500, acc-old=900 (updated_at=T1);
        snapshot: acc-1=120, acc-2=80.5 (fresh), acc-1 usd=50,
        selected={acc-1, acc-2}; acc-old выпал из configured set.
        """
        _seed_previous_snapshot(db, _T1)

        written = db.replace_cash_balances_snapshot(
            'TBank',
            [
                _balance('acc-1', money='120'),
                _balance('acc-2', money='100.5', blocked='20'),
                _balance('acc-1', currency='usd', money='50'),
            ],
            {'acc-1', 'acc-2'},
        )
        assert written == 3

        rows = _cash_rows(db)
        assert {(a, c) for a, c, _, _ in rows} == {
            ('acc-1', 'rub'), ('acc-2', 'rub'), ('acc-1', 'usd'),
        }, f"выбывший acc-old обязан быть удалён, в БД: {rows}"
        by_key = {(a, c): (v, t) for a, c, v, t in rows}
        assert by_key[('acc-1', 'rub')][0] == Decimal('120')
        assert by_key[('acc-2', 'rub')][0] == Decimal('80.5')
        assert by_key[('acc-1', 'usd')][0] == Decimal('50')

        # все строки snapshot — один transaction timestamp, продвинутый за T1
        timestamps = {t for _, _, _, t in rows}
        assert len(timestamps) == 1, (
            f"все RUB rows успешного snapshot обязаны иметь один transaction "
            f"timestamp, было: {timestamps}"
        )
        assert timestamps.pop() > _T1

        result = db.get_available_cash_rub()
        assert result['cash_available_rub'] == Decimal('200.5')  # 120 + 80.5
        assert result['cash_updated_at'] > _T1

    def test_rollback_on_prune_error_keeps_previous_snapshot(self, db, monkeypatch):
        """Тест 27 / Case D rollback: ошибка между upsert и prune.

        Искусственная ошибка prune-шага -> ROLLBACK: новые values НЕ видны,
        старые rows НЕ удалены, cash_available_rub и cash_updated_at полностью
        от прошлого успешного snapshot (cash_updated_at НЕ продвинут).
        """
        _seed_previous_snapshot(db, _T1)

        def _boom(*args, **kwargs):
            raise RuntimeError('boom: prune failed')

        monkeypatch.setattr(db, '_delete_cash_accounts_not_in_rows', _boom)
        with pytest.raises(RuntimeError):
            db.replace_cash_balances_snapshot(
                'TBank',
                [_balance('acc-1', money='999')],
                {'acc-1', 'acc-2'},
            )

        rows = _cash_rows(db)
        by_key = {(a, c): (v, t) for a, c, v, t in rows}
        # новые values НЕ видны после rollback
        assert by_key[('acc-1', 'rub')][0] == Decimal('100'), (
            f"upsert до ошибки не должен быть виден после rollback: {by_key}"
        )
        # старые rows НЕ удалены (prune не выполнен)
        assert set(by_key) == {
            ('acc-1', 'rub'), ('acc-2', 'rub'), ('acc-old', 'rub'),
        }
        # агрегат и timestamp — полностью от прошлого успешного snapshot
        result = db.get_available_cash_rub()
        assert result['cash_available_rub'] == Decimal('1500')  # 100+500+900
        assert result['cash_updated_at'] == _T1, (
            "rollback НЕ должен продвигать cash_updated_at (тест 27)"
        )

    def test_sync_publishes_via_atomic_method_not_pair(self, db, fake_tbank, monkeypatch):
        """_sync_tbank_cash() использует атомарный метод вместо пары upsert+prune."""
        _seed_previous_snapshot(db, _T1)

        calls = []
        monkeypatch.setattr(
            db, 'upsert_cash_balances',
            lambda *a, **kw: calls.append('upsert_cash_balances'))
        monkeypatch.setattr(
            db, 'delete_cash_accounts_not_in',
            lambda *a, **kw: calls.append('delete_cash_accounts_not_in'))

        client = _FakeTbankClient(account_ids=['acc-1', 'acc-2'])
        client.get_portfolio_positions = lambda: []
        use_case = SyncPortfolioUseCase(_FakeFactory(db, client))
        use_case.execute(_make_sync_args())

        assert calls == [], (
            f"пара upsert+prune не должна вызываться, было: {calls}"
        )
        # sync опубликовал свежий snapshot (acc-1=120, acc-2=80.5), acc-old удалён
        result = db.get_available_cash_rub()
        assert result['cash_available_rub'] == Decimal('200.5')
        assert result['cash_updated_at'] > _T1
        assert {(a, c) for a, c, _, _ in _cash_rows(db)} == {
            ('acc-1', 'rub'), ('acc-2', 'rub'),
        }
