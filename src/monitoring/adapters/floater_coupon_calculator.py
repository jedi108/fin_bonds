import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, TYPE_CHECKING
from .base import BaseAdapter, MonitoringResult
from src.monitoring.storage import Database
from src.use_cases.interfaces import ICbrApiClient

if TYPE_CHECKING:
    from src.data_models import PortfolioBond

logger = logging.getLogger(__name__)


class FloaterCouponCalculatorAdapter(BaseAdapter):
    """
    Адаптер-калькулятор для прогнозирования ставки купона у флоатеров.
    Использует ставку RUONIA от ЦБ и спред из локальной БД.
    """
    def __init__(self, db: Database, config: dict, bond_data: 'PortfolioBond', **dependencies):
        super().__init__(db, config, bond_data, **dependencies)
        self.cbr_client: ICbrApiClient = self.dependencies['cbr_client']
        self.verbose_name = "Расчетный купон флоатера"
        logger.info("Адаптер для расчета купонов флоатеров инициализирован.")

    @property
    def name(self) -> str:
        return 'floater_coupon_calculator'

    def fetch(self) -> MonitoringResult:
        """
        Выполняет расчет прогнозной ставки купона для флоатера,
        сохраняет результат и определяет, изменилось ли значение.
        """
        if not self.bond_data:
            return MonitoringResult(self.name, "Unknown", 'error', 'bond_data не передан в адаптер')
            
        isin = self.bond_data.isin

        if not self.bond_data.floating_coupon_flag:
            return MonitoringResult(self.name, isin, 'skipped', 'Облигация не является флоатером.')

        spread = self.bond_data.coupon_spread
        if spread is None:
            return MonitoringResult(self.name, isin, 'skipped', 'Для облигации не задан спред (coupon_spread).')

        try:
            # 1. Рассчитываем новую ставку
            ruonia_rate = self.cbr_client.get_latest_ruonia_rate()
            if ruonia_rate is None:
                return MonitoringResult(self.name, isin, 'error', 'Не удалось получить ставку RUONIA.')
            
            calculated_rate = ruonia_rate + Decimal(str(spread))
            
            # 2. Получаем предыдущее значение из общей таблицы мониторинга
            last_check = self.db.get_last_monitoring_check(isin, self.name)
            previous_rate = None
            if last_check and last_check[1] is not None:
                try:
                    previous_rate = Decimal(last_check[1])
                except InvalidOperation:
                    logger.warning(f"Не удалось преобразовать в Decimal предыдущее значение '{last_check[1]}' для {isin}")

            # 3. Сохраняем новое значение в общую таблицу мониторинга
            self.db.add_monitoring_check(isin, self.name, f"{calculated_rate:.4f}")

            # 4. Сравниваем и формируем результат, следуя общему паттерну
            if previous_rate is None:
                message = f"{self.verbose_name}: {calculated_rate:.4f}%"
                return MonitoringResult(self.name, isin, 'new', message, is_significant=False)
            
            if previous_rate.quantize(Decimal('0.0001')) != calculated_rate.quantize(Decimal('0.0001')):
                # Убеждаемся, что порядок "старое -> новое" правильный.
                # previous_rate - это старое значение из БД.
                # calculated_rate - это только что вычисленное новое значение.
                message = f"{self.verbose_name} изменился: {previous_rate:.4f}% -> {calculated_rate:.4f}%"
                # Помечаем изменение как значительное
                return MonitoringResult(self.name, isin, 'changed', message, is_significant=True)

            message = f"без изменений ({calculated_rate:.4f}%)"
            return MonitoringResult(self.name, isin, 'no_change', message, is_significant=False)

        except Exception as e:
            logger.error(f"Ошибка при расчете купона для {isin}: {e}", exc_info=True)
            return MonitoringResult(self.name, isin, 'error', f"Исключение: {e}") 