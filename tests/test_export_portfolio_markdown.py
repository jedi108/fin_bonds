import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.data_models import Bond
from src.use_cases.export_portfolio import (
    ExportPortfolioUseCase,
    _format_timestamp,
    _render_markdown,
)


# ==============================================================================
# 1. Pure Unit Tests for _render_markdown
# ==============================================================================


def test_format_timestamp_helper():
    assert _format_timestamp(None) == "—"
    assert _format_timestamp("") == "—"
    dt = datetime(2026, 9, 29, 10, 15, 30)
    assert _format_timestamp(dt) == "2026-09-29 10:15:30"
    d = date(2026, 9, 29)
    assert _format_timestamp(d) == "2026-09-29"


def parse_md_table(md: str):
    table_lines = [l for l in md.splitlines() if l.startswith("|")]
    parsed = []
    for l in table_lines:
        cells = [c.strip() for c in l.split("|")[1:-1]]
        if cells and not all(set(c) <= {"-", ":"} for c in cells):
            parsed.append(cells)
    headers = parsed[0] if parsed else []
    rows = parsed[1:] if len(parsed) > 1 else []
    return headers, rows


def test_render_markdown_without_isin():
    gen_at = datetime(2026, 9, 29, 12, 0, 0)
    pos_dt = datetime(2026, 9, 29, 11, 0, 0)
    market_dt = datetime(2026, 9, 29, 11, 30, 0)
    ytm_dt = datetime(2026, 9, 29, 11, 45, 0)
    freshness = (pos_dt, market_dt, ytm_dt)

    rows = [
        {
            "isin": "RU000A101AAA",
            "name": "ОФЗ 26238",
            "quantity": Decimal("100"),
            "current_price": Decimal("540.20"),
            "total_value": Decimal("54020.00"),
            "ytm": Decimal("16.50"),
        },
        {
            "isin": "RU000A102BBB",
            "name": "Газпром Капитал БО-002Р-01",
            "quantity": Decimal("50"),
            "current_price": Decimal("919.60"),
            "total_value": Decimal("45980.00"),
            "ytm": None,  # NULL YTM
        },
    ]

    md = _render_markdown(rows, freshness, generated_at=gen_at, with_isin=False)

    # 1. Заголовок H1 и метки свежести
    assert "# Экспорт портфеля на 2026-09-29 12:00:00" in md
    assert "- Позиции актуальны на: 2026-09-29 11:00:00" in md
    assert "- Каталожные рыночные данные актуальны на: 2026-09-29 11:30:00" in md
    assert "- YTM рассчитана на: 2026-09-29 11:45:00" in md

    # 2. Заголовок таблицы и строки
    headers, table_rows = parse_md_table(md)
    assert headers == ["Название", "Цена", "Стоимость, ₽", "Доля, %", "YTM, %"]

    # 3. Приватность: ISIN не должен присутствовать в тексте
    assert "RU000A101AAA" not in md
    assert "RU000A102BBB" not in md
    assert "ISIN" not in headers

    # 4. Данные строк: NULL YTM выводится как —
    assert table_rows[0] == ["ОФЗ 26238", "540.20", "54020.00", "54.02", "16.50"]
    assert table_rows[1] == ["Газпром Капитал БО-002Р-01", "919.60", "45980.00", "45.98", "—"]

    # 5. Итоговая строка и схождение долей
    # Total = 54020.00 + 45980.00 = 100000.00
    # Share 1 = 54.02%, Share 2 = 45.98% -> sum = 100.00%
    assert table_rows[2] == ["Итого (2)", "—", "100000.00", "100.00", "—"]


def test_render_markdown_with_isin():
    gen_at = datetime(2026, 9, 29, 12, 0, 0)
    freshness = (None, None, None)

    rows = [
        {
            "isin": "RU000A101AAA",
            "name": "ОФЗ 26238",
            "quantity": Decimal("10"),
            "current_price": Decimal("1000.00"),
            "total_value": Decimal("10000.00"),
            "ytm": Decimal("15.25"),
        }
    ]

    md = _render_markdown(rows, freshness, generated_at=gen_at, with_isin=True)

    # 1. Метки свежести пусты
    assert "- Позиции актуальны на: —" in md
    assert "- Каталожные рыночные данные актуальны на: —" in md
    assert "- YTM рассчитана на: —" in md

    # 2. Заголовок с колонкой ISIN
    headers, table_rows = parse_md_table(md)
    assert headers == ["ISIN", "Название", "Цена", "Стоимость, ₽", "Доля, %", "YTM, %"]

    # 3. ISIN присутствует в данных
    assert table_rows[0] == ["RU000A101AAA", "ОФЗ 26238", "1000.00", "10000.00", "100.00", "15.25"]
    assert table_rows[1] == ["—", "Итого (1)", "—", "10000.00", "100.00", "—"]


def test_render_markdown_zero_total_and_empty():
    gen_at = datetime(2026, 9, 29, 12, 0, 0)
    freshness = (None, None, None)

    # Строки с нулевой стоимостью
    rows = [
        {
            "isin": "RU000A101AAA",
            "name": "ОФЗ Пустой",
            "quantity": Decimal("0"),
            "current_price": Decimal("0.00"),
            "total_value": Decimal("0.00"),
            "ytm": None,
        }
    ]

    md = _render_markdown(rows, freshness, generated_at=gen_at, with_isin=False)
    headers, table_rows = parse_md_table(md)
    # При нулевом total — доля выводится как "—"
    assert table_rows[0] == ["ОФЗ Пустой", "0.00", "0.00", "—", "—"]
    assert table_rows[1] == ["Итого (1)", "—", "0.00", "—", "—"]

    # Пустой список строк
    md_empty = _render_markdown([], freshness, generated_at=gen_at, with_isin=False)
    empty_headers, empty_rows = parse_md_table(md_empty)
    assert empty_headers == ["Название", "Цена", "Стоимость, ₽", "Доля, %", "YTM, %"]
    assert empty_rows[0] == ["Итого (0)", "—", "0.00", "—", "—"]


def test_render_markdown_share_convergence_tolerance():
    """Проверка, что сумма отображаемых долей сходится к 100% с погрешностью <= 0.5%."""
    gen_at = datetime(2026, 9, 29, 12, 0, 0)
    # 3 позиции равной стоимости (33.33% каждая -> сумма округлений 99.99%)
    rows = [
        {
            "isin": f"RU000TEST{i}",
            "name": f"Бонд {i}",
            "quantity": Decimal("1"),
            "current_price": Decimal("100.00"),
            "total_value": Decimal("100.00"),
            "ytm": Decimal("14.00"),
        }
        for i in range(3)
    ]

    md = _render_markdown(rows, (None, None, None), generated_at=gen_at, with_isin=False)
    lines = md.splitlines()
    data_lines = [l for l in lines if l.startswith("| Бонд")]
    shares = []
    for dl in data_lines:
        parts = [p.strip() for p in dl.split("|") if p.strip()]
        # parts: [Название, Цена, Стоимость, Доля, YTM]
        shares.append(Decimal(parts[3]))

    sum_displayed_shares = sum(shares)
    assert abs(sum_displayed_shares - Decimal("100")) <= Decimal("0.5")


# ==============================================================================
# 2. Storage Integration Tests on PostgreSQL (db fixture)
# ==============================================================================


def test_storage_get_portfolio_markdown_rows_and_freshness(db):
    now = datetime.now(timezone.utc)
    t_pos_1 = now - timedelta(hours=3)
    t_pos_2 = now - timedelta(hours=2)
    t_market_in = now - timedelta(hours=4)
    t_ytm_in = now - timedelta(hours=5)

    # Вне портфеля: более свежие штампы
    t_market_out = now - timedelta(minutes=10)
    t_ytm_out = now - timedelta(minutes=5)

    # Добавляем две бумаги в каталог
    db.add_bonds_to_catalog([
        Bond(
            isin="RU000IN00001",
            name="Бумага В Портфеле",
            nominal=Decimal("1000"),
            maturity_date=date(2028, 1, 1),
            currency="RUB",
            market_price=Decimal("102.50"),
        ),
        Bond(
            isin="RU000OUT0002",
            name="Бумага Вне Портфеля",
            nominal=Decimal("1000"),
            maturity_date=date(2029, 1, 1),
            currency="RUB",
            market_price=Decimal("105.00"),
        ),
    ])

    with db.conn.cursor() as cur:
        cur.execute("""
            UPDATE bonds_catalog
            SET market_price_updated_at = %s, ytm = 15.75, ytm_updated_at = %s
            WHERE isin = 'RU000IN00001'
        """, (t_market_in, t_ytm_in))
        cur.execute("""
            UPDATE bonds_catalog
            SET market_price_updated_at = %s, ytm = 14.00, ytm_updated_at = %s
            WHERE isin = 'RU000OUT0002'
        """, (t_market_out, t_ytm_out))

    # Добавляем позиции в portfolio_positions:
    # 1) RU000IN00001 на счете acc_1 (стоимость: 10 * 1000 = 10000)
    # 2) RU000IN00001 на счете acc_2 (стоимость: 20 * 1000 = 20000)
    # 3) RU000NO_CATALOG на счете acc_1 (стоимость: 5 * 500 = 2500)
    with db.conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portfolio_positions (
                isin, broker_name, account_id, name, quantity, current_price, current_value, updated_at
            ) VALUES
            ('RU000IN00001', 'T-Bank', 'acc_1', 'Бумага В Портфеле', 10, 1000.0, 10000.0, %s),
            ('RU000IN00001', 'T-Bank', 'acc_2', 'Бумага В Портфеле', 20, 1000.0, 20000.0, %s),
            ('RU000NO_CAT',  'T-Bank', 'acc_1', 'Вне Каталога',      5,  500.0,  2500.0,  %s)
        """, (t_pos_1, t_pos_2, t_pos_1))
    db.conn.commit()

    rows = db.get_portfolio_markdown_rows()
    # Должно быть 3 строки (включая разные счета)
    assert len(rows) == 3

    # Сортировка по стоимости DESC: 20000, 10000, 2500
    assert rows[0]['total_value'] == Decimal('20000.00')
    assert rows[0]['isin'] == 'RU000IN00001'
    assert rows[0]['ytm'] == Decimal('15.75')

    assert rows[1]['total_value'] == Decimal('10000.00')
    assert rows[1]['isin'] == 'RU000IN00001'
    assert rows[1]['ytm'] == Decimal('15.75')

    # LEFT JOIN: для RU000NO_CAT YTM отсутствует, строка не потеряна
    assert rows[2]['total_value'] == Decimal('2500.00')
    assert rows[2]['isin'] == 'RU000NO_CAT'
    assert rows[2]['ytm'] is None

    # Проверка свежести: scope только по бумагам текущего портфеля!
    # t_market_out и t_ytm_out не должны учитываться, т.к. RU000OUT0002 нет в портфеле.
    pos_max, market_max, ytm_max = db.get_portfolio_markdown_freshness()

    # Сравнение с точностью до секунд/микросекунд в зависимости от базы
    assert abs((pos_max - t_pos_2).total_seconds()) < 1.0
    assert abs((market_max - t_market_in).total_seconds()) < 1.0
    assert abs((ytm_max - t_ytm_in).total_seconds()) < 1.0


# ==============================================================================
# 3. UseCase Execution Test (без TBank API и сети)
# ==============================================================================


def test_export_portfolio_use_case_markdown_execution(tmp_path):
    out_file = tmp_path / "test_report.md"

    mock_storage = MagicMock()
    mock_storage.get_portfolio_markdown_rows.return_value = [
        {
            "isin": "RU000TEST111",
            "name": "Тестовая Бумага",
            "quantity": Decimal("10"),
            "current_price": Decimal("100.00"),
            "total_value": Decimal("1000.00"),
            "ytm": Decimal("18.50"),
        }
    ]
    mock_storage.get_portfolio_markdown_freshness.return_value = (
        datetime(2026, 9, 29, 10, 0, 0),
        datetime(2026, 9, 29, 10, 5, 0),
        datetime(2026, 9, 29, 10, 10, 0),
    )

    use_case = ExportPortfolioUseCase(
        config={},
        storage=mock_storage,
        tinkoff_token="fake_token",
    )

    args = argparse.Namespace(
        format="md",
        output=str(out_file),
        with_isin=False,
    )

    # Проверяем, что Client (tinkoff.invest) не вызывается
    with patch("src.use_cases.export_portfolio.Client") as mock_client:
        use_case.execute(args)
        mock_client.assert_not_called()

    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "# Экспорт портфеля на" in content
    assert "Тестовая Бумага" in content
    assert "18.50" in content
    assert "RU000TEST111" not in content  # with_isin=False
