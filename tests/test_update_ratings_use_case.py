"""
Тесты задачи 002.9: use case update-ratings — парсер CLI, сборка таргетов
по скоупам, итоговая сводка (Р7).

Хранилище — тестовая PostgreSQL (фикстура db); фабрика use case — заглушка.
"""
import argparse
import logging
from datetime import date

import pytest

from src.data_models import Bond, RatingTarget
from src.storage import PortfolioStorage
from src.use_cases.update_ratings import SCOPES, UpdateRatingsUseCase


class _StubUseCaseFactory:
    """Минимум для конструктора UpdateRatingsUseCase/AdapterFactory без сети."""

    def __init__(self, config=None):
        self.config = config or {}
        self.tinkoff_token = ''

    def get_db_connection(self):
        return None

    def get_moex_api_client(self):
        return None

    def get_cbr_api_client(self):
        return None


def make_use_case(db: PortfolioStorage) -> UpdateRatingsUseCase:
    return UpdateRatingsUseCase(
        storage=db, notifier=None, use_case_factory=_StubUseCaseFactory({'monitoring_adapters': ['credit_rating']})
    )


def make_args(**overrides) -> argparse.Namespace:
    """Аргументы CLI по умолчанию (как из парсера)."""
    defaults = dict(isin=None, scope='portfolio', limit=None, delay=0.5)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def add_catalog_row(db: PortfolioStorage, isin: str):
    db.add_bonds_to_catalog([
        Bond(isin=isin, name=f'Бонд {isin}', nominal=1000, maturity_date=date(2030, 1, 1), currency='RUB')
    ])


def add_position(db: PortfolioStorage, isin: str):
    with db.conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO portfolio_positions (isin, ticker, name, quantity, average_price, "
            "current_price, broker_name) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (isin, 'TICK', 'Позиция', 10, 1000, 1000, 'TBank'),
        )


class TestParser:
    def test_defaults(self):
        parser = argparse.ArgumentParser()
        UpdateRatingsUseCase.setup_parser(parser)
        args = parser.parse_args([])
        assert args.scope == 'portfolio'
        assert args.isin is None
        assert args.limit is None
        assert args.delay == 0.5

    def test_scope_choices_validated(self, capsys):
        parser = argparse.ArgumentParser()
        UpdateRatingsUseCase.setup_parser(parser)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(['--scope', 'everything'])
        assert exc.value.code == 2

    def test_scopes_constant_matches_choices(self):
        assert SCOPES == ('portfolio', 'missing', 'catalog')


class TestCollectTargets:
    ISIN_PORT, ISIN_OUT, ISIN_NA = 'RU000PORT001', 'RU000OUT001', 'RU000NA0001'

    def test_isin_found_outside_portfolio(self, db):
        add_catalog_row(db, self.ISIN_OUT)

        scope_label, targets = make_use_case(db)._collect_targets(make_args(isin=self.ISIN_OUT, scope='missing'))

        assert scope_label == f'isin:{self.ISIN_OUT}'
        assert targets == [RatingTarget(isin=self.ISIN_OUT)]

    def test_isin_takes_priority_over_scope_and_logged(self, db, caplog):
        add_catalog_row(db, self.ISIN_OUT)

        with caplog.at_level(logging.INFO, logger='src.use_cases.update_ratings'):
            scope_label, targets = make_use_case(db)._collect_targets(make_args(isin=self.ISIN_OUT, scope='catalog'))

        assert scope_label == f'isin:{self.ISIN_OUT}'
        assert any("игнорируется" in r.getMessage() for r in caplog.records)

    def test_isin_unknown_is_empty(self, db):
        scope_label, targets = make_use_case(db)._collect_targets(make_args(isin='RU000UNKNOWN'))

        assert scope_label == 'isin:RU000UNKNOWN'
        assert targets == []

    def test_portfolio_scope_positions_only(self, db):
        add_catalog_row(db, self.ISIN_PORT)
        add_catalog_row(db, self.ISIN_OUT)
        add_position(db, self.ISIN_PORT)

        scope_label, targets = make_use_case(db)._collect_targets(make_args())

        assert scope_label == 'portfolio'
        assert [t.isin for t in targets] == [self.ISIN_PORT]

    def test_limit_ignored_for_portfolio(self, db, caplog):
        add_catalog_row(db, self.ISIN_PORT)
        add_position(db, self.ISIN_PORT)

        with caplog.at_level(logging.INFO, logger='src.use_cases.update_ratings'):
            scope_label, targets = make_use_case(db)._collect_targets(make_args(limit=1))

        assert scope_label == 'portfolio'
        assert len(targets) == 1
        assert any("--limit" in r.getMessage() for r in caplog.records)

    def test_missing_scope_uses_storage_query(self, db):
        add_catalog_row(db, self.ISIN_NA)
        db.add_monitoring_check(self.ISIN_NA, 'credit_rating', 'N/A')

        scope_label, targets = make_use_case(db)._collect_targets(make_args(scope='missing'))

        assert scope_label == 'missing'
        assert targets == [RatingTarget(isin=self.ISIN_NA)]


class TestSummary:
    def test_format_single_line(self, db):
        summary = make_use_case(db)._format_summary('missing', checked=10, rated=7, na=2, errors=1)
        assert summary == 'Итог: scope=missing; checked=10; rated=7; na=2; errors=1'
