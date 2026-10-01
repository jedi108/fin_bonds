"""
Тесты strict configured-account preflight (задача 029-T01, Track A).

Кейс (спека 029, раздел 5.2; тесты 27 и 33.3.2 Case D2):

    TBANK_ACCOUNT_IDS=A,B
    users.get_accounts() -> только A

При частично missing configured set sync опаснее undercount cash:
clear/add TBank positions стирает позиции отсутствующего счёта B, как
будто брокер их закрыл. Поэтому preflight един и строг:

- ВСЕ configured account IDs обязаны присутствовать в users.get_accounts(),
  иначе ValueError ДО GetPortfolio/GetPositions и ДО записи в БД;
- пустой TBANK_ACCOUNT_IDS — все счета токена (текущее поведение);
- ошибки API (не preflight) в cash path по-прежнему не публикуют cash,
  но не прерывают sync (семантика 022 сохранена).

Сетевые вызовы TBank эмулируются подменой src.tbank.api_client.Client.
DB-регрессии (Case D2) — синтетика на тестовой БД (POSTGRES_DSN_TEST).
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.tbank.api_client as api_client_module
from src.data_models import PortfolioPosition
from src.storage import PortfolioStorage
from src.tbank.api_client import TbankApiClient
from src.use_cases.sync_portfolio import SyncPortfolioUseCase


# ---------------------------------------------------------------------------
# Fakes: TBank Client (UsersService GetAccounts + OperationsService) и фабрика
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


class _FakeUsersService:
    def __init__(self, accounts):
        self._accounts = accounts

    def get_accounts(self):
        return SimpleNamespace(accounts=list(self._accounts))


class _FakeOperations:
    """Логирует вызовы GetPortfolio/GetPositions в _FakeClient.calls."""

    def get_portfolio(self, account_id: str):
        _FakeClient.calls.append(('portfolio', account_id))
        return SimpleNamespace(positions=[])

    def get_positions(self, account_id: str):
        _FakeClient.calls.append(('positions', account_id))
        if account_id not in _FakeClient.money_responses:
            raise AssertionError(f"Неожиданный запрос get_positions: {account_id}")
        return _FakeClient.money_responses[account_id]


class _FakeClient:
    """Подмена tinkoff.invest.Client в api_client (context manager)."""

    accounts: list = []
    money_responses: dict = {}
    calls: list = []

    def __init__(self, token: str):
        assert token, "токен обязателен"

    def __enter__(self):
        self.users = _FakeUsersService(list(_FakeClient.accounts))
        self.operations = _FakeOperations()
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

    Токен видит счета acc-1 и acc-2; acc-b — «закрытый» счёт, которого у
    токена больше нет (кейс задачи: A=acc-1, B=acc-b, get_accounts -> только A).
    """
    monkeypatch.setattr(api_client_module, 'Client', _FakeClient)
    _FakeClient.accounts = [
        _FakeAccount('acc-1', 'Основной'),
        _FakeAccount('acc-2', 'ИИС'),
    ]
    _FakeClient.money_responses = {
        'acc-1': _FakePositionsResponse(
            money=[_FakeMoney('rub', 100)],
            blocked=[],
        ),
        'acc-2': _FakePositionsResponse(
            money=[_FakeMoney('rub', 250)],
            blocked=[],
        ),
    }
    _FakeClient.calls = []
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
# Unit: preflight в get_portfolio_positions (ДО GetPortfolio)
# ---------------------------------------------------------------------------

class TestPreflightPortfolioPositions:
    def test_missing_one_of_many_raises_listing_missing(self, fake_tbank):
        """Configured A найден, B отсутствует -> ValueError со списком missing."""
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-b'])
        with pytest.raises(ValueError) as exc:
            client.get_portfolio_positions()
        assert 'acc-b' in str(exc.value), (
            "Ошибка должна явно перечислять missing configured account IDs"
        )

    def test_preflight_runs_before_get_portfolio(self, fake_tbank):
        """Failed preflight: GetPortfolio не вызывается ни для одного счёта."""
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-b'])
        with pytest.raises(ValueError):
            client.get_portfolio_positions()
        assert _FakeClient.calls == [], "preflight обязан сработать ДО GetPortfolio"

    def test_all_configured_present_fetches_each(self, fake_tbank):
        """Полный configured set — обходятся все настроенные счета."""
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-2'])
        positions = client.get_portfolio_positions()
        assert positions == []
        assert _FakeClient.calls == [('portfolio', 'acc-1'), ('portfolio', 'acc-2')]

    def test_empty_account_ids_fetches_all_token_accounts(self, fake_tbank):
        """Пустой configured set — все счета токена (поведение сохранено)."""
        client = _FakeTbankClient(account_ids=[])
        client.get_portfolio_positions()
        assert _FakeClient.calls == [
            ('portfolio', 'acc-1'), ('portfolio', 'acc-2'),
        ]

    def test_none_of_configured_found_still_raises(self, fake_tbank):
        """Ни один configured счёт не найден — по-прежнему ValueError."""
        client = _FakeTbankClient(account_ids=['acc-unknown'])
        with pytest.raises(ValueError):
            client.get_portfolio_positions()
        assert _FakeClient.calls == []


# ---------------------------------------------------------------------------
# Unit: preflight в get_money_positions (ДО GetPositions), тот же helper
# ---------------------------------------------------------------------------

class TestPreflightMoneyPositions:
    def test_missing_one_of_many_raises_listing_missing(self, fake_tbank):
        """Частично missing configured set в cash path — тоже ValueError."""
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-b'])
        with pytest.raises(ValueError) as exc:
            client.get_money_positions()
        assert 'acc-b' in str(exc.value)

    def test_preflight_runs_before_get_positions(self, fake_tbank):
        """Failed preflight: GetPositions не вызывается ни для одного счёта."""
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-b'])
        with pytest.raises(ValueError):
            client.get_money_positions()
        assert _FakeClient.calls == [], "preflight обязан сработать ДО GetPositions"

    def test_all_configured_present_fetches_each(self, fake_tbank):
        client = _FakeTbankClient(account_ids=['acc-1', 'acc-2'])
        balances = client.get_money_positions()
        assert {b['account_id'] for b in balances} == {'acc-1', 'acc-2'}
        assert _FakeClient.calls == [('positions', 'acc-1'), ('positions', 'acc-2')]

    def test_empty_account_ids_fetches_all_token_accounts(self, fake_tbank):
        """Пустой configured set — все счета токена (поведение 022 сохранено)."""
        client = _FakeTbankClient(account_ids=[])
        balances = client.get_money_positions()
        assert {b['account_id'] for b in balances} == {'acc-1', 'acc-2'}

    def test_none_of_configured_found_still_raises(self, fake_tbank):
        client = _FakeTbankClient(account_ids=['acc-unknown'])
        with pytest.raises(ValueError):
            client.get_money_positions()
        assert _FakeClient.calls == []


# ---------------------------------------------------------------------------
# DB-регрессии (Case D2): failed preflight не публикует partial snapshot
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


def _seed_position(db, account_id: str, isin: str):
    db.add_portfolio_positions([PortfolioPosition(
        isin=isin,
        ticker=f'T{isin[-4:]}',
        name='Test Bond',
        broker_name='TBank',
        account_id=account_id,
        quantity=Decimal('10'),
        average_price=Decimal('100'),
        current_price=Decimal('100'),
        current_value=Decimal('1000'),
    )])


def _position_keys(db):
    return {
        (p.isin, p.broker_name, p.account_id)
        for p in db.get_portfolio_positions()
    }


def _cash_accounts(db):
    with db.conn.cursor() as cur:
        cur.execute("SELECT account_id, available FROM portfolio_cash_balances")
        return {row[0]: row[1] for row in cur.fetchall()}


class TestSyncPreflightFailFast:
    def test_case_d2_missing_account_does_not_publish_new_cash(self, db, fake_tbank):
        """Case D2: missing one-of-many не публикует новый cash snapshot.

        У токена остался только acc-1; в конфиге acc-1 + acc-b. Старый
        успешный cash (acc-b) остаётся, новых строк (acc-1) не появляется.
        """
        db.upsert_cash_balances([_balance('acc-b', money='500')])
        before = db.get_available_cash_rub()

        client = _FakeTbankClient(account_ids=['acc-1', 'acc-b'])
        use_case = SyncPortfolioUseCase(_FakeFactory(db, client))
        with pytest.raises(ValueError):
            use_case.execute(_make_sync_args())

        after = db.get_available_cash_rub()
        assert after['cash_known'] is True
        assert after['cash_available_rub'] == Decimal('500')
        assert after['cash_updated_at'] == before['cash_updated_at']
        assert set(_cash_accounts(db)) == {'acc-b'}

    def test_missing_account_positions_not_cleared(self, db, fake_tbank):
        """B (acc-b) остаётся в последнем успешном portfolio snapshot."""
        _seed_position(db, 'acc-1', 'RU000A0AAAA1')
        _seed_position(db, 'acc-b', 'RU000A0BBBB2')
        before = _position_keys(db)
        assert ('RU000A0BBBB2', 'TBank', 'acc-b') in before

        client = _FakeTbankClient(account_ids=['acc-1', 'acc-b'])
        use_case = SyncPortfolioUseCase(_FakeFactory(db, client))
        with pytest.raises(ValueError):
            use_case.execute(_make_sync_args())

        assert _position_keys(db) == before, (
            "Позиции отсутствующего счёта B обязаны остаться в БД без изменений"
        )

    def test_no_partial_snapshot_published_for_found_account(self, db, fake_tbank):
        """Partial snapshot по A (acc-1) не публикуется ни для positions, ни для cash.

        B не «исчезает», A не появляется: состояние БД байт-в-байт то же.
        """
        _seed_position(db, 'acc-b', 'RU000A0BBBB2')
        db.upsert_cash_balances([_balance('acc-b', money='500')])
        positions_before = _position_keys(db)
        cash_before = db.get_available_cash_rub()

        client = _FakeTbankClient(account_ids=['acc-1', 'acc-b'])
        use_case = SyncPortfolioUseCase(_FakeFactory(db, client))
        with pytest.raises(ValueError):
            use_case.execute(_make_sync_args())

        assert _position_keys(db) == positions_before
        assert db.get_available_cash_rub() == cash_before

    def test_clear_and_add_not_executed_on_failed_preflight(self, db, fake_tbank, monkeypatch):
        """sync-portfolio --source=tbank: failed preflight — clear/add не выполняются."""
        _seed_position(db, 'acc-b', 'RU000A0BBBB2')
        db.upsert_cash_balances([_balance('acc-b', money='500')])

        db_calls = []
        for method in (
            'clear_portfolio_positions',
            'clear_portfolio_positions_by_broker',
            'add_portfolio_positions',
            'upsert_cash_balances',
            'delete_cash_accounts_not_in',
            'replace_cash_balances_snapshot',
        ):
            monkeypatch.setattr(
                db, method,
                lambda *a, _m=method, **kw: db_calls.append(_m),
            )

        client = _FakeTbankClient(account_ids=['acc-1', 'acc-b'])
        use_case = SyncPortfolioUseCase(_FakeFactory(db, client))
        with pytest.raises(ValueError):
            use_case.execute(_make_sync_args())

        assert db_calls == [], (
            f"При failed preflight не должно быть ни одной записи в БД, "
            f"было: {db_calls}"
        )

    def test_cash_preflight_failure_aborts_sync_before_positions_write(self, db, fake_tbank):
        """Race: positions preflight прошёл, cash preflight упал -> sync прерван.

        ValueError из cash path не глотается в «cash не обновлён» (022), а
        прерывает sync ДО clear/add positions (029-T01).
        """
        _seed_position(db, 'acc-b', 'RU000A0BBBB2')
        db.upsert_cash_balances([_balance('acc-b', money='500')])
        before = db.get_available_cash_rub()

        client = _FakeTbankClient(account_ids=['acc-1'])
        client.get_money_positions = lambda: (_ for _ in ()).throw(
            ValueError("strict preflight: acc-b"))
        use_case = SyncPortfolioUseCase(_FakeFactory(db, client))
        with pytest.raises(ValueError):
            use_case.execute(_make_sync_args())

        assert ('RU000A0BBBB2', 'TBank', 'acc-b') in _position_keys(db)
        assert db.get_available_cash_rub() == before


# ---------------------------------------------------------------------------
# Unit sync-потока без БД: in-memory замена хранилища (те же гарантии
# «нет clear/add при failed preflight», проверяются локально без тестовой БД)
# ---------------------------------------------------------------------------

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


class TestSyncPreflightFailFastUnit:
    def _seed(self, storage):
        _seed_position_storage(storage, 'acc-b', 'RU000A0BBBB2')
        storage.upsert_cash_balances([_balance('acc-b', money='500')])
        storage.calls = []

    def test_missing_one_of_many_no_clear_add_and_no_cash_publish(self, fake_tbank):
        """Case D2 (unit): failed preflight — ни одной мутирующей записи в БД."""
        storage = _FakeStorage()
        self._seed(storage)

        client = _FakeTbankClient(account_ids=['acc-1', 'acc-b'])
        use_case = SyncPortfolioUseCase(_FakeFactory(storage, client))
        with pytest.raises(ValueError) as exc:
            use_case.execute(_make_sync_args())
        assert 'acc-b' in str(exc.value)

        assert storage.calls == [], (
            f"Failed preflight не должен писать в БД, было: {storage.calls}"
        )
        assert [(p.isin, p.account_id) for p in storage.positions] == [
            ('RU000A0BBBB2', 'acc-b'),
        ], "Позиции счёта B обязаны остаться в последнем успешном snapshot"
        assert set(storage.cash) == {('acc-b', 'rub')}, (
            "Строки cash счёта B не удаляются из последнего успешного snapshot"
        )

    def test_cash_phase_preflight_failure_aborts_before_positions_write(self, fake_tbank):
        """Cash preflight упал после успешного positions fetch -> sync прерван."""
        storage = _FakeStorage()
        self._seed(storage)

        client = _FakeTbankClient(account_ids=['acc-1'])
        client.get_money_positions = lambda: (_ for _ in ()).throw(
            ValueError("strict preflight: acc-b"))
        use_case = SyncPortfolioUseCase(_FakeFactory(storage, client))
        with pytest.raises(ValueError):
            use_case.execute(_make_sync_args())

        assert storage.calls == []
        assert [(p.isin, p.account_id) for p in storage.positions] == [
            ('RU000A0BBBB2', 'acc-b'),
        ]
        assert set(storage.cash) == {('acc-b', 'rub')}


def _seed_position_storage(storage, account_id: str, isin: str):
    storage.positions.append(PortfolioPosition(
        isin=isin,
        ticker=f'T{isin[-4:]}',
        name='Test Bond',
        broker_name='TBank',
        account_id=account_id,
        quantity=Decimal('10'),
        average_price=Decimal('100'),
        current_price=Decimal('100'),
        current_value=Decimal('1000'),
    ))
