import logging
import argparse
from typing import Dict, Any, TYPE_CHECKING

from src.storage import PortfolioStorage
from src.monitoring.plotter import filter_rating_changes
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class CheckChangesUseCase(UseCase):
    """
    Сценарий: проверка изменений в рейтингах без полного обновления.
    """

    def __init__(self, config: Dict[str, Any], db: PortfolioStorage):
        self.config = config
        self.db = db

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Команда не принимает аргументов."""
        pass

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'CheckChangesUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(config=factory.config, db=factory.get_db_connection())

    def execute(self, args: argparse.Namespace):
        logger.info("Проверка изменений в рейтингах...")
        
        # Источник истины для риск-проверки — активные позиции PostgreSQL,
        # а не устаревающий data/portfolio.txt.
        portfolio_bonds = self.db.get_portfolio_bonds_for_monitoring()
        isins = sorted({bond.isin for bond in portfolio_bonds if bond.isin})
        if not isins:
            logger.warning("Активные позиции в PostgreSQL не найдены. Проверка невозможна.")
            return
        logger.info("Загружено %d ISIN-кодов из активного портфеля PostgreSQL.", len(isins))

        history = self.db.get_rating_history_for_bonds(isins)
        
        upgrades = filter_rating_changes(history, 'up')
        downgrades = filter_rating_changes(history, 'down')

        if not upgrades and not downgrades:
            logger.info("Изменений в рейтингах с последнего запуска не обнаружено.")
        else:
            if upgrades:
                logger.info(f"Повышения: {list(upgrades.keys())}")
            if downgrades:
                logger.info(f"Понижения: {list(downgrades.keys())}") 