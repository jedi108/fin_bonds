import logging
import argparse
from typing import TYPE_CHECKING, List

from .base import UseCase
from ..monitoring.storage import Database

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class ClearDataUseCase(UseCase):
    def __init__(self, factory: 'UseCaseFactory'):
        super().__init__(factory)
        self.db: Database = self.factory.get_db_connection()

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--table',
            type=str,
            help='Specify a single table to clear (e.g., portfolio_positions). If not provided, clears all historical and portfolio data.'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'ClearDataUseCase':
        return cls(factory)

    def execute(self, args: argparse.Namespace):
        logger.info("Запуск процесса очистки данных...")

        if args.table:
            tables_to_clear = [args.table]
            logger.info(f"Будет очищена указанная таблица: {args.table}")
        else:
            tables_to_clear = [
                'portfolio_positions',
                'monitoring_checks',
                'rating_history',
                'risk_history',
                'listlevel_history',
                'calculated_coupons'
            ]
            logger.info("Будут очищены все таблицы с историческими данными и данными портфеля.")

        self._clear_tables(tables_to_clear)

        logger.info("Процесс очистки данных завершен.")

    def _clear_tables(self, tables: List[str]):
        with self.db.conn as conn:
            cursor = conn.cursor()
            for table in tables:
                try:
                    # Проверяем, существует ли таблица
                    cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}';")
                    if cursor.fetchone():
                        logger.debug(f"Очистка таблицы '{table}'...")
                        cursor.execute(f"DELETE FROM {table};")
                        logger.info(f"Таблица '{table}' успешно очищена.")
                    else:
                        logger.warning(f"Таблица '{table}' не найдена в базе данных. Пропускаем.")
                except Exception as e:
                    logger.error(f"Ошибка при очистке таблицы '{table}': {e}")
                    conn.rollback()
                    raise
            conn.commit() 