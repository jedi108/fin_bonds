import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Optional, Dict, Type, Any, List
from dotenv import load_dotenv
from functools import lru_cache

from src.cbr.api_client import CbrApiClient
from src.moex.api_client import MoexApiClient
from src.storage import PortfolioStorage
from src.monitoring.notifier import TelegramNotifier
from src.use_cases.base import UseCase
from src.utils import load_config
from src.tbank.api_client import TbankApiClient
from src.monitoring.adapters.factory import AdapterFactory

from src.use_cases.add_bond_to_catalog import AddBondToCatalogUseCase
from src.use_cases.analysis_snapshot import AnalysisSnapshotUseCase
from src.use_cases.analyze_liquidity import AnalyzeLiquidityUseCase
from src.use_cases.analyze_buy_candidates import AnalyzeBuyCandidatesUseCase
from src.use_cases.calculate_spread import CalculateSpreadUseCase
from src.use_cases.calculate_ytm import CalculateYtmUseCase
from src.use_cases.calculate_monthly_profit import CalculateMonthlyProfitUseCase
from src.use_cases.check_changes import CheckChangesUseCase
from src.use_cases.check_db import CheckDbUseCase
from src.use_cases.check_offers import CheckOffersUseCase
from src.use_cases.cleanup_portfolio import CleanupPortfolioUseCase
from src.use_cases.clear_data import ClearDataUseCase
from src.use_cases.export_bonds import ExportBondsUseCase
from src.use_cases.export_monitoring_results import ExportMonitoringResultsUseCase
from src.use_cases.export_portfolio import ExportPortfolioUseCase
from src.use_cases.generate_plots import GeneratePlotsUseCase
from src.use_cases.import_from_sqlite import ImportFromSqliteUseCase
from src.use_cases.link_companies import LinkCompaniesUseCase
from src.use_cases.migrate_db import MigrateDbUseCase
from src.use_cases.seed_data import SeedDataUseCase
from src.use_cases.set_spread import SetSpreadUseCase
from src.use_cases.sync_offers_to_calendar import SyncOffersToCalendarUseCase
from src.use_cases.sync_portfolio import SyncPortfolioUseCase
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
from src.use_cases.calculate_duration import CalculateDurationUseCase
from src.use_cases.rebalance_report import RebalanceReportUseCase
from src.use_cases.rebalance_plan import RebalancePlanUseCase
from src.use_cases.sync_chatgpt_portfolio import SyncChatgptPortfolioUseCase

logger = logging.getLogger(__name__)

class UseCaseFactory:
    """Управляет жизненным циклом общих зависимостей для UseCases."""
    def __init__(self):
        # Явный путь: find_dotenv() у python-dotenv падает с AssertionError,
        # если скрипт запущен через stdin/-c (у __main__ нет __file__).
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
        self.config = load_config()
        self.tinkoff_token = self._get_tinkoff_token()
        self._db_connection: Optional[PortfolioStorage] = None
        self._moex_api_client: Optional[MoexApiClient] = None
        self._cbr_api_client: Optional[CbrApiClient] = None
        self._tbank_api_client = None
        self._notifier: Optional[TelegramNotifier] = None
        self._adapter_factory = None
        self._all_use_cases = None

    def _get_tinkoff_token(self) -> str:
        token = os.getenv('INVEST_TOKEN', '')
        if not token or 'YOUR_TINKOFF' in token:
            raise ValueError("Токен TBank (INVEST_TOKEN) не найден или используется значение по умолчанию.")
        return token

    def _get_tbank_account_ids(self) -> Optional[List[str]]:
        """Список счетов TBank из TBANK_ACCOUNT_IDS (через запятую).

        Пусто/не задано → None (обход всех счетов токена, обратная совместимость).
        ID счетов — персональные данные, поэтому env/SOPS, а не config.yaml.
        """
        raw = os.getenv('TBANK_ACCOUNT_IDS', '')
        if not raw.strip():
            return None
        return raw.split(',')

    def get_db_connection(self) -> PortfolioStorage:
        """Единственная точка создания хранилища: DSN берётся из POSTGRES_DSN."""
        if self._db_connection is None:
            dsn = os.getenv('POSTGRES_DSN', '')
            if not dsn:
                raise ValueError(
                    "Задайте POSTGRES_DSN в .env "
                    "(формат: postgresql://user:password@host:port/dbname)."
                )
            self._db_connection = PortfolioStorage(dsn)
        return self._db_connection

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
            self._tbank_api_client = TbankApiClient(
                self.tinkoff_token,
                account_ids=self._get_tbank_account_ids(),
            )
        return self._tbank_api_client

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
            'link-companies': LinkCompaniesUseCase,
            'update-ratings': UpdateRatingsUseCase,
            'seed-data': SeedDataUseCase,
            'clear-data': ClearDataUseCase,
            'check-db': CheckDbUseCase,
            'migrate-db': MigrateDbUseCase,
            'import-from-sqlite': ImportFromSqliteUseCase,
            'export-bonds': ExportBondsUseCase,
            'export-portfolio': ExportPortfolioUseCase,
            'sync-portfolio': SyncPortfolioUseCase,
            'cleanup-portfolio': CleanupPortfolioUseCase,
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
            'analyze-liquidity': AnalyzeLiquidityUseCase,
            'sync-offers-to-calendar': SyncOffersToCalendarUseCase,
            'export-monitoring': ExportMonitoringResultsUseCase,
            'update-liquidity': UpdateLiquidityUseCase,
            'calculate-ytm': CalculateYtmUseCase,
            'calculate-monthly-profit': CalculateMonthlyProfitUseCase,
            'analyze-buy-candidates': AnalyzeBuyCandidatesUseCase,
            'update-market-prices': UpdateMarketPrices,
            'update-floaters': UpdateFloatersUseCase,
            'calculate-duration': CalculateDurationUseCase,
            'rebalance-report': RebalanceReportUseCase,
            'rebalance-plan': RebalancePlanUseCase,
            'rebalance-analysis-snapshot': AnalysisSnapshotUseCase,
            'sync-chatgpt-portfolio': SyncChatgptPortfolioUseCase,
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

            
            