import pandas as pd
import argparse
from typing import Dict, Any, TYPE_CHECKING
import logging

from src.monitoring.storage import Database
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

    def __init__(self, config: Dict[str, Any], db: Database):
        self.config = config
        self.db = db
        self.logger = logging.getLogger(self.__class__.__name__)

    def execute(self, args: argparse.Namespace):
        self.logger.info("Executing CheckDbUseCase")

        if args.query:
            query = args.query
            params = ()
        else:
            query = """
            WITH LatestRisk AS (
                SELECT
                    isin,
                    risk_level as risk_rating,
                    risk_date as last_update,
                    ROW_NUMBER() OVER(PARTITION BY isin ORDER BY risk_date DESC) as rn
                FROM risk_history
            ),
            LatestListLevel AS (
                SELECT
                    isin,
                    list_level,
                    check_date,
                    ROW_NUMBER() OVER(PARTITION BY isin ORDER BY check_date DESC) as rn
                FROM listlevel_history
            )
            SELECT
                lr.isin,
                lr.risk_rating,
                ll.list_level,
                lr.last_update
            FROM LatestRisk lr
            LEFT JOIN LatestListLevel ll ON lr.isin = ll.isin AND ll.rn = 1
            WHERE lr.rn = 1
            ORDER BY lr.last_update DESC
            LIMIT ?;
            """
            params = (args.limit,)

        try:
            df = pd.read_sql_query(query, self.db.conn, params=params)

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