"""
Интеграционные тесты хранения и пересчёта YTM в PostgreSQL (007_ytm_storage).
"""

from datetime import date, timedelta
from decimal import Decimal
import pytest

from src.data_models import Bond


def test_migration_008_columns_and_dto(db):
    """Проверяет наличие колонок ytm, ytm_null_reason, ytm_updated_at в bonds_catalog и в Bond DTO."""
    cursor = db._cursor()
    cursor.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'bonds_catalog'
          AND column_name IN ('ytm', 'ytm_null_reason', 'ytm_updated_at')
    """)
    cols = {row['column_name']: row['data_type'] for row in cursor.fetchall()}
    assert 'ytm' in cols, "ytm column missing in bonds_catalog"
    assert 'ytm_null_reason' in cols, "ytm_null_reason column missing in bonds_catalog"
    assert 'ytm_updated_at' in cols, "ytm_updated_at column missing in bonds_catalog"

    bond = Bond(
        isin='RU000TESTYTM0',
        ticker='TEST0',
        name='Test YTM DTO',
        currency='RUB',
        ytm=Decimal('0.152500'),
        ytm_null_reason=None,
    )
    assert bond.ytm == Decimal('0.152500')
    assert bond.ytm_null_reason is None
