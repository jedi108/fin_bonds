import logging
import argparse
from typing import Dict, Any, TYPE_CHECKING

from src.monitoring.storage import Database
from src.monitoring.plotter import filter_rating_changes
from src.utils import load_isins_from_file
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class CheckChangesUseCase(UseCase):
    """
    Сценарий: проверка изменений в рейтингах без полного обновления.
    """

    def __init__(self, config: Dict[str, Any], db: Database):
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
        
        isins_file_path = self.config.get('portfolio_isins_txt_file')
        isins = load_isins_from_file(isins_file_path)
        if not isins:
            logger.warning("Список ISIN для обработки пуст. Проверка невозможна.")
            return

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