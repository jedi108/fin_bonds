import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
from datetime import date, datetime, timedelta
from decimal import Decimal
from collections import defaultdict

from src.data_models import Bond, CalculatedCoupon, PortfolioPosition, PortfolioBond
from src.use_cases.interfaces import IPortfolioStorage

logger = logging.getLogger(__name__)

def _row_to_bond(row: sqlite3.Row) -> Optional[Bond]:
    """Преобразует sqlite3.Row в DTO Bond, обрабатывая типы данных."""
    if not row:
        return None
    row_dict = dict(row)
    try:
        maturity_date = datetime.strptime(row_dict['maturity_date'], '%Y-%m-%d').date() if row_dict.get('maturity_date') else None
        offer_date = datetime.strptime(row_dict['offer_date'], '%Y-%m-%d').date() if row_dict.get('offer_date') else None
        
        return Bond(
            figi=row_dict.get('figi'),
            isin=row_dict.get('isin'),
            ticker=row_dict.get('ticker'),
            name=row_dict.get('name'),
            currency=row_dict.get('currency'),
            nominal=Decimal(str(row_dict['nominal'])) if row_dict.get('nominal') else None,
            maturity_date=maturity_date,
            offer_date=offer_date,
            coupon_quantity_per_year=row_dict.get('coupon_quantity_per_year'),
            coupon_rate_percent=Decimal(str(row_dict['coupon_rate_percent'])) if row_dict.get('coupon_rate_percent') is not None else None,
            coupon_spread=Decimal(str(row_dict['coupon_spread'])) if row_dict.get('coupon_spread') is not None else None,
            list_level=row_dict.get('list_level'),
            risk_level=row_dict.get('risk_level'),
            floating_coupon_flag=bool(row_dict.get('floating_coupon_flag', 0)),
            perpetual_flag=bool(row_dict.get('perpetual_flag', 0)),
            amortization_flag=bool(row_dict.get('amortization_flag', 0)),
            coupon_type=row_dict.get('coupon_type'),
            is_trade_available=bool(row_dict.get('is_trade_available')),
            created_at=row_dict.get('created_at'),
            updated_at=row_dict.get('updated_at'),
            is_for_qualified_investors=bool(row_dict.get('is_for_qualified_investors')),
        )
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"Failed to convert row to Bond DTO for ISIN {row_dict.get('isin')}: {e}")
        return None

def _row_to_portfolio_position(row: sqlite3.Row) -> Optional[PortfolioPosition]:
    """Преобразует sqlite3.Row в DTO PortfolioPosition."""
    if not row:
        return None
    row_dict = dict(row)
    # Явно указываем поля, которые ожидаем, чтобы избежать ошибок с лишними колонками
    expected_fields = {
        'isin', 'ticker', 'name', 'quantity', 'average_price', 
        'current_price', 'currency', 'yield_to_maturity', 
        'portfolio_percent', 'current_value', 'broker_name',
        'instrument_type', 'figi', 'liquidity_loss_ratio', 'coupon_rate_percent'
    }
    
    # Фильтруем row_dict, оставляя только ожидаемые поля
    filtered_data = {key: row_dict[key] for key in row_dict if key in expected_fields and row_dict[key] is not None}

    try:
        # Преобразуем Decimal поля, но сохраняем None для liquidity_loss_ratio
        for key in ['quantity', 'average_price', 'current_price', 'yield_to_maturity', 'portfolio_percent', 'current_value', 'coupon_rate_percent']:
            if filtered_data.get(key) is not None:
                filtered_data[key] = Decimal(str(filtered_data[key]))
        
        # Для liquidity_loss_ratio сохраняем None если значение пустое
        if 'liquidity_loss_ratio' in filtered_data and filtered_data['liquidity_loss_ratio'] is not None:
            filtered_data['liquidity_loss_ratio'] = Decimal(str(filtered_data['liquidity_loss_ratio']))
        else:
            filtered_data['liquidity_loss_ratio'] = None
        
        return PortfolioPosition(**filtered_data)
        
    except (ValueError, TypeError, KeyError) as e:
        logger.error(f"Failed to convert row to PortfolioPosition DTO for ISIN {row_dict.get('isin')}: {e}")
        return None

class Database(IPortfolioStorage):
    """
    Реализация интерфейса IPortfolioStorage для работы с SQLite.
    Отвечает за инициализацию, подключение и выполнение всех SQL-запросов.
    """

    def __init__(self, db_path: str):
        """
        Инициализирует объект базы данных.

        Args:
            db_path (str): Путь к файлу базы данных.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
        )
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _add_column_if_not_exists(self, cursor: sqlite3.Cursor, table_name: str, column_name: str, column_type: str):
        """
        Добавляет колонку в таблицу, если она еще не существует.
        """
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [column[1] for column in cursor.fetchall()]
        if column_name not in columns:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            logger.info(f"Добавлена колонка '{column_name}' в таблицу '{table_name}'.")

    def _create_tables(self):
        """Создает таблицы в базе данных, если они не существуют."""
        with self.conn:
            cursor = self.conn.cursor()
            
            # Основной каталог облигаций
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS bonds_catalog (
                isin TEXT PRIMARY KEY NOT NULL,
                figi TEXT,
                ticker TEXT,
                name TEXT,
                currency TEXT,
                nominal REAL,
                maturity_date TEXT,
                offer_date TEXT,
                coupon_quantity_per_year INTEGER,
                coupon_rate_percent REAL,
                coupon_type TEXT,
                list_level INTEGER,
                risk_level INTEGER,
                coupon_spread REAL,
                is_trade_available INTEGER,
                is_for_qualified_investors INTEGER,
                issue_size INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                floating_coupon_flag INTEGER,
                perpetual_flag INTEGER,
                amortization_flag INTEGER,
                market_price REAL,
                market_price_source TEXT,
                market_price_updated_at TEXT
            )
            """)
            
            # Добавляем новые колонки в существующую таблицу bonds_catalog (если нужно)
            self._add_column_if_not_exists(cursor, 'bonds_catalog', 'market_price', 'REAL')
            self._add_column_if_not_exists(cursor, 'bonds_catalog', 'market_price_source', 'TEXT')
            self._add_column_if_not_exists(cursor, 'bonds_catalog', 'market_price_updated_at', 'TEXT')
            
            # Таблица для хранения истории рейтингов
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS rating_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                isin TEXT NOT NULL,
                rating_date TEXT NOT NULL,
                rating_code TEXT,
                rating_score INTEGER,
                FOREIGN KEY (isin) REFERENCES bonds_catalog(isin)
            )
            """)
            
            # Таблица для хранения истории уровней риска от Тинькоф
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS risk_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                isin TEXT NOT NULL,
                risk_date TEXT NOT NULL,
                risk_level INTEGER,
                FOREIGN KEY (isin) REFERENCES bonds_catalog(isin)
            )
            """)
            
            # Таблица для хранения истории уровней листинга на Мосбирже
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS listlevel_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                isin TEXT NOT NULL,
                check_date TEXT NOT NULL,
                list_level INTEGER,
                FOREIGN KEY (isin) REFERENCES bonds_catalog(isin)
            )
            """)
            
            # Таблица для хранения рассчитанных значений купонов
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS calculated_coupons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                isin TEXT NOT NULL,
                check_date TEXT NOT NULL,
                calculated_rate REAL NOT NULL,
                UNIQUE(isin, check_date)
            )
            """)
            
            # Таблица для хранения текущих позиций в портфеле
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                isin TEXT NOT NULL,
                ticker TEXT,
                name TEXT,
                quantity REAL,
                average_price REAL,
                current_price REAL,
                currency TEXT,
                yield_to_maturity REAL,
                portfolio_percent REAL,
                current_value REAL,
                broker_name TEXT NOT NULL,
                instrument_type TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(isin, broker_name)
            )
            """)
            
            # Добавляем новые колонки, если их нет (для миграции старых баз данных)
            self._add_column_if_not_exists(
                cursor, 
                'portfolio_positions', 
                'liquidity_loss_ratio', 
                'REAL'
            )
            self._add_column_if_not_exists(
                cursor, 
                'portfolio_positions', 
                'coupon_rate_percent', 
                'REAL'
            )

            # Таблица для хранения истории анализа ликвидности
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS liquidity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                isin TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                base_volume REAL NOT NULL,
                market_exit_value REAL NOT NULL,
                loss_ratio REAL NOT NULL,
                depth_level INTEGER NOT NULL,
                data_source TEXT NOT NULL,
                FOREIGN KEY (isin) REFERENCES bonds_catalog(isin)
            )
            """)
            
            # Добавление триггера для автоматического обновления updated_at
            cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS update_positions_updated_at
            AFTER UPDATE ON portfolio_positions
            BEGIN
                UPDATE portfolio_positions SET updated_at = CURRENT_TIMESTAMP WHERE isin = OLD.isin AND broker_name = OLD.broker_name;
            END;
            """)
            
            # Таблица для хранения результатов мониторинга
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitoring_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                isin TEXT NOT NULL,
                check_date TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                metric_value TEXT,
                UNIQUE(isin, check_date, metric_name)
            )
            """)
            
            logger.info("Проверка и создание таблиц в БД завершены.")

    def _migrate_schema(self):
        # Этот метод больше не нужен, так как мы работаем с актуальной схемой.
        # Оставляем его пустым на случай, если он где-то вызывается, чтобы не было ошибки.
        pass

    def close(self):
        if self.conn:
            self.conn.close()

    def get_bond_by_isin(self, isin: str) -> Optional[Bond]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM bonds_catalog WHERE isin = ?", (isin,))
        row = cursor.fetchone()
        return _row_to_bond(row)

    def get_bond_names_by_isins(self, isins: List[str]) -> Dict[str, str]:
        cursor = self.conn.cursor()
        placeholders = ','.join('?' for _ in isins)
        cursor.execute(f"SELECT isin, name FROM bonds_catalog WHERE isin IN ({placeholders})", isins)
        return {row['isin']: row['name'] for row in cursor.fetchall()}

    def get_rating_history_for_bonds(self, isins: List[str]) -> Dict[str, List[Tuple[date, int]]]:
        if not isins:
            return {}
        cursor = self.conn.cursor()
        placeholders = ','.join('?' for _ in isins)
        query = f"""
            SELECT r.isin, r.rating_date, r.rating_score
            FROM rating_history r
            WHERE r.isin IN ({placeholders})
            ORDER BY r.isin, r.rating_date
        """
        cursor.execute(query, isins)
        history_data = defaultdict(list)
        for row in cursor.fetchall():
            try:
                dt = datetime.strptime(row['rating_date'], '%Y-%m-%d').date()
                history_data[row['isin']].append((dt, row['rating_score']))
            except (ValueError, TypeError) as e:
                logging.warning(f"Некорректные данные для isin {row['isin']} в rating_history: {e}")
        return history_data

    def get_risk_history_for_bonds(self, isins: List[str]) -> Dict[str, List[Tuple[date, int]]]:
        if not isins:
            return {}
        cursor = self.conn.cursor()
        placeholders = ','.join('?' for _ in isins)
        query = f"""
            SELECT r.isin, r.risk_date, r.risk_level
            FROM risk_history r
            WHERE r.isin IN ({placeholders})
            ORDER BY r.isin, r.risk_date
        """
        cursor.execute(query, isins)
        history_data = defaultdict(list)
        for row in cursor.fetchall():
            try:
                dt = datetime.strptime(row['risk_date'], '%Y-%m-%d').date()
                history_data[row['isin']].append((dt, row['risk_level']))
            except (ValueError, TypeError) as e:
                logging.warning(f"Некорректные данные для isin {row['isin']} в risk_history: {e}")
        return history_data

    def get_listlevel_history_for_bonds(self, isins: List[str]) -> Dict[str, List[Tuple[date, int]]]:
        if not isins:
            return {}
        cursor = self.conn.cursor()
        placeholders = ','.join('?' for _ in isins)
        query = f"""
            SELECT l.isin, l.check_date, l.list_level
            FROM listlevel_history l
            WHERE l.isin IN ({placeholders})
            ORDER BY l.isin, l.check_date
        """
        cursor.execute(query, isins)
        history_data = defaultdict(list)
        for row in cursor.fetchall():
            try:
                dt = datetime.strptime(row['check_date'], '%Y-%m-%d').date()
                history_data[row['isin']].append((dt, row['list_level']))
            except (ValueError, TypeError) as e:
                logging.warning(f"Некорректные данные для isin {row['isin']} в listlevel_history: {e}")
        return history_data
    
    def is_catalog_empty(self) -> bool:
        cursor = self.conn.cursor()
        cursor.execute("SELECT EXISTS(SELECT 1 FROM bonds_catalog);")
        return cursor.fetchone()[0] == 0

    def get_catalog_columns(self) -> List[str]:
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(bonds_catalog)")
        return [row['name'] for row in cursor.fetchall()]

    def get_all_isins_from_catalog(self) -> Set[str]:
        """Возвращает множество всех ISIN из каталога для быстрой проверки существования."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT isin FROM bonds_catalog")
        result = {row['isin'] for row in cursor.fetchall()}
        logger.info(f"get_all_isins_from_catalog: найдено {len(result)} ISIN в базе данных")
        return result

    def add_bonds_to_catalog(self, bonds_dto_list: List[Bond]):
        """Добавляет или обновляет список облигаций в каталоге."""
        bonds_data = []
        for bond in bonds_dto_list:
            bonds_data.append((
                bond.isin, bond.figi, bond.ticker, bond.name, bond.currency,
                float(bond.nominal) if bond.nominal else None,
                bond.maturity_date.strftime('%Y-%m-%d') if bond.maturity_date else None,
                bond.offer_date.strftime('%Y-%m-%d') if bond.offer_date else None,
                bond.coupon_quantity_per_year, float(bond.coupon_rate_percent) if bond.coupon_rate_percent else None, bond.coupon_type,
                bond.list_level, bond.risk_level, float(bond.coupon_spread) if bond.coupon_spread else None,
                bond.is_trade_available, bond.is_for_qualified_investors, bond.issue_size,
                bond.created_at, bond.updated_at, bond.floating_coupon_flag, bond.perpetual_flag, bond.amortization_flag,
                float(bond.market_price) if bond.market_price else None, bond.market_price_source, 
                bond.market_price_updated_at.strftime('%Y-%m-%d %H:%M:%S') if bond.market_price_updated_at else None
            ))

        with self.conn:
            self.conn.executemany("""
                INSERT OR REPLACE INTO bonds_catalog ( 
                    isin, figi, ticker, name, currency, nominal, maturity_date, offer_date,
                    coupon_quantity_per_year, coupon_rate_percent, coupon_type,
                    list_level, risk_level, coupon_spread,
                    is_trade_available, is_for_qualified_investors, issue_size,
                    created_at, updated_at, floating_coupon_flag, perpetual_flag, amortization_flag,
                    market_price, market_price_source, market_price_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, bonds_data)
        logger.info(f"Добавлено/обновлено {len(bonds_data)} облигаций в каталоге.")

    def get_bonds_without_list_level(self) -> List[sqlite3.Row]:
        """Возвращает облигации, у которых не установлен уровень листинга."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT isin, ticker FROM bonds_catalog WHERE list_level IS NULL OR list_level = 0")
        return cursor.fetchall()
    
    def get_bonds_for_moex_update(self) -> List[Bond]:
        """
        Возвращает облигации, которые нужно обновить данными с MOEX.
        Критерии:
        - Плавающий купон (для обновления ставки).
        - Отсутствует уровень листинга.
        - Ставка купона не задана (NULL или 0).
        """
        cursor = self.conn.cursor()
        query = """
            SELECT * FROM bonds_catalog
            WHERE floating_coupon_flag = 1
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
            self.conn.execute("""
                UPDATE bonds_catalog
                SET list_level = ?, coupon_rate_percent = ?, offer_date = ?, updated_at = CURRENT_TIMESTAMP
                WHERE isin = ?
            """, (list_level, str(coupon_rate) if coupon_rate else None, offer_date.strftime('%Y-%m-%d') if offer_date else None, isin))
        logger.debug(f"Обновлены MOEX данные для {isin}.")

    def get_full_catalog(self) -> List[Bond]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM bonds_catalog")
        return [bond for row in cursor.fetchall() if (bond := _row_to_bond(row)) is not None]

    def get_bonds_for_export(self, min_maturity_date: str, max_maturity_date: str, min_coupon_rate: float, min_coupon_frequency: int) -> List[sqlite3.Row]:
        with self.conn:
            cursor = self.conn.cursor()
            query = "SELECT * FROM bonds_catalog WHERE 1=1"
            params = []
            if min_maturity_date:
                query += " AND maturity_date >= ?"
                params.append(min_maturity_date)
            if max_maturity_date:
                query += " AND maturity_date <= ?"
                params.append(max_maturity_date)
            if min_coupon_rate:
                query += " AND coupon_rate_percent >= ?"
                params.append(min_coupon_rate)
            if min_coupon_frequency:
                query += " AND coupon_quantity_per_year >= ?"
                params.append(min_coupon_frequency)
            cursor.execute(query, params)
            return cursor.fetchall()

    def add_monitoring_check(self, isin: str, metric_name: str, metric_value: str):
        """
        Добавляет результат проверки мониторинга в БД.
        
        ВАЖНО: Для метрики 'liquidity_analyzer' значение является биржевым свойством ISIN,
        а не свойством брокера. Стакан заявок одинаков для всех брокеров на MOEX.
        """
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(isin, check_date, metric_name) DO UPDATE SET metric_value=excluded.metric_value
                """,
                (isin, date.today().isoformat(), metric_name, metric_value)
            )

    def get_last_monitoring_check(self, isin: str, metric_name: str) -> Optional[Tuple[date, str]]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT check_date, metric_value FROM monitoring_checks
            WHERE isin = ? AND metric_name = ?
            ORDER BY check_date DESC LIMIT 1
        """, (isin, metric_name))
        row = cursor.fetchone()
        if row:
            return datetime.strptime(row['check_date'], '%Y-%m-%d').date(), row['metric_value']
        return None

    def get_latest_calculated_coupon(self, isin: str) -> Optional[CalculatedCoupon]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM calculated_coupons WHERE isin = ? ORDER BY check_date DESC LIMIT 1", (isin,))
        row = cursor.fetchone()
        if not row:
            return None
        return CalculatedCoupon(
            isin=row['isin'],
            check_date=datetime.strptime(row['check_date'], '%Y-%m-%d').date(),
            calculated_rate=Decimal(str(row['calculated_rate']))
        )

    def add_calculated_coupon(self, coupon: CalculatedCoupon):
        query = """
            INSERT INTO calculated_coupons (isin, check_date, calculated_rate) VALUES (?, ?, ?)
            ON CONFLICT(isin, check_date) DO UPDATE SET calculated_rate=excluded.calculated_rate;
        """
        with self.conn:
            self.conn.execute(query, (coupon.isin, coupon.check_date.isoformat(), str(coupon.calculated_rate)))

    def set_coupon_spread(self, isin: str, spread: float) -> bool:
        query = "UPDATE bonds_catalog SET coupon_spread = ? WHERE isin = ?"
        cursor = self.conn.cursor()
        try:
            cursor.execute(query, (spread, isin))
            self.conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"Не удалось обновить спред для {isin}: {e}")
            self.conn.rollback()
            return False

    def get_portfolio_floaters_without_spread(self) -> List[Bond]:
        """
        Возвращает список облигаций из портфеля, которые являются флоатерами
        и для которых еще не рассчитан спред.
        """
        try:
            cursor = self.conn.cursor()
            query = """
                SELECT bc.*
                FROM portfolio_positions pp
                JOIN bonds_catalog bc ON pp.isin = bc.isin
                WHERE bc.floating_coupon_flag = 1
                  AND bc.coupon_spread IS NULL;
            """
            cursor.execute(query)
            rows = cursor.fetchall()
            bonds = [_row_to_bond(row) for row in rows if _row_to_bond(row) is not None]
            logger.info(f"Найдено {len(bonds)} флоатеров в портфеле без рассчитанного спреда.")
            return bonds
        except sqlite3.Error as e:
            logger.error(f"Ошибка при запросе флоатеров из портфеля: {e}")
            return []

    def clear_portfolio_positions(self):
        """Полностью очищает таблицу `portfolio_positions`."""
        logger.info("Clearing all portfolio positions from the database.")
        try:
            with self.conn:
                self.conn.execute("DELETE FROM portfolio_positions")
            logger.info("Таблица portfolio_positions успешно очищена.")
        except sqlite3.Error as e:
            logger.error(f"Ошибка при очистке portfolio_positions: {e}")

    def clear_portfolio_positions_by_broker(self, broker_name: str):
        """Очищает позиции только для указанного брокера."""
        logger.info(f"Clearing portfolio positions for broker: {broker_name}")
        try:
            with self.conn:
                self.conn.execute("DELETE FROM portfolio_positions WHERE broker_name = ?", (broker_name,))
            logger.info(f"Позиции брокера {broker_name} успешно удалены из портфеля.")
        except sqlite3.Error as e:
            logger.error(f"Ошибка при удалении позиций брокера {broker_name}: {e}")

    def update_position_liquidity(self, isin: str, broker_name: str, liquidity_value: float):
        """Обновляет значение liquidity_loss_ratio для позиции в портфеле."""
        try:
            with self.conn:
                self.conn.execute("""
                    UPDATE portfolio_positions 
                    SET liquidity_loss_ratio = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE isin = ? AND broker_name = ?
                """, (liquidity_value, isin, broker_name))
            logger.debug(f"Обновлена ликвидность для {isin} ({broker_name}): {liquidity_value}")
        except sqlite3.Error as e:
            logger.error(f"Ошибка при обновлении ликвидности для {isin} ({broker_name}): {e}")
            raise

    def add_portfolio_positions(self, positions: List['PortfolioPosition']):
        """
        Сохраняет или обновляет позиции в портфеле.
        Использует INSERT OR REPLACE для атомарности.
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
            cursor = self.conn.cursor()
            # Получаем имена столбцов из первого словаря, чтобы обеспечить их соответствие
            columns = ', '.join(positions_to_save[0].keys())
            placeholders = ', '.join('?' for _ in positions_to_save[0])
            
            query = f"INSERT OR REPLACE INTO portfolio_positions ({columns}) VALUES ({placeholders})"
            
            # Преобразуем словари в кортежи в правильном порядке
            data_tuples = [tuple(p.values()) for p in positions_to_save]
            
            cursor.executemany(query, data_tuples)
            logger.info(f"Успешно сохранено/обновлено {len(positions)} позиций в портфеле.")

    def get_portfolio_positions(self) -> List['PortfolioPosition']:
        """
        Возвращает все позиции из портфеля, обогащенные FIGI из каталога.
        """
        self.conn.row_factory = sqlite3.Row
        cursor = self.conn.cursor()
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
        self.conn.row_factory = sqlite3.Row
        cursor = self.conn.cursor()
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
            WHERE p.isin = ?
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

        cursor = self.conn.cursor()
        placeholders = ','.join('?' for _ in isins)
        query = f"""
            SELECT h.isin, h.timestamp, h.loss_ratio
            FROM liquidity_history h
            WHERE h.isin IN ({placeholders})
            ORDER BY h.isin, h.timestamp
        """
        cursor.execute(query, isins)
        history_data = defaultdict(list)
        for row in cursor.fetchall():
            try:
                # Преобразуем ISO timestamp в дату
                dt = datetime.fromisoformat(row['timestamp']).date()
                # Преобразуем строку коэффициента потерь в float
                loss_ratio = float(row['loss_ratio'])
                history_data[row['isin']].append((dt, loss_ratio))
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
            cursor = self.conn.cursor()
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
            
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении данных для мониторинга: {e}")
            return []

    def get_bond_data_for_monitoring(self, isin: str) -> Optional['PortfolioBond']:
        """
        Возвращает объект PortfolioBond для одной облигации по ISIN,
        объединяя данные из portfolio_positions и bonds_catalog.
        """
        try:
            cursor = self.conn.cursor()
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
                WHERE p.isin = ?
            """
            cursor.execute(query, (isin,))
            row = cursor.fetchone()
            
            if row:
                return self._row_to_portfolio_bond(row)
            else:
                logger.warning(f"Облигация {isin} не найдена в портфеле или каталоге")
                return None
                
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении данных для {isin}: {e}")
            return None

    def _row_to_portfolio_bond(self, row: sqlite3.Row) -> Optional['PortfolioBond']:
        """
        Преобразует sqlite3.Row в DTO PortfolioBond,
        корректно обрабатывая типы данных.
        """
        if not row:
            return None
            
        try:
            from src.data_models import PortfolioBond
            from decimal import Decimal
            from datetime import datetime
            
            # Функция для безопасного извлечения значений из sqlite3.Row
            def safe_get(row, key):
                try:
                    return row[key]
                except (IndexError, KeyError):
                    return None
            
            # Преобразование дат
            maturity_date = None
            maturity_str = safe_get(row, 'maturity_date')
            if maturity_str:
                maturity_date = datetime.strptime(maturity_str, '%Y-%m-%d').date()
                
            offer_date = None
            offer_str = safe_get(row, 'offer_date')
            if offer_str:
                offer_date = datetime.strptime(offer_str, '%Y-%m-%d').date()
            
            return PortfolioBond(
                # Основные идентификаторы
                isin=row['isin'],
                figi=safe_get(row, 'figi'),
                
                # Данные из bonds_catalog
                ticker=safe_get(row, 'ticker'),
                name=safe_get(row, 'name'),
                currency=safe_get(row, 'currency'),
                nominal=Decimal(str(safe_get(row, 'nominal'))) if safe_get(row, 'nominal') is not None else None,
                maturity_date=maturity_date,
                offer_date=offer_date,
                coupon_quantity_per_year=safe_get(row, 'coupon_quantity_per_year'),
                coupon_rate_percent=Decimal(str(safe_get(row, 'coupon_rate_percent'))) if safe_get(row, 'coupon_rate_percent') is not None else None,
                coupon_type=safe_get(row, 'coupon_type'),
                coupon_spread=Decimal(str(safe_get(row, 'coupon_spread'))) if safe_get(row, 'coupon_spread') is not None else None,
                list_level=safe_get(row, 'list_level'),
                risk_level=safe_get(row, 'risk_level'),
                issue_size=safe_get(row, 'issue_size'),
                is_trade_available=bool(safe_get(row, 'is_trade_available') or 0),
                is_for_qualified_investors=bool(safe_get(row, 'is_for_qualified_investors') or 0),
                floating_coupon_flag=bool(safe_get(row, 'floating_coupon_flag') or 0),
                perpetual_flag=bool(safe_get(row, 'perpetual_flag') or 0),
                amortization_flag=bool(safe_get(row, 'amortization_flag') or 0),
                
                # Данные из portfolio_positions
                quantity=Decimal(str(safe_get(row, 'quantity'))) if safe_get(row, 'quantity') is not None else None,
                average_price=Decimal(str(safe_get(row, 'average_price'))) if safe_get(row, 'average_price') is not None else None,
                current_price=Decimal(str(safe_get(row, 'current_price'))) if safe_get(row, 'current_price') is not None else None,
                yield_to_maturity=Decimal(str(safe_get(row, 'yield_to_maturity'))) if safe_get(row, 'yield_to_maturity') is not None else None,
                portfolio_percent=Decimal(str(safe_get(row, 'portfolio_percent'))) if safe_get(row, 'portfolio_percent') is not None else None,
                current_value=Decimal(str(safe_get(row, 'current_value'))) if safe_get(row, 'current_value') is not None else None,
                broker_name=safe_get(row, 'broker_name')
            )
            
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Ошибка преобразования строки в PortfolioBond для ISIN {safe_get(row, 'isin') or 'unknown'}: {e}")
            return None 

    def get_offers_due_soon(self, days_threshold: int) -> List[Tuple[str, date]]:
        cursor = self.conn.cursor()
        today = date.today().isoformat()
        future_date = (date.today() + timedelta(days=days_threshold)).isoformat()

        # Ищем облигации в портфеле, у которых дата оферты попадает в заданный порог
        cursor.execute("""
            SELECT bc.isin, bc.offer_date
            FROM bonds_catalog bc
            INNER JOIN portfolio_positions pp ON bc.isin = pp.isin
            WHERE bc.offer_date IS NOT NULL 
              AND bc.offer_date > ? 
              AND bc.offer_date <= ?
            ORDER BY bc.offer_date ASC
        """, (today, future_date))
        
        offers = []
        for row in cursor.fetchall():
            offers.append((row['isin'], datetime.strptime(row['offer_date'], '%Y-%m-%d').date()))
        return offers

    def get_instruments_by_type(self, instrument_type: str) -> List[Bond]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT *
            FROM bonds_catalog
            WHERE instrument_type = ?
        """, (instrument_type,))
        return [bond for row in cursor.fetchall() if (bond := _row_to_bond(row)) is not None]

    def get_isins_with_monitoring_metrics(self, metric_name: str) -> Set[str]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT DISTINCT isin FROM monitoring_checks WHERE metric_name = ?", (metric_name,))
        return {row['isin'] for row in cursor.fetchall()}

    def get_monitoring_check_history(self, isin: str, metric_name: str) -> List[Tuple[date, str]]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT check_date, metric_value FROM monitoring_checks
            WHERE isin = ? AND metric_name = ?
            ORDER BY check_date ASC
        """, (isin, metric_name))
        history = []
        for row in cursor.fetchall():
            try:
                dt = datetime.strptime(row['check_date'], '%Y-%m-%d').date()
                history.append((dt, row['metric_value']))
            except (ValueError, TypeError) as e:
                logger.warning(f"Некорректные данные для isin {isin}, metric {metric_name} в monitoring_checks: {e}")
        return history

    def get_all_monitoring_checks(self) -> List[Tuple[str, str, str, str]]:
        """Получает все записи мониторинга."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT isin, metric_name, metric_value, check_date
            FROM monitoring_checks 
            ORDER BY isin, check_date DESC
        """)
        
        return cursor.fetchall()

    def get_monitoring_checks_by_isin(self, isin: str) -> List[Tuple[str, str, str, str]]:
        """Получает все записи мониторинга для конкретного ISIN."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT isin, metric_name, metric_value, check_date
            FROM monitoring_checks 
            WHERE isin = ?
            ORDER BY check_date DESC
        """, (isin,))
        
        return cursor.fetchall()

    def get_monitoring_checks_by_adapter(self, adapter_name: str) -> List[Tuple[str, str, str, str]]:
        """Получает все записи мониторинга для конкретного адаптера."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT isin, metric_name, metric_value, check_date
            FROM monitoring_checks 
            WHERE metric_name = ?
            ORDER BY isin, check_date DESC
        """, (adapter_name,))

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
            
        cursor = self.conn.cursor()
        updated_count = 0
        
        with self.conn:
            for isin, price, source in prices_data:
                try:
                    cursor.execute("""
                        UPDATE bonds_catalog 
                        SET market_price = ?, 
                            market_price_source = ?, 
                            market_price_updated_at = CURRENT_TIMESTAMP
                        WHERE isin = ?
                    """, (float(price), source, isin))
                    
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
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT isin FROM bonds_catalog 
            WHERE market_price IS NULL 
            AND is_trade_available = 1
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
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT isin FROM bonds_catalog 
            WHERE market_price IS NOT NULL 
            AND (market_price_updated_at IS NULL 
                 OR datetime(market_price_updated_at) < datetime('now', '-{} hours'))
            AND is_trade_available = 1
        """.format(hours_threshold))
        return [row['isin'] for row in cursor.fetchall()]
        
        return cursor.fetchall()

    def get_all_floaters(self) -> List[Bond]:
        """
        Возвращает список всех облигаций-флоатеров из портфеля.
        """
        try:
            cursor = self.conn.cursor()
            query = """
                SELECT bc.* 
                FROM bonds_catalog bc
                JOIN portfolio_positions pp ON bc.isin = pp.isin
                WHERE bc.floating_coupon_flag = 1
            """
            cursor.execute(query)
            rows = cursor.fetchall()
            
            bonds = [_row_to_bond(row) for row in rows if _row_to_bond(row) is not None]
            
            logger.info(f"Найдено {len(bonds)} облигаций-флоатеров в портфеле.")
            return bonds
        except sqlite3.Error as e:
            logger.error(f"Ошибка при запросе флоатеров из портфеля: {e}")
            return []

    def update_bond_coupon_rate(self, isin: str, new_rate: Decimal):
        """
        Обновляет процентную ставку купона для указанной облигации.
        """
        query = "UPDATE bonds_catalog SET coupon_rate_percent = ?, updated_at = CURRENT_TIMESTAMP WHERE isin = ?"
        cursor = self.conn.cursor()
        try:
            logger.debug(f"DB_WRITE: Preparing to update ISIN {isin} with new rate {new_rate} (as float: {float(new_rate)}).")
            cursor.execute(query, (float(new_rate), isin))
            logger.debug(f"DB_WRITE: Executed UPDATE for ISIN {isin}. Rowcount: {cursor.rowcount}.")
            
            self.conn.commit()
            logger.debug(f"DB_WRITE: Committed transaction for ISIN {isin}.")

            if cursor.rowcount == 0:
                logger.warning(f"Не удалось обновить ставку для {isin}: облигация не найдена (rowcount=0).")
            else:
                logger.info(f"DB_WRITE: Successfully updated and committed ISIN {isin}.")

        except sqlite3.Error as e:
            logger.error(f"DB_WRITE: SQLite error while updating {isin}: {e}")
            try:
                self.conn.rollback()
                logger.warning(f"DB_WRITE: Rolled back transaction for ISIN {isin}.")
            except sqlite3.Error as re:
                logger.error(f"DB_WRITE: Additional error during rollback for {isin}: {re}")
 