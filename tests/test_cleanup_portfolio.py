"""
Тесты задачи 002.1: зомби-позиции не блокируют ребалансировку.

Покрывает:
- миграция 010: колонка portfolio_positions.status (active|matured|closed),
  CHECK-домен, бэкфилл;
- view-guard: nominal-фолбэк не применяется к погашенным бумагам;
- storage.find_zombies / mark_matured_positions / mark_closed_positions;
- CleanupPortfolioUseCase: --dry-run ничего не меняет, без него — помечает;
- гейт-поля get_db_freshness (подозрительные = НЕпогашенные).
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from src.data_models import Bond, PortfolioPosition
from src.storage import (
    POSITION_STATUS_ACTIVE,
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_MATURED,
)
from src.use_cases.cleanup_portfolio import CleanupPortfolioUseCase


def make_bond(isin: str, maturity: date) -> Bond:
    return Bond(
        isin=isin,
        name=f'Бонд {isin}',
        nominal=Decimal(1000),
        maturity_date=maturity,
        currency='RUB',
    )


def add_catalog_row(db, isin: str, maturity: date):
    db.add_bonds_to_catalog([make_bond(isin, maturity)])


def insert_position(db, isin: str, broker: str, account: str, qty: str, value: str, price: str = '0'):
    with db.conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO portfolio_positions
              (isin, broker_name, account_id, quantity, current_price, current_value)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (isin, broker, account, Decimal(qty), Decimal(price), Decimal(value)),
        )


class TestMigration010Status:
    """Колонка status и её домен (миграция 010)."""

    def test_status_column_exists_with_default_active(self, db):
        insert_position(db, 'RU000A0STAT01', 'TBank', 't1', '5', '100')
        with db.conn.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM portfolio_positions WHERE isin = 'RU000A0STAT01'"
            )
            assert cursor.fetchone()[0] == POSITION_STATUS_ACTIVE

    def test_status_domain_is_constrained(self, db):
        from psycopg2 import errors as pg_errors
        with pytest.raises(pg_errors.CheckViolation):
            with db.conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO portfolio_positions (isin, broker_name, account_id, status)
                    VALUES ('RU000A0STAT02', 'TBank', 't1', 'zombie')
                    """
                )
        db.conn.rollback()


class TestViewValuationGuard:
    """Nominal-фолбэк не даёт фантомной стоимости погашенным бумагам (002.1 п.2)."""

    def test_matured_position_gets_no_nominal_fallback(self, db):
        add_catalog_row(db, 'RU000A0MTRD01', date(2025, 12, 4))
        insert_position(db, 'RU000A0MTRD01', 'TBank', 't1', '35', '0')
        with db.conn.cursor() as cursor:
            cursor.execute(
                "SELECT position_value_rub FROM v_portfolio_positions_valuation "
                "WHERE isin = 'RU000A0MTRD01'"
            )
            # Кейс МОНОП 1P02: 35 шт * 1000 номинал = 35000 фантомной стоимости
            assert cursor.fetchone()[0] == Decimal('0.00')

    def test_unmatured_position_keeps_nominal_fallback(self, db):
        add_catalog_row(db, 'RU000A0LIVE01', date(2030, 1, 1))
        insert_position(db, 'RU000A0LIVE01', 'TBank', 't1', '5', '0')
        with db.conn.cursor() as cursor:
            cursor.execute(
                "SELECT position_value_rub, position_status "
                "FROM v_portfolio_positions_valuation WHERE isin = 'RU000A0LIVE01'"
            )
            row = cursor.fetchone()
            assert row[0] == Decimal('5000.00')  # легитимный фолбэк по номиналу
            assert row[1] == POSITION_STATUS_ACTIVE

    def test_position_status_exposed_in_view(self, db):
        add_catalog_row(db, 'RU000A0VIEW01', date(2025, 6, 1))
        insert_position(db, 'RU000A0VIEW01', 'TBank', 't1', '1', '100')
        db.mark_matured_positions()
        with db.conn.cursor() as cursor:
            cursor.execute(
                "SELECT position_status FROM v_portfolio_positions_valuation "
                "WHERE isin = 'RU000A0VIEW01'"
            )
            assert cursor.fetchone()[0] == POSITION_STATUS_MATURED


class TestStorageZombieMethods:
    def test_mark_matured_positions(self, db):
        add_catalog_row(db, 'RU000A0MTRD02', date(2025, 1, 1))   # погашена
        add_catalog_row(db, 'RU000A0LIVE02', date(2030, 1, 1))   # живая
        insert_position(db, 'RU000A0MTRD02', 'TBank', 't1', '35', '0')
        insert_position(db, 'RU000A0LIVE02', 'TBank', 't1', '5', '1000')

        marked = db.mark_matured_positions()

        assert [(m['isin'], m['broker_name'], m['account_id']) for m in marked] == \
            [('RU000A0MTRD02', 'TBank', 't1')]
        with db.conn.cursor() as cursor:
            cursor.execute("SELECT isin, status FROM portfolio_positions ORDER BY isin")
            statuses = dict(cursor.fetchall())
        assert statuses['RU000A0MTRD02'] == POSITION_STATUS_MATURED
        assert statuses['RU000A0LIVE02'] == POSITION_STATUS_ACTIVE

        # Повторный вызов — idempotent, ничего не помечает
        assert db.mark_matured_positions() == []

    def test_mark_closed_positions_zero_quantity_and_absent(self, db):
        insert_position(db, 'RU000A0ZERO01', 'TBank', 't1', '0', '0')       # нулевое qty
        insert_position(db, 'RU000A0GONE01', 'TBank', 't1', '5', '500')     # нет у брокера
        insert_position(db, 'RU000A0GONE02', 'Fake', 'a1', '3', '300')      # другой брокер
        insert_position(db, 'RU000A0KEEP01', 'TBank', 't1', '5', '500')     # у брокера есть

        keep = {
            ('RU000A0GONE02', 'Fake', 'a1'),
            ('RU000A0KEEP01', 'TBank', 't1'),
        }
        closed = db.mark_closed_positions(keep_keys=keep, brokers=['TBank'])

        by_isin = {row['isin']: row for row in closed}
        assert set(by_isin) == {'RU000A0ZERO01', 'RU000A0GONE01'}
        assert by_isin['RU000A0ZERO01']['reason'] == 'zero_quantity'
        assert by_isin['RU000A0GONE01']['reason'] == 'absent_at_broker'
        with db.conn.cursor() as cursor:
            cursor.execute("SELECT isin, status FROM portfolio_positions ORDER BY isin")
            statuses = dict(cursor.fetchall())
        assert statuses['RU000A0ZERO01'] == POSITION_STATUS_CLOSED
        assert statuses['RU000A0GONE01'] == POSITION_STATUS_CLOSED
        assert statuses['RU000A0GONE02'] == POSITION_STATUS_ACTIVE  # брокер не в области
        assert statuses['RU000A0KEEP01'] == POSITION_STATUS_ACTIVE

    def test_mark_closed_positions_without_keep_keys_only_zero_qty(self, db):
        insert_position(db, 'RU000A0ZERO02', 'TBank', 't1', '0', '0')
        insert_position(db, 'RU000A0KEEP02', 'TBank', 't1', '5', '500')
        closed = db.mark_closed_positions()
        assert [row['isin'] for row in closed] == ['RU000A0ZERO02']

    def test_find_zombies_categories(self, db):
        # matured: погашенная бумага с ненулевым qty (кейс МОНОП)
        add_catalog_row(db, 'RU000A0ZMB01', date(2025, 12, 4))
        # zero_value_live: живая бумага, цена 0 от брокера (кейс ГлобалФ)
        add_catalog_row(db, 'RU000A0ZMB02', date(2028, 3, 14))
        # ничего подозрительного
        add_catalog_row(db, 'RU000A0OK001', date(2030, 1, 1))
        insert_position(db, 'RU000A0ZMB01', 'TBank', 't1', '35', '0')
        insert_position(db, 'RU000A0ZMB02', 'TBank', 't1', '5', '0')
        insert_position(db, 'RU000A0ZMB02', 'Fake', 'a1', '2', '0')
        insert_position(db, 'RU000A0OK001', 'TBank', 't1', '1', '1000')
        insert_position(db, 'RU000A0ZMB03', 'TBank', 't1', '0', '0')  # вне каталога, qty 0

        zombies = db.find_zombies()

        assert [z['isin'] for z in zombies['matured']] == ['RU000A0ZMB01']
        assert [z['isin'] for z in zombies['zero_quantity']] == ['RU000A0ZMB03']
        assert {(z['isin'], z['account_id']) for z in zombies['zero_value_live']} == \
            {('RU000A0ZMB02', 't1'), ('RU000A0ZMB02', 'a1')}

    def test_position_status_roundtrip_dto(self, db):
        add_catalog_row(db, 'RU000A0RT001', date(2025, 6, 1))
        pos = PortfolioPosition(
            isin='RU000A0RT001', broker_name='TBank', account_id='t1',
            quantity=Decimal('5'), current_price=Decimal('100'),
            current_value=Decimal('500'),
        )
        db.add_portfolio_positions([pos])
        saved = db.get_portfolio_position_by_isin('RU000A0RT001')
        assert saved is not None
        assert saved.status == POSITION_STATUS_ACTIVE
        # После пометки matured статус виден и через DTO (SELECT p.status)
        db.mark_matured_positions()
        assert db.get_portfolio_position_by_isin('RU000A0RT001').status == POSITION_STATUS_MATURED


class TestCleanupPortfolioUseCase:
    def _make_use_case(self, db):
        return CleanupPortfolioUseCase(config={}, db=db)

    def test_dry_run_changes_nothing(self, db, capsys):
        add_catalog_row(db, 'RU000A0DRY01', date(2025, 1, 1))
        insert_position(db, 'RU000A0DRY01', 'TBank', 't1', '35', '0')
        insert_position(db, 'RU000A0DRY02', 'TBank', 't1', '0', '0')

        use_case = self._make_use_case(db)

        class Args:
            dry_run = True

        use_case.execute(Args())
        out = capsys.readouterr().out
        assert 'DRY-RUN' in out

        with db.conn.cursor() as cursor:
            cursor.execute("SELECT isin, status FROM portfolio_positions ORDER BY isin")
            statuses = dict(cursor.fetchall())
        assert statuses == {
            'RU000A0DRY01': POSITION_STATUS_ACTIVE,
            'RU000A0DRY02': POSITION_STATUS_ACTIVE,
        }

    def test_apply_marks_matured_and_closed(self, db, capsys):
        add_catalog_row(db, 'RU000A0CLN01', date(2025, 1, 1))
        insert_position(db, 'RU000A0CLN01', 'TBank', 't1', '35', '0')
        insert_position(db, 'RU000A0CLN02', 'TBank', 't1', '0', '0')
        # Живая позиция с нулевой оценкой (ГлобалФ) — не зомби, не трогается
        add_catalog_row(db, 'RU000A0CLN03', date(2028, 3, 14))
        insert_position(db, 'RU000A0CLN03', 'TBank', 't1', '5', '0')

        use_case = self._make_use_case(db)

        class Args:
            dry_run = False

        use_case.execute(Args())
        out = capsys.readouterr().out
        assert "status='closed': 1" in out
        assert "status='matured': 1" in out

        with db.conn.cursor() as cursor:
            cursor.execute("SELECT isin, status FROM portfolio_positions ORDER BY isin")
            statuses = dict(cursor.fetchall())
        assert statuses == {
            'RU000A0CLN01': POSITION_STATUS_MATURED,
            'RU000A0CLN02': POSITION_STATUS_CLOSED,
            'RU000A0CLN03': POSITION_STATUS_ACTIVE,  # не зомби
        }


class TestFreshnessSuspiciousField:
    """get_db_freshness: подозрительные = zero-строки на НЕпогашенных бумагах."""

    def test_matured_zero_rows_not_suspicious(self, db):
        now = datetime.now(timezone.utc)
        add_catalog_row(db, 'RU000A0FRS01', date(2025, 1, 1))   # погашена
        add_catalog_row(db, 'RU000A0FRS02', date(2030, 1, 1))   # живая
        insert_position(db, 'RU000A0FRS01', 'TBank', 't1', '35', '0')
        insert_position(db, 'RU000A0FRS02', 'Fake', 'a1', '2', '0')
        with db.conn.cursor() as cursor:
            cursor.execute(
                "UPDATE portfolio_positions SET updated_at = %s", (now,)
            )
        db.conn.commit()

        freshness = db.get_db_freshness()
        assert freshness['portfolio_zero_value_positions'] == 2
        assert freshness['portfolio_zero_rows_suspicious'] == 1  # только живая
        assert freshness['portfolio_zero_value_isins'] == 'RU000A0FRS01;RU000A0FRS02'
