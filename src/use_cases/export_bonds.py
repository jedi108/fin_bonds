"""
Use case для экспорта отфильтрованного списка облигаций в CSV-файл.
"""
import logging
import csv
import os
import argparse
from typing import TYPE_CHECKING

from src.monitoring.storage import Database
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class ExportBondsUseCase(UseCase):
    """
    Фильтрует облигации по заданным критериям и выгружает их в CSV файл.
    """
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды экспорта облигаций."""
        parser.add_argument('output_file', help='Путь к файлу для сохранения.')
        parser.add_argument('--min-maturity', type=str, default='2026-01-01', help='Мин. дата погашения.')
        parser.add_argument('--max-maturity', type=str, default='2030-12-31', help='Макс. дата погашения.')
        parser.add_argument('--min-coupon-rate', type=float, default=25.0, help='Мин. ставка купона (%%).')
        parser.add_argument('--min-coupon-freq', type=int, default=8, help='Мин. частота купонов в год.')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'ExportBondsUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(db=factory.get_db_connection())

    def execute(self, args: argparse.Namespace):
        logger.info("Запуск use case для экспорта облигаций...")
        logger.info("Критерии фильтрации:")
        logger.info(f"  - Уровень листинга: <= 1")
        logger.info(f"  - Валюта: RUB")
        logger.info(f"  - Не бессрочные")
        logger.info(f"  - Дата погашения: с {args.min_maturity} по {args.max_maturity}")
        logger.info(f"  - Ставка купона: >= {args.min_coupon_rate}%")
        logger.info(f"  - Частота купона: >= {args.min_coupon_freq} раз/год")

        try:
            bonds_to_export = self.db.get_bonds_for_export(
                min_maturity_date=args.min_maturity,
                max_maturity_date=args.max_maturity,
                min_coupon_rate=args.min_coupon_rate,
                min_coupon_frequency=args.min_coupon_freq,
            )

            if not bonds_to_export:
                logger.warning("Не найдено облигаций, соответствующих заданным критериям. Файл не будет создан.")
                return

            logger.info(f"Найдено {len(bonds_to_export)} облигаций для экспорта.")
            logger.info(f"Подготовка к сохранению данных в файл: {args.output_file}")

            bonds_as_dicts = [dict(row) for row in bonds_to_export]
            
            for bond in bonds_as_dicts:
                bond['avg_daily_volume_rub'] = ''
                bond['rating_agency'] = ''
                bond['rating'] = ''

            fieldnames = list(bonds_as_dicts[0].keys()) if bonds_as_dicts else []

            if fieldnames:
                try:
                    with open(args.output_file, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerows(bonds_as_dicts)
                    
                    logger.info(f"Данные успешно сохранены в файл: {args.output_file}")
                    logger.info(f"Полный путь к файлу: {os.path.abspath(args.output_file)}")

                except IOError as e:
                    logger.error(f"Не удалось создать или записать в CSV файл: {e}")

        except Exception as e:
            logger.exception(f"Произошла непредвиденная ошибка в use case: {e}")
        
        finally:
            logger.info("Use case завершил работу.") 