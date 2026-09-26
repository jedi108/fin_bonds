import pandas as pd
import argparse
from typing import Dict, Any, TYPE_CHECKING
import logging

from src.storage import PortfolioStorage
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

class CheckDbUseCase(UseCase):
    """
    Сценарий: Проверка последних записей в базе данных.
    """
    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды проверки БД."""
        parser.add_argument(
            '--limit', type=int, default=10,
            help='Ограничивает количество выводимых записей (по умолчанию 10).'
        )
        parser.add_argument(
            '--query', type=str, default=None,
            help='Выполняет произвольный SQL-запрос вместо запроса по умолчанию.'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'CheckDbUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(config=factory.config, db=factory.get_db_connection())

    def __init__(self, config: Dict[str, Any], db: PortfolioStorage):
        self.config = config
        self.db = db
        self.logger = logging.getLogger(self.__class__.__name__)

    def execute(self, args: argparse.Namespace):
        self.logger.info("Executing CheckDbUseCase")

        try:
            if args.query:
                rows = self.db.execute_debug_query(args.query)
            else:
                rows = self.db.get_last_risk_listlevel(args.limit)

            df = pd.DataFrame(rows)

            if df.empty:
                self.logger.info("No data found in the database matching the criteria.")
                print("No data found.")
            else:
                self.logger.info(f"Successfully fetched {len(df)} records.")
                print("Query result:")
                print(df.to_string())

        except Exception as e:
            self.logger.error(f"An unexpected error occurred: {e}")
            print(f"An unexpected error occurred: {e}")

        self.logger.info("CheckDbUseCase finished") 