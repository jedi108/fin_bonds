import logging
import argparse
from typing import Dict, Any, TYPE_CHECKING

from src.monitoring.storage import Database
from src.monitoring.plotter import plot_ratings_history, plot_risk_history, plot_listlevel_history, plot_liquidity_history
from src.utils import load_isins_from_file
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class GeneratePlotsUseCase(UseCase):
    """
    Генерирует и сохраняет графики динамики кредитных рейтингов,
    уровней листинга и уровней риска Tinkoff для облигаций из портфеля
    и списка наблюдения.
    
    Логика зависит от выбранного в конфиге адаптера мониторинга.
    """
    def __init__(self, config: Dict[str, Any], db: Database):
        self.config = config
        self.db = db

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """
        Команда не принимает аргументов командной строки,
        поскольку все параметры берутся из конфигурационного файла.
        """
        pass

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'GeneratePlotsUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(config=factory.config, db=factory.get_db_connection())

    def execute(self, args: argparse.Namespace):
        logger.info("Построение графиков для бумаг из портфеля и списка наблюдения...")

        # Загружаем ISINы из обоих файлов
        portfolio_isins = set(load_isins_from_file(self.config.get('portfolio_file', '')))
        watchlist_isins = set(load_isins_from_file(self.config.get('watchlist_file', '')))
        all_isins = list(portfolio_isins.union(watchlist_isins))

        if not all_isins:
            logger.warning("Списки ISIN-кодов (портфель и наблюдение) пусты. Построение графиков невозможно.")
            return

        logger.info(f"Будут построены графики для {len(all_isins)} ISIN-кодов из текстовых файлов.")
        
        output_dir = self.config['plotter']['output_dir']

        # --- График кредитных рейтингов ---
        rating_scale = {k.upper(): v for k, v in self.config.get('rating_scale', {}).items()}
        rating_history = self.db.get_rating_history_for_bonds(all_isins)
        if rating_history:
            plot_ratings_history(
                history_data=rating_history,
                title="Динамика кредитных рейтингов",
                output_filename="rating_dynamics.html",
                rating_scale=rating_scale,
                output_dir=output_dir
            )

        # --- График уровней листинга ---
        listlevel_history = self.db.get_listlevel_history_for_bonds(all_isins)
        if listlevel_history:
            plot_listlevel_history(
                history_data=listlevel_history,
                title="Динамика уровней листинга MOEX",
                output_filename="listlevel_dynamics.html",
                output_dir=output_dir
            )

        # --- График уровней риска Tinkoff ---
        if self.config.get('track_tinkoff_risk', False):
            risk_history = self.db.get_risk_history_for_bonds(all_isins)
            if risk_history:
                plot_risk_history(
                    history_data=risk_history,
                    title="Динамика уровня риска Tinkoff",
                    output_filename="risk_dynamics.html",
                    output_dir=output_dir
                )

        # --- График ликвидности ---
        liquidity_history = self.db.get_liquidity_history_for_bonds(all_isins)
        if liquidity_history:
            plot_liquidity_history(
                history_data=liquidity_history,
                title="Динамика ликвидности облигаций",
                output_filename="liquidity_dynamics.html",
                output_dir=output_dir
            )
        
        logger.info(f"Графики сохранены в директорию '{output_dir}'.") 