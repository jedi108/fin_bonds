"""
One-off перенос данных из старой SQLite-базы (data/ratings.db) в PostgreSQL.

Единственное место проекта, где sqlite3 остаётся легальным (§3 п.1 корневого
ТЗ, план _00_05); «обратный» перенос PG -> SQLite не поддерживается.

Подключение к PostgreSQL — по паттерну migrate-db (--dsn | env POSTGRES_DSN)
напрямую через psycopg2, без фабричного PortfolioStorage: one-off перенос —
оформленное исключение из правила «одна точка создания соединения».

Гарантии:
  * старая база открывается только в read-only режиме (mode=ro): файл не
    создаётся и не изменяется, отсутствующий файл — явная ошибка;
  * порядок вставки удовлетворяет FK (companies -> bonds_catalog -> ...);
  * идемпотентность по реальным UNIQUE-ключам (ON CONFLICT), повторный
    запуск на той же паре баз не создаёт дубликатов;
  * после вставки с явными id выставляются секвенции, иначе следующий
    INSERT без id (upsert_company, add_monitoring_check, ...) получит
    nextval с 1 и упрётся в duplicate key;
  * отчёт «таблица: было в SQLite / стало в PG / дельта».
"""

import argparse
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Tuple, TYPE_CHECKING

import psycopg2

from src.use_cases.base import UseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

# Дефолтный путь к старой базе якорится к корню проекта от __file__, а не к
# CWD процесса (болезнь №1 аудита; по образцу MIGRATIONS_DIR в src/migrations.py,
# файл лежит уровнем глубже — parents[2]).
DEFAULT_SQLITE_PATH = Path(__file__).resolve().parents[2] / 'data' / 'ratings.db'


# --- Приведение типов при чтении SQLite (шаг 4 подзадачи) ---

def _raw(value: Any) -> Any:
    """Колонка переносится как есть."""
    return value


def _to_date(value: Any):
    """Строка 'YYYY-MM-DD' -> date; '' и None -> None (NULLIF-семантика _00_01)."""
    if value in (None, ''):
        return None
    return date.fromisoformat(str(value))


def _to_ts(value: Any):
    """
    Метка времени -> наивный datetime. В SQLite два формата: 'YYYY-MM-DD
    HH:MM:SS' (CURRENT_TIMESTAMP) и ISO с 'T' (datetime.now().isoformat()) —
    fromisoformat понимает оба; наивные значения PG трактует в TimeZone
    сервера — та же конвенция, что в миграции 005.
    """
    if value in (None, ''):
        return None
    return datetime.fromisoformat(str(value))


def _to_decimal(value: Any):
    """REAL или число-строка -> Decimal через строку (без артефактов двоичной дроби)."""
    if value is None:
        return None
    return Decimal(str(value))


def _to_bool(value: Any):
    """Флаг 0/1 -> bool, NULL -> None."""
    return None if value is None else bool(value)


def _to_int(value: Any):
    return None if value is None else int(value)


# --- Правила переноса таблиц (шаги 3-5 подзадачи) ---

@dataclass(frozen=True)
class TableSpec:
    """Правило переноса одной таблицы."""
    name: str
    # Колонки PG; порядок = порядку SELECT из SQLite и порядку converters.
    columns: Tuple[str, ...]
    converters: Tuple[Callable[[Any], Any], ...]
    # ON CONFLICT ... — идемпотентность по реальному ключу из миграций.
    conflict_sql: str
    # True — вставка с явными SQLite-id, после переноса нужен setval.
    explicit_ids: bool = False


# Порядок кортежа = порядок, удовлетворяющий FK (шаг 3 подзадачи):
# bonds_catalog -> companies, history-таблицы -> bonds_catalog.
TABLE_SPECS: Tuple[TableSpec, ...] = (
    TableSpec(
        name='companies',
        columns=('id', 'name', 'full_name', 'inn', 'entity_type', 'created_at', 'updated_at'),
        converters=(_to_int, _raw, _raw, _raw, _raw, _to_ts, _to_ts),
        conflict_sql='ON CONFLICT (id) DO NOTHING',
        explicit_ids=True,
    ),
    TableSpec(
        name='bonds_catalog',
        # Колонки SQLite rusbonds_link нет в PG-схеме — не переносится (в
        # исходной базе значений нет).
        columns=(
            'isin', 'figi', 'ticker', 'name', 'currency', 'nominal',
            'maturity_date', 'offer_date', 'coupon_quantity_per_year',
            'coupon_rate_percent', 'coupon_type', 'list_level', 'risk_level',
            'coupon_spread', 'is_trade_available', 'is_for_qualified_investors',
            'issue_size', 'created_at', 'updated_at', 'floating_coupon_flag',
            'perpetual_flag', 'amortization_flag', 'market_price',
            'market_price_source', 'market_price_updated_at', 'company_id',
        ),
        converters=(
            _raw, _raw, _raw, _raw, _raw, _to_decimal,
            _to_date, _to_date, _to_int,
            _to_decimal, _raw, _to_int, _to_int,
            _to_decimal, _to_bool, _to_bool,
            _to_int, _to_ts, _to_ts, _to_bool,
            _to_bool, _to_bool, _to_decimal,
            _raw, _to_ts, _to_int,
        ),
        # Свежие данные каталога выигрывают; created_at существующей строки
        # сохраняется.
        conflict_sql=(
            'ON CONFLICT (isin) DO UPDATE SET '
            'figi=EXCLUDED.figi, ticker=EXCLUDED.ticker, name=EXCLUDED.name, '
            'currency=EXCLUDED.currency, nominal=EXCLUDED.nominal, '
            'maturity_date=EXCLUDED.maturity_date, offer_date=EXCLUDED.offer_date, '
            'coupon_quantity_per_year=EXCLUDED.coupon_quantity_per_year, '
            'coupon_rate_percent=EXCLUDED.coupon_rate_percent, '
            'coupon_type=EXCLUDED.coupon_type, list_level=EXCLUDED.list_level, '
            'risk_level=EXCLUDED.risk_level, coupon_spread=EXCLUDED.coupon_spread, '
            'is_trade_available=EXCLUDED.is_trade_available, '
            'is_for_qualified_investors=EXCLUDED.is_for_qualified_investors, '
            'issue_size=EXCLUDED.issue_size, '
            'floating_coupon_flag=EXCLUDED.floating_coupon_flag, '
            'perpetual_flag=EXCLUDED.perpetual_flag, '
            'amortization_flag=EXCLUDED.amortization_flag, '
            'market_price=EXCLUDED.market_price, '
            'market_price_source=EXCLUDED.market_price_source, '
            'market_price_updated_at=EXCLUDED.market_price_updated_at, '
            'company_id=EXCLUDED.company_id, updated_at=EXCLUDED.updated_at'
        ),
    ),
    # SQLite-колонки quantity и т.п. — TEXT с числами внутри -> Decimal.
    TableSpec(
        name='portfolio_positions',
        columns=(
            'isin', 'broker_name', 'ticker', 'name', 'currency', 'quantity',
            'average_price', 'current_price', 'yield_to_maturity',
            'portfolio_percent', 'current_value', 'instrument_type',
            'created_at', 'updated_at', 'liquidity_loss_ratio',
            'coupon_rate_percent',
        ),
        converters=(
            _raw, _raw, _raw, _raw, _raw, _to_decimal,
            _to_decimal, _to_decimal, _to_decimal,
            _to_decimal, _to_decimal, _raw,
            _to_ts, _to_ts, _to_decimal,
            _to_decimal,
        ),
        # account_id в INSERT не входит: в SQLite-схеме его нет, PG подставит
        # DEFAULT '', а inference по трёхколоночному индексу работает и без него.
        conflict_sql='ON CONFLICT (isin, broker_name, account_id) DO NOTHING',
    ),
    # SQLite-колонки rating_agency/rating_value в PG-схеме отсутствуют —
    # не переносятся.
    TableSpec(
        name='rating_history',
        columns=('id', 'isin', 'rating_date', 'rating_score'),
        converters=(_to_int, _raw, _to_date, _to_int),
        conflict_sql='ON CONFLICT (id) DO NOTHING',
        explicit_ids=True,
    ),
    TableSpec(
        name='risk_history',
        columns=('id', 'isin', 'risk_date', 'risk_level'),
        converters=(_to_int, _raw, _to_date, _to_int),
        conflict_sql='ON CONFLICT (id) DO NOTHING',
        explicit_ids=True,
    ),
    TableSpec(
        name='listlevel_history',
        columns=('id', 'isin', 'check_date', 'list_level'),
        converters=(_to_int, _raw, _to_date, _to_int),
        conflict_sql='ON CONFLICT (id) DO NOTHING',
        explicit_ids=True,
    ),
    TableSpec(
        name='calculated_coupons',
        columns=('isin', 'check_date', 'calculated_rate'),
        converters=(_raw, _to_date, _to_decimal),
        conflict_sql='ON CONFLICT (isin, check_date) DO NOTHING',
    ),
    TableSpec(
        name='monitoring_checks',
        columns=('isin', 'check_date', 'metric_name', 'metric_value'),
        converters=(_raw, _to_date, _raw, _raw),
        conflict_sql='ON CONFLICT (isin, check_date, metric_name) DO NOTHING',
    ),
    TableSpec(
        name='liquidity_history',
        columns=(
            'id', 'isin', 'timestamp', 'base_volume', 'market_exit_value',
            'loss_ratio', 'depth_level', 'data_source',
        ),
        converters=(_to_int, _raw, _to_ts, _to_decimal, _to_decimal, _to_decimal, _to_int, _raw),
        conflict_sql='ON CONFLICT (id) DO NOTHING',
        explicit_ids=True,
    ),
)

# Таблицы, куда id вставлялся явно: секвенции нужно выставить вручную.
SEQUENCE_TABLES: Tuple[str, ...] = tuple(
    spec.name for spec in TABLE_SPECS if spec.explicit_ids
)


def _insert_sql(spec: TableSpec) -> str:
    columns = ', '.join(spec.columns)
    placeholders = ', '.join(['%s'] * len(spec.columns))
    return f"INSERT INTO {spec.name} ({columns}) VALUES ({placeholders}) {spec.conflict_sql}"


def _fix_sequences(cursor: psycopg2.extensions.cursor) -> None:
    """setval(pg_get_serial_sequence(...), MAX(id)+1, false) для таблиц с явными id."""
    for table in SEQUENCE_TABLES:
        cursor.execute(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)",
            (table,),
        )


def _connect_sqlite_readonly(path: Path) -> sqlite3.Connection:
    """Read-only подключение к старой базе: не создаёт файл, не пишет в него."""
    if not path.is_file():
        raise FileNotFoundError(
            f"SQLite-база не найдена: {path}. "
            "Укажите путь флагом --sqlite-path."
        )
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    return conn


def _sqlite_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    cursor = conn.cursor()
    return {
        spec.name: cursor.execute(f"SELECT COUNT(*) FROM {spec.name}").fetchone()[0]
        for spec in TABLE_SPECS
    }


def _pg_counts(conn: psycopg2.extensions.connection) -> Dict[str, int]:
    with conn.cursor() as cursor:
        counts = {}
        for spec in TABLE_SPECS:
            cursor.execute(f"SELECT COUNT(*) FROM {spec.name}")
            counts[spec.name] = cursor.fetchone()[0]
        return counts


def _log_report(source: Dict[str, int], target: Dict[str, int]) -> None:
    logger.info("Отчёт переноса — таблица: было в SQLite / стало в PG / дельта:")
    total_source, total_delta = 0, 0
    for spec in TABLE_SPECS:
        delta = target[spec.name] - source[spec.name]
        total_source += source[spec.name]
        total_delta += delta
        logger.info(f"  {spec.name}: {source[spec.name]} / {target[spec.name]} / {delta:+d}")
    logger.info(f"  ИТОГО: {total_source} / {sum(target.values())} / {total_delta:+d}")


def _transfer(sqlite_conn: sqlite3.Connection, pg_conn: psycopg2.extensions.connection) -> None:
    """Перенос всех таблиц одной транзакцией: сбой откатывает всё."""
    with pg_conn:
        with pg_conn.cursor() as pg_cursor:
            for spec in TABLE_SPECS:
                sqlite_cursor = sqlite_conn.execute(
                    f"SELECT {', '.join(spec.columns)} FROM {spec.name}"
                )
                rows = [
                    tuple(convert(value) for convert, value in zip(spec.converters, row))
                    for row in sqlite_cursor
                ]
                if rows:
                    pg_cursor.executemany(_insert_sql(spec), rows)
                logger.info(f"Перенос {spec.name}: строк {len(rows)}.")
            _fix_sequences(pg_cursor)


class ImportFromSqliteUseCase(UseCase):
    """
    Сценарий: one-off перенос данных из старой SQLite-базы в PostgreSQL
    (идемпотентно, со сверкой counts; --dry-run — сверка без записи).
    """

    def __init__(self, factory: 'UseCaseFactory'):
        # Намеренно не вызываем UseCase.__init__ (по образцу MigrateDbUseCase):
        # команда управляет соединениями сама — SQLite read-only и прямое
        # psycopg2-подключение, фабричное хранилище не нужно.
        self.factory = factory

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--sqlite-path', type=str, default=str(DEFAULT_SQLITE_PATH),
            help='Путь к старой SQLite-базе. По умолчанию data/ratings.db '
                 'от корня проекта.'
        )
        parser.add_argument(
            '--dsn', type=str, default=None,
            help='DSN PostgreSQL (postgresql://user:pass@host:port/dbname). '
                 'По умолчанию берется POSTGRES_DSN из .env.'
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Только сверка counts обеих баз, без записи.'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'ImportFromSqliteUseCase':
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
        logger.info("Executing ImportFromSqliteUseCase")
        try:
            self._run(args)
        except Exception as e:
            # main.py глотает исключения команд, поэтому выходим с ненулевым
            # кодом: перенос, молча считающийся успешным в cron, опаснее ошибки.
            logger.error(f"Ошибка переноса данных из SQLite: {e}", exc_info=True)
            sys.exit(1)
        logger.info("ImportFromSqliteUseCase finished")

    def _run(self, args: argparse.Namespace) -> None:
        sqlite_path = Path(args.sqlite_path)
        dsn = self._resolve_dsn(args)

        sqlite_conn = _connect_sqlite_readonly(sqlite_path)
        try:
            source_counts = _sqlite_counts(sqlite_conn)
            pg_conn = psycopg2.connect(dsn)
            try:
                if args.dry_run:
                    logger.info("Dry-run: запись в базы не выполняется.")
                    _log_report(source_counts, _pg_counts(pg_conn))
                    return

                pg_before = _pg_counts(pg_conn)
                _transfer(sqlite_conn, pg_conn)
                _log_report(source_counts, _pg_counts(pg_conn))
                logger.info(
                    "Перенос завершён. Секвенции выставлены для: "
                    f"{', '.join(SEQUENCE_TABLES)}."
                )
                # Информативно для повторных запусков на непустой приёмной базе.
                logger.info(f"Строк в PG до переноса: {sum(pg_before.values())}.")
            finally:
                pg_conn.close()
        finally:
            sqlite_conn.close()
