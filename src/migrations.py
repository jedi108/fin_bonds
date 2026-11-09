"""
Ранер SQL-миграций PostgreSQL: единая точка discovery и применения схемы.

Используется CLI-командой migrate-db (src/use_cases/migrate_db.py) и будет
использоваться fail-fast проверкой версий миграций в storage-слое:
apply_migrations() применяет pending-миграции, get_applied_versions()
читает применённые версии без побочных эффектов.
"""

import logging
from pathlib import Path
from typing import List, Set, Tuple

import psycopg2

logger = logging.getLogger(__name__)

# Каталог с SQL-миграциями PostgreSQL: <project_root>/migrations/postgres/
# Якорится к корню проекта от __file__, а не к CWD процесса.
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / 'migrations' / 'postgres'


def discover_migrations() -> List[Tuple[int, Path]]:
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


def get_applied_versions(conn: psycopg2.extensions.connection) -> Set[int]:
    """
    Возвращает применённые версии из schema_migrations по открытому соединению.

    Ничего не применяет, не создаёт таблиц и не печатает; если таблицы
    schema_migrations нет — пустое множество.
    """
    with conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('schema_migrations')")
        if cursor.fetchone()[0] is None:
            return set()
        cursor.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in cursor.fetchall()}


def _report(migrations: List[Tuple[int, Path]], applied: Set[int], pending: List[Tuple[int, Path]]):
    print(f"Миграции: найдено {len(migrations)}, применено {len(applied)}, ожидают {len(pending)}")
    for version, path in pending:
        print(f"  [ ] {version:03d} {path.name}")
    if not pending:
        print("Схема актуальна — нечего применять.")


def apply_migrations(dsn: str, dry_run: bool = False, status: bool = False) -> None:
    """
    Подключается к БД по DSN и приводит схему к актуальному состоянию.

    Соединением владеет функция: psycopg2.connect(dsn) здесь, close() — в
    finally. Каждая pending-миграция применяется в своей транзакции;
    при сбое транзакция откатывается и исключение уходит наверх —
    код выхода (sys.exit) — зона вызывающего CLI-кода.
    """
    migrations = discover_migrations()

    conn = psycopg2.connect(dsn)
    try:
        # Сессионный advisory lock (не xact: при autocommit=True транзакционный
        # lock освободился бы сразу после execute) берётся до чтения применённых
        # версий и держится до закрытия соединения — защищает от параллельного
        # запуска ранера.
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

        applied = get_applied_versions(conn)
        pending = [item for item in migrations if item[0] not in applied]

        _report(migrations, applied, pending)

        if status or dry_run:
            if status:
                print("\nПримененные миграции:")
                for version, path in migrations:
                    marker = 'x' if version in applied else ' '
                    print(f"  [{marker}] {version:03d} {path.name}")
            return

        with conn.cursor() as cursor:
            for version, path in pending:
                sql = path.read_text(encoding='utf-8')
                logger.info(f"Применение миграции {version:03d} ({path.name})...")
                cursor.execute("BEGIN")
                try:
                    cursor.execute(sql)
                    # Запись о версии — в той же транзакции, что и SQL миграции:
                    # ROLLBACK при сбое отменяет и миграцию, и запись о ней.
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
    finally:
        conn.close()
