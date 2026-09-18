"""
Use case для автоматического расчета спреда для флоатеров.
"""
import logging
import argparse
from typing import TYPE_CHECKING
from decimal import Decimal
from datetime import datetime, timezone

from src.use_cases.base import UseCase
from src.use_cases.interfaces import IPortfolioStorage
from tinkoff.invest import Client, RequestError
from tinkoff.invest.utils import quotation_to_decimal
from src.use_cases.interfaces import ITbankApiClient
from src.data_models import Bond

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class CalculateSpreadUseCase(UseCase):
    """
    Рассчитывает и сохраняет спред для облигации с плавающим купоном.
    Использует историю выплат из Tinkoff API и исторические ставки RUONIA от ЦБ РФ.

    ВАЖНО: Данный метод основан на анализе *прошлых* купонных выплат.
    Если эмитент изменяет спред в дату оферты, этот калькулятор сможет
    узнать новый спред только после первой фактической выплаты купона
    по новым условиям. Это создает временной лаг (обычно один купонный период),
    в течение которого прогнозы доходности могут быть неверными.
    """
    def __init__(self, factory: 'UseCaseFactory'):
        self.factory = factory
        self.db = self.factory.get_db_connection()
        self.cbr_client = self.factory.get_cbr_api_client()
        self.tinkoff_token = self.factory.tinkoff_token

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды."""
        parser.add_argument(
            '--ticker',
            type=str,
            required=False,
            help='Тикер облигации для расчета спреда. Если не указан, расчет будет для всех флоатеров в портфеле.'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'CalculateSpreadUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(factory)

    def execute(self, args: argparse.Namespace):
        if args.ticker:
            self._execute_for_single_ticker(args.ticker)
        else:
            self._execute_for_portfolio()

    def _execute_for_single_ticker(self, ticker: str):
        """Выполняет расчет для одной облигации по тикеру."""
        logger.info(f"🚀 Запуск расчета спреда для {ticker}...")

        all_bonds = self.db.get_full_catalog()
        bond = next((b for b in all_bonds if b.ticker == ticker), None)

        if not bond:
            logger.error(f"Облигация с тикером {ticker} не найдена в локальном каталоге.")
            return

        self._calculate_and_save_spread(bond)

    def _execute_for_portfolio(self):
        """Выполняет расчет для всех подходящих облигаций в портфеле."""
        logger.info("🚀 Запуск автоматического расчета спреда для всех флоатеров в портфеле...")
        bonds_to_process = self.db.get_portfolio_floaters_without_spread()
        
        if not bonds_to_process:
            logger.info("✅ Не найдено облигаций для расчета спреда (флоатеры в портфеле, где спред еще не указан).")
            return

        logger.info(f"Найдено {len(bonds_to_process)} облигаций для обработки.")
        
        success_count = 0
        fail_count = 0

        for bond in bonds_to_process:
            try:
                logger.info(f"--- Обработка {bond.name} ({bond.ticker}) ---")
                self._calculate_and_save_spread(bond)
                success_count += 1
            except Exception as e:
                logger.error(f"❌ Ошибка при обработке {bond.name} ({bond.ticker}): {e}", exc_info=True)
                fail_count += 1
        
        logger.info("---")
        logger.info("🏁 Автоматический расчет спреда завершен.")
        logger.info(f"Успешно обработано: {success_count}")
        logger.info(f"С ошибками: {fail_count}")

    def _calculate_and_save_spread(self, bond: Bond):
        """
        Основная логика расчета и сохранения спреда для одной облигации.
        """
        if not bond.floating_coupon_flag:
            logger.warning(f"Облигация {bond.name} ({bond.ticker}) не является флоатером. Пропуск.")
            return

        if not bond.figi:
            logger.error(f"У облигации {bond.name} ({bond.ticker}) отсутствует FIGI. Невозможно получить историю купонов.")
            return

        logger.info(f"✅ Проверки пройдены для {bond.name}. Облигация является флоатером и имеет FIGI.")

        # 2. Получение истории купонов (Tinkoff)
        try:
            logger.info(f"Запрос истории купонов для FIGI: {bond.figi}")
            with Client(self.tinkoff_token) as client:
                coupons_response = client.instruments.get_bond_coupons(figi=bond.figi)
        except RequestError as e:
            logger.error(f"Ошибка Tinkoff API при запросе купонов для {bond.ticker}: {e.details}")
            # Поднимаем исключение, чтобы его можно было поймать в цикле
            raise RuntimeError(f"Tinkoff API error for {bond.ticker}") from e

        if not coupons_response or not coupons_response.events:
            logger.warning(f"Не найдено купонных выплат для {bond.ticker}. Пропуск.")
            return

        logger.info(f"Получено {len(coupons_response.events)} купонных выплат для {bond.ticker}.")

        # 3. Анализ последнего выплаченного купона
        now = datetime.now(timezone.utc)
        paid_coupons = [c for c in coupons_response.events if c.coupon_date <= now]

        if not paid_coupons:
            logger.warning(f"Не найдено выплаченных купонов для {bond.ticker}. Расчет спреда невозможен.")
            return

        # Сортируем по дате выплаты, чтобы найти самый последний
        latest_coupon = sorted(paid_coupons, key=lambda c: c.coupon_date, reverse=True)[0]
        
        logger.info(f"Последний выплаченный купон для {bond.ticker}: дата {latest_coupon.coupon_date.strftime('%Y-%m-%d')}, выплата {quotation_to_decimal(latest_coupon.pay_one_bond)} {bond.currency}")

        # 4. Расчет годовой процентной ставки
        coupon_start_date = latest_coupon.coupon_start_date.date()
        coupon_end_date = latest_coupon.coupon_end_date.date()
        coupon_period_days = (coupon_end_date - coupon_start_date).days
        
        if coupon_period_days <= 0:
            logger.error(f"Некорректный купонный период для {bond.ticker}: {coupon_period_days} дней. Расчет невозможен.")
            return

        pay_one_bond = quotation_to_decimal(latest_coupon.pay_one_bond)
        nominal = Decimal(str(bond.nominal))

        coupon_rate = (pay_one_bond / nominal) * (Decimal('365') / Decimal(coupon_period_days)) * Decimal('100')
        logger.info(f"Расчетная годовая ставка последнего купона для {bond.ticker}: {coupon_rate:.2f}%")

        # 5. Получение ставки RUONIA (ЦБ РФ)
        try:
            ruonia_rate = self.cbr_client.get_ruonia_rate_on_date(coupon_start_date)
            logger.info(f"Ставка RUONIA на {coupon_start_date.strftime('%Y-%m-%d')} составила {ruonia_rate}%")
        except ValueError as e:
            logger.error(f"Не удалось получить ставку RUONIA на дату {coupon_start_date.strftime('%Y-%m-%d')} для {bond.ticker}: {e}")
            raise RuntimeError(f"CBR API error for {bond.ticker}") from e
        
        # 6. Расчет и сохранение спреда
        spread = Decimal(str(coupon_rate)) - Decimal(str(ruonia_rate))
        # Округляем до 2 знаков после запятой для аккуратности
        spread = spread.quantize(Decimal("0.01"))
        
        logger.info(f"Расчетный спред для {bond.ticker}: {coupon_rate:.2f}% (ставка купона) - {ruonia_rate}% (RUONIA) = {spread}%")

        try:
            self.db.set_coupon_spread(bond.isin, float(spread))
            logger.info(f"✅ Спред {spread}% успешно сохранен для {bond.name} ({bond.ticker}).")
        except Exception as e:
            logger.error(f"Не удалось сохранить спред в базу данных для {bond.ticker}: {e}")
            raise RuntimeError(f"DB save error for {bond.ticker}") from e 