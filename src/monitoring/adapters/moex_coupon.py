import logging
from typing import Optional
from decimal import Decimal, InvalidOperation

from .base import BaseAdapter, MonitoringResult
from src.use_cases.interfaces import IMoexApiClient
from src.monitoring.storage import Database

logger = logging.getLogger(__name__)


class MoexCouponAdapter(BaseAdapter):
    """Адаптер для проверки изменения текущего купона на Московской Бирже."""

    @property
    def name(self) -> str:
        return 'moex_coupon'

    def __init__(self, db: Database, config: dict, bond_data, **dependencies):
        super().__init__(db, config, bond_data, **dependencies)
        self.moex_client: IMoexApiClient = self.dependencies['moex_client']
        self.verbose_name = "Ставка купона MOEX"

    def fetch(self) -> MonitoringResult:
        """
        Проверяет текущую ставку купона облигации.
        """
        isin = self.bond_data.isin
        bond_from_db = self.bond_data
        
        if not bond_from_db:            return MonitoringResult(self.name, isin, 'error', f'Облигация {isin} не найдена в каталоге.')

        if not bond_from_db.floating_coupon_flag:
            return MonitoringResult(self.name, isin, 'skipped', 'Облигация не является флоатером.')

        try:
            # Используем более надежный метод get_bond_data
            moex_data = self.moex_client.get_bond_data(isin, bond_from_db.nominal)
            if not moex_data or moex_data.coupon_rate is None:
                msg = 'Не удалось получить ставку купона от MOEX API.'
                logger.warning(f"[{self.name}] {isin}: {msg}")
                return MonitoringResult(self.name, isin, 'skipped', msg)

            current_rate = moex_data.coupon_rate

        except Exception as e:
            logger.error(f"Ошибка при получении данных от MOEX API для {isin}: {e}", exc_info=True)
            return MonitoringResult(self.name, isin, 'error', f"Критическая ошибка MOEX API: {e}")

        # Используем актуальные методы для работы с БД
        last_check = self.db.get_last_monitoring_check(isin, self.name)
        latest_saved_rate = None
        if last_check and last_check[1] is not None:
            try:
                latest_saved_rate = Decimal(last_check[1])
            except InvalidOperation:
                logger.warning(f"Не удалось преобразовать в Decimal предыдущее значение '{last_check[1]}' для {isin}")

        # Сохраняем новое значение
        self.db.add_monitoring_check(isin, self.name, f"{current_rate:.4f}")

        if latest_saved_rate is None:
            return MonitoringResult(
                adapter_name=self.name,
                isin=isin,
                status='new',
                message=f"{self.verbose_name}: {current_rate:.2f}%",
                is_significant=False
            )

        # Сравниваем с точностью до 2 знаков после запятой
        # Округляем до 2 знаков для сравнения, чтобы избежать ложных срабатываний из-за точности
        current_rate_quantized = current_rate.quantize(Decimal('0.01'))
        latest_saved_rate_quantized = latest_saved_rate.quantize(Decimal('0.01'))

        if current_rate_quantized != latest_saved_rate_quantized:
            message = f"{self.verbose_name} изменилась: {latest_saved_rate:.2f}% -> {current_rate:.2f}%."
            
            # Считаем изменение значимым только если новая ставка МЕНЬШЕ старой
            is_significant = current_rate_quantized < latest_saved_rate_quantized

            if is_significant:
                logger.info(f"Обнаружено ЗНАЧИМОЕ падение ставки для {isin}: {latest_saved_rate_quantized}% -> {current_rate_quantized}%")
            else:
                logger.info(f"Обнаружено НЕЗНАЧИМОЕ изменение ставки для {isin}: {latest_saved_rate_quantized}% -> {current_rate_quantized}%")
            
            return MonitoringResult(
                adapter_name=self.name,
                isin=isin,
                status='changed',
                message=message,
                is_significant=is_significant
            )

        return MonitoringResult(
            adapter_name=self.name,
            isin=isin,
            status='no_change',
            message=f"без изменений ({current_rate:.2f}%)",
            is_significant=False
        )