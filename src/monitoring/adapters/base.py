from abc import ABC, abstractmethod
from typing import Optional, Any, Dict, TYPE_CHECKING
from dataclasses import dataclass
from datetime import date
import logging

from src.monitoring.storage import Database

if TYPE_CHECKING:
    from src.data_models import PortfolioBond

logger = logging.getLogger(__name__)


@dataclass
class MonitoringResult:
    """Стандартизированный результат проверки адаптера."""
    adapter_name: str
    isin: str
    status: str  # 'success', 'changed', 'error', 'skipped'
    message: str
    value: Optional[Any] = None
    is_significant: bool = False


class BaseAdapter(ABC):
    """Абстрактный базовый класс для всех адаптеров мониторинга."""

    def __init__(self, db: Database, config: Dict[str, Any], bond_data: Optional['PortfolioBond'] = None, **dependencies):
        self.db = db
        self.config = config
        self.bond_data = bond_data  # Новое поле для хранения данных облигации
        self.dependencies = dependencies
        self.today = date.today()

    @property
    @abstractmethod
    def name(self) -> str:
        """Уникальное имя адаптера, как в конфиге."""
        raise NotImplementedError

    def fetch(self) -> "MonitoringResult":
        """Основной метод, который выполняет всю логику. Теперь работает с self.bond_data."""
        raise NotImplementedError("Метод fetch должен быть реализован в дочернем классе.")