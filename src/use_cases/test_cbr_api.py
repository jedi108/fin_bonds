import logging
import argparse
from typing import TYPE_CHECKING

from src.use_cases.base import UseCase
from src.cbr.api_client import CbrApiClient

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class TestCbrApiUseCase(UseCase):
    """Тестирование API Центробанка."""

    def __init__(self, client: CbrApiClient):
        self.client = client

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Эта команда не принимает дополнительных аргументов."""
        pass

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'TestCbrApiUseCase':
        """Создает экземпляр с зависимостями из фабрики."""
        return cls(client=factory.get_cbr_api_client())

    def execute(self, args: argparse.Namespace):
        logger.info("🚀 Запуск теста API Центробанка...")
        try:
            rate = self.client.get_latest_ruonia_rate()
            logger.info(f"✅ Успешно получена последняя ставка RUONIA: {rate}%")
        except Exception as e:
            logger.error(f"❌ Не удалось получить ставку RUONIA: {e}", exc_info=True)
        finally:
            logger.info("Команда завершила работу.") 