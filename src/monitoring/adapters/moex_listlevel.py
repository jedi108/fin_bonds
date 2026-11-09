import logging
from datetime import date
from typing import TYPE_CHECKING

from .base import BaseAdapter, MonitoringResult
from src.use_cases.interfaces import IMoexApiClient

if TYPE_CHECKING:
    from src.data_models import PortfolioBond

logger = logging.getLogger(__name__)


class MoexListLevelAdapter(BaseAdapter):
    """Адаптер для проверки изменения уровня листинга на Московской Бирже."""

    @property
    def name(self) -> str:
        return 'moex_listlevel'

    def __init__(self, db, config, bond_data: 'PortfolioBond', moex_client: IMoexApiClient, **kwargs):
        super().__init__(db, config, bond_data, **kwargs)
        self.moex_client = moex_client
        self.verbose_name = "Уровень листинга MOEX"

    def fetch(self) -> MonitoringResult:
        """
        Получает и обновляет уровень листинга для облигации.
        """
        if not self.bond_data:
            return self._handle_error("Unknown", "bond_data не передан в адаптер")
            
        isin = self.bond_data.isin
        
        try:
            bond_info = self.moex_client.get_bond_by_isin(isin)
            if not bond_info:
                return self._handle_error(isin, "Бонд не найден в MOEX")

            current_level = bond_info.get('list_level')
            if current_level is None:
                return MonitoringResult(self.name, isin, 'skipped', 'В ответе MOEX API отсутствует уровень листинга.')

        except Exception as e:
            logger.error(f"Ошибка при получении данных от MOEX API для {isin}: {e}")
            return self._handle_error(isin, f"Ошибка MOEX API: {e}")

        last_check = self.db.get_last_monitoring_check(isin, self.name)
        # metric_value хранится как TEXT, поэтому приводим к int
        latest_saved_level = int(last_check[1]) if last_check else None

        self.db.add_monitoring_check(isin, self.name, current_level)

        if latest_saved_level is None:
            return MonitoringResult(
                adapter_name=self.name,
                isin=isin,
                status='success',
                message=f"Первичная проверка: уровень листинга {current_level}.",
                value=current_level
            )

        if current_level != latest_saved_level:
            change_type = 'upgrade' if current_level < latest_saved_level else 'downgrade'
            message = f"Уровень листинга изменился: {latest_saved_level} -> {current_level}."
            return MonitoringResult(
                adapter_name=self.name,
                isin=isin,
                status='changed',
                message=message,
                value=current_level,
                is_significant=True
            )

        return MonitoringResult(
            adapter_name=self.name,
            isin=isin,
            status='success',
            message=f"Уровень листинга без изменений: {current_level}.",
            value=current_level
        )

    def _handle_error(self, isin: str, message: str) -> MonitoringResult:
        return MonitoringResult(self.name, isin, 'error', message)

    def _update_db(self, isin: str, current_level: int) -> tuple[int, int]:
        """
        Обновляет уровень листинга в базе данных.
        """
        last_check = self.db.get_last_monitoring_check(isin, self.name)
        # metric_value хранится как TEXT, поэтому приводим к int
        latest_saved_level = int(last_check[1]) if last_check else None

        self.db.add_monitoring_check(isin, self.name, current_level)

        return latest_saved_level, current_level