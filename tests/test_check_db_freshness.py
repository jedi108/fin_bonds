import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.data_models import Bond
from src.use_cases.check_db import CheckDbUseCase


def make_bond(isin: str, name: str) -> Bond:
    return Bond(
        isin=isin,
        name=name,
        nominal=Decimal(1000),
        maturity_date=date(2030, 1, 1),
        currency='RUB',
    )


def test_freshness_empty_db(db):
    freshness = db.get_db_freshness()
    assert freshness['bonds_catalog_rows'] == 0
    assert freshness['bonds_catalog_max_updated_at'] is None
    assert freshness['bonds_with_market_price'] == 0
    assert freshness['market_price_max_updated_at'] is None
    assert freshness['bonds_with_stale_market_price'] == 0
    assert freshness['portfolio_positions_rows'] == 0
    assert freshness['portfolio_max_updated_at'] is None
    assert freshness['monitoring_checks_max_check_date'] is None


def test_freshness_with_data_fresh_stale_and_null(db):
    now = datetime.now(timezone.utc)
    fresh_time = now - timedelta(hours=2)
    stale_time = now - timedelta(hours=25)

    # 1. Свежая цена, торгуется
    db.add_bonds_to_catalog([make_bond('RU000A1', 'Бонд Свежий')])
    # 2. Устаревшая цена (> 24ч), торгуется
    db.add_bonds_to_catalog([make_bond('RU000A2', 'Бонд Устаревший')])
    # 3. NULL время обновления цены, есть market_price, торгуется -> считается устаревшей
    db.add_bonds_to_catalog([make_bond('RU000A3', 'Бонд Без Времени Цены')])
    # 4. Устаревшая цена, но НЕ торгуется (is_trade_available = False) -> НЕ должна считаться устаревшей
    db.add_bonds_to_catalog([make_bond('RU000A4', 'Бонд Неторгуемый')])
    # 5. Нет рыночной цены (market_price IS NULL), торгуется -> НЕ считается устаревшей ценой
    db.add_bonds_to_catalog([make_bond('RU000A5', 'Бонд Без Цены')])

    with db.conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE bonds_catalog
            SET market_price = 101.5, market_price_updated_at = %s, is_trade_available = TRUE, updated_at = %s
            WHERE isin = 'RU000A1'
            """,
            (fresh_time, fresh_time),
        )
        cursor.execute(
            """
            UPDATE bonds_catalog
            SET market_price = 99.0, market_price_updated_at = %s, is_trade_available = TRUE, updated_at = %s
            WHERE isin = 'RU000A2'
            """,
            (stale_time, stale_time),
        )
        cursor.execute(
            """
            UPDATE bonds_catalog
            SET market_price = 95.0, market_price_updated_at = NULL, is_trade_available = TRUE
            WHERE isin = 'RU000A3'
            """,
        )
        cursor.execute(
            """
            UPDATE bonds_catalog
            SET market_price = 90.0, market_price_updated_at = %s, is_trade_available = FALSE
            WHERE isin = 'RU000A4'
            """,
            (stale_time,),
        )
        cursor.execute(
            """
            UPDATE bonds_catalog
            SET market_price = NULL, market_price_updated_at = NULL, is_trade_available = TRUE
            WHERE isin = 'RU000A5'
            """,
        )

        # Позиции портфеля
        cursor.execute(
            """
            INSERT INTO portfolio_positions (isin, broker_name, name, quantity, current_price, updated_at)
            VALUES ('RU000A1', 'tbank', 'Бонд Свежий', 10, 1015, %s)
            """,
            (fresh_time,),
        )

        # Мониторинг проверки
        check_d = date(2026, 9, 28)
        cursor.execute(
            """
            INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value)
            VALUES ('RU000A1', %s, 'rating', 'OK')
            """,
            (check_d,),
        )
    db.conn.commit()

    freshness = db.get_db_freshness()

    assert freshness['bonds_catalog_rows'] == 5
    assert freshness['bonds_catalog_max_updated_at'] is not None
    assert freshness['bonds_with_market_price'] == 4  # RU000A1, A2, A3, A4
    assert freshness['market_price_max_updated_at'] == fresh_time
    assert freshness['bonds_with_stale_market_price'] == 2  # RU000A2 (stale) и RU000A3 (NULL timestamp)
    assert freshness['portfolio_positions_rows'] == 1
    assert freshness['portfolio_max_updated_at'] == fresh_time
    assert freshness['monitoring_checks_max_check_date'] == check_d


def test_use_case_freshness_json_output(db, capsys):
    # Добавляем одну запись для проверки
    db.add_bonds_to_catalog([make_bond('RU000A1', 'Бонд 1')])
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value) "
            "VALUES ('RU000A1', '2026-09-28', 'rating', 'OK')"
        )
    db.conn.commit()

    use_case = CheckDbUseCase(config={}, db=db)
    
    class Args:
        freshness = True
        json = True
        query = None
        limit = 10

    use_case.execute(Args())

    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1, f"Ожидалась ровно одна JSON-строка, получено: {lines}"
    
    data = json.loads(lines[0])
    expected_keys = {
        'generated_at',
        'bonds_catalog_rows',
        'bonds_catalog_max_updated_at',
        'bonds_with_market_price',
        'market_price_max_updated_at',
        'bonds_with_stale_market_price',
        'portfolio_positions_rows',
        'portfolio_max_updated_at',
        'monitoring_checks_max_check_date',
    }
    assert set(data.keys()) == expected_keys
    assert data['bonds_catalog_rows'] == 1
    assert data['monitoring_checks_max_check_date'] == '2026-09-28'
    assert isinstance(data['generated_at'], str)
    assert '+' in data['generated_at'] or data['generated_at'].endswith('Z')


def test_cli_freshness_contracts(db):
    python_bin = sys.executable
    env = os.environ.copy()
    env['PYTHONPATH'] = '.'

    # 1. Успешный запуск --freshness --json: rc == 0, stdout ровно валидный JSON
    res = subprocess.run(
        [python_bin, 'main.py', 'check-db', '--freshness', '--json'],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, f"Stderr: {res.stderr}"
    lines = res.stdout.strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert 'generated_at' in data
    assert 'bonds_catalog_rows' in data

    # 2. Недопустимые аргументы: --freshness вместе с --query
    res_err1 = subprocess.run(
        [python_bin, 'main.py', 'check-db', '--freshness', '--query', 'SELECT 1'],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_err1.returncode != 0
    assert res_err1.stdout == ""
    assert res_err1.stderr != ""

    # 3. Недопустимые аргументы: --json без --freshness
    res_err2 = subprocess.run(
        [python_bin, 'main.py', 'check-db', '--json'],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_err2.returncode != 0
    assert res_err2.stdout == ""
    assert res_err2.stderr != ""
