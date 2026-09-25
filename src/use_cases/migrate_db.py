import argparse
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, List, Tuple

import psycopg2

from src.use_cases.base import UseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

# Каталог с SQL-миграциями PostgreSQL: <project_root>/migrations/postgres/
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / 'migrations' / 'postgres'


class MigrateDbUseCase(UseCase):
    """
    Сценарий: Применение миграций схемы PostgreSQL.

    Читает SQL-файлы из migrations/postgres/ (имя вида NNN_description.sql,
    применяются по возрастанию номера NNN), отслеживает примененные версии
    в таблице schema_migrations. Повторный запуск идемпотентен.
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

    @staticmethod
    def _discover_migrations() -> List[Tuple[int, Path]]:
        """Возвращает список (version, path) SQL-миграций, отсортированный по version."""
        if not MIGRATIONS_DIR.is_dir():
            raise FileNotFoundError(f"Каталог миграций не найден: {MIGRATIONS_DIR}")

        migrations: List[Tuple[int, Path]] = []
        for path in sorted(MIGRATIONS_DIR.glob('*.sql')):
            version_str = path.name.split('_', 1)[0]
            if not version_str.isdigit():
                raise ValueError(
                    f"Некорректное имя файла миграции: {path.name}. "
                    "Ожидается формат NNN_description.sql."
                )
            migrations.append((int(version_str), path))

        versions = [version for version, _ in migrations]
        if len(set(versions)) != len(versions):
            raise ValueError("Обнаружены дублирующиеся номера версий миграций.")

        if not migrations:
            logger.warning(f"В каталоге {MIGRATIONS_DIR} нет SQL-файлов миграций.")

        return sorted(migrations, key=lambda item: item[0])

    def _report(self, migrations: List[Tuple[int, Path]], applied: set, pending: List[Tuple[int, Path]]):
        print(f"Миграции: найдено {len(migrations)}, применено {len(applied)}, ожидают {len(pending)}")
        for version, path in pending:
            print(f"  [ ] {version:03d} {path.name}")
        if not pending:
            print("Схема актуальна — нечего применять.")

    def execute(self, args: argparse.Namespace):
        logger.info("Executing MigrateDbUseCase")

        conn = None
        try:
            dsn = self._resolve_dsn(args)
            migrations = self._discover_migrations()
            conn = psycopg2.connect(dsn)
            # Каждая миграция выполняется в своей транзакции;
            # advisory lock защищает от параллельного запуска ранера.
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(hashtext('fin_bonds_migrations'))")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """)
                cursor.execute("SELECT version FROM schema_migrations")
                applied = {row[0] for row in cursor.fetchall()}
                pending = [item for item in migrations if item[0] not in applied]

                self._report(migrations, applied, pending)

                if args.status or args.dry_run:
                    if args.status:
                        print("\nПримененные миграции:")
                        for version, path in migrations:
                            marker = 'x' if version in applied else ' '
                            print(f"  [{marker}] {version:03d} {path.name}")
                    return

                for version, path in pending:
                    sql = path.read_text(encoding='utf-8')
                    logger.info(f"Применение миграции {version:03d} ({path.name})...")
                    cursor.execute("BEGIN")
                    try:
                        cursor.execute(sql)
                        cursor.execute(
                            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                            (version, path.name)
                        )
                        cursor.execute("COMMIT")
                        print(f"  [x] {version:03d} {path.name} — применена")
                    except Exception:
                        cursor.execute("ROLLBACK")
                        logger.error(f"Миграция {version:03d} ({path.name}) провалилась, транзакция отменена.")
                        raise

                if pending:
                    logger.info(f"Успешно применено миграций: {len(pending)}.")
        except Exception as e:
            # main.py глотает исключения команд, поэтому выходим с ненулевым кодом:
            # миграция, молча считающаяся успешной в cron/CI, опаснее ошибки.
            logger.error(f"Ошибка применения миграций: {e}", exc_info=True)
            sys.exit(1)
        finally:
            if conn is not None:
                conn.close()

        logger.info("MigrateDbUseCase finished")
