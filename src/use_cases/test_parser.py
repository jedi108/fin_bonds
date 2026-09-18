import argparse
import logging

from src.use_cases.base import UseCase
# from src.monitoring.rating_parser import get_full_bond_data_from_rusbonds, get_full_bond_data_from_smartlab

logger = logging.getLogger(__name__)


class TestParserUseCase(UseCase):
    """
    Use case для ручного тестирования парсеров.
    """
    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument('--isin', type=str, required=True, help='ISIN для тестирования.')
        parser.add_argument('--source', type=str, choices=['smartlab'], default='smartlab', help='Источник данных.')

    def execute(self, args: argparse.Namespace):
        logger.info(f"Тестирование парсера '{args.source}' для ISIN: {args.isin}")

        if args.source == 'smartlab':
            # data = get_full_bond_data_from_smartlab(args.isin)
            logger.warning("Парсер smartlab отключен.")
            data = None
        else:
            logger.error(f"Неизвестный источник: {args.source}")
            return

        if data:
            import pprint
            pprint.pprint(data)
        else:
            logger.info("Данные не получены.") 