from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from datetime import date
from decimal import Decimal
from src.data_models import Bond, PortfolioPosition

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
    def get_money_positions(self) -> List[Dict[str, Any]]:
        """Возвращает денежные позиции (cash) по настроенным счетам (022).

        Строка: broker_name, account_id, currency, money, blocked, available.
        Ошибка API пробрасывается наверх (не превращается в cash=0).
        """
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

