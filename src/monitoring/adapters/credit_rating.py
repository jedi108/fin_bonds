import logging
from typing import Optional, Dict, Any, Callable

from .base import BaseAdapter, MonitoringResult
from src.use_cases.interfaces import IMoexApiClient

logger = logging.getLogger(__name__)


class CreditRatingAdapter(BaseAdapter):
    """
    Агрегирующий адаптер для проверки кредитного рейтинга.
    Использует несколько провайдеров с логикой отката (fallback).
    """
    @property
    def name(self) -> str:
        return 'credit_rating'

    def __init__(self, db, config, **dependencies):
        super().__init__(db, config, **dependencies)
        self.moex_client: IMoexApiClient = self.dependencies['moex_client']
        self.rating_providers: Dict[str, Callable] = self.dependencies['rating_providers']
        self.verbose_name = 'Кредитный рейтинг'

    def fetch(self, bond_data: dict) -> MonitoringResult:
        """
        Проверяет кредитный рейтинг облигации.
        Пробует разные провайдеры, пока не найдет рейтинг.
        """
        isin = bond_data.isin
        current_rating_value = None
        used_provider = None
        for provider_name, provider in self.rating_providers.items():
            try:
                # Передаем и ISIN, и активный клиент moex
                result = provider(isin, client=self.moex_client)
                if result and result.get('rating'):
                    current_rating_value = result['rating']
                    used_provider = provider_name
                    logger.info(f"Рейтинг '{current_rating_value}' успешно получен от '{provider_name}' для {isin}.")
                    break  # Рейтинг найден, выходим из цикла
            except Exception as e:
                logger.warning(f"Ошибка при получении рейтинга от '{provider_name}' для {isin}: {e}")

        # Если рейтинг не найден ни одним провайдером, явно сохраняем 'N/A'
        if not current_rating_value:
            current_rating_value = 'N/A'
            used_provider = None
            logger.info(f"Рейтинг не найден ни одним провайдером для {isin}. Сохраняем 'N/A'.")

        # Сохраняем результат в базу
        self.db.add_monitoring_check(isin, self.name, str(current_rating_value))
        
        # Формируем сообщение
        if current_rating_value == 'N/A':
            message = f"Рейтинг не найден ни одним провайдером"
        else:
            message = f"Рейтинг: {current_rating_value} ({used_provider})"
        
        return MonitoringResult(
            adapter_name=self.name,
            isin=isin,
            status='success',
            message=message,
            value=current_rating_value,
            is_significant=False
        )