import argparse
import logging
import pprint
from typing import TYPE_CHECKING

from src.monitoring.rating_parser import get_rating_from_smartlab
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class TestSmartlabUseCase(UseCase):
    """
    Выполняет тестовые запросы к парсеру smart-lab.ru.
    """

    def __init__(self):
        pass

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Настраивает команду для test_smartlab."""
        parser.add_argument('isin', type=str, help='ISIN-код ценной бумаги.')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'TestSmartlabUseCase':
        """Создает экземпляр use case. Зависимости не требуются."""
        return cls()

    def execute(self, args: argparse.Namespace):
        logger.info(f"Тестирование парсера smartlab для ISIN: {args.isin}")
        
        result = get_rating_from_smartlab(args.isin)

        if result:
            logger.info(f"Успешный ответ от парсера:\n{pprint.pformat(result)}")
        else:
            logger.warning("Ответ от парсера не получен или пуст.")

        logger.info("Команда завершила работу.")