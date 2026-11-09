import argparse
import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from src.use_cases.base import UseCase
from src.use_cases.interfaces import ICbrApiClient

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class UpdateFloatersUseCase(UseCase):
    """
    Обновляет процентную ставку купона для облигаций с плавающим купоном (флоатеров)
    в таблице `bonds_catalog`.
    """

    def __init__(self, factory: 'UseCaseFactory', cbr_client: ICbrApiClient):
        super().__init__(factory)
        self.cbr_client = cbr_client

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Для этого use case аргументы не требуются."""
        pass

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'UpdateFloatersUseCase':
        """Создает экземпляр `UpdateFloatersUseCase`."""
        cbr_client = factory.get_cbr_api_client()
        return cls(factory, cbr_client)

    def execute(self, args: argparse.Namespace):
        """Выполняет обновление ставок для флоатеров."""
        logger.info("Запуск обновления ставок для облигаций-флоатеров...")

        # 1. (Новый шаг) Вызов use case для автоматического расчета спредов
        try:
            logger.info("Запускаем автоматический расчет спредов для флоатеров в портфеле...")
            calculate_spread_uc = self.factory.get_use_case('calculate-spread')
            # Мы передаем Namespace(ticker=None), чтобы запустить расчет для всего портфеля
            calculate_spread_uc.execute(argparse.Namespace(ticker=None))
            logger.info("Автоматический расчет спредов завершен.")
        except Exception as e:
            logger.error(f"Произошла ошибка во время автоматического расчета спредов: {e}", exc_info=True)
            # Мы не прерываем основной процесс, а просто логируем ошибку

        try:
            # 2. Получаем последнюю ставку RUONIA
            ruonia_rate = self.cbr_client.get_latest_ruonia_rate()
            if ruonia_rate is None:
                logger.error("Не удалось получить актуальную ставку RUONIA. Обновление прервано.")
                return
            logger.info(f"Актуальная ставка RUONIA: {ruonia_rate}%")

            # 3. Получаем список всех флоатеров из портфеля
            portfolio_floaters = self.storage.get_all_floaters()
            if not portfolio_floaters:
                logger.info("Облигации с плавающим купоном в портфеле не найдены.")
                return
            
            # Создаем уникальный список ISIN для обработки
            isins_to_process = sorted(list({bond.isin for bond in portfolio_floaters}))
            logger.info(f"Найдено {len(isins_to_process)} уникальных облигаций-флоатеров в портфеле для обновления.")

            updated_count = 0
            # 4. Обновляем каждую облигацию по уникальному ISIN
            for isin in isins_to_process:
                bond = self.storage.get_bond_by_isin(isin)
                if not bond:
                    logger.warning(f"Не удалось получить данные для ISIN {isin} перед обновлением. Пропускаем.")
                    continue

                if bond.coupon_spread is None:
                    logger.warning(f"Для облигации {bond.isin} ({bond.name}) не задан спред. Пропускаем.")
                    continue

                try:
                    old_rate = bond.coupon_rate_percent
                    spread = Decimal(str(bond.coupon_spread))
                    new_rate = ruonia_rate + spread
                    
                    # Обновляем поле в базе данных
                    self.storage.update_bond_coupon_rate(bond.isin, new_rate)
                    
                    # Форматируем старую ставку для лога
                    old_rate_str = f"{old_rate}%" if old_rate is not None else "NULL"

                    logger.info(
                        f"Обновлена ставка для {bond.isin} ({bond.name}): "
                        f"старая ставка {old_rate_str}, новая {new_rate:.4f}%"
                    )
                    updated_count += 1
                except InvalidOperation:
                    logger.error(f"Некорректный формат спреда '{bond.coupon_spread}' для {bond.isin}. Пропускаем.")
                except Exception as e:
                    logger.error(f"Ошибка при обновлении ставки для {bond.isin}: {e}", exc_info=True)

            logger.info(f"Обновление завершено. Успешно обновлено {updated_count} из {len(isins_to_process)} облигаций.")

        except Exception as e:
            logger.critical(f"Критическая ошибка в процессе обновления ставок флоатеров: {e}", exc_info=True)
