"""
Единый слой хранения данных портфеля на PostgreSQL (psycopg2).

Класс PortfolioStorage — единственный владелец SQL: use-case'ы зовут его
методы и работают с DTO. Соединение создаётся в фабрике (POSTGRES_DSN) и
передаётся сюда готовым DSN; схема БД здесь не создаётся и не мутирует —
при несовпадении версий миграций конструктор падает с понятной ошибкой
(fail-fast, см. _check_schema_versions).
"""
import logging
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from datetime import date, timedelta
from decimal import Decimal
from collections import defaultdict

import psycopg2
from psycopg2.extras import RealDictCursor

from src.data_models import Bond, CalculatedCoupon, PortfolioPosition, PortfolioBond
from src.migrations import discover_migrations, get_applied_versions
from src.use_cases.interfaces import IPortfolioStorage

logger = logging.getLogger(__name__)


def _row_to_bond(row: Optional[Mapping[str, Any]]) -> Optional[Bond]:
    """Преобразует строку результата psycopg2 (dict) в DTO Bond.

    psycopg2 возвращает даты/Decimal/bool нативно — конвертации не нужны.
    """
    if not row:
        return None
    try:
        return Bond(
            figi=row.get('figi'),
            isin=row.get('isin'),
            ticker=row.get('ticker'),
            name=row.get('name'),
            currency=row.get('currency'),
            nominal=row.get('nominal'),
            maturity_date=row.get('maturity_date'),
            offer_date=row.get('offer_date'),
            coupon_quantity_per_year=row.get('coupon_quantity_per_year'),
            coupon_rate_percent=row.get('coupon_rate_percent'),
            coupon_spread=row.get('coupon_spread'),
            list_level=row.get('list_level'),
            risk_level=row.get('risk_level'),
            floating_coupon_flag=bool(row.get('floating_coupon_flag') or False),
            perpetual_flag=bool(row.get('perpetual_flag') or False),
            amortization_flag=bool(row.get('amortization_flag') or False),
            coupon_type=row.get('coupon_type'),
            is_trade_available=bool(row.get('is_trade_available') or False),
            created_at=row.get('created_at'),
            updated_at=row.get('updated_at'),
            is_for_qualified_investors=bool(row.get('is_for_qualified_investors') or False),
            company_id=row.get('company_id'),
        )
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"Failed to convert row to Bond DTO for ISIN {row.get('isin')}: {e}")
        return None


def _row_to_portfolio_position(row: Mapping[str, Any]) -> Optional[PortfolioPosition]:
    """Преобразует строку результата psycopg2 (dict) в DTO PortfolioPosition."""
    if not row:
        return None
    # Явно указываем поля, которые ожидаем, чтобы избежать ошибок с лишними колонками
    expected_fields = {
        'isin', 'ticker', 'name', 'quantity', 'average_price',
        'current_price', 'currency', 'yield_to_maturity',
        'portfolio_percent', 'current_value', 'broker_name',
        'instrument_type', 'figi', 'liquidity_loss_ratio', 'coupon_rate_percent'
    }

    # Фильтруем строку, оставляя только ожидаемые и непустые поля
    filtered_data = {key: row[key] for key in row if key in expected_fields and row[key] is not None}

    try:
        # Для liquidity_loss_ratio сохраняем None если значение пустое
        if 'liquidity_loss_ratio' not in filtered_data:
            filtered_data['liquidity_loss_ratio'] = None

        return PortfolioPosition(**filtered_data)

    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"Failed to convert row to PortfolioPosition DTO for ISIN {row.get('isin')}: {e}")
        return None


class PortfolioStorage(IPortfolioStorage):
    """
    Хранилище данных портфеля на PostgreSQL.
    Отвечает за выполнение всех SQL-запросов; схему создают миграции.
    """

    def __init__(self, dsn: str):
        """
        Инициализирует объект базы данных.

        Args:
            dsn (str): DSN соединения PostgreSQL (переменная POSTGRES_DSN).
        """
        if not dsn:
            raise ValueError(
                "Не задан DSN PostgreSQL. Укажите POSTGRES_DSN в .env "
                "(формат: postgresql://user:password@host:port/dbname)."
            )
        self.conn = psycopg2.connect(dsn)
        self._check_schema_versions()

    def _check_schema_versions(self):
        """Fail-fast: схема БД должна соответствовать SQL-миграциям (контракт _00_02).

        Соединение здесь уже открыто — вторая точка подключения не создаётся.
        """
        applied = get_applied_versions(self.conn)
        expected = {version for version, _ in discover_migrations()}
        if applied == expected:
            return
        details = []
        missing = sorted(expected - applied)
        extra = sorted(applied - expected)
        if missing:
            details.append(f"не применены версии {missing}")
        if extra:
            details.append(f"в schema_migrations лишние версии {extra}")
        raise RuntimeError(
            "Схема БД не соответствует миграциям. "
            "Выполните: python3 main.py migrate-db. "
            f"Расхождение: {'; '.join(details)}."
        )

    def _cursor(self) -> psycopg2.extensions.cursor:
        """Курсор со строками-словарями (аналог прежнего Row-фабричного доступа)."""
        return self.conn.cursor(cursor_factory=RealDictCursor)

    def close(self):
        if self.conn:
            self.conn.close()

    def get_bond_by_isin(self, isin: str) -> Optional[Bond]:
        cursor = self._cursor()
        cursor.execute("SELECT * FROM bonds_catalog WHERE isin = %s", (isin,))
        row = cursor.fetchone()
        return _row_to_bond(row)

    def get_bond_names_by_isins(self, isins: List[str]) -> Dict[str, str]:
        cursor = self._cursor()
        cursor.execute("SELECT isin, name FROM bonds_catalog WHERE isin = ANY(%s)", (isins,))
        return {row['isin']: row['name'] for row in cursor.fetchall()}

    def get_rating_history_for_bonds(self, isins: List[str]) -> Dict[str, List[Tuple[date, int]]]:
        if not isins:
            return {}
        cursor = self._cursor()
        query = """
            SELECT r.isin, r.rating_date, r.rating_score
            FROM rating_history r
            WHERE r.isin = ANY(%s)
            ORDER BY r.isin, r.rating_date
        """
        cursor.execute(query, (isins,))
        history_data = defaultdict(list)
        for row in cursor.fetchall():
            history_data[row['isin']].append((row['rating_date'], row['rating_score']))
        return history_data

    def get_risk_history_for_bonds(self, isins: List[str]) -> Dict[str, List[Tuple[date, int]]]:
        if not isins:
            return {}
        cursor = self._cursor()
        query = """
            SELECT r.isin, r.risk_date, r.risk_level
            FROM risk_history r
            WHERE r.isin = ANY(%s)
            ORDER BY r.isin, r.risk_date
        """
        cursor.execute(query, (isins,))
        history_data = defaultdict(list)
        for row in cursor.fetchall():
            history_data[row['isin']].append((row['risk_date'], row['risk_level']))
        return history_data

    def get_listlevel_history_for_bonds(self, isins: List[str]) -> Dict[str, List[Tuple[date, int]]]:
        if not isins:
            return {}
        cursor = self._cursor()
        query = """
            SELECT l.isin, l.check_date, l.list_level
            FROM listlevel_history l
            WHERE l.isin = ANY(%s)
            ORDER BY l.isin, l.check_date
        """
        cursor.execute(query, (isins,))
        history_data = defaultdict(list)
        for row in cursor.fetchall():
            history_data[row['isin']].append((row['check_date'], row['list_level']))
        return history_data

    def is_catalog_empty(self) -> bool:
        cursor = self._cursor()
        cursor.execute("SELECT EXISTS(SELECT 1 FROM bonds_catalog) AS has_rows")
        row = cursor.fetchone()
        return not row['has_rows']

    def get_all_isins_from_catalog(self) -> Set[str]:
        """Возвращает множество всех ISIN из каталога для быстрой проверки существования."""
        cursor = self._cursor()
        cursor.execute("SELECT isin FROM bonds_catalog")
        result = {row['isin'] for row in cursor.fetchall()}
        logger.info(f"get_all_isins_from_catalog: найдено {len(result)} ISIN в базе данных")
        return result

    def add_bonds_to_catalog(self, bonds_dto_list: List[Bond]):
        """Добавляет или обновляет список облигаций в каталоге.

        Существующие записи обновляются без стирания данных, которых нет
        в источнике TBank: company_id, created_at, обогащения из MOEX
        (list_level, купон) и рыночных цен (COALESCE с существующими).
        """
        bonds_data = []
        for bond in bonds_dto_list:
            bonds_data.append((
                bond.isin, bond.figi, bond.ticker, bond.name, bond.currency,
                bond.nominal if bond.nominal else None,
                bond.maturity_date if bond.maturity_date else None,
                bond.offer_date if bond.offer_date else None,
                bond.coupon_quantity_per_year,
                bond.coupon_rate_percent if bond.coupon_rate_percent else None,
                bond.coupon_type,
                bond.list_level, bond.risk_level,
                bond.coupon_spread if bond.coupon_spread else None,
                bond.is_trade_available, bond.is_for_qualified_investors, bond.issue_size,
                bond.floating_coupon_flag, bond.perpetual_flag, bond.amortization_flag,
                bond.market_price if bond.market_price else None, bond.market_price_source,
                bond.market_price_updated_at if bond.market_price_updated_at else None
            ))

        with self.conn:
            cursor = self._cursor()
            cursor.executemany("""
                INSERT INTO bonds_catalog (
                    isin, figi, ticker, name, currency, nominal, maturity_date, offer_date,
                    coupon_quantity_per_year, coupon_rate_percent, coupon_type,
                    list_level, risk_level, coupon_spread,
                    is_trade_available, is_for_qualified_investors, issue_size,
                    floating_coupon_flag, perpetual_flag, amortization_flag,
                    market_price, market_price_source, market_price_updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(isin) DO UPDATE SET
                    figi = excluded.figi,
                    ticker = excluded.ticker,
                    name = excluded.name,
                    currency = excluded.currency,
                    nominal = excluded.nominal,
                    maturity_date = excluded.maturity_date,
                    offer_date = excluded.offer_date,
                    coupon_quantity_per_year = excluded.coupon_quantity_per_year,
                    coupon_rate_percent = COALESCE(excluded.coupon_rate_percent, bonds_catalog.coupon_rate_percent),
                    coupon_type = COALESCE(excluded.coupon_type, bonds_catalog.coupon_type),
                    list_level = COALESCE(excluded.list_level, bonds_catalog.list_level),
                    risk_level = excluded.risk_level,
                    coupon_spread = COALESCE(excluded.coupon_spread, bonds_catalog.coupon_spread),
                    is_trade_available = excluded.is_trade_available,
                    is_for_qualified_investors = excluded.is_for_qualified_investors,
                    issue_size = excluded.issue_size,
                    floating_coupon_flag = excluded.floating_coupon_flag,
                    perpetual_flag = excluded.perpetual_flag,
                    amortization_flag = excluded.amortization_flag,
                    market_price = COALESCE(excluded.market_price, bonds_catalog.market_price),
                    market_price_source = COALESCE(excluded.market_price_source, bonds_catalog.market_price_source),
                    market_price_updated_at = COALESCE(excluded.market_price_updated_at, bonds_catalog.market_price_updated_at),
                    updated_at = CURRENT_TIMESTAMP
            """, bonds_data)
        logger.info(f"Добавлено/обновлено {len(bonds_data)} облигаций в каталоге.")

    # Методы для работы с компаниями-эмитентами (юрлицами)

    def upsert_company(
        self,
        name: str,
        entity_type: str = 'company',
        inn: Optional[str] = None,
        full_name: Optional[str] = None
    ) -> int:
        """
        Находит компанию по ИНН (если задан), затем по имени (без учёта регистра);
        при отсутствии создаёт новую. Возвращает id компании.

        ИНН у физлиц не передавать (персональные данные).

        Регистронезависимое сравнение — через ICU-коллацию "und-x-icu":
        нативный lower() в кластерах с lc_ctype='C' не сворачивает кириллицу.
        """
        with self.conn:
            cursor = self._cursor()
            if inn:
                cursor.execute("SELECT id FROM companies WHERE inn = %s", (inn,))
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        "UPDATE companies SET name = %s, entity_type = %s, "
                        "full_name = COALESCE(%s, full_name) WHERE id = %s",
                        (name, entity_type, full_name, row['id'])
                    )
                    return row['id']
            cursor.execute(
                "SELECT id FROM companies "
                "WHERE lower(name COLLATE \"und-x-icu\") = lower(%s COLLATE \"und-x-icu\")",
                (name,)
            )
            row = cursor.fetchone()
            if row:
                # ИНН мог появиться позже имени — заполняем только пустой
                if inn:
                    cursor.execute(
                        "UPDATE companies SET inn = %s WHERE id = %s AND (inn IS NULL OR inn = '')",
                        (inn, row['id'])
                    )
                return row['id']
            cursor.execute(
                "INSERT INTO companies (name, entity_type, inn, full_name) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (name, entity_type, inn, full_name)
            )
            return cursor.fetchone()['id']

    def get_bonds_for_company_linking(self, only_unlinked: bool = True) -> List[Tuple[str, str]]:
        """Возвращает список пар (isin, name) для привязки к компаниям."""
        cursor = self._cursor()
        query = "SELECT isin, name FROM bonds_catalog"
        if only_unlinked:
            query += " WHERE company_id IS NULL"
        cursor.execute(query)
        return [(row['isin'], row['name']) for row in cursor.fetchall()]

    def link_bonds_to_companies(self, links: List[Tuple[str, int]]) -> int:
        """Привязывает облигации к компаниям: список пар (isin, company_id)."""
        if not links:
            return 0
        with self.conn:
            cursor = self._cursor()
            cursor.executemany(
                "UPDATE bonds_catalog SET company_id = %s WHERE isin = %s",
                [(company_id, isin) for isin, company_id in links]
            )
        return len(links)

    def get_companies_overview(self) -> List[Dict[str, Any]]:
        """Компании с количеством облигаций — для отчётов и контроля группировки."""
        cursor = self._cursor()
        cursor.execute("""
            SELECT c.id, c.name, c.entity_type, c.inn, COUNT(b.isin) AS bonds_count
            FROM companies c
            LEFT JOIN bonds_catalog b ON b.company_id = c.id
            GROUP BY c.id
            ORDER BY bonds_count DESC, c.name
        """)
        return [dict(row) for row in cursor.fetchall()]

    def get_catalog_company_coverage(self) -> Tuple[int, int]:
        """Возвращает (всего облигаций, привязано к компаниям)."""
        cursor = self._cursor()
        cursor.execute("SELECT COUNT(*) AS total, COUNT(company_id) AS linked FROM bonds_catalog")
        row = cursor.fetchone()
        return row['total'], row['linked']

    def get_bonds_for_moex_update(self) -> List[Bond]:
        """
        Возвращает облигации, которые нужно обновить данными с MOEX.
        Критерии:
        - Плавающий купон (для обновления ставки).
        - Отсутствует уровень листинга.
        - Ставка купона не задана (NULL или 0).
        """
        cursor = self._cursor()
        query = """
            SELECT * FROM bonds_catalog
            WHERE floating_coupon_flag IS TRUE
               OR list_level IS NULL
               OR coupon_rate_percent IS NULL
               OR coupon_rate_percent = 0
        """
        cursor.execute(query)
        rows = cursor.fetchall()

        bonds = [_row_to_bond(row) for row in rows if row]
        if None in bonds:
            logger.warning("Найдены облигации, которые не удалось преобразовать в DTO. Они будут пропущены.")
            bonds = [b for b in bonds if b is not None]

        return bonds

    def update_bond_moex_data(self, isin: str, list_level: Optional[int], coupon_rate: Optional[Decimal], offer_date: Optional[date]):
        """Обновляет данные по одной облигации из MOEX."""
        with self.conn:
            cursor = self._cursor()
            cursor.execute("""
                UPDATE bonds_catalog
                SET list_level = %s, coupon_rate_percent = %s, offer_date = %s, updated_at = CURRENT_TIMESTAMP
                WHERE isin = %s
            """, (list_level, coupon_rate, offer_date, isin))
        logger.debug(f"Обновлены MOEX данные для {isin}.")

    def get_full_catalog(self) -> List[Bond]:
        cursor = self._cursor()
        cursor.execute("SELECT * FROM bonds_catalog")
        return [bond for row in cursor.fetchall() if (bond := _row_to_bond(row)) is not None]

    def get_bonds_for_export(self, min_maturity_date: str, max_maturity_date: str, min_coupon_rate: float, min_coupon_frequency: int) -> List[Dict[str, Any]]:
        cursor = self._cursor()
        query = "SELECT * FROM bonds_catalog WHERE 1=1"
        params = []
        if min_maturity_date:
            query += " AND maturity_date >= %s"
            params.append(min_maturity_date)
        if max_maturity_date:
            query += " AND maturity_date <= %s"
            params.append(max_maturity_date)
        if min_coupon_rate:
            query += " AND coupon_rate_percent >= %s"
            params.append(min_coupon_rate)
        if min_coupon_frequency:
            query += " AND coupon_quantity_per_year >= %s"
            params.append(min_coupon_frequency)
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def add_monitoring_check(self, isin: str, metric_name: str, metric_value: str):
        """
        Добавляет результат проверки мониторинга в БД.

        ВАЖНО: Для метрики 'liquidity_analyzer' значение является биржевым свойством ISIN,
        а не свойством брокера. Стакан заявок одинаков для всех брокеров на MOEX.
        """
        with self.conn:
            cursor = self._cursor()
            cursor.execute(
                """
                INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(isin, check_date, metric_name) DO UPDATE SET metric_value=excluded.metric_value
                """,
                (isin, date.today().isoformat(), metric_name, metric_value)
            )

    def get_last_monitoring_check(self, isin: str, metric_name: str) -> Optional[Tuple[date, str]]:
        cursor = self._cursor()
        cursor.execute("""
            SELECT check_date, metric_value FROM monitoring_checks
            WHERE isin = %s AND metric_name = %s
            ORDER BY check_date DESC LIMIT 1
        """, (isin, metric_name))
        row = cursor.fetchone()
        if row:
            return row['check_date'], row['metric_value']
        return None

    def get_latest_calculated_coupon(self, isin: str) -> Optional[CalculatedCoupon]:
        cursor = self._cursor()
        cursor.execute("SELECT * FROM calculated_coupons WHERE isin = %s ORDER BY check_date DESC LIMIT 1", (isin,))
        row = cursor.fetchone()
        if not row:
            return None
        return CalculatedCoupon(
            isin=row['isin'],
            check_date=row['check_date'],
            calculated_rate=row['calculated_rate']
        )

    def add_calculated_coupon(self, coupon: CalculatedCoupon):
        query = """
            INSERT INTO calculated_coupons (isin, check_date, calculated_rate) VALUES (%s, %s, %s)
            ON CONFLICT(isin, check_date) DO UPDATE SET calculated_rate=excluded.calculated_rate;
        """
        with self.conn:
            cursor = self._cursor()
            cursor.execute(query, (coupon.isin, coupon.check_date, coupon.calculated_rate))

    def set_coupon_spread(self, isin: str, spread: float) -> bool:
        query = "UPDATE bonds_catalog SET coupon_spread = %s WHERE isin = %s"
        cursor = self._cursor()
        try:
            with self.conn:
                cursor.execute(query, (spread, isin))
            return cursor.rowcount > 0
        except psycopg2.Error as e:
            logger.error(f"Не удалось обновить спред для {isin}: {e}")
            return False

    def get_portfolio_floaters_without_spread(self) -> List[Bond]:
        """
        Возвращает список облигаций из портфеля, которые являются флоатерами
        и для которых еще не рассчитан спред.
        """
        try:
            cursor = self._cursor()
            query = """
                SELECT bc.*
                FROM portfolio_positions pp
                JOIN bonds_catalog bc ON pp.isin = bc.isin
                WHERE bc.floating_coupon_flag IS TRUE
                  AND bc.coupon_spread IS NULL;
            """
            cursor.execute(query)
            rows = cursor.fetchall()
            bonds = [_row_to_bond(row) for row in rows if _row_to_bond(row) is not None]
            logger.info(f"Найдено {len(bonds)} флоатеров в портфеле без рассчитанного спреда.")
            return bonds
        except psycopg2.Error as e:
            logger.error(f"Ошибка при запросе флоатеров из портфеля: {e}")
            return []

    def clear_portfolio_positions(self):
        """Полностью очищает таблицу `portfolio_positions`."""
        logger.info("Clearing all portfolio positions from the database.")
        try:
            with self.conn:
                cursor = self._cursor()
                cursor.execute("DELETE FROM portfolio_positions")
            logger.info("Таблица portfolio_positions успешно очищена.")
        except psycopg2.Error as e:
            logger.error(f"Ошибка при очистке portfolio_positions: {e}")

    def clear_portfolio_positions_by_broker(self, broker_name: str):
        """Очищает позиции только для указанного брокера."""
        logger.info(f"Clearing portfolio positions for broker: {broker_name}")
        try:
            with self.conn:
                cursor = self._cursor()
                cursor.execute("DELETE FROM portfolio_positions WHERE broker_name = %s", (broker_name,))
            logger.info(f"Позиции брокера {broker_name} успешно удалены из портфеля.")
        except psycopg2.Error as e:
            logger.error(f"Ошибка при удалении позиций брокера {broker_name}: {e}")

    def update_position_liquidity(self, isin: str, broker_name: str, liquidity_value: float):
        """Обновляет значение liquidity_loss_ratio для позиции в портфеле."""
        try:
            with self.conn:
                cursor = self._cursor()
                cursor.execute("""
                    UPDATE portfolio_positions
                    SET liquidity_loss_ratio = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE isin = %s AND broker_name = %s
                """, (liquidity_value, isin, broker_name))
            logger.debug(f"Обновлена ликвидность для {isin} ({broker_name}): {liquidity_value}")
        except psycopg2.Error as e:
            logger.error(f"Ошибка при обновлении ликвидности для {isin} ({broker_name}): {e}")
            raise

    def add_portfolio_positions(self, positions: List['PortfolioPosition']):
        """
        Сохраняет или обновляет позиции в портфеле.
        Конфликт по (isin, broker_name) разрешается полным обновлением записи.
        """
        if not positions:
            return

        # Убираем figi из DTO перед записью в БД, так как его нет в таблице portfolio_positions
        positions_to_save = []
        for p in positions:
            pos_dict = p.to_db_dict()
            pos_dict.pop('figi', None)  # Безопасно удаляем figi
            positions_to_save.append(pos_dict)

        if not positions_to_save:
            return

        with self.conn:
            cursor = self._cursor()
            # Получаем имена столбцов из первого словаря, чтобы обеспечить их соответствие
            columns = ', '.join(positions_to_save[0].keys())
            placeholders = ', '.join('%s' for _ in positions_to_save[0])

            query = f"""
                INSERT INTO portfolio_positions ({columns})
                VALUES ({placeholders})
                ON CONFLICT (isin, broker_name) DO UPDATE SET
                    ticker = excluded.ticker,
                    name = excluded.name,
                    quantity = excluded.quantity,
                    average_price = excluded.average_price,
                    current_price = excluded.current_price,
                    currency = excluded.currency,
                    yield_to_maturity = excluded.yield_to_maturity,
                    portfolio_percent = excluded.portfolio_percent,
                    current_value = excluded.current_value,
                    instrument_type = excluded.instrument_type,
                    liquidity_loss_ratio = excluded.liquidity_loss_ratio,
                    coupon_rate_percent = excluded.coupon_rate_percent,
                    updated_at = CURRENT_TIMESTAMP
            """

            # Преобразуем словари в кортежи в правильном порядке
            data_tuples = [tuple(p.values()) for p in positions_to_save]

            cursor.executemany(query, data_tuples)
            logger.info(f"Успешно сохранено/обновлено {len(positions)} позиций в портфеле.")

    def get_portfolio_positions(self) -> List['PortfolioPosition']:
        """
        Возвращает все позиции из портфеля, обогащенные FIGI из каталога.
        """
        cursor = self._cursor()
        # Используем LEFT JOIN, чтобы не потерять позиции, которых вдруг нет в каталоге
        cursor.execute("""
            SELECT
                p.isin,
                p.ticker,
                p.name,
                p.quantity,
                p.average_price,
                p.current_price,
                p.currency,
                p.yield_to_maturity,
                p.portfolio_percent,
                p.current_value,
                p.broker_name,
                p.instrument_type,
                b.figi
            FROM portfolio_positions p
            LEFT JOIN bonds_catalog b ON p.isin = b.isin
        """)
        rows = cursor.fetchall()

        positions = []
        for row in rows:
            pos = _row_to_portfolio_position(row)
            if pos:
                # _row_to_portfolio_position не знает о figi, добавляем его вручную
                pos.figi = row['figi']
                positions.append(pos)
        return positions

    def get_portfolio_position_by_isin(self, isin: str) -> Optional[PortfolioPosition]:
        """
        Возвращает одну позицию портфеля по ISIN, обогащенную FIGI.
        """
        cursor = self._cursor()
        cursor.execute("""
            SELECT
                p.isin,
                p.ticker,
                p.name,
                p.quantity,
                p.average_price,
                p.current_price,
                p.currency,
                p.yield_to_maturity,
                p.portfolio_percent,
                p.current_value,
                p.broker_name,
                p.instrument_type,
                b.figi
            FROM portfolio_positions p
            LEFT JOIN bonds_catalog b ON p.isin = b.isin
            WHERE p.isin = %s
        """, (isin,))
        row = cursor.fetchone()
        if row:
            pos = _row_to_portfolio_position(row)
            if pos:
                pos.figi = row['figi']
                return pos
        return None

    def get_liquidity_history_for_bonds(self, isins: List[str]) -> Dict[str, List[Tuple[date, float]]]:
        """
        Возвращает историю изменений ликвидности для указанных облигаций.

        Args:
            isins: Список ISIN облигаций.

        Returns:
            Словарь, где ключ — ISIN, значение — список кортежей (дата, коэффициент потерь в %).
        """
        if not isins:
            return {}

        cursor = self._cursor()
        query = """
            SELECT h.isin, h.timestamp, h.loss_ratio
            FROM liquidity_history h
            WHERE h.isin = ANY(%s)
            ORDER BY h.isin, h.timestamp
        """
        cursor.execute(query, (isins,))
        history_data = defaultdict(list)
        for row in cursor.fetchall():
            try:
                # TIMESTAMPTZ приходит timezone-aware datetime, loss_ratio — Decimal
                history_data[row['isin']].append((row['timestamp'].date(), float(row['loss_ratio'])))
            except (ValueError, TypeError) as e:
                logger.warning(f"Некорректные данные для isin {row['isin']} в liquidity_history: {e}")
        return history_data

    def get_portfolio_bonds_for_monitoring(self) -> List['PortfolioBond']:
        """
        Возвращает список объектов PortfolioBond для мониторинга,
        объединяя данные из portfolio_positions и bonds_catalog.
        Использует INNER JOIN, чтобы получить только облигации,
        присутствующие и в портфеле, и в каталоге.
        """
        try:
            cursor = self._cursor()
            query = """
                SELECT
                    -- Основные идентификаторы
                    p.isin,
                    bc.figi,

                    -- Данные из bonds_catalog
                    bc.ticker,
                    bc.name,
                    bc.currency,
                    bc.nominal,
                    bc.maturity_date,
                    bc.offer_date,
                    bc.coupon_quantity_per_year,
                    bc.coupon_rate_percent,
                    bc.coupon_type,
                    bc.coupon_spread,
                    bc.list_level,
                    bc.risk_level,
                    bc.issue_size,
                    bc.is_trade_available,
                    bc.is_for_qualified_investors,
                    bc.floating_coupon_flag,
                    bc.perpetual_flag,
                    bc.amortization_flag,

                    -- Данные из portfolio_positions
                    p.quantity,
                    p.average_price,
                    p.current_price,
                    p.yield_to_maturity,
                    p.portfolio_percent,
                    p.current_value,
                    p.broker_name
                FROM portfolio_positions p
                INNER JOIN bonds_catalog bc ON p.isin = bc.isin
            """
            cursor.execute(query)
            rows = cursor.fetchall()

            portfolio_bonds = []
            for row in rows:
                portfolio_bond = self._row_to_portfolio_bond(row)
                if portfolio_bond:
                    portfolio_bonds.append(portfolio_bond)

            logger.info(f"Найдено {len(portfolio_bonds)} облигаций для мониторинга (объединение портфеля и каталога)")
            return portfolio_bonds

        except psycopg2.Error as e:
            logger.error(f"Ошибка при получении данных для мониторинга: {e}")
            return []

    def get_bond_data_for_monitoring(self, isin: str) -> Optional['PortfolioBond']:
        """
        Возвращает объект PortfolioBond для одной облигации по ISIN,
        объединяя данные из portfolio_positions и bonds_catalog.
        """
        try:
            cursor = self._cursor()
            query = """
                SELECT
                    -- Основные идентификаторы
                    p.isin,
                    bc.figi,

                    -- Данные из bonds_catalog
                    bc.ticker,
                    bc.name,
                    bc.currency,
                    bc.nominal,
                    bc.maturity_date,
                    bc.offer_date,
                    bc.coupon_quantity_per_year,
                    bc.coupon_rate_percent,
                    bc.coupon_type,
                    bc.coupon_spread,
                    bc.list_level,
                    bc.risk_level,
                    bc.issue_size,
                    bc.is_trade_available,
                    bc.is_for_qualified_investors,
                    bc.floating_coupon_flag,
                    bc.perpetual_flag,
                    bc.amortization_flag,

                    -- Данные из portfolio_positions
                    p.quantity,
                    p.average_price,
                    p.current_price,
                    p.yield_to_maturity,
                    p.portfolio_percent,
                    p.current_value,
                    p.broker_name
                FROM portfolio_positions p
                INNER JOIN bonds_catalog bc ON p.isin = bc.isin
                WHERE p.isin = %s
            """
            cursor.execute(query, (isin,))
            row = cursor.fetchone()

            if row:
                return self._row_to_portfolio_bond(row)
            else:
                logger.warning(f"Облигация {isin} не найдена в портфеле или каталоге")
                return None

        except psycopg2.Error as e:
            logger.error(f"Ошибка при получении данных для {isin}: {e}")
            return None

    def _row_to_portfolio_bond(self, row: Mapping[str, Any]) -> Optional['PortfolioBond']:
        """
        Преобразует строку результата psycopg2 (dict) в DTO PortfolioBond.
        psycopg2 возвращает даты/Decimal/bool нативно — конвертации не нужны.
        """
        if not row:
            return None

        try:
            return PortfolioBond(
                # Основные идентификаторы
                isin=row['isin'],
                figi=row.get('figi'),

                # Данные из bonds_catalog
                ticker=row.get('ticker'),
                name=row.get('name'),
                currency=row.get('currency'),
                nominal=row.get('nominal'),
                maturity_date=row.get('maturity_date'),
                offer_date=row.get('offer_date'),
                coupon_quantity_per_year=row.get('coupon_quantity_per_year'),
                coupon_rate_percent=row.get('coupon_rate_percent'),
                coupon_type=row.get('coupon_type'),
                coupon_spread=row.get('coupon_spread'),
                list_level=row.get('list_level'),
                risk_level=row.get('risk_level'),
                issue_size=row.get('issue_size'),
                is_trade_available=bool(row.get('is_trade_available') or False),
                is_for_qualified_investors=bool(row.get('is_for_qualified_investors') or False),
                floating_coupon_flag=bool(row.get('floating_coupon_flag') or False),
                perpetual_flag=bool(row.get('perpetual_flag') or False),
                amortization_flag=bool(row.get('amortization_flag') or False),

                # Данные из portfolio_positions
                quantity=row.get('quantity'),
                average_price=row.get('average_price'),
                current_price=row.get('current_price'),
                yield_to_maturity=row.get('yield_to_maturity'),
                portfolio_percent=row.get('portfolio_percent'),
                current_value=row.get('current_value'),
                broker_name=row.get('broker_name')
            )

        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка преобразования строки в PortfolioBond для ISIN {row.get('isin') or 'unknown'}: {e}")
            return None

    def get_offers_due_soon(self, days_threshold: int) -> List[Tuple[str, date]]:
        cursor = self._cursor()
        today = date.today()
        future_date = today + timedelta(days=days_threshold)

        # Ищем облигации в портфеле, у которых дата оферты попадает в заданный порог
        cursor.execute("""
            SELECT bc.isin, bc.offer_date
            FROM bonds_catalog bc
            INNER JOIN portfolio_positions pp ON bc.isin = pp.isin
            WHERE bc.offer_date IS NOT NULL
              AND bc.offer_date > %s
              AND bc.offer_date <= %s
            ORDER BY bc.offer_date ASC
        """, (today, future_date))

        return [(row['isin'], row['offer_date']) for row in cursor.fetchall()]

    def get_isins_with_monitoring_metrics(self, metric_name: str) -> Set[str]:
        cursor = self._cursor()
        cursor.execute("SELECT DISTINCT isin FROM monitoring_checks WHERE metric_name = %s", (metric_name,))
        return {row['isin'] for row in cursor.fetchall()}

    def get_monitoring_check_history(self, isin: str, metric_name: str) -> List[Tuple[date, str]]:
        cursor = self._cursor()
        cursor.execute("""
            SELECT check_date, metric_value FROM monitoring_checks
            WHERE isin = %s AND metric_name = %s
            ORDER BY check_date ASC
        """, (isin, metric_name))
        history = []
        for row in cursor.fetchall():
            history.append((row['check_date'], row['metric_value']))
        return history

    def get_all_monitoring_checks(self) -> List[Tuple[str, str, str, str]]:
        """Получает все записи мониторинга."""
        cursor = self._cursor()
        cursor.execute("""
            SELECT isin, metric_name, metric_value, check_date
            FROM monitoring_checks
            ORDER BY isin, check_date DESC
        """)
        return [(r['isin'], r['metric_name'], r['metric_value'], r['check_date']) for r in cursor.fetchall()]

    def get_monitoring_checks_by_isin(self, isin: str) -> List[Tuple[str, str, str, str]]:
        """Получает все записи мониторинга для конкретного ISIN."""
        cursor = self._cursor()
        cursor.execute("""
            SELECT isin, metric_name, metric_value, check_date
            FROM monitoring_checks
            WHERE isin = %s
            ORDER BY check_date DESC
        """, (isin,))
        return [(r['isin'], r['metric_name'], r['metric_value'], r['check_date']) for r in cursor.fetchall()]

    def get_monitoring_checks_by_adapter(self, adapter_name: str) -> List[Tuple[str, str, str, str]]:
        """Получает все записи мониторинга для конкретного адаптера."""
        cursor = self._cursor()
        cursor.execute("""
            SELECT isin, metric_name, metric_value, check_date
            FROM monitoring_checks
            WHERE metric_name = %s
            ORDER BY isin, check_date DESC
        """, (adapter_name,))
        return [(r['isin'], r['metric_name'], r['metric_value'], r['check_date']) for r in cursor.fetchall()]

    def update_market_prices(self, prices_data: List[Tuple[str, Decimal, str]]) -> int:
        """
        Обновляет рыночные цены для списка облигаций.

        Args:
            prices_data: Список кортежей (isin, price, source)

        Returns:
            int: Количество обновленных записей
        """
        if not prices_data:
            return 0

        cursor = self._cursor()
        updated_count = 0

        with self.conn:
            for isin, price, source in prices_data:
                try:
                    cursor.execute("""
                        UPDATE bonds_catalog
                        SET market_price = %s,
                            market_price_source = %s,
                            market_price_updated_at = CURRENT_TIMESTAMP
                        WHERE isin = %s
                    """, (price, source, isin))

                    if cursor.rowcount > 0:
                        updated_count += 1

                except Exception as e:
                    logger.error(f"Ошибка обновления цены для {isin}: {e}")

        return updated_count

    def get_bonds_without_market_prices(self) -> List[str]:
        """
        Получает список ISIN облигаций без рыночных цен.

        Returns:
            List[str]: Список ISIN
        """
        cursor = self._cursor()
        cursor.execute("""
            SELECT isin FROM bonds_catalog
            WHERE market_price IS NULL
            AND is_trade_available IS TRUE
        """)
        return [row['isin'] for row in cursor.fetchall()]

    def get_bonds_with_old_market_prices(self, hours_threshold: int = 24) -> List[str]:
        """
        Получает список ISIN облигаций с устаревшими рыночными ценами.

        Args:
            hours_threshold: Порог в часах для определения устаревших цен

        Returns:
            List[str]: Список ISIN
        """
        cursor = self._cursor()
        cursor.execute("""
            SELECT isin FROM bonds_catalog
            WHERE market_price IS NOT NULL
            AND (market_price_updated_at IS NULL
                 OR market_price_updated_at < now() - make_interval(hours => %s))
            AND is_trade_available IS TRUE
        """, (hours_threshold,))
        return [row['isin'] for row in cursor.fetchall()]

    def get_all_floaters(self) -> List[Bond]:
        """
        Возвращает список всех облигаций-флоатеров из портфеля.
        """
        try:
            cursor = self._cursor()
            query = """
                SELECT bc.*
                FROM bonds_catalog bc
                JOIN portfolio_positions pp ON bc.isin = pp.isin
                WHERE bc.floating_coupon_flag IS TRUE
            """
            cursor.execute(query)
            rows = cursor.fetchall()

            bonds = [_row_to_bond(row) for row in rows if _row_to_bond(row) is not None]

            logger.info(f"Найдено {len(bonds)} облигаций-флоатеров в портфеле.")
            return bonds
        except psycopg2.Error as e:
            logger.error(f"Ошибка при запросе флоатеров из портфеля: {e}")
            return []

    def update_bond_coupon_rate(self, isin: str, new_rate: Decimal):
        """
        Обновляет процентную ставку купона для указанной облигации.
        """
        query = "UPDATE bonds_catalog SET coupon_rate_percent = %s, updated_at = CURRENT_TIMESTAMP WHERE isin = %s"
        cursor = self._cursor()
        try:
            logger.debug(f"DB_WRITE: Preparing to update ISIN {isin} with new rate {new_rate}.")
            with self.conn:
                cursor.execute(query, (new_rate, isin))
            rowcount = cursor.rowcount
            logger.debug(f"DB_WRITE: Executed UPDATE for ISIN {isin}. Rowcount: {rowcount}.")

            if rowcount == 0:
                logger.warning(f"Не удалось обновить ставку для {isin}: облигация не найдена (rowcount=0).")
            else:
                logger.info(f"DB_WRITE: Successfully updated ISIN {isin}.")

        except psycopg2.Error as e:
            logger.error(f"DB_WRITE: Ошибка БД при обновлении ставки для {isin}: {e}")
