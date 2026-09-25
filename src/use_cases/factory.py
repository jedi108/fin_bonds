import logging
import os
from typing import Optional, Dict, Type, Any, List
from dotenv import load_dotenv
from functools import lru_cache

from src.cbr.api_client import CbrApiClient
from src.moex.api_client import MoexApiClient
from src.monitoring.storage import Database
from src.monitoring.notifier import TelegramNotifier
from src.use_cases.base import UseCase
from src.use_cases.interfaces import IPortfolioStorage
from src.utils import load_config
from src.tbank.api_client import TbankApiClient
from src.monitoring.adapters.factory import AdapterFactory

from src.use_cases.add_bond_to_catalog import AddBondToCatalogUseCase
from src.use_cases.analyze_liquidity import AnalyzeLiquidityUseCase
from src.use_cases.analyze_buy_candidates import AnalyzeBuyCandidatesUseCase
from src.use_cases.calculate_spread import CalculateSpreadUseCase
from src.use_cases.calculate_ytm import CalculateYtmUseCase
from src.use_cases.calculate_monthly_profit import CalculateMonthlyProfitUseCase
from src.use_cases.check_changes import CheckChangesUseCase
from src.use_cases.check_db import CheckDbUseCase
from src.use_cases.check_offers import CheckOffersUseCase
from src.use_cases.clear_data import ClearDataUseCase
from src.use_cases.export_bonds import ExportBondsUseCase
from src.use_cases.export_monitoring_results import ExportMonitoringResultsUseCase
from src.use_cases.export_portfolio import ExportPortfolioUseCase
from src.use_cases.generate_plots import GeneratePlotsUseCase
from src.use_cases.migrate_db import MigrateDbUseCase
from src.use_cases.seed_data import SeedDataUseCase
from src.use_cases.set_spread import SetSpreadUseCase
from src.use_cases.sync_offers_to_calendar import SyncOffersToCalendarUseCase
from src.use_cases.sync_portfolio import SyncPortfolioUseCase
from src.use_cases.test_alor_api import TestAlorApiUseCase
from src.use_cases.test_cbr_api import TestCbrApiUseCase
from src.use_cases.test_moex_api import TestMoexApiUseCase
from src.use_cases.test_moex_update_logic import TestMoexUpdateLogicUseCase
from src.use_cases.test_parser import TestParserUseCase
from src.use_cases.test_smartlab import TestSmartlabUseCase
from src.use_cases.test_tbank_api import TestTbankApiUseCase
from src.use_cases.update_bonds_catalog import UpdateBondsCatalogUseCase
from src.use_cases.update_liquidity import UpdateLiquidityUseCase
from src.use_cases.update_market_prices import UpdateMarketPrices
from src.use_cases.update_ratings import UpdateRatingsUseCase
from src.use_cases.update_floaters import UpdateFloatersUseCase

logger = logging.getLogger(__name__)

class UseCaseFactory:
    """Управляет жизненным циклом общих зависимостей для UseCases."""
    def __init__(self):
        load_dotenv()
        self.config = load_config()
        self.tinkoff_token = self._get_tinkoff_token()
        self._db_connection: Optional[Database] = None
        self._moex_api_client: Optional[MoexApiClient] = None
        self._cbr_api_client: Optional[CbrApiClient] = None
        self._tbank_api_client = None
        self._alor_api_client = None
        self._notifier: Optional[TelegramNotifier] = None
        self._adapter_factory = None
        self._all_use_cases = None

    def _get_tinkoff_token(self) -> str:
        token = os.getenv('INVEST_TOKEN', '')
        if not token or 'YOUR_TINKOFF' in token:
            raise ValueError("Токен TBank (INVEST_TOKEN) не найден или используется значение по умолчанию.")
        return token

    def get_db_connection(self) -> IPortfolioStorage:
        if self._db_connection is None:
            db_path = self.config['db_path']
            self._db_connection = Database(db_path)
        return self._db_connection

    def get_db_path(self) -> str:
        """Возвращает путь к файлу БД из конфигурации."""
        return self.config['db_path']

    def get_moex_api_client(self) -> MoexApiClient:
        if self._moex_api_client is None:
            self._moex_api_client = MoexApiClient()
        return self._moex_api_client

    def get_cbr_api_client(self) -> CbrApiClient:
        if self._cbr_api_client is None:
            self._cbr_api_client = CbrApiClient()
        return self._cbr_api_client

    def get_notifier(self) -> Optional[TelegramNotifier]:
        """Создает и возвращает экземпляр уведомителя (например, Telegram)."""
        if self._notifier is None:
            # Конфигурация для уведомителя берется из config.yaml
            notifier_config = self.config.get('telegram')

            # Проверяем, существует ли конфигурация и включена ли она
            if not notifier_config or not notifier_config.get('enabled', False):
                logger.warning("Уведомления в Telegram отключены или секция 'telegram' отсутствует/не настроена в config.yaml.")
                return None
            
            # TelegramNotifier сам загружает токен и chat_id из .env,
            # ему нужно передать только саму конфигурацию.
            logger.info("Конфигурация Telegram найдена и включена. Создание уведомителя...")
            self._notifier = TelegramNotifier(config=notifier_config)
            
            # Дополнительная проверка, что сам уведомитель включился (проверил .env)
            if not self._notifier.enabled:
                logger.warning("TelegramNotifier был создан, но остался отключен (проверьте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env).")
                # Важно вернуть None, чтобы остальная часть системы знала, что уведомлений не будет
                return None

        return self._notifier

    def get_tbank_api_client(self) -> 'TbankApiClient':
        from src.tbank.api_client import TbankApiClient
        if self._tbank_api_client is None:
            self._tbank_api_client = TbankApiClient(self.tinkoff_token)
        return self._tbank_api_client

    def _get_alor_tokens(self) -> Dict[str, str]:
        """Получает токены Alor из переменных окружения."""
        token = os.getenv('ALOR_TOKEN', '')
        refresh_token = os.getenv('ALOR_REFRESH_TOKEN', '')
        client_id = os.getenv('ALOR_CLIENT_ID', '')
        client_secret = os.getenv('ALOR_CLIENT_SECRET', '')
        login = os.getenv('ALOR_LOGIN', '')
        
        # Проверяем минимально необходимые токены для тестирования
        if not refresh_token:
            logger.warning("ALOR_REFRESH_TOKEN не настроен - минимально необходимый токен")
            return {}
        
        # Для базового тестирования достаточно refresh_token
        tokens = {
            'token': token or 'test_token',  # Используем тестовый токен если основной не настроен
            'refresh_token': refresh_token,
            'client_id': client_id or 'test_client_id',
            'client_secret': client_secret or 'test_client_secret',
            'login': login
        }
        
        if not all([token, client_id, client_secret]):
            logger.info("Частично настроены токены Alor - режим тестирования")
            
        return tokens
    
    def get_alor_api_client(self) -> Optional['AlorApiClient']:
        """Создает и возвращает клиент Alor API."""
        if self._alor_api_client is None:
            from src.alor.api_client import AlorApiClient
            tokens = self._get_alor_tokens()
            if tokens:
                try:
                    # Добавляем db_path к токенам
                    tokens['db_path'] = self.get_db_path()
                    self._alor_api_client = AlorApiClient(**tokens)
                    logger.info("Alor API клиент успешно создан")
                except Exception as e:
                    logger.error(f"Ошибка создания Alor API клиента: {e}")
                    return None
            else:
                logger.warning("Alor API клиент не создан - отсутствуют токены")
                return None
        return self._alor_api_client

    def get_adapter_factory(self) -> AdapterFactory:
        if self._adapter_factory is None:
            self._adapter_factory = AdapterFactory(self)
        return self._adapter_factory

    def get_use_case(self, name: str) -> 'UseCase':
        use_case_map = self.get_use_case_map()
        use_case_class = use_case_map.get(name)

        if use_case_class:
            return use_case_class.create(self)
        raise ValueError(f"Unknown use case: {name}")

    def close_db(self):
        if self._db_connection:
            self._db_connection.close()

    def get_use_case_map(self) -> Dict[str, Type[UseCase]]:
        """
        Возвращает словарь с маппингом имени use case на его класс.
        
        Returns:
            Словарь {имя_use_case: класс_use_case}
        """
        return {
            'add-bond': AddBondToCatalogUseCase,
            'update-bonds': UpdateBondsCatalogUseCase,
            'update-ratings': UpdateRatingsUseCase,
            'seed-data': SeedDataUseCase,
            'clear-data': ClearDataUseCase,
            'check-db': CheckDbUseCase,
            'migrate-db': MigrateDbUseCase,
            'export-bonds': ExportBondsUseCase,
            'export-portfolio': ExportPortfolioUseCase,
            'sync-portfolio': SyncPortfolioUseCase,
            'check-changes': CheckChangesUseCase,
            'check-offers': CheckOffersUseCase,
            'generate-plots': GeneratePlotsUseCase,
            'calculate-spread': CalculateSpreadUseCase,
            'set-spread': SetSpreadUseCase,
            'test-cbr': TestCbrApiUseCase,
            'test-moex': TestMoexApiUseCase,
            'test-parser': TestParserUseCase,
            'test-smartlab': TestSmartlabUseCase,
            'test-tbank': TestTbankApiUseCase,
            'test-moex-update-logic': TestMoexUpdateLogicUseCase,
            'test-alor': TestAlorApiUseCase,
            'analyze-liquidity': AnalyzeLiquidityUseCase,
            'sync-offers-to-calendar': SyncOffersToCalendarUseCase,
            'export-monitoring': ExportMonitoringResultsUseCase,
            'update-liquidity': UpdateLiquidityUseCase,
            'calculate-ytm': CalculateYtmUseCase,
            'calculate-monthly-profit': CalculateMonthlyProfitUseCase,
            'analyze-buy-candidates': AnalyzeBuyCandidatesUseCase,
            'update-market-prices': UpdateMarketPrices,
            'update-floaters': UpdateFloatersUseCase,
        }

    def get_all_use_cases(self) -> List[Type[UseCase]]:
        """
        Возвращает список всех доступных юз-кейсов.
        
        Returns:
            List[Type[UseCase]]: Список классов юз-кейсов.
        """
        if not self._all_use_cases:
            use_case_map = self.get_use_case_map()
            self._all_use_cases = list(use_case_map.values())
        return self._all_use_cases

            
            