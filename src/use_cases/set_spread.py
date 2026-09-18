import logging
import argparse
from typing import TYPE_CHECKING, Dict, Any

from src.use_cases.base import UseCase
from src.monitoring.storage import Database

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class SetSpreadUseCase(UseCase):
    """Устанавливает спред для облигации с плавающим купоном."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Настраивает парсер для команды set_spread."""
        parser.add_argument('--isin', type=str, required=True, help="ISIN облигации, для которой устанавливается спред.")
        parser.add_argument('--spread', type=float, required=True, help="Значение спреда в процентах (например, 1.5).")

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'SetSpreadUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(db=factory.get_db_connection())

    def execute(self, args: argparse.Namespace):
        logger.info(f"🚀 Попытка установить спред {args.spread}% для облигации {args.isin}...")
        
        success = self.db.set_coupon_spread(args.isin, args.spread)

        if success:
            logger.info("✅ Команда успешно завершена.")
        else:
            logger.error("❌ Команда завершилась с ошибкой. См. логи выше для деталей.") 