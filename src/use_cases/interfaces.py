from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, Set, Tuple
from datetime import date
from decimal import Decimal
from src.data_models import Bond, PortfolioPosition, PortfolioBond, CalculatedCoupon

class IPortfolioStorage(ABC):
    """
    Абстрактный интерфейс для хранилища данных портфеля.
    Определяет контракт для всех операций с данными.
    """
    
    # Методы для работы с каталогом облигаций
    @abstractmethod
    def get_bond_by_isin(self, isin: str) -> Optional[Bond]:
        """Получить данные облигации по ISIN."""
        pass
    
    @abstractmethod
    def add_bonds_to_catalog(self, bonds: List[Bond]) -> None:
        """Добавить облигации в каталог."""
        pass
    
    @abstractmethod
    def get_all_isins_from_catalog(self) -> Set[str]:
        """Получить все ISIN из каталога облигаций."""
        pass
    
    # Методы для работы с портфелем
    @abstractmethod
    def get_portfolio_positions(self) -> List[PortfolioPosition]:
        """Получить все позиции портфеля."""
        pass
    
    @abstractmethod
    def add_portfolio_positions(self, positions: List[PortfolioPosition]) -> None:
        """Добавить позиции в портфель."""
        pass
    
    @abstractmethod
    def clear_portfolio_positions(self) -> None:
        """Очистить все позиции портфеля."""
        pass
    
    @abstractmethod
    def clear_portfolio_positions_by_broker(self, broker_name: str) -> None:
        """Очистить все позиции портфеля только для указанного брокера."""
        pass
    
    @abstractmethod
    def update_position_liquidity(self, isin: str, broker_name: str, liquidity_value: float) -> None:
        """Обновить значение liquidity_loss_ratio для позиции в портфеле."""
        pass
    
    # Оптимизированные методы (новые)
    @abstractmethod
    def get_portfolio_bonds_for_monitoring(self) -> List[PortfolioBond]:
        """Получить облигации портфеля с объединенными данными для мониторинга."""
        pass
    
    @abstractmethod
    def get_bond_data_for_monitoring(self, isin: str) -> Optional[PortfolioBond]:
        """Получить данные конкретной облигации для мониторинга."""
        pass
    
    # Методы для мониторинга
    @abstractmethod
    def add_monitoring_check(self, isin: str, metric_name: str, metric_value: str) -> None:
        """Добавить результат проверки мониторинга."""
        pass
    
    @abstractmethod
    def get_last_monitoring_check(self, isin: str, metric_name: str) -> Optional[Tuple[date, str]]:
        """Получить последнюю проверку мониторинга для метрики."""
        pass
    
    # Служебные методы
    @abstractmethod
    def close(self) -> None:
        """Закрыть соединение с хранилищем."""
        pass 
# --------------------
# Interfaces for external API clients (generated for Clean Architecture)
# --------------------

class ITbankApiClient(ABC):
    """Абстракция клиента TBank API."""

    @abstractmethod
    def get_portfolio_positions(self) -> List[PortfolioPosition]:
        """Возвращает позиции портфеля пользователя."""
        pass

    @abstractmethod
    def enrich_bonds_by_tickers(self, tickers: List[str]) -> List[Dict[str, Any]]:
        """Дополняет информацию об облигациях на основании тикеров."""
        pass

    @abstractmethod
    def get_bonds(self) -> List[Bond]:
        """Возвращает список облигаций из TBank."""
        pass

    @abstractmethod
    def get_market_prices(self, figi_list: List[str]) -> Dict[str, Decimal]:
        """Получает рыночные цены для списка FIGI."""
        pass


class IMoexApiClient(ABC):
    """Абстракция клиента MOEX ISS API."""

    @abstractmethod
    def get_bond_data(self, isin: str, nominal: Optional[Decimal] = None) -> Optional['MoexBondData']:
        """Получает данные облигации с МОEX."""
        pass


class ICbrApiClient(ABC):
    """Абстракция клиента ЦБ РФ для получения ставки RUONIA."""

    @abstractmethod
    def get_ruonia_rate_on_date(self, target_date: date) -> Decimal:
        pass

    @abstractmethod
    def get_latest_ruonia_rate(self) -> Decimal:
        pass


class IAlorApiClient(ABC):
    """Абстракция клиента Alor API."""

    @abstractmethod
    def get_portfolio_positions(self) -> List[PortfolioPosition]:
        """Возвращает позиции портфеля пользователя."""
        pass

    @abstractmethod
    def enrich_bonds_by_tickers(self, tickers: List[str]) -> List[Dict[str, Any]]:
        """Дополняет информацию об облигациях на основании тикеров."""
        pass

    @abstractmethod
    def get_bonds(self) -> List[Bond]:
        """Возвращает список облигаций из Alor."""
        pass
