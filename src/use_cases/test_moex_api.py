import argparse
import logging
import pprint
import sys
from typing import List, TYPE_CHECKING

from src.moex.api_client import MoexApiClient
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class TestMoexApiUseCase(UseCase):
    """
    Выполняет тестовые запросы к MOEX API.
    """
    def __init__(self, moex_client: MoexApiClient):
        self.moex_client = moex_client

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Настраивает вложенные команды для test_moex."""
        subparsers = parser.add_subparsers(dest='moex_command', help='Доступные API команды', required=True)

        desc_parser = subparsers.add_parser('description', help='Получить описание бумаги по ISIN.')
        desc_parser.add_argument('isin', type=str, help='ISIN-код ценной бумаги.')

        raw_desc_parser = subparsers.add_parser('raw_desc', help='Получить сырое описание бумаги по ISIN.')
        raw_desc_parser.add_argument('isin', type=str, help='ISIN-код ценной бумаги.')

        market_parser = subparsers.add_parser('marketdata', help='Получить рыночные данные по ISIN.')
        market_parser.add_argument('isin', type=str, help='ISIN-код ценной бумаги.')

        find_parser = subparsers.add_parser('find', help='Найти бумагу по части тикера или названия.')
        find_parser.add_argument('query', type=str, help='Строка для поиска.')

        raw_find_parser = subparsers.add_parser('find_raw', help='Показать сырой ответ поиска по бумаге.')
        raw_find_parser.add_argument('query', type=str, help='Строка для поиска.')

        market_all_data_parser = subparsers.add_parser('market_all_data', help='Получить все блоки данных по ISIN с рынка.')
        market_all_data_parser.add_argument('isin', type=str, help='ISIN ценной бумаги.')

        full_spec_parser = subparsers.add_parser('full_spec', help='Получить полную спецификацию по SECID.')
        full_spec_parser.add_argument('secid', type=str, help='SECID ценной бумаги.')

        candles_parser = subparsers.add_parser('candles', help='Получить исторические свечи по SECID.')
        candles_parser.add_argument('secid', type=str, help='SECID бумаги (не ISIN!).')
        candles_parser.add_argument('--from_date', type=str, default='', help='Дата начала в формате YYYY-MM-DD.')
        candles_parser.add_argument('--interval', type=int, default=24, help='Интервал свечи (например, 24 для дневных).')
        candles_parser.add_argument('--engine', type=str, default='stock', help='Торговая система.')
        candles_parser.add_argument('--market', type=str, default='bonds', help='Рынок.')

        subparsers.add_parser('engines', help='Получить список торговых систем (engines).')

        markets_parser = subparsers.add_parser('markets', help='Получить список рынков для торговой системы.')
        markets_parser.add_argument('engine', type=str, help='Название торговой системы (например, stock).')

        rating_parser = subparsers.add_parser('rating', help='Получить кредитный рейтинг по SECID (требует подписки, не работает).')
        rating_parser.add_argument('secid', type=str, help='SECID бумаги.')

        market_full_spec_parser = subparsers.add_parser('market_full_spec', help='Получить всю информацию по ISIN с рынка.')
        market_full_spec_parser.add_argument('isin', type=str, help='ISIN код бумаги.')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'TestMoexApiUseCase':
        """Создает экземпляр с зависимостями из фабрики."""
        return cls(moex_client=factory.get_moex_api_client())

    def execute(self, args: argparse.Namespace):
        result = None
        logger.info(f"Выполнение команды 'test_moex {args.moex_command}' с параметрами: {vars(args)}")

        if args.moex_command == 'description':
            result = self.moex_client.get_security_description(args.isin)
        elif args.moex_command == 'raw_desc':
            result = self.moex_client.get_raw_security_description(args.isin)
        elif args.moex_command == 'marketdata':
            result = self.moex_client.get_market_data(args.isin)
        elif args.moex_command == 'find':
            result = self.moex_client.find_securities(args.query)
        elif args.moex_command == 'find_raw':
            result = self.moex_client.raw_find_securities(args.query)
        elif args.moex_command == 'market_all_data':
            result = self.moex_client.get_all_data_on_market_for_bond(args.isin)
        elif args.moex_command == 'full_spec':
            result = self.moex_client.get_full_security_spec(args.secid)
        elif args.moex_command == 'candles':
            result = self.moex_client.get_candles(
                secid=args.secid,
                from_date=args.from_date,
                interval=args.interval,
                engine=args.engine,
                market=args.market
            )
        elif args.moex_command == 'engines':
            result = self.moex_client.get_engines()
        elif args.moex_command == 'markets':
            result = self.moex_client.get_markets(args.engine)
        elif args.moex_command == 'rating':
            logger.warning("Эта функция не работает, так как требует платной подписки на API Мосбиржи.")
            result = None
        elif args.moex_command == 'market_full_spec':
            result = self.moex_client.get_all_security_info_on_market(args.isin)

        if result:
            logger.info(f"Успешный ответ от API:\n{pprint.pformat(result)}")
        else:
            logger.warning("Ответ от API не получен или пуст.") 