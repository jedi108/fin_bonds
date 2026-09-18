import logging
import yaml
import sys
from pathlib import Path
from datetime import date, timedelta
import random
import argparse
from typing import Dict, Any, TYPE_CHECKING

from src.monitoring.storage import Database
from src.monitoring.data_fetcher import get_bond_details
from src.utils import load_isins_from_file
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class SeedDataUseCase(UseCase):
    """
    Заполняет базу данных тестовыми историческими данными о рейтингах
    без запроса подтверждения. Используется для разработки и тестирования.
    """
    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """
        Команда не принимает аргументов командной строки,
        поскольку все параметры берутся из конфигурационного файла.
        """
        pass

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'SeedDataUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(
            config=factory.config, 
            db=factory.get_db_connection(), 
            tinkoff_token=factory.tinkoff_token
        )

    def __init__(self, config: Dict[str, Any], db: Database, tinkoff_token: str):
        self.config = config
        self.db = db
        self.tinkoff_token = tinkoff_token

    def execute(self, args: argparse.Namespace):
        logger.info("Запуск заполнения базы данных тестовыми данными...")
        rating_scale = self.config.get('rating_scale', {})

        # 1. Получаем ISIN из файла
        isins_to_seed = load_isins_from_file(self.config.get('portfolio_isins_txt_file'))
        if not isins_to_seed:
            logger.warning("Список ISIN для обработки пуст. Заполнение невозможно.")
            return

        # Пытаемся получить информацию из Tinkoff API, если есть токен
        bonds_info = []
        if self.tinkoff_token and 'YOUR_TINKOFF' not in self.tinkoff_token:
            logger.info(f"Получение информации для {len(isins_to_seed)} облигаций из Tinkoff API...")
            bonds_info = get_bond_details(self.tinkoff_token, isins_to_seed)
        else:
            logger.warning("Токен Tinkoff не найден или используется заглушка. Будут использованы ISIN вместо имен.")
            # Создаем "заглушки" на основе ISIN
            bonds_info = [{'isin': isin, 'name': isin, 'ticker': isin} for isin in isins_to_seed]

        if not bonds_info:
            logger.warning("Не удалось получить информацию ни по одной облигации. Завершение работы.")
            return

        # 2. Удаляем существующие исторические данные
        logger.info("Очистка старых данных (рейтинги, уровни риска и информация об облигациях)...")
        conn = self.db._get_connection()
        conn.execute("DELETE FROM rating_history;")
        conn.execute("DELETE FROM listlevel_history;")
        conn.execute("DELETE FROM risk_history;")
        conn.execute("DELETE FROM bonds;")
        conn.commit()
        
        # 3. Генерируем и вставляем тестовые данные
        logger.info("Генерация и вставка тестовых данных...")
        for bond in bonds_info:
            # Используем доступ через атрибуты, так как bond - это dataclass BondData
            bond_name = bond.name
            bond_ticker = bond.ticker
            isin = bond.isin

            logger.info(f"  -> Для {bond_name} ({bond_ticker})")
            self.db.add_or_update_bond(isin, bond_ticker, bond_name)
            
            start_rating_score = random.randint(4, 9)
            start_risk_level = random.randint(1, 2)

            for i in range(180, 0, -15): 
                current_date = date.today() - timedelta(days=i)
                
                # Генерация кредитного рейтинга
                rating_change = random.choice([-1, 0, 0, 0, 1])
                current_rating_score = max(0, min(10, start_rating_score + rating_change))
                rating_value = next((k for k, v in rating_scale.items() if v == current_rating_score), "N/A")
                self.db.add_rating(
                    isin=isin, rating_date=current_date, agency="АКРА (тест)",
                    value=rating_value, score=current_rating_score
                )

                # Генерация уровня риска, если включено в конфиге
                if self.config.get('use_tinkoff_risk_level', False):
                    risk_change = random.choice([0, 0, 0, 0, 1])
                    current_risk_level = max(1, min(3, start_risk_level + risk_change))
                    self.db.add_risk_level(
                        isin=isin, risk_date=current_date, risk_level=current_risk_level
                    )
                    start_risk_level = current_risk_level
                
                start_rating_score = current_rating_score

        logger.info("База данных успешно заполнена тестовыми данными.") 