"""
Общие фикстуры pytest: хранилище на тестовой базе PostgreSQL.

DSN берётся из POSTGRES_DSN_TEST (.env). Миграции применяются один раз за
сессию (apply_migrations идемпотентен); пользовательские таблицы очищаются
между тестами (TRUNCATE ... RESTART IDENTITY CASCADE).
"""
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from src.migrations import apply_migrations
from src.storage import PortfolioStorage

# Пользовательские таблицы схемы (миграции 001-005, 015) для очистки между тестами
_TABLES = (
    'bonds_catalog',
    'companies',
    'rating_history',
    'risk_history',
    'listlevel_history',
    'calculated_coupons',
    'portfolio_positions',
    'liquidity_history',
    'monitoring_checks',
    'portfolio_cash_balances',
)


@pytest.fixture(scope='session')
def db() -> PortfolioStorage:
    """Хранилище на тестовой БД: один коннект на сессию, схема из миграций."""
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    dsn = os.getenv('POSTGRES_DSN_TEST', '')
    if not dsn:
        pytest.skip('POSTGRES_DSN_TEST не задан — тесты хранилища пропущены.')
    apply_migrations(dsn)
    storage = PortfolioStorage(dsn)
    yield storage
    storage.close()


@pytest.fixture(autouse=True)
def _clean_tables(request):
    """Чистая база перед каждым тестом, использующим хранилище.

    Активируется только для тестов, которые запрашивают фикстуру db:
    чистые юнит-тесты без БД не зависят от POSTGRES_DSN_TEST (не
    пропускаются, если он не задан) и очистки не требуют.
    """
    if 'db' not in request.fixturenames:
        yield
        return
    storage = request.getfixturevalue('db')
    with storage.conn.cursor() as cursor:
        for table in _TABLES:
            cursor.execute(f'TRUNCATE TABLE {table} RESTART IDENTITY CASCADE')
    storage.conn.commit()
    yield
