"""
Тесты задачи 002.9: скоупы обновления рейтингов update-ratings.

Покрывает storage-слой:
- get_bond_for_rating_refresh — точечная проверка любой бумаги каталога
  (без INNER JOIN с портфелем; регресс: старый метод не изменился);
- get_isins_for_rating_refresh — семантика скоупов missing/catalog:
  живые бумаги, последняя проверка 'N/A' или отсутствует; погашенные
  исключены; сортировка по isin; limit.

ISIN синтетические — репозиторий публичный.
"""
from datetime import date, timedelta

import pytest

from src.data_models import Bond, RatingTarget
from src.storage import PortfolioStorage


def add_catalog_row(db: PortfolioStorage, isin: str, maturity_date: date = date(2030, 1, 1)):
    """Строка каталога: FK monitoring_checks/rating_history -> bonds_catalog(isin)."""
    db.add_bonds_to_catalog([
        Bond(
            isin=isin,
            name=f'Бонд {isin}',
            nominal=1000,
            maturity_date=maturity_date,
            currency='RUB',
        )
    ])


def add_position(db: PortfolioStorage, isin: str):
    """Позиция портфеля — для регресс-теста portfolio-методов."""
    with db.conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO portfolio_positions (isin, ticker, name, quantity, average_price, "
            "current_price, broker_name) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (isin, 'TICK', 'Позиция', 10, 1000, 1000, 'TBank'),
        )


class TestGetBondForRatingRefresh:
    """--isin по каталогу: работает и для бумаг вне портфеля (002.9, Р2)."""

    ISIN = 'RU000TEST001'

    def test_catalog_only_bond_found(self, db):
        add_catalog_row(db, self.ISIN)

        target = db.get_bond_for_rating_refresh(self.ISIN)

        assert target == RatingTarget(isin=self.ISIN)

    def test_unknown_isin_returns_none(self, db):
        assert db.get_bond_for_rating_refresh('RU000UNKNOWN') is None

    def test_portfolio_method_unchanged_for_catalog_only_bond(self, db):
        """Регресс: старый мониторинг-метод по-прежнему требует позицию."""
        add_catalog_row(db, self.ISIN)

        assert db.get_bond_data_for_monitoring(self.ISIN) is None


class TestRatingRefreshScopes:
    """Семантика скоупов missing/catalog (002.9, Р3)."""

    def test_missing_includes_na_and_never_checked(self, db):
        add_catalog_row(db, 'RU000NA00001')
        add_catalog_row(db, 'RU000NOCHK01')
        db.add_monitoring_check('RU000NA00001', 'credit_rating', 'N/A')

        isins = [t.isin for t in db.get_isins_for_rating_refresh('missing')]

        assert isins == ['RU000NA00001', 'RU000NOCHK01']

    def test_missing_latest_check_wins(self, db):
        """Свежая проверка N/A попадает, даже если раньше был рейтинг."""
        add_catalog_row(db, 'RU000LASTNA1')
        with db.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value) "
                "VALUES (%s, %s, %s, %s)",
                ('RU000LASTNA1', date.today() - timedelta(days=5), 'credit_rating', 'A-'),
            )
        db.add_monitoring_check('RU000LASTNA1', 'credit_rating', 'N/A')

        isins = [t.isin for t in db.get_isins_for_rating_refresh('missing')]

        assert isins == ['RU000LASTNA1']

    def test_missing_excludes_actual_check(self, db):
        add_catalog_row(db, 'RU000ACTUAL1')
        db.add_monitoring_check('RU000ACTUAL1', 'credit_rating', 'A-(RU)')

        isins = [t.isin for t in db.get_isins_for_rating_refresh('missing')]

        assert isins == []

    def test_missing_excludes_matured(self, db):
        add_catalog_row(db, 'RU000MATURED', maturity_date=date(2020, 1, 1))

        assert db.get_isins_for_rating_refresh('missing') == []

    def test_catalog_includes_all_alive_ordered(self, db):
        add_catalog_row(db, 'RU000BBB0001')
        add_catalog_row(db, 'RU000AAA0001')
        add_catalog_row(db, 'RU000MATURED', maturity_date=date(2020, 1, 1))
        db.add_monitoring_check('RU000BBB0001', 'credit_rating', 'AA')

        isins = [t.isin for t in db.get_isins_for_rating_refresh('catalog')]

        # Погашенная исключена, порядок детерминированный, проверенные не фильтруются
        assert isins == ['RU000AAA0001', 'RU000BBB0001']

    def test_limit(self, db):
        for i in range(1, 4):
            add_catalog_row(db, f'RU000LIMIT0{i}')

        isins = [t.isin for t in db.get_isins_for_rating_refresh('missing', limit=2)]

        assert isins == ['RU000LIMIT01', 'RU000LIMIT02']

    def test_unknown_scope_raises(self, db):
        with pytest.raises(ValueError):
            db.get_isins_for_rating_refresh('everything')

    def test_portfolio_scope_unaffected(self, db):
        """Регресс: выбор для портфеля не изменился (дефолтный ночной прогон)."""
        add_catalog_row(db, 'RU000INPORT1')
        add_catalog_row(db, 'RU000OUTPT01')
        add_position(db, 'RU000INPORT1')

        portfolio_bonds = db.get_portfolio_bonds_for_monitoring()

        assert [b.isin for b in portfolio_bonds] == ['RU000INPORT1']
        # Вне портфеля доступны только через новые методы
        assert db.get_isins_for_rating_refresh('catalog') == [
            RatingTarget(isin='RU000INPORT1'), RatingTarget(isin='RU000OUTPT01')
        ]
