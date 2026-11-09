import argparse
import logging
from decimal import Decimal

from src.moex.api_client import MoexApiClient


logger = logging.getLogger(__name__)

class TestMoexUpdateLogicUseCase:
    """
    Тестовый use case для отладки вызова MOEX API.
    """

    def __init__(self, moex_client: MoexApiClient):
        self.moex_client = moex_client

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды."""
        parser.add_argument('isin', type=str, help='ISIN облигации для проверки.')
        parser.add_argument('--nominal', type=Decimal, default=Decimal('1000.0'), help='Номинал облигации.')

    @classmethod
    def create(cls, factory):
        """Создает экземпляр use case с зависимостями."""
        return cls(
            moex_client=factory.get_moex_api_client(),
        )

    def execute(self, args: argparse.Namespace):
        logger.info(f"--- Запрос данных с MOEX для {args.isin} с номиналом {args.nominal} ---")
        
        moex_data = self.moex_client.get_bond_data(args.isin, args.nominal)

        if moex_data:
            logger.info(f"Получены данные с MOEX: {moex_data}")
        else:
            logger.warning("Не удалось получить данные с MOEX.")

        logger.info("Тестовый use case завершил работу.") 