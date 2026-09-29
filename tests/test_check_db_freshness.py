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
    assert freshness['portfolio_zero_value_positions'] == 0
    assert freshness['portfolio_zero_rows_suspicious'] == 0
    assert freshness['portfolio_zero_value_isins'] is None
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
    # 5. Нет рыночной цены (market_price IS NULL), торгуется -> ухудшает
    #    bonds_tradeable_stale_or_missing (торгуемая облигация без цены)
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
    assert freshness['bonds_tradeable_count'] == 4  # A1, A2, A3, A5 (A4 не торгуется)
    # RU000A2 (устаревшая цена), RU000A3 (NULL timestamp) и RU000A5 — торговая
    # облигация без market_price тоже должна ухудшать метрику
    assert freshness['bonds_tradeable_stale_or_missing'] == 3
    assert freshness['max_market_price_time'] == fresh_time
    assert freshness['portfolio_positions_rows'] == 1
    assert freshness['portfolio_max_updated_at'] == fresh_time
    assert freshness['last_sync_time'] == fresh_time
    assert freshness['portfolio_zero_value_positions'] == 1  # позиция без current_value
    # 002.1: RU000A1 не погашена (maturity 2030) → zero-строка подозрительная
    assert freshness['portfolio_zero_rows_suspicious'] == 1
    assert freshness['portfolio_zero_value_isins'] == 'RU000A1'
    assert freshness['portfolio_total_value'] == Decimal(0)
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
        'status',
        'gate_status',
        'is_fresh',
        'issues',
        'bonds_catalog_rows',
        'bonds_catalog_max_updated_at',
        'bonds_with_market_price',
        'market_price_max_updated_at',
        'bonds_with_stale_market_price',
        'bonds_tradeable_count',
        'bonds_tradeable_stale_or_missing',
        'portfolio_positions_rows',
        'portfolio_zero_value_positions',
        'portfolio_zero_rows_suspicious',
        'portfolio_zero_rows_isins',
        'portfolio_total_value',
        'portfolio_max_updated_at',
        'monitoring_checks_max_check_date',
    }
    assert set(data.keys()) == expected_keys
    assert data['bonds_catalog_rows'] == 1
    assert data['monitoring_checks_max_check_date'] == '2026-09-28'
    assert isinstance(data['generated_at'], str)
    assert '+' in data['generated_at'] or data['generated_at'].endswith('Z')
    # Пустой каталог цен -> гейт не пропускает
    assert data['bonds_with_market_price'] == 0
    assert data['is_fresh'] is False
    assert data['status'] == 'CRITICAL'
    # Канонический статус гейта v2 (артефакт A1) согласован с SQL-точкой:
    # портфель пуст → первый порядок проверок A1 даёт EMPTY_PORTFOLIO
    assert data['gate_status'] == 'EMPTY_PORTFOLIO'
    assert data['issues']


def test_freshness_gate_empty_price_catalog_is_critical(db, capsys):
    """Инцидент 2026-09-29: пустой каталог цен (bonds_with_market_price=0) —
    это CRITICAL и is_fresh=false, а не «0 устаревших из 0» = OK."""
    db.add_bonds_to_catalog([make_bond('RU000B1', 'Бонд Без Цены')])
    now = datetime.now(timezone.utc)
    with db.conn.cursor() as cursor:
        cursor.execute(
            "UPDATE bonds_catalog SET is_trade_available = TRUE WHERE isin = 'RU000B1'"
        )
        cursor.execute(
            """
            INSERT INTO portfolio_positions (isin, broker_name, name, quantity, current_price, current_value, updated_at)
            VALUES ('RU000B1', 'tbank', 'Бонд Без Цены', 5, 990.0, 4950.0, %s)
            """,
            (now,),
        )
    db.conn.commit()

    use_case = CheckDbUseCase(config={}, db=db)

    class Args:
        freshness = True
        json = True
        query = None
        limit = 10
        fail_stale = False

    use_case.execute(Args())

    data = json.loads(capsys.readouterr().out.strip())
    assert data['bonds_with_market_price'] == 0
    assert data['bonds_tradeable_stale_or_missing'] == 1  # торгуемая облигация без цены
    assert data['portfolio_zero_value_positions'] == 0
    assert data['is_fresh'] is False
    assert data['status'] == 'CRITICAL'
    # 002.1: канонический статус v2 согласован с SQL-гейтом (A1)
    assert data['gate_status'] == 'CRITICAL_NO_PRICES'
    assert any('рыночных цен пуст' in issue for issue in data['issues'])


def test_fail_stale_exits_with_code_1_when_not_fresh(db):
    """--fail-stale при is_fresh=false завершает процесс с SystemExit(1)."""
    use_case = CheckDbUseCase(config={}, db=db)  # пустая БД -> is_fresh=false

    class Args:
        freshness = True
        json = True
        query = None
        limit = 10
        fail_stale = True

    with pytest.raises(SystemExit) as excinfo:
        use_case.execute(Args())
    assert excinfo.value.code == 1


def test_cli_freshness_contracts(db):
    python_bin = sys.executable
    env = os.environ.copy()
    env['PYTHONPATH'] = '.'
    # Подпроцесс main.py должен работать с той же тестовой БД, что и фикстура:
    # load_dotenv не перекрывает уже выставленные переменные окружения, поэтому
    # явный POSTGRES_DSN защищает и от случайного выхода на боевой DSN из .env
    # (fail-fast по версии миграций ловит расхождение, но лучше не ходить туда вовсе).
    test_dsn = env.get('POSTGRES_DSN_TEST')
    assert test_dsn, "POSTGRES_DSN_TEST не задан — CLI-тест не может идти на тестовую БД"
    env['POSTGRES_DSN'] = test_dsn

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


# ---------------------------------------------------------------------------
# Гейт v2 (задача 002.1): зомби-строки не блокируют ребалансировку
# ---------------------------------------------------------------------------

def _make_fresh_catalog(db, isin: str, maturity: date):
    """Торгуемая бумага со свежей ценой — чтобы гейт дошёл до проверки zero-строк."""
    db.add_bonds_to_catalog([make_bond(isin, f'Бонд {isin}')])
    now = datetime.now(timezone.utc)
    with db.conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE bonds_catalog
            SET is_trade_available = TRUE, market_price = 1000.0, market_price_updated_at = %s
            WHERE isin = %s
            """,
            (now, isin),
        )


def test_zombie_rows_are_warning_not_critical(db, capsys):
    """002.1, кейс сессии 2026-09-29: погашенная МОНОП (zero-строка) +
    ГлобалФ (цена 0 от брокера) → WARN_ZOMBIE_ROWS, is_fresh=true,
    ручной разбор не требуется."""
    now = datetime.now(timezone.utc)
    # Погашенная бумага с zero-оценкой (МОНОП 1P02)
    db.add_bonds_to_catalog([Bond(
        isin='RU000A10AA02', name='МОНОП 1P02', nominal=Decimal(1000),
        maturity_date=date(2025, 12, 4), currency='RUB',
    )])
    # Живая бумага, брокер дал цену 0 (ГлобалФ 1P5)
    _make_fresh_catalog(db, 'RU000A108VZ0', maturity=date(2028, 3, 14))
    with db.conn.cursor() as cursor:
        cursor.execute(
            "UPDATE bonds_catalog SET market_price = 0 WHERE isin = 'RU000A108VZ0'"
        )
        cursor.execute(
            """
            INSERT INTO portfolio_positions (isin, broker_name, account_id, quantity, current_price, current_value, updated_at)
            VALUES
              ('RU000A10AA02', 'TBank', 't1', 35, 0, 0, %s),
              ('RU000A108VZ0', 'TBank', 't1', 5, 0, 0, %s),
              ('RU000A108VZ0', 'Alor', 'a1', 2, 0, 0, %s)
            """,
            (now, now, now),
        )
    db.conn.commit()

    use_case = CheckDbUseCase(config={}, db=db)

    class Args:
        freshness = True
        json = True
        query = None
        limit = 10
        fail_stale = False

    use_case.execute(Args())
    data = json.loads(capsys.readouterr().out.strip())

    assert data['portfolio_positions_rows'] == 3
    assert data['portfolio_zero_value_positions'] == 3
    # Погашенная МОНОП не подозрительная, ГлобалФ (2 строки) — подозрительные
    assert data['portfolio_zero_rows_suspicious'] == 2
    assert data['portfolio_zero_rows_isins'] == 'RU000A108VZ0;RU000A10AA02'
    # Главное: зомби-строки не дают ложного CRITICAL и не ломают is_fresh
    assert data['gate_status'] == 'WARN_ZOMBIE_ROWS'
    assert data['status'] == 'WARNING'
    assert data['is_fresh'] is True


def test_more_than_two_suspicious_zero_rows_is_critical(db, capsys):
    """002.1: >2 zero-строк на НЕпогашенных бумагах — массовый сбой синка
    (класс бага из 001) → CRITICAL_ZERO_VALUATION, is_fresh=false."""
    now = datetime.now(timezone.utc)
    for i in (1, 2, 3):
        _make_fresh_catalog(db, f'RU000B000000{i}', maturity=date(2030, 1, 1))
    with db.conn.cursor() as cursor:
        for i in (1, 2, 3):
            cursor.execute(
                """
                INSERT INTO portfolio_positions (isin, broker_name, account_id, quantity, current_price, current_value, updated_at)
                VALUES (%s, 'TBank', 't1', 10, 0, 0, %s)
                """,
                (f'RU000B000000{i}', now),
            )
    db.conn.commit()

    use_case = CheckDbUseCase(config={}, db=db)

    class Args:
        freshness = True
        json = True
        query = None
        limit = 10
        fail_stale = False

    use_case.execute(Args())
    data = json.loads(capsys.readouterr().out.strip())

    assert data['portfolio_zero_value_positions'] == 3
    assert data['portfolio_zero_rows_suspicious'] == 3
    assert data['gate_status'] == 'CRITICAL_ZERO_VALUATION'
    assert data['status'] == 'CRITICAL'
    assert data['is_fresh'] is False
