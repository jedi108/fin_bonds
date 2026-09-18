import logging
import argparse
from typing import TYPE_CHECKING

from src.use_cases.base import UseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

class TestAlorApiUseCase(UseCase):
    """Тестовый UseCase для проверки работы Alor API клиента."""
    
    def __init__(self, factory: 'UseCaseFactory'):
        super().__init__(factory)
        self.alor_api_client = factory.get_alor_api_client()
    
    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--test-type',
            type=str,
            default='portfolio',
            choices=['portfolio', 'bonds', 'enrich', 'connection'],
            help="Тип теста: 'portfolio', 'bonds', 'enrich', 'connection'"
        )
        parser.add_argument(
            '--tickers',
            type=str,
            nargs='+',
            default=['RU000A100KQ4', 'RU000A0JX0J6'],
            help="Список тикеров для тестирования обогащения данных"
        )
    
    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'TestAlorApiUseCase':
        return cls(factory)
    
    def execute(self, args: argparse.Namespace):
        if not self.alor_api_client:
            logger.error("Alor API клиент не доступен. Проверьте настройку токенов в .env файле.")
            return
        
        logger.info(f"Запуск тестирования Alor API (тип: {args.test_type})...")
        
        if args.test_type == 'connection':
            self._test_connection()
        elif args.test_type == 'portfolio':
            self._test_portfolio()
        elif args.test_type == 'bonds':
            self._test_bonds()
        elif args.test_type == 'enrich':
            self._test_enrich(args.tickers)
    
    def _test_connection(self):
        """Тестирует базовое подключение к Alor API."""
        logger.info("Тестирование подключения к Alor API...")
        try:
            # Пока просто проверяем, что клиент создан
            if self.alor_api_client:
                logger.info("✅ Alor API клиент успешно создан")
                logger.info(f"Base URL: {self.alor_api_client.base_url}")
                logger.info(f"Token configured: {'Да' if self.alor_api_client.token else 'Нет'}")
            else:
                logger.error("❌ Alor API клиент не создан")
        except Exception as e:
            logger.error(f"❌ Ошибка при тестировании подключения: {e}")
    
    def _test_portfolio(self):
        """Тестирует получение портфеля."""
        logger.info("Тестирование получения портфеля из Alor...")
        try:
            positions = self.alor_api_client.get_portfolio_positions()
            logger.info(f"✅ Получено {len(positions)} позиций из Alor")
            
            if positions:
                logger.info("Первые 3 позиции:")
                for i, pos in enumerate(positions[:3]):
                    logger.info(f"  {i+1}. {pos}")
            else:
                logger.info("Портфель пуст или API возвращает пустой список")
                
        except Exception as e:
            logger.error(f"❌ Ошибка при получении портфеля: {e}")
    
    def _test_bonds(self):
        """Тестирует получение списка облигаций."""
        logger.info("Тестирование получения списка облигаций из Alor...")
        try:
            bonds = self.alor_api_client.get_bonds()
            logger.info(f"✅ Получено {len(bonds)} облигаций из Alor")
            
            if bonds:
                logger.info("Первые 3 облигации:")
                for i, bond in enumerate(bonds[:3]):
                    logger.info(f"  {i+1}. {bond}")
            else:
                logger.info("Список облигаций пуст или API возвращает пустой список")
                
        except Exception as e:
            logger.error(f"❌ Ошибка при получении облигаций: {e}")
    
    def _test_enrich(self, tickers):
        """Тестирует обогащение данных по тикерам."""
        logger.info(f"Тестирование обогащения данных для тикеров: {tickers}")
        try:
            enriched_data = self.alor_api_client.enrich_bonds_by_tickers(tickers)
            logger.info(f"✅ Обогащено {len(enriched_data)} записей")
            
            if enriched_data:
                logger.info("Обогащенные данные:")
                for i, data in enumerate(enriched_data[:3]):
                    logger.info(f"  {i+1}. {data}")
            else:
                logger.info("Нет данных для обогащения или API возвращает пустой список")
                
        except Exception as e:
            logger.error(f"❌ Ошибка при обогащении данных: {e}") 