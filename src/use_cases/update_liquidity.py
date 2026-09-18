import logging
import argparse
from typing import List
from decimal import Decimal

from src.use_cases.base import UseCase
from src.monitoring.adapters.factory import AdapterFactory
from src.monitoring.storage import Database

logger = logging.getLogger(__name__)

class UpdateLiquidityUseCase(UseCase):
    """
    Обновляет данные о ликвидности (liquidity_loss_ratio) для позиций в портфеле.
    Анализирует только бумаги, которые есть в портфеле.
    """
    
    def __init__(self, factory):
        super().__init__(factory)
        self.db = factory.get_db_connection()
        self.adapter_factory = factory.get_adapter_factory()

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--all',
            action='store_true',
            help="Обновить ликвидность для всех позиций в портфеле (по умолчанию только для тех, у кого liquidity_loss_ratio = NULL)"
        )
        parser.add_argument(
            '--force',
            action='store_true', 
            help="Принудительно обновить ликвидность для всех позиций (перезаписать существующие значения)"
        )

    @classmethod
    def create(cls, factory) -> 'UpdateLiquidityUseCase':
        return cls(factory)

    def execute(self, args: argparse.Namespace):
        logger.info("Запуск обновления данных о ликвидности для позиций в портфеле...")
        
        # Получаем позиции портфеля
        portfolio_positions = self.db.get_portfolio_positions()
        if not portfolio_positions:
            logger.warning("В портфеле нет позиций для анализа ликвидности.")
            return

        # Фильтруем позиции в зависимости от аргументов
        positions_to_analyze = self._filter_positions(portfolio_positions, args)
        
        if not positions_to_analyze:
            logger.info("Нет позиций для анализа ликвидности после фильтрации.")
            return

        logger.info(f"Анализируем ликвидность для {len(positions_to_analyze)} позиций...")
        
        updated_count = 0
        for position in positions_to_analyze:
            try:
                # Получаем данные облигации из каталога
                bond_data = self.db.get_bond_by_isin(position.isin)
                if not bond_data:
                    logger.warning(f"Облигация {position.isin} не найдена в каталоге, пропускаем.")
                    continue

                # Создаем PortfolioBond для адаптера
                from src.data_models import PortfolioBond
                portfolio_bond = PortfolioBond(
                    isin=bond_data.isin,
                    figi=bond_data.figi,
                    ticker=bond_data.ticker,
                    name=bond_data.name,
                    currency=bond_data.currency,
                    nominal=bond_data.nominal,
                    maturity_date=bond_data.maturity_date,
                    offer_date=bond_data.offer_date,
                    coupon_quantity_per_year=bond_data.coupon_quantity_per_year,
                    coupon_rate_percent=bond_data.coupon_rate_percent,
                    coupon_type=bond_data.coupon_type,
                    coupon_spread=bond_data.coupon_spread,
                    list_level=bond_data.list_level,
                    risk_level=bond_data.risk_level,
                    issue_size=bond_data.issue_size,
                    is_trade_available=bond_data.is_trade_available,
                    is_for_qualified_investors=bond_data.is_for_qualified_investors,
                    floating_coupon_flag=bond_data.floating_coupon_flag,
                    perpetual_flag=bond_data.perpetual_flag,
                    amortization_flag=bond_data.amortization_flag,
                    quantity=position.quantity,
                    average_price=position.average_price,
                    current_price=position.current_price,
                    yield_to_maturity=position.yield_to_maturity,
                    portfolio_percent=position.portfolio_percent,
                    current_value=position.current_value,
                    broker_name=position.broker_name
                )

                # Получаем адаптер для анализа ликвидности
                liquidity_adapter = self.adapter_factory.get_adapter('liquidity_analyzer', portfolio_bond)
                if not liquidity_adapter:
                    logger.error("Не удалось создать адаптер для анализа ликвидности.")
                    continue

                # Анализируем ликвидность
                liquidity_result = liquidity_adapter.fetch()
                if liquidity_result and liquidity_result.value:
                    # Обновляем позицию в портфеле
                    self._update_position_liquidity(position.isin, position.broker_name, liquidity_result.value)
                    updated_count += 1
                    logger.info(f"Обновлена ликвидность для {position.isin} ({position.broker_name}): {liquidity_result.value}")
                else:
                    logger.warning(f"Не удалось получить данные о ликвидности для {position.isin}")

            except Exception as e:
                logger.error(f"Ошибка при анализе ликвидности для {position.isin}: {e}")

        logger.info(f"Обновление ликвидности завершено. Обновлено позиций: {updated_count}")

    def _filter_positions(self, positions: List, args: argparse.Namespace) -> List:
        """Фильтрует позиции в зависимости от аргументов команды."""
        if args.force:
            logger.info("Режим --force: анализируем все позиции в портфеле")
            return positions
        
        if args.all:
            logger.info("Режим --all: анализируем все позиции в портфеле")
            return positions
        
        # По умолчанию анализируем только позиции без данных о ликвидности
        positions_without_liquidity = [
            pos for pos in positions 
            if pos.liquidity_loss_ratio is None
        ]
        logger.info(f"Анализируем только позиции без данных о ликвидности: {len(positions_without_liquidity)} из {len(positions)}")
        return positions_without_liquidity

    def _update_position_liquidity(self, isin: str, broker_name: str, liquidity_value: str):
        """Обновляет значение liquidity_loss_ratio для позиции в портфеле."""
        try:
            self.db.update_position_liquidity(isin, broker_name, float(liquidity_value))
        except Exception as e:
            logger.error(f"Ошибка при обновлении ликвидности для {isin} ({broker_name}): {e}")
            raise 