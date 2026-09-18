import logging
from typing import Dict, Any, Optional, Type, TYPE_CHECKING

from .base import BaseAdapter
from .moex_listlevel import MoexListLevelAdapter
from .tinkoff_risk_level import TinkoffRiskLevelAdapter
from .moex_coupon import MoexCouponAdapter
from .credit_rating import CreditRatingAdapter
from .floater_coupon_calculator import FloaterCouponCalculatorAdapter
from .liquidity_analyzer import LiquidityAnalyzerAdapter

from src.monitoring.storage import Database
from src.use_cases.interfaces import IMoexApiClient
from src.use_cases.interfaces import ICbrApiClient
from src.use_cases.interfaces import ITbankApiClient

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory
    from src.data_models import PortfolioBond

logger = logging.getLogger(__name__)


class AdapterFactory:
    """
    Фабрика для создания адаптеров мониторинга.
    Инкапсулирует логику создания и передачи зависимостей адаптерам.
    """

    def __init__(self, use_case_factory: 'UseCaseFactory'):
        """
        Инициализирует фабрику адаптеров.

        Args:
            use_case_factory (UseCaseFactory): Фабрика use case'ов, 
                                               предоставляющая доступ к общим зависимостям.
        """
        self.use_case_factory = use_case_factory
        self.config = use_case_factory.config
        self.db = use_case_factory.get_db_connection()
        self.tinkoff_token = self.use_case_factory.tinkoff_token
        self.moex_api_client = use_case_factory.get_moex_api_client()
        self.cbr_api_client = use_case_factory.get_cbr_api_client()

    def get_adapter(self, name: str, bond_data: 'PortfolioBond') -> Optional[BaseAdapter]:
        """
        Возвращает экземпляр адаптера по его имени.
        
        Args:
            name: Имя адаптера.
            bond_data: Объект PortfolioBond с данными облигации.
        """
        adapter_class = self.get_adapter_class(name)
        if not adapter_class:
            return None

        # Собираем базовые аргументы для конструктора BaseAdapter
        base_kwargs = {
            'db': self.db,
            'config': self.config,
            'bond_data': bond_data
        }

        # Добавляем специфичные зависимости для каждого адаптера
        if name in ['moex_listlevel', 'moex_coupon', 'credit_rating']:
            base_kwargs['moex_client'] = self.moex_api_client
        
        if name in ['tinkoff.risk_level', 'liquidity_analyzer']:
            base_kwargs['tbank_api_client'] = self.use_case_factory.get_tbank_api_client()
            
        if name == 'floater_coupon_calculator':
            base_kwargs['cbr_client'] = self.cbr_api_client
            
        # Специальная обработка для credit_rating - добавляем провайдеры рейтингов
        if name == 'credit_rating':
            from src.monitoring.rating_parser import get_rating_from_moex, get_rating_from_smartlab
            base_kwargs['rating_providers'] = {
                'moex': get_rating_from_moex,
                'smartlab': get_rating_from_smartlab
            }

        try:
            logger.debug(f"Создание адаптера '{name}' с аргументами: {list(base_kwargs.keys())}")
            adapter = adapter_class(**base_kwargs)
            logger.debug(f"Адаптер '{name}' успешно создан")
            return adapter
        except TypeError as e:
            logger.error(f"Ошибка при создании адаптера '{name}'. Несоответствие аргументов: {e}")
            return None
        except Exception as e:
            logger.error(f"Неожиданная ошибка при создании адаптера '{name}': {e}", exc_info=True)
            return None
    
    def get_tinkoff_risk_level_adapter(self) -> TinkoffRiskLevelAdapter:
        # Этот метод, вероятно, устарел или используется специфично.
        # Пока оставляем его, но основной путь - через get_adapter.
        return TinkoffRiskLevelAdapter(
            db=self.db,
            tinkoff_token=self.tinkoff_token
        )

    @staticmethod
    def get_adapter_class(name: str) -> Optional[Type[BaseAdapter]]:
        """
        Возвращает класс адаптера по его имени.
        """
        ADAPTER_CLASSES = {
            "moex_listlevel": MoexListLevelAdapter,
            "tinkoff.risk_level": TinkoffRiskLevelAdapter,
            "credit_rating": CreditRatingAdapter,
            "moex_coupon": MoexCouponAdapter,
            "floater_coupon_calculator": FloaterCouponCalculatorAdapter,
            "liquidity_analyzer": LiquidityAnalyzerAdapter
        }
        return ADAPTER_CLASSES.get(name)