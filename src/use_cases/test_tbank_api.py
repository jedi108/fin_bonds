import argparse
import logging
import pprint
from typing import TYPE_CHECKING

from src.tbank.api_client import TbankApiClient
from src.use_cases.base import UseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class TestTbankApiUseCase(UseCase):
    """
    Выполняет тестовые запросы к TBank API.
    """

    def __init__(self, tbank_client: TbankApiClient):
        self.tbank_client = tbank_client

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Настраивает вложенные команды для test_tbank."""
        subparsers = parser.add_subparsers(dest='tbank_command', help='Доступные API команды', required=True)

        subparsers.add_parser('all_bonds_with_risk', help='Получить все облигации с их уровнями риска.')

        find_parser = subparsers.add_parser('find', help='Искать инструмент по тикеру, ISIN или названию.')
        find_parser.add_argument('query', type=str, help='Строка для поиска')

        figi_parser = subparsers.add_parser('get-by-figi', help='Получить детальную информацию по FIGI.')
        figi_parser.add_argument('figi', type=str, help='FIGI инструмента')
        
        coupons_parser = subparsers.add_parser('get-bond-coupons', help='Получить график купонов по FIGI.')
        coupons_parser.add_argument('figi', type=str, help='FIGI облигации')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'TestTbankApiUseCase':
        """Создает экземпляр с зависимостями из фабрики."""
        return cls(tbank_client=factory.get_tbank_api_client())

    def execute(self, args: argparse.Namespace):
        result = None
        logger.info(f"Выполнение команды 'test_tbank {args.tbank_command}' с параметрами: {vars(args)}")

        if args.tbank_command == 'all_bonds_with_risk':
            result = self.tbank_client.get_all_bonds_with_risk()
        elif args.tbank_command == 'find':
            result = self.tbank_client.find_instrument(args.query)
        elif args.tbank_command == 'get-by-figi':
            result = self.tbank_client.get_instrument_by_figi(args.figi)
        elif args.tbank_command == 'get-bond-coupons':
            result = self.tbank_client.get_bond_coupons(args.figi)

        if result:
            # Для all_bonds_with_risk не выводим содержимое, т.к. оно слишком большое
            if args.tbank_command == 'all_bonds_with_risk':
                 logger.info(f"Успешный ответ от API. Количество записей: {len(result)}")
            else:
                pretty_result = pprint.pformat(result)
                logger.info(f"Успешный ответ от API:\n{pretty_result}")
        else:
            logger.warning("Ответ от API не получен или пуст.") 