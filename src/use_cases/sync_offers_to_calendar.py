import argparse
import logging
from typing import TYPE_CHECKING, List
from src.use_cases.base import UseCase
from src.services.calendar_generator import CalendarGenerator
from src.monitoring.notifier import TelegramNotifier
from src.use_cases.interfaces import IPortfolioStorage
import tempfile
import os

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

class SyncOffersToCalendarUseCase(UseCase):
    """
    Генерирует .ics файл с датами оферт и отправляет его в Telegram или сохраняет на диск.
    """
    def __init__(self, storage: IPortfolioStorage, notifier: TelegramNotifier, config: dict):
        self.storage = storage
        self.notifier = notifier
        self.config = config

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--output-mode',
            type=str,
            choices=['telegram', 'file'],
            default='telegram',
            help="Куда отправлять .ics файл: 'telegram' (по умолчанию) или 'file' (сохранить на диск)"
        )
        parser.add_argument(
            '--output-path',
            type=str,
            default=None,
            help="Путь для сохранения .ics файла (используется только при --output-mode file)"
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'SyncOffersToCalendarUseCase':
        return cls(
            storage=factory.get_db_connection(),
            notifier=factory.get_notifier(),
            config=factory.config
        )

    def execute(self, args: argparse.Namespace):
        logger.info("Запуск генерации .ics файла с датами оферт...")
        bonds = self.storage.get_portfolio_bonds_for_monitoring()
        # Оставляем только те, у кого есть offer_date
        bonds_with_offers = [b for b in bonds if getattr(b, 'offer_date', None)]
        # Преобразуем к dict для CalendarGenerator
        bonds_dicts = [
            {
                'isin': getattr(b, 'isin', None),
                'name': getattr(b, 'name', None),
                'offer_date': getattr(b, 'offer_date', None)
            }
            for b in bonds_with_offers
        ]
        if not bonds_dicts:
            logger.info("Нет облигаций с датами оферт для генерации календаря.")
            return
        calendar_generator = CalendarGenerator()
        if args.output_mode == 'telegram':
            with tempfile.TemporaryDirectory() as tmpdir:
                filepath = calendar_generator.generate_calendar(bonds_dicts, filename="offers.ics")
                self.notifier.send_document(filepath, caption="Файл с датами оферт. Импортируйте в календарь!")
                logger.info(f"Файл отправлен в Telegram: {filepath}")
        elif args.output_mode == 'file':
            output_path = args.output_path or os.path.abspath("offers.ics")
            calendar_generator = CalendarGenerator(output_dir=os.path.dirname(output_path))
            filepath = calendar_generator.generate_calendar(bonds_dicts, filename=os.path.basename(output_path))
            logger.info(f"Файл сохранен: {filepath}") 