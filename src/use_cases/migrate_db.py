import argparse
import logging
import os
import sys
from typing import TYPE_CHECKING

from src.migrations import apply_migrations
from src.use_cases.base import UseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class MigrateDbUseCase(UseCase):
    """
    Сценарий: Применение миграций схемы PostgreSQL.

    CLI-обёртка над src/migrations.py: резолвит DSN (флаг --dsn или
    POSTGRES_DSN из .env) и передаёт его в apply_migrations(), где живёт
    вся логика discovery/применения (advisory lock, таблица учёта версий,
    транзакция на файл). Повторный запуск идемпотентен.
    """

    def __init__(self, factory: 'UseCaseFactory'):
        # Намеренно не вызываем UseCase.__init__: команда работает только
        # с PostgreSQL и не должна открывать SQLite-подключение.
        self.factory = factory

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--dsn', type=str, default=None,
            help='DSN PostgreSQL (postgresql://user:pass@host:port/dbname). '
                 'По умолчанию берется POSTGRES_DSN из .env.'
        )
        parser.add_argument(
            '--status', action='store_true',
            help='Показать примененные и ожидающие миграции, ничего не менять.'
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, какие миграции будут применены, без их выполнения.'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'MigrateDbUseCase':
        return cls(factory)

    @staticmethod
    def _resolve_dsn(args: argparse.Namespace) -> str:
        dsn = args.dsn or os.getenv('POSTGRES_DSN', '')
        if not dsn:
            raise ValueError(
                "Не задан DSN PostgreSQL: укажите --dsn "
                "или переменную POSTGRES_DSN в .env "
                "(формат: postgresql://user:password@host:port/dbname)."
            )
        return dsn

    def execute(self, args: argparse.Namespace):
        logger.info("Executing MigrateDbUseCase")

        try:
            dsn = self._resolve_dsn(args)
            apply_migrations(dsn, dry_run=args.dry_run, status=args.status)
        except Exception as e:
            # main.py глотает исключения команд, поэтому выходим с ненулевым кодом:
            # миграция, молча считающаяся успешной в cron/CI, опаснее ошибки.
            logger.error(f"Ошибка применения миграций: {e}", exc_info=True)
            sys.exit(1)

        logger.info("MigrateDbUseCase finished")
