from abc import ABC, abstractmethod
import argparse
from typing import TYPE_CHECKING

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

from src.use_cases.interfaces import IPortfolioStorage


class UseCase(ABC):
    """Абстрактный базовый класс для всех команд приложения."""

    def __init__(self, factory: 'UseCaseFactory'):
        self.factory = factory
        self.storage: IPortfolioStorage = factory.get_db_connection()

    @staticmethod
    @abstractmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """
        Добавляет аргументы командной строки, специфичные для этого use case.
        Вызывается для настройки саб-парсера команды.
        """
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def create(cls, factory: 'UseCaseFactory') -> 'UseCase':
        """
        Фабричный метод для создания экземпляра UseCase.
        Получает зависимости (например, db, config) из factory.
        """
        raise NotImplementedError

    @abstractmethod
    def execute(self, args: argparse.Namespace):
        """
        Основной метод, выполняющий логику use case.
        Получает доступ к аргументам командной строки через объект `args`.
        """
        raise NotImplementedError
