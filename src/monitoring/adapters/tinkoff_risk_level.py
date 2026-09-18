import logging
from typing import Dict, Any, Optional, TYPE_CHECKING

from .base import BaseAdapter, MonitoringResult
from src.monitoring.storage import Database
from src.use_cases.interfaces import ITbankApiClient

if TYPE_CHECKING:
    from src.data_models import PortfolioBond

logger = logging.getLogger(__name__)


class TinkoffRiskLevelAdapter(BaseAdapter):
    """
    Адаптер для проверки и обновления уровня риска облигации по данным Tinkoff.
    """
    @property
    def name(self) -> str:
        return 'tinkoff.risk_level'

    def __init__(self, db: Database, config: dict, bond_data: 'PortfolioBond', tbank_api_client: ITbankApiClient, **kwargs):
        super().__init__(db, config, bond_data, **kwargs)
        self.tbank_api_client: ITbankApiClient = tbank_api_client
        self.verbose_name = "Уровень риска (Tinkoff)"

    def fetch(self) -> MonitoringResult:
        """
        Получает уровень риска облигации, используя FIGI из self.bond_data и TBank API.
        Всегда создает запись в БД, даже если уровень риска не найден в TBank.
        """
        if not self.bond_data:
            return self._handle_error("Unknown", "bond_data не передан в адаптер")
            
        isin = self.bond_data.isin
        
        try:
            # 1. Получить figi из bond_data
            if not self.bond_data.figi:
                return self._handle_error(isin, f"FIGI не найден для {isin}")

            figi = self.bond_data.figi

            # 2. Запросить risk_level по figi
            instrument_info = self.tbank_api_client.get_instrument_by_figi(figi)
            current_risk_level = instrument_info.risk_level if instrument_info else None

            # 3. Получаем старое значение из БД
            last_check = self.db.get_last_monitoring_check(isin, self.name)

            # 4. СРАЗУ сохраняем новое значение.
            value_to_save = str(current_risk_level) if current_risk_level is not None else None
            self.db.add_monitoring_check(isin, self.name, value_to_save)

            # 5. Обрабатываем случай, когда старого значения нет (первый запуск)
            if not last_check:
                message = f"{self.verbose_name}: {current_risk_level if current_risk_level is not None else 'не определен'}"
                return MonitoringResult(
                    adapter_name=self.name,
                    isin=isin,
                    status='new',
                    message=message,
                    is_significant=False
                )

            # 6. Если старое значение есть, парсим его
            saved_value = last_check[1]
            latest_saved_level = None
            if saved_value is not None:
                try:
                    latest_saved_level = int(saved_value)
                except (ValueError, TypeError):
                    logger.warning(f"Не удалось преобразовать в int предыдущее значение '{saved_value}' для {isin}")

            # 7. Сравниваем и возвращаем результат
            return self._compare_and_create_result(isin, latest_saved_level, current_risk_level)
        except Exception as e:
            logger.error(f"Ошибка при работе адаптера {self.name} для {isin}: {e}", exc_info=True)
            return self._handle_error(isin, f"Ошибка: {e}")

    def _compare_and_create_result(self, isin: str, old_value: Optional[int], new_value: Optional[int]) -> MonitoringResult:
        """Сравнивает значения (включая None) и формирует результат."""
        if old_value == new_value:
            value_str = new_value if new_value is not None else "не определен"
            return MonitoringResult(self.name, isin, 'no_change', f"без изменений ({value_str})", is_significant=False)

        old_str = old_value if old_value is not None else "N/A"
        new_str = new_value if new_value is not None else "N/A"
        message = f"{self.verbose_name} изменился: {old_str} -> {new_str}"

        # Считаем изменение значимым, если уровень риска повысился (число стало больше) или он исчез
        is_significant = (old_value is not None and new_value is None) or \
                         (old_value is not None and new_value is not None and new_value > old_value)

        return MonitoringResult(self.name, isin, 'changed', message, is_significant=is_significant)

    def _handle_error(self, isin: str, message: str) -> MonitoringResult:
        return MonitoringResult(self.name, isin, 'error', message)