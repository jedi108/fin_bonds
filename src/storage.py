"""
Единый слой хранения данных портфеля на PostgreSQL (psycopg2).

Класс PortfolioStorage — единственный владелец SQL: use-case'ы зовут его
методы и работают с DTO. Соединение создаётся в фабрике (POSTGRES_DSN) и
передаётся сюда готовым DSN; схема БД здесь не создаётся и не мутирует —
при несовпадении версий миграций конструктор падает с понятной ошибкой
(fail-fast, см. _check_schema_versions).
"""
import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from collections import defaultdict

import psycopg2
from psycopg2.extras import RealDictCursor

from src.data_models import Bond, CalculatedCoupon, PortfolioPosition, PortfolioBond, RatingTarget
from src.migrations import discover_migrations, get_applied_versions
from src.services.cashflow import calculate_bond_cashflow_metrics
from src.use_cases.bond_filters import (
    BUY_MATURITY_FLOOR_SQL,
    STRUCTURAL_BUY_WHERE_SQL,
)
from src.utils import round_coupon_rate

logger = logging.getLogger(__name__)

# Жизненный цикл позиции portfolio_positions.status (миграция 010, задача 002.1).
POSITION_STATUS_ACTIVE = 'active'
POSITION_STATUS_MATURED = 'matured'
POSITION_STATUS_CLOSED = 'closed'

# Подзапрос «последняя рассчитанная ставка купона флоатера» — общий фрагмент
# аналитических выборок. metric_value в monitoring_checks хранится строкой
# (TEXT), поэтому в расчётах она приводится к numeric.
_LATEST_FLOATER_RATE_JOIN = """
            LEFT JOIN (
                SELECT isin, metric_value
                FROM monitoring_checks
                WHERE metric_name = 'floater_coupon_calculator'
                AND check_date = (
                    SELECT MAX(check_date)
                    FROM monitoring_checks
                    WHERE isin = monitoring_checks.isin
                    AND metric_name = 'floater_coupon_calculator'
                )
            ) mc_floater ON bc.isin = mc_floater.isin
"""

# Последний кредитный рейтинг бумаги по rating_history (023): canonical
# buy-кандидат несёт настоящий рейтинг (credit_rating + rating_score);
# risk_level Т-Банка рейтингом не подменяется. Паттерн повторяет
# latest_ratings из get_chatgpt_portfolio_rows (DISTINCT ON по дате);
# строки с NULL-кодом пропускаются — адаптер credit_rating пишет только
# коды с числовым баллом по шкале rating_scale.
# 024: тот же join переиспользуют get_rebalance_portfolio_rows и
# get_scenario_universe_rows — финальный валидатор ограничений проверяет
# --min-credit-rating по всему целевому портфелю (positions_after), включая
# уже держимые бумаги; второй расчёт рейтинга не создаётся.
_LATEST_RATING_JOIN = """
            LEFT JOIN (
                SELECT DISTINCT ON (isin)
                       isin, rating_code AS credit_rating, rating_score
                FROM rating_history
                WHERE rating_code IS NOT NULL
                ORDER BY isin, rating_date DESC, id DESC
            ) lr ON lr.isin = bc.isin
"""

# Агрегат доли бумаги в портфеле «сейчас» по ISIN — бейдж held (N%) (задача 002.2).
# Агрегация через view v_portfolio_positions_valuation обязательна: прямой
# LEFT JOIN portfolio_positions задвоил бы строки для ISIN на двух счетах
# (кейс — ГлобалФ 5 шт + 2 шт на разных брокерах/счетах). Зомби-позиции
# (status matured/closed, миграция 010) дают position_value_rub = 0 и на
# долю практически не влияют — контракт бейджа единый с buy_candidates.sql.
_HELD_SHARE_JOIN = """
            LEFT JOIN (
                SELECT v.isin,
                       SUM(v.quantity) AS held_qty,
                       ROUND(100.0 * SUM(v.position_value_rub)
                             / NULLIF((SELECT SUM(position_value_rub) FROM v_portfolio_positions_valuation), 0), 2) AS held_share_pct
                FROM v_portfolio_positions_valuation v
                GROUP BY v.isin
            ) held ON held.isin = bc.isin
"""

# Колонки бейджа для SELECT-списков (алиас held доступен в самом запросе):
# ведущая запятая — фрагмент подставляется после последней «обычной» колонки.
# %% — экранированный процент для psycopg2 (в выдаче — 'held (N%)').
_HELD_BADGE_SELECT = """,
                CASE WHEN held.isin IS NOT NULL
                     THEN 'held (' || held.held_share_pct || '%%)' END as held_badge,
                held.held_share_pct"""

# Прежнее жёсткое исключение ISIN портфеля (include_held=False): запросы
# остаются идентичны прошлому поведению — обратная совместимость SOP (002.2).
_LEGACY_PORTFOLIO_EXCLUSION_JOIN = """
            LEFT JOIN portfolio_positions pp ON bc.isin = pp.isin
"""

# Агрегат позиций портфеля по ISIN для rebalance-report (005.1): строки разных
# брокеров/счетов сворачиваются в одну позицию. Стоимость — каноническая оценка
# из v_portfolio_positions_valuation (контракт A3/002.2: номинальный фолбэк
# живых позиций, зомби-строки погашенных бумаг — 0). price_x_qty — заготовка
# взвешенной по количеству фактической цены (фолбэк — market_price каталога).
_AGGREGATED_PORTFOLIO_SUBQUERY = """
        SELECT v.isin,
               MAX(v.ticker) AS pos_ticker,
               MAX(v.name) AS pos_name,
               SUM(v.quantity) AS quantity,
               SUM(v.position_value_rub) AS value_rub,
               SUM(v.current_price * v.quantity) AS price_x_qty,
               -- 030.7: источник оценки наибольшей позиции ISIN (та же
               -- семантика, что в get_chatgpt_portfolio_rows, 008) — fitter
               -- показывает рядом valuation_source и planning_price_basis
               -- (разные понятия: чем оценена позиция vs по какой цене план).
               (array_agg(v.valuation_source
                          ORDER BY v.position_value_rub DESC NULLS LAST))[1]
                   AS valuation_source
        FROM v_portfolio_positions_valuation v
        GROUP BY v.isin
"""


def _portfolio_exclusion_fragments(include_held: bool) -> Tuple[str, Optional[str], str]:
    """SQL-фрагменты фильтра «вне портфеля» для скрининговых выборок (002.2).

    Возвращает (join, not_held_condition | None, badge_select):
    - include_held=False — прежний LEFT JOIN portfolio_positions + условие
      pp.isin IS NULL; текст запроса не меняется (совместимость вывода).
    - include_held=True — агрегат доли по ISIN (_HELD_SHARE_JOIN), условие
      исключения снимается (None — не добавлять в WHERE), в SELECT
      добавляются held_badge / held_share_pct.
    """
    if include_held:
        return _HELD_SHARE_JOIN, None, _HELD_BADGE_SELECT
    return _LEGACY_PORTFOLIO_EXCLUSION_JOIN, "pp.isin IS NULL", ""


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
            market_price=row.get('market_price'),
            market_price_source=row.get('market_price_source'),
            market_price_updated_at=row.get('market_price_updated_at'),
            duration_macaulay=row.get('duration_macaulay'),
            duration_modified=row.get('duration_modified'),
            duration_null_reason=row.get('duration_null_reason'),
            duration_updated_at=row.get('duration_updated_at'),
            ytm=row.get('ytm'),
            ytm_null_reason=row.get('ytm_null_reason'),
            ytm_updated_at=row.get('ytm_updated_at'),
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
        'instrument_type', 'figi', 'liquidity_loss_ratio', 'coupon_rate_percent',
        'account_id', 'status'
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


class PortfolioStorage:
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

    @contextmanager
    def snapshot_transaction(self):
        """Read-only REPEATABLE READ транзакция согласованного среза (030.3).

        Гарантия: все чтения внутри блока видят один snapshot БД (REPEATABLE
        READ), а CURRENT_DATE/CURRENT_TIMESTAMP внутри транзакции зафиксированы
        на её старте (transaction_timestamp) — это и есть единый `as_of`
        PlanningContext. Дефолтный READ COMMITTED такой гарантии не даёт:
        каждый statement видит свою версию данных. READ ONLY — страховка от
        случайных записей. По выходе сессия возвращается к дефолтному
        READ COMMITTED (read-write) — поведение остального кода не меняется.
        """
        self.conn.rollback()  # закрыть возможную неявную транзакцию
        self.conn.set_session(
            isolation_level=psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ,
            readonly=True,
        )
        try:
            yield self
        finally:
            self.conn.rollback()  # read-only: коммитить нечего
            self.conn.set_session(
                isolation_level=psycopg2.extensions.ISOLATION_LEVEL_READ_COMMITTED,
                readonly=False,
            )

    def get_transaction_timestamp(self) -> Dict[str, Any]:
        """Транзакционные время и дата одним SELECT (030.3).

        CURRENT_TIMESTAMP (= now()/transaction_timestamp()) в PostgreSQL
        зафиксирован на старте транзакции — внутри snapshot_transaction он
        одинаков для всех statement'ов, как и CURRENT_DATE. Отдаём оба
        значения: PlanningContext.as_of берёт именно их, а не wall-clock
        Python'а.
        """
        cursor = self._cursor()
        cursor.execute('SELECT CURRENT_TIMESTAMP AS ts, CURRENT_DATE AS day')
        row = cursor.fetchone()
        return {'ts': row['ts'], 'day': row['day']}

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

    def get_figis_by_isins(self, isins: List[str]) -> Dict[str, str]:
        """Возвращает соответствие ISIN -> FIGI (только облигации с заданным FIGI)."""
        if not isins:
            return {}
        cursor = self._cursor()
        cursor.execute("SELECT isin, figi FROM bonds_catalog WHERE isin = ANY(%s)", (isins,))
        return {row['isin']: row['figi'] for row in cursor.fetchall() if row['figi']}

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

    def add_rating(self, isin: str, rating_date: date, rating_code: str, rating_score: int):
        """Добавляет запись в историю кредитных рейтингов.

        rating_code — значение по шкале рейтингов из конфигурации
        (rating_scale, в нормализованном виде — см. utils.normalize_rating_code);
        колонка agency в схеме отсутствует.

        Идемпотентно в пределах дня (002.6): уникальный индекс
        (isin, rating_date, rating_code) — миграция 012; повторная запись
        того же кода за ту же дату (перезапуск update-ratings) — no-op.
        Изменение кода в течение дня создаёт отдельную строку (история
        изменений сохраняется).
        """
        with self.conn:
            cursor = self._cursor()
            cursor.execute("""
                INSERT INTO rating_history (isin, rating_date, rating_code, rating_score)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (isin, rating_date, rating_code) DO NOTHING
            """, (isin, rating_date, rating_code, rating_score))

    def add_risk_level(self, isin: str, risk_date: date, risk_level: int):
        """Добавляет запись в историю уровней риска."""
        with self.conn:
            cursor = self._cursor()
            cursor.execute("""
                INSERT INTO risk_history (isin, risk_date, risk_level)
                VALUES (%s, %s, %s)
            """, (isin, risk_date, risk_level))

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
                # coupon_type выводим из флага флоатера, если источник его не
                # принёс (002.5, P4): каталог TBank пишет поле без купонного
                # типа, NULL ломал бы фильтры coupon_type='FLOAT' в ad-hoc SQL.
                bond.coupon_type or ('FLOAT' if bond.floating_coupon_flag else 'FIX'),
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
        self.update_bonds_derived_metrics([b.isin for b in bonds_dto_list])

    def add_or_update_bond(self, isin: str, ticker: str, name: str):
        """Вставляет или обновляет карточку облигации по минимальному набору полей.

        Используется seed-data: остальные поля каталога заполняются
        отдельными обогащениями (TBank/MOEX), здесь они не трогаются.
        """
        with self.conn:
            cursor = self._cursor()
            cursor.execute("""
                INSERT INTO bonds_catalog (isin, ticker, name)
                VALUES (%s, %s, %s)
                ON CONFLICT(isin) DO UPDATE SET
                    ticker = excluded.ticker,
                    name = excluded.name,
                    updated_at = CURRENT_TIMESTAMP
            """, (isin, ticker, name))

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
            """, (list_level, round_coupon_rate(coupon_rate), offer_date, isin))
        self.update_bonds_derived_metrics([isin])
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

    # ------------------------------------------------------------------
    # Зомби-позиции (задача 002.1): погашенные бумаги и нулевые количества
    # не должны оставаться активными строками с нулевой оценкой.
    # ------------------------------------------------------------------

    def find_zombies(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Возвращает кандидатов на чистку среди активных позиций (status='active').

        Ключи результата:
          - 'matured': бумага погашена (maturity_date < CURRENT_DATE) — зомби,
            чистится (status → 'matured');
          - 'zero_quantity': нулевое/NULL количество — зомби, чистится
            (status → 'closed');
          - 'zero_value_live': ненулевое количество и нулевая/NULL оценка на
            НЕпогашенной бумаге (класс ГлобалФ: цена 0 от брокера) — это НЕ
            ошибка синка, позиция не чистится, а гейт свежести считает её
            через WARN_ZOMBIE_ROWS. Возвращается только для отчёта.
        """
        cursor = self._cursor()
        cursor.execute("""
            SELECT
                pp.isin, pp.broker_name, pp.account_id, pp.ticker, pp.name,
                pp.quantity, pp.current_value, pp.status
            FROM portfolio_positions pp
            JOIN bonds_catalog c ON c.isin = pp.isin
            WHERE pp.status = 'active'
              AND c.maturity_date < CURRENT_DATE
            ORDER BY pp.isin, pp.broker_name, pp.account_id
        """)
        matured = [dict(r) for r in cursor.fetchall()]

        cursor.execute("""
            SELECT
                pp.isin, pp.broker_name, pp.account_id, pp.ticker, pp.name,
                pp.quantity, pp.current_value, pp.status
            FROM portfolio_positions pp
            WHERE pp.status = 'active'
              AND COALESCE(pp.quantity, 0) <= 0
            ORDER BY pp.isin, pp.broker_name, pp.account_id
        """)
        zero_quantity = [dict(r) for r in cursor.fetchall()]

        cursor.execute("""
            SELECT
                pp.isin, pp.broker_name, pp.account_id, pp.ticker, pp.name,
                pp.quantity, pp.current_value, pp.status,
                COALESCE(c.maturity_date, CURRENT_DATE) >= CURRENT_DATE AS is_unmatured
            FROM portfolio_positions pp
            LEFT JOIN bonds_catalog c ON c.isin = pp.isin
            WHERE pp.status = 'active'
              AND COALESCE(pp.quantity, 0) > 0
              AND (pp.current_value IS NULL OR pp.current_value <= 0)
              AND COALESCE(c.maturity_date, CURRENT_DATE) >= CURRENT_DATE
            ORDER BY pp.isin, pp.broker_name, pp.account_id
        """)
        zero_value_live = [dict(r) for r in cursor.fetchall()]

        return {
            'matured': matured,
            'zero_quantity': zero_quantity,
            'zero_value_live': zero_value_live,
        }

    def mark_matured_positions(self) -> List[Dict[str, Any]]:
        """
        Помечает status='matured' у активных позиций по погашенным бумагам
        (maturity_date < CURRENT_DATE). Возвращает список помеченных строк.
        """
        try:
            with self.conn:
                cursor = self._cursor()
                cursor.execute("""
                    UPDATE portfolio_positions pp
                    SET status = 'matured', updated_at = CURRENT_TIMESTAMP
                    FROM bonds_catalog c
                    WHERE c.isin = pp.isin
                      AND c.maturity_date < CURRENT_DATE
                      AND pp.status = 'active'
                    RETURNING pp.isin, pp.broker_name, pp.account_id, pp.quantity
                """)
                marked = [dict(r) for r in cursor.fetchall()]
        except psycopg2.Error as e:
            logger.error(f"Ошибка при пометке погашенных позиций: {e}")
            raise
        if marked:
            logger.info(f"Помечено status='matured': {len(marked)} позиций.")
        return marked

    def mark_closed_positions(
        self,
        keep_keys: Optional[Set[Tuple[str, str, str]]] = None,
        brokers: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Помечает status='closed' у активных позиций:
          - с нулевым/NULL количеством, и/или
          - отсутствующих в keep_keys — наборе актуальных ключей
            (isin, broker_name, account_id), если он передан.

        brokers ограничивает область проверки (частичный синк одного брокера:
        чужие брокерские строки не трогаются до их синка); keep_keys=None
        означает «проверять только нулевое количество» (cleanup-portfolio).
        Возвращает список помеченных строк.
        """
        query = """
            SELECT isin, broker_name, account_id, quantity
            FROM portfolio_positions
            WHERE status = 'active'
        """
        params: List[Any] = []
        if brokers:
            query += " AND broker_name = ANY(%s)"
            params.append(list(brokers))
        cursor = self._cursor()
        cursor.execute(query, tuple(params))
        candidates = cursor.fetchall()

        to_close: List[Dict[str, Any]] = []
        for row in candidates:
            account_id = row['account_id'] or ''
            quantity = row['quantity']
            key = (row['isin'], row['broker_name'], account_id)
            if quantity is None or Decimal(str(quantity)) <= 0:
                to_close.append({**dict(row), 'reason': 'zero_quantity'})
            elif keep_keys is not None and key not in keep_keys:
                to_close.append({**dict(row), 'reason': 'absent_at_broker'})
        if not to_close:
            return []

        try:
            with self.conn:
                upd = self._cursor()
                for row in to_close:
                    upd.execute(
                        """
                        UPDATE portfolio_positions
                        SET status = 'closed', updated_at = CURRENT_TIMESTAMP
                        WHERE isin = %s AND broker_name = %s AND account_id = %s
                          AND status = 'active'
                        """,
                        (row['isin'], row['broker_name'], row['account_id'] or ''),
                    )
        except psycopg2.Error as e:
            logger.error(f"Ошибка при пометке закрытых позиций: {e}")
            raise
        logger.info(f"Помечено status='closed': {len(to_close)} позиций.")
        return to_close

    # Таблицы, которые разрешено очищать командой clear-data и seed-data.
    # Белый список для динамического DELETE: имя таблицы плейсхолдером
    # передать нельзя (§8 п.3 корневого ТЗ).
    CLEARABLE_TABLES = frozenset({
        'portfolio_positions',
        'monitoring_checks',
        'rating_history',
        'risk_history',
        'listlevel_history',
        'calculated_coupons',
        'liquidity_history',
    })

    def clear_tables(self, tables: List[str]):
        """Очищает перечисленные таблицы в одной транзакции.

        Имена таблиц принимаются только из белого списка CLEARABLE_TABLES:
        посторонние имена отклоняются до начала очистки (fail-fast, ничего
        не удаляется). Наличие таблиц гарантируют миграции — отдельно не
        проверяется (§4 корневого ТЗ).
        """
        unknown = [table for table in tables if table not in self.CLEARABLE_TABLES]
        if unknown:
            raise ValueError(
                f"Таблицы {unknown} не входят в белый список очистки. "
                f"Разрешены: {sorted(self.CLEARABLE_TABLES)}."
            )
        with self.conn:
            cursor = self._cursor()
            for table in tables:
                logger.debug(f"Очистка таблицы '{table}'...")
                cursor.execute(f"DELETE FROM {table}")  # имя из белого списка
                logger.info(f"Таблица '{table}' успешно очищена.")

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
        Конфликт по (isin, broker_name, account_id) разрешается полным обновлением записи.
        """
        if not positions:
            return

        # Убираем figi из DTO перед записью в БД, так как его нет в таблице portfolio_positions
        positions_to_save = []
        for p in positions:
            pos_dict = p.to_db_dict()
            pos_dict.pop('figi', None)  # Безопасно удаляем figi
            # NULL в account_id невидим для ON CONFLICT (NULL != NULL) — нормализуем в ''
            pos_dict['account_id'] = pos_dict.get('account_id') or ''
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
                ON CONFLICT (isin, broker_name, account_id) DO UPDATE SET
                    ticker = excluded.ticker,
                    name = excluded.name,
                    quantity = excluded.quantity,
                    average_price = excluded.average_price,
                    current_price = excluded.current_price,
                    currency = excluded.currency,
                    yield_to_maturity = excluded.yield_to_maturity,
                    portfolio_percent = excluded.portfolio_percent,
                    current_value = COALESCE(NULLIF(excluded.current_value, 0),
                                             excluded.quantity * excluded.current_price,
                                             portfolio_positions.current_value, 0),
                    instrument_type = excluded.instrument_type,
                    liquidity_loss_ratio = COALESCE(excluded.liquidity_loss_ratio,
                                                    portfolio_positions.liquidity_loss_ratio),
                    coupon_rate_percent = excluded.coupon_rate_percent,
                    status = excluded.status,
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
                p.liquidity_loss_ratio,
                p.coupon_rate_percent,
                p.account_id,
                p.status,
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
                p.liquidity_loss_ratio,
                p.coupon_rate_percent,
                p.account_id,
                p.status,
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

    # ------------------------------------------------------------------
    # Canonical cash snapshot (миграция 015, задача 022 roadmap ChatGPT v3).
    # Позиции и cash относятся к одному sync workflow: пишет таблицу только
    # sync-portfolio при успешном ответе API; ошибки API прошлый cash не
    # трогают. Валюты не конвертируются — RUB-агрегат фильтруется по валюте.
    # ------------------------------------------------------------------

    # Тела cash-операций без управления транзакцией: транзакцию открывает
    # и завершает публичный метод (029-T03 — атомарная публикация snapshot
    # переиспользует те же SQL-тела в одной транзакции).
    @staticmethod
    def _upsert_cash_balances_rows(cursor, balances: List[Dict[str, Any]]) -> int:
        for b in balances:
            cursor.execute(
                """
                INSERT INTO portfolio_cash_balances
                    (broker_name, account_id, currency, money, blocked, available, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (broker_name, account_id, currency) DO UPDATE SET
                    money = EXCLUDED.money,
                    blocked = EXCLUDED.blocked,
                    available = EXCLUDED.available,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    b['broker_name'], b['account_id'], b['currency'],
                    b['money'], b.get('blocked', Decimal(0)), b['available'],
                ),
            )
        return len(balances)

    @staticmethod
    def _delete_cash_accounts_not_in_rows(
        cursor, broker_name: str, account_ids: Set[str]
    ) -> int:
        if not account_ids:
            return 0
        cursor.execute(
            """
            DELETE FROM portfolio_cash_balances
            WHERE broker_name = %s AND NOT (account_id = ANY(%s))
            """,
            (broker_name, list(account_ids)),
        )
        return cursor.rowcount

    def upsert_cash_balances(self, balances: List[Dict[str, Any]]) -> int:
        """Идемпотентный upsert cash-балансов (portfolio_cash_balances).

        Каждый элемент: broker_name, account_id, currency, money, blocked,
        available. updated_at = момент успешной записи (timestamp последнего
        успешного sync). Возвращает число записанных строк.
        """
        if not balances:
            return 0
        cursor = self._cursor()
        try:
            written = self._upsert_cash_balances_rows(cursor, balances)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return written

    def delete_cash_accounts_not_in(self, broker_name: str, account_ids: Set[str]) -> int:
        """Удаляет cash-строки брокера для счетов вне актуального configured set.

        Вызывается sync-portfolio после успешного sync, чтобы при смене
        TBANK_ACCOUNT_IDS исключённые счета перестали попадать в агрегат
        («агрегация только по configured accounts»). Возвращает число удалённых.
        """
        if not account_ids:
            return 0
        cursor = self._cursor()
        try:
            deleted = self._delete_cash_accounts_not_in_rows(
                cursor, broker_name, account_ids
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return deleted

    def replace_cash_balances_snapshot(
        self,
        broker_name: str,
        balances: List[Dict[str, Any]],
        selected_account_ids: Set[str],
    ) -> int:
        """Атомарная публикация cash snapshot (029-T03): upsert + prune одной транзакцией.

        Upsert актуальных balances и удаление cash-строк брокера для счетов
        вне selected_account_ids выполняются в ОДНОЙ DB transaction (BEGIN ->
        upsert всех balances -> prune rows вне selected_account_ids -> COMMIT).
        При любой ошибке ROLLBACK: БД остаётся на прошлом успешном snapshot,
        cash_updated_at частично не продвигается (промежуточного commit между
        upsert и prune нет). PostgreSQL now() стабилен внутри транзакции —
        после успешной публикации все строки snapshot получают один
        transaction timestamp.

        Пустой balances — no-op (снимок без строк не публикуется, семантика
        upsert_cash_balances), пустой selected_account_ids — prune-часть не
        выполняется (семантика delete_cash_accounts_not_in). Возвращает число
        записанных (upsert) строк.
        """
        if not balances:
            return 0
        cursor = self._cursor()
        try:
            written = self._upsert_cash_balances_rows(cursor, balances)
            self._delete_cash_accounts_not_in_rows(
                cursor, broker_name, selected_account_ids
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return written

    def get_available_cash_rub(self) -> Dict[str, Any]:
        """Canonical reader свободного RUB (задача 022).

        Возвращает:
        - cash_available_rub: Decimal — сумма available по всем строкам RUB
          (только configured accounts: таблицу пишет sync по настроенным
          счетам и удаляет выбывшие); None при unknown;
        - cash_updated_at: datetime метка последнего успешного sync, None
          при unknown;
        - cash_known: False — RUB-строк нет (sync ещё не выполнялся);
          True — снапшот есть, в том числе «реальный 0 RUB» (строка с
          available=0 отличима от unknown).
        """
        cursor = self._cursor()
        cursor.execute(
            """
            SELECT
                COUNT(*) AS rows_cnt,
                COALESCE(SUM(available), 0) AS available_rub,
                MAX(updated_at) AS updated_at
            FROM portfolio_cash_balances
            WHERE lower(currency) = 'rub'
            """
        )
        row = cursor.fetchone()
        if not row or row['rows_cnt'] == 0:
            return {'cash_available_rub': None, 'cash_updated_at': None, 'cash_known': False}
        return {
            'cash_available_rub': row['available_rub'],
            'cash_updated_at': row['updated_at'],
            'cash_known': True,
        }

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

    def add_liquidity_history(self, isin: str, timestamp: datetime, base_volume: Decimal,
                              market_exit_value: Decimal, loss_ratio: Decimal,
                              depth_level: int, data_source: str):
        """Сохраняет результат анализа стакана заявок в историю ликвидности.

        Используется адаптером monitoring.adapters.liquidity_analyzer.
        """
        with self.conn:
            cursor = self._cursor()
            cursor.execute("""
                INSERT INTO liquidity_history
                    (timestamp, isin, base_volume, market_exit_value, loss_ratio, depth_level, data_source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (timestamp, isin, base_volume, market_exit_value, loss_ratio, depth_level, data_source))

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

    def get_bond_for_rating_refresh(self, isin: str) -> Optional[RatingTarget]:
        """Лёгкий таргет для точечного обновления рейтинга по каталогу (002.9, Р2).

        В отличие от get_bond_data_for_monitoring ищет только в bonds_catalog,
        без JOIN с portfolio_positions: рейтинг нужен и бумагам вне портфеля
        (адаптеру credit_rating из bond_data достаточно .isin).
        """
        cursor = self._cursor()
        cursor.execute("SELECT isin FROM bonds_catalog WHERE isin = %s", (isin,))
        row = cursor.fetchone()
        if not row:
            logger.warning(f"Облигация {isin} не найдена в каталоге")
            return None
        return RatingTarget(isin=row['isin'])

    def get_isins_for_rating_refresh(self, scope: str, limit: Optional[int] = None) -> List[RatingTarget]:
        """Выбор таргетов для скоупов обновления рейтингов (002.9, Р3).

        scope='missing' — живые бумаги каталога (maturity_date >= CURRENT_DATE),
        у которых последняя проверка credit_rating в monitoring_checks = 'N/A'
        или отсутствует; scope='catalog' — все живые бумаги каталога.
        Порядок детерминированный (ORDER BY isin), лимит — опциональный.
        """
        if scope not in ('missing', 'catalog'):
            raise ValueError(f"Неизвестный скоуп обновления рейтингов: {scope}")

        query = """
            SELECT bc.isin
            FROM bonds_catalog bc
            LEFT JOIN LATERAL (
                SELECT mc.metric_value
                FROM monitoring_checks mc
                WHERE mc.isin = bc.isin AND mc.metric_name = 'credit_rating'
                ORDER BY mc.check_date DESC
                LIMIT 1
            ) last_check ON TRUE
            WHERE bc.maturity_date >= CURRENT_DATE
        """
        params: Tuple = ()
        if scope == 'missing':
            query += " AND (last_check.metric_value IS NULL OR last_check.metric_value = 'N/A')"
        query += " ORDER BY bc.isin"
        if limit is not None:
            query += " LIMIT %s"
            params = (limit,)

        cursor = self._cursor()
        cursor.execute(query, params)
        targets = [RatingTarget(isin=row['isin']) for row in cursor.fetchall()]
        logger.info(f"get_isins_for_rating_refresh: скоуп '{scope}', найдено {len(targets)} бумаг"
                    + (f" (limit {limit})" if limit is not None else ""))
        return targets

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
        updated_isins: List[str] = []

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
                        updated_isins.append(isin)

                except Exception as e:
                    logger.error(f"Ошибка обновления цены для {isin}: {e}")

        if updated_isins:
            self.update_bonds_derived_metrics(updated_isins)

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

    def get_db_freshness(
        self, stale_threshold_hours: int = 24, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Возвращает метки свежести данных из каталога, позиций и проверок мониторинга.

        Новые поля по сравнению с предыдущей версией:
        - bonds_tradeable_count: торгуемые облигации
        - bonds_tradeable_stale_or_missing: торгуемые облигации без актуальной цены
          (market_price IS NULL ИЛИ market_price_updated_at устарел/NULL)
        - portfolio_zero_value_positions: позиции с current_value IS NULL или <= 0
        - portfolio_zero_rows_suspicious: из них подозрительные — zero-строка на
          НЕпогашенной бумаге (класс бага синка из 001); зомби-строки погашенных
          бумаг сюда не входят (002.1, правило A1)
        - portfolio_zero_value_isins: ISIN-список всех zero-строк через ';'
        - portfolio_total_value: суммарная оценка портфеля
        - last_sync_time: время последней синхронизации (MAX updated_at portfolio_positions)
        - max_market_price_time: время последнего обновления цены

        Выполняется одним параметризованным SELECT-запросом.

        now (030.3): точка отсчёта окна свежести (cutoff = now - threshold).
        None — прежнее поведение (wall-clock); PlanningContext передаёт frozen
        as_of, чтобы границы «24 часа» считались на срезе, а не по настенным
        часам момента вызова.
        """
        cutoff_now = now if now is not None else datetime.now(timezone.utc)
        stale_cutoff = cutoff_now - timedelta(hours=stale_threshold_hours)
        cursor = self._cursor()
        cursor.execute("""
            SELECT
                COUNT(*) AS bonds_catalog_rows,
                MAX(updated_at) AS bonds_catalog_max_updated_at,
                COUNT(*) FILTER (WHERE market_price IS NOT NULL) AS bonds_with_market_price,
                MAX(market_price_updated_at) AS market_price_max_updated_at,
                COUNT(*) FILTER (
                    WHERE market_price IS NOT NULL
                    AND (market_price_updated_at IS NULL OR market_price_updated_at < %s)
                    AND is_trade_available IS TRUE
                ) AS bonds_with_stale_market_price,
                COUNT(*) FILTER (WHERE is_trade_available IS TRUE) AS bonds_tradeable_count,
                COUNT(*) FILTER (
                    WHERE is_trade_available IS TRUE
                    AND (
                        market_price IS NULL
                        OR market_price_updated_at IS NULL
                        OR market_price_updated_at < %s
                    )
                ) AS bonds_tradeable_stale_or_missing,
                MAX(market_price_updated_at) AS max_market_price_time,
                (SELECT COUNT(*) FROM portfolio_positions) AS portfolio_positions_rows,
                (SELECT MAX(updated_at) FROM portfolio_positions) AS portfolio_max_updated_at,
                (SELECT MAX(updated_at) FROM portfolio_positions) AS last_sync_time,
                (SELECT COUNT(*) FROM portfolio_positions
                 WHERE current_value IS NULL OR current_value <= 0) AS portfolio_zero_value_positions,
                -- Подозрительные (002.1, правило A1 гейта): zero-строка на
                -- НЕпогашенной бумаге (брокер занулил живую позицию — класс
                -- бага из 001). ISIN нет в каталоге или дата погашения
                -- неизвестна → считаем подозрительной (COALESCE → CURRENT_DATE).
                (SELECT COUNT(*) FROM portfolio_positions pp
                 WHERE (pp.current_value IS NULL OR pp.current_value <= 0)
                   AND COALESCE((SELECT c.maturity_date FROM bonds_catalog c
                                 WHERE c.isin = pp.isin), CURRENT_DATE) >= CURRENT_DATE
                ) AS portfolio_zero_rows_suspicious,
                (SELECT string_agg(DISTINCT isin, ';' ORDER BY isin)
                   FROM portfolio_positions
                  WHERE current_value IS NULL OR current_value <= 0
                ) AS portfolio_zero_value_isins,
                (SELECT COALESCE(SUM(current_value), 0) FROM portfolio_positions) AS portfolio_total_value,
                (SELECT MAX(check_date) FROM monitoring_checks) AS monitoring_checks_max_check_date
            FROM bonds_catalog
        """, (stale_cutoff, stale_cutoff))
        row = cursor.fetchone()
        if not row:
            return {
                'bonds_catalog_rows': 0,
                'bonds_catalog_max_updated_at': None,
                'bonds_with_market_price': 0,
                'market_price_max_updated_at': None,
                'bonds_with_stale_market_price': 0,
                'bonds_tradeable_count': 0,
                'bonds_tradeable_stale_or_missing': 0,
                'max_market_price_time': None,
                'portfolio_positions_rows': 0,
                'portfolio_max_updated_at': None,
                'last_sync_time': None,
                'portfolio_zero_value_positions': 0,
                'portfolio_zero_rows_suspicious': 0,
                'portfolio_zero_value_isins': None,
                'portfolio_total_value': Decimal(0),
                'monitoring_checks_max_check_date': None,
            }
        return dict(row)


    def get_bonds_for_market_price_update(self, isin: Optional[str] = None) -> List[Dict[str, Any]]:
        """Выборка облигаций для команды update-market-prices.

        С заданным isin — только эта облигация; без него — все торговые
        рублёвые облигации с незакрытой датой погашения (не вечные).
        """
        cursor = self._cursor()
        if isin:
            query = """
                SELECT isin, ticker, name, figi, nominal
                FROM bonds_catalog
                WHERE isin = %s
                ORDER BY isin
            """
            cursor.execute(query, (isin,))
        else:
            query = """
                SELECT isin, ticker, name, figi, nominal
                FROM bonds_catalog
                WHERE currency = 'rub'
                    AND (is_trade_available IS TRUE OR is_trade_available IS NULL)
                    AND perpetual_flag IS NOT TRUE
                    AND maturity_date >= CURRENT_DATE
                ORDER BY isin
            """
            cursor.execute(query)
        return [dict(row) for row in cursor.fetchall()]

    def get_bond_figi(self, isin: str) -> Optional[str]:
        """Возвращает FIGI облигации из каталога или None, если он не задан."""
        cursor = self._cursor()
        cursor.execute("SELECT figi FROM bonds_catalog WHERE isin = %s", (isin,))
        row = cursor.fetchone()
        return row['figi'] if row else None

    def save_market_price(self, isin: str, price: Decimal, source: str = 'tbank'):
        """Сохраняет рыночную цену одной облигации, источник и метку времени,

        и пересчитывает производные метрики (дюрацию и YTM).
        """
        with self.conn:
            cursor = self._cursor()
            cursor.execute("""
                UPDATE bonds_catalog
                SET market_price = %s,
                    market_price_source = %s,
                    market_price_updated_at = CURRENT_TIMESTAMP
                WHERE isin = %s
            """, (price, source, isin))
        self.update_bonds_derived_metrics([isin])

    def update_bonds_derived_metrics(
        self,
        isins: Optional[List[str]] = None,
        valuation_date: Optional[date] = None
    ) -> int:
        """Пересчитывает дюрацию Маколея, модифицированную дюрацию и YTM для списка ISIN

        или для всех бумаг в каталоге.
        Обновляет duration_macaulay, duration_modified, duration_null_reason, duration_updated_at,
        а также ytm, ytm_null_reason и ytm_updated_at (включая осознанный NULL).
        """
        cursor = self._cursor()
        if isins is not None:
            if not isins:
                return 0
            unique_isins = list(set(isins))
            cursor.execute("""
                SELECT isin, nominal, maturity_date, coupon_quantity_per_year,
                       coupon_rate_percent, market_price, perpetual_flag,
                       floating_coupon_flag, amortization_flag
                FROM bonds_catalog
                WHERE isin = ANY(%s)
            """, (unique_isins,))
        else:
            cursor.execute("""
                SELECT isin, nominal, maturity_date, coupon_quantity_per_year,
                       coupon_rate_percent, market_price, perpetual_flag,
                       floating_coupon_flag, amortization_flag
                FROM bonds_catalog
            """)

        rows = cursor.fetchall()
        if not rows:
            return 0

        update_data = []
        for r in rows:
            metrics = calculate_bond_cashflow_metrics(
                valuation_date=valuation_date,
                nominal=r['nominal'],
                maturity_date=r['maturity_date'],
                coupon_quantity_per_year=r['coupon_quantity_per_year'],
                coupon_rate_percent=r['coupon_rate_percent'],
                market_price=r['market_price'],
                perpetual_flag=r['perpetual_flag'],
                floating_coupon_flag=r['floating_coupon_flag'],
                amortization_flag=r['amortization_flag'],
            )
            update_data.append((
                metrics.duration_macaulay,
                metrics.duration_modified,
                metrics.null_reason,
                # 005.4 (миграция 013): bonds_catalog.ytm хранится в ПРОЦЕНТАХ
                # годовых (24.01 = 24.01%). Движок возвращает долю (0.2401) —
                # на границе записи умножаем на 100. Этот метод — единственный
                # писатель колонки; читатели берут значение как есть.
                None if metrics.ytm is None else metrics.ytm * 100,
                metrics.null_reason if metrics.ytm is None else None,
                r['isin'],
            ))

        with self.conn:
            cursor.executemany("""
                UPDATE bonds_catalog
                SET duration_macaulay = %s,
                    duration_modified = %s,
                    duration_null_reason = %s,
                    duration_updated_at = CURRENT_TIMESTAMP,
                    ytm = %s,
                    ytm_null_reason = %s,
                    ytm_updated_at = CURRENT_TIMESTAMP
                WHERE isin = %s
            """, update_data)

        return len(update_data)

    def update_bonds_duration(
        self,
        isins: Optional[List[str]] = None,
        valuation_date: Optional[date] = None
    ) -> int:
        """Обратная совместимость: делегирует в update_bonds_derived_metrics."""
        return self.update_bonds_derived_metrics(isins=isins, valuation_date=valuation_date)

    def get_portfolio_durations(self) -> List[Dict[str, Any]]:
        """Возвращает позиции портфеля с дюрацией и параметрами облигаций."""
        cursor = self._cursor()
        cursor.execute("""
            SELECT
                p.isin,
                p.ticker,
                p.name,
                p.quantity,
                p.current_value,
                p.current_price,
                c.nominal,
                c.market_price,
                c.coupon_rate_percent,
                c.coupon_quantity_per_year,
                c.maturity_date,
                c.duration_macaulay,
                c.duration_modified,
                c.duration_null_reason,
                c.duration_updated_at
            FROM portfolio_positions p
            LEFT JOIN bonds_catalog c ON c.isin = p.isin
            ORDER BY p.current_value DESC NULLS LAST
        """)
        return [dict(r) for r in cursor.fetchall()]

    def get_portfolio_markdown_rows(self) -> List[Dict[str, Any]]:
        """Возвращает строки портфеля для Markdown-экспорта (с честной YTM из bonds_catalog).

        Каждая строка portfolio_positions (включая разные брокерские счета)
        возвращается отдельно; стоимость = quantity * current_price, сортировка DESC.
        """
        cursor = self._cursor()
        cursor.execute("""
            SELECT
                p.isin,
                p.name,
                p.quantity,
                p.current_price,
                (COALESCE(p.quantity, 0) * COALESCE(p.current_price, 0)) AS total_value,
                b.ytm
            FROM portfolio_positions p
            LEFT JOIN bonds_catalog b ON b.isin = p.isin
            ORDER BY total_value DESC NULLS LAST, p.isin, p.account_id
        """)
        return [dict(r) for r in cursor.fetchall()]

    def get_portfolio_markdown_freshness(self) -> Tuple[Optional[datetime], Optional[datetime], Optional[datetime]]:
        """Возвращает штампы свежести данных, ограниченные только текущими позициями портфеля:
        (max_positions_updated_at, max_market_price_updated_at, max_ytm_updated_at).
        """
        cursor = self._cursor()
        cursor.execute("""
            SELECT
                MAX(p.updated_at) AS max_positions_updated_at,
                MAX(b.market_price_updated_at) AS max_market_price_updated_at,
                MAX(b.ytm_updated_at) AS max_ytm_updated_at
            FROM portfolio_positions p
            LEFT JOIN bonds_catalog b ON b.isin = p.isin
        """)
        row = cursor.fetchone()
        if not row:
            return None, None, None
        return (
            row.get('max_positions_updated_at'),
            row.get('max_market_price_updated_at'),
            row.get('max_ytm_updated_at'),
        )

    def save_bond_figi(self, isin: str, figi: str):
        """Сохраняет FIGI облигации в каталоге."""
        with self.conn:
            cursor = self._cursor()
            cursor.execute("UPDATE bonds_catalog SET figi = %s WHERE isin = %s", (figi, isin))

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
        Ставка нормализуется к 4 знакам после запятой (002.5, P9.4):
        канал зовут update-floaters (RUONIA + spread).
        """
        query = "UPDATE bonds_catalog SET coupon_rate_percent = %s, updated_at = CURRENT_TIMESTAMP WHERE isin = %s"
        cursor = self._cursor()
        try:
            logger.debug(f"DB_WRITE: Preparing to update ISIN {isin} with new rate {new_rate}.")
            with self.conn:
                cursor.execute(query, (round_coupon_rate(new_rate), isin))
            rowcount = cursor.rowcount
            logger.debug(f"DB_WRITE: Executed UPDATE for ISIN {isin}. Rowcount: {rowcount}.")

            if rowcount == 0:
                logger.warning(f"Не удалось обновить ставку для {isin}: облигация не найдена (rowcount=0).")
            else:
                logger.info(f"DB_WRITE: Successfully updated ISIN {isin}.")

        except psycopg2.Error as e:
            logger.error(f"DB_WRITE: Ошибка БД при обновлении ставки для {isin}: {e}")

    # --- Аналитические выборки для отчётов (перенос из use-case'ов, _00_04) ---
    # SQL живёт только здесь; use-case'ы получают словари и занимаются
    # форматированием и бизнес-расчётами (YTM, формат таблиц).

    def get_top_buy_candidates(self, max_risk: int = 1, min_days_to_maturity: int = 30,
                               only_fixed_coupon: bool = False, only_floating_coupon: bool = False,
                               only_amortization: bool = False, exclude_amortization: bool = False,
                               limit: int = 20,
                               coupon_freq: Optional[int] = None,
                               entity_type_filter: Optional[str] = None,
                               include_held: bool = False) -> List[Dict[str, Any]]:
        """Кандидаты на покупку, топ по текущей доходности
        (режим top команды analyze-buy-candidates).

        min_days_to_maturity — минимальный срок до погашения в днях; месяцы
        до погашения считаются делением на 30.0 (не на 30 — иначе целочисленное
        деление PG меняет расчёт, §4 корневого ТЗ).

        include_held=False — позиции портфеля исключены (прежнее поведение);
        include_held=True — докупаемые позиции остаются в выдаче с бейджем
        held (доля N%) (задача 002.2).
        """
        cursor = self._cursor()
        held_join, not_held_condition, held_select = _portfolio_exclusion_fragments(include_held)

        conditions = [
            "bc.currency = 'rub'",
            "bc.is_trade_available IS TRUE",
            "bc.perpetual_flag IS NOT TRUE",
        ]
        if not_held_condition is not None:
            # Нет в портфеле; при include_held=True условие снимается (002.2)
            conditions.append(not_held_condition)
        conditions += [
            "bc.company_id IS NOT NULL",
            "bc.is_for_qualified_investors IS NOT TRUE",
            "bc.market_price IS NOT NULL",
            "bc.market_price > 0",
            "bc.market_price_updated_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'",
            "bc.issue_size * bc.nominal >= 500000000",
            "COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) IS NOT NULL",
            "bc.risk_level <= %s",
            "(bc.maturity_date - CURRENT_DATE) > %s",
        ]
        params: List[Any] = [max_risk, min_days_to_maturity]

        if only_fixed_coupon:
            conditions.append("bc.floating_coupon_flag IS NOT TRUE")
        elif only_floating_coupon:
            conditions.append("bc.floating_coupon_flag IS TRUE")

        if only_amortization:
            conditions.append("bc.amortization_flag IS TRUE")
        elif exclude_amortization:
            conditions.append("bc.amortization_flag IS NOT TRUE")

        if coupon_freq is not None and coupon_freq > 0:
            conditions.append("bc.coupon_quantity_per_year = %s")
            params.append(coupon_freq)

        if entity_type_filter in ('only_ofz', 'ofz_only', 'sovereign'):
            conditions.append("co.entity_type = 'sovereign'")
        elif entity_type_filter in ('exclude_ofz', 'no_ofz', 'exclude_sovereign'):
            conditions.append("co.entity_type IS NOT NULL AND co.entity_type != 'sovereign'")

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT
                bc.isin,
                bc.ticker,
                bc.name,
                bc.company_id,
                co.name as company,
                co.entity_type,
                CASE
                    WHEN bc.floating_coupon_flag IS TRUE THEN 'FLOAT'
                    ELSE 'FIX'
                END as coupon_type,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) as coupon_rate_percent,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) as coupon_rate,
                bc.coupon_quantity_per_year as coupon_freq,
                bc.coupon_quantity_per_year,
                bc.risk_level,
                bc.list_level,
                bc.maturity_date,
                bc.offer_date,

                -- Выплата по купону на 1 облигацию
                ROUND(
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / NULLIF(bc.coupon_quantity_per_year, 0),
                    2
                ) as coupon_payment_per_bond,

                -- Средний месячный купонный доход на 1 облигацию
                ROUND(
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / 12.0,
                    2
                ) as average_monthly_coupon_per_bond,

                ROUND(
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / 12.0,
                    2
                ) as monthly_coupon_income_per_bond,

                -- Рыночная цена
                bc.market_price,
                bc.market_price_updated_at,

                -- Текущая купонная доходность
                ROUND(
                    ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.market_price) * 100,
                    2
                ) as current_coupon_yield,

                ROUND(
                    ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.market_price) * 100,
                    2
                ) as annual_yield_percent,

                -- До погашения (месяцев)
                ROUND(
                    (bc.maturity_date - CURRENT_DATE) / 30.0,
                    1
                ) as months_to_maturity,

                -- Тип облигации
                CASE
                    WHEN bc.floating_coupon_flag IS TRUE THEN 'Флоатер'
                    WHEN bc.amortization_flag IS TRUE THEN 'Амортизируемая'
                    ELSE 'Обычная'
                END as bond_type,

                rh.rating_code as rating,
                rh.rating_date
                {held_select}

            FROM bonds_catalog bc
            {held_join}
            JOIN companies co ON co.id = bc.company_id
            LEFT JOIN (
                SELECT DISTINCT ON (isin) isin, rating_code, rating_date
                FROM rating_history
                ORDER BY isin, rating_date DESC, id DESC
            ) rh ON rh.isin = bc.isin
            {_LATEST_FLOATER_RATE_JOIN}
            WHERE {where_clause}
            ORDER BY current_coupon_yield DESC NULLS LAST, bc.isin
            LIMIT %s
        """
        params.append(limit)
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_floater_buy_candidates(self, limit: int = 15, max_risk: int = 1,
                                   coupon_freq: Optional[int] = None,
                                   entity_type_filter: Optional[str] = None,
                                   include_held: bool = False) -> List[Dict[str, Any]]:
        """Флоатеры с рассчитанной ставкой и установленным спредом
        (режим floaters команды analyze-buy-candidates).

        include_held=False — позиции портфеля исключены (прежнее поведение);
        include_held=True — докупаемые позиции остаются в выдаче с бейджем
        held (доля N%) (задача 002.2).
        """
        cursor = self._cursor()
        held_join, not_held_condition, held_select = _portfolio_exclusion_fragments(include_held)

        conditions = [
            "bc.currency = 'rub'",
            "bc.is_trade_available IS TRUE",
            "bc.perpetual_flag IS NOT TRUE",
        ]
        if not_held_condition is not None:
            # Нет в портфеле; при include_held=True условие снимается (002.2)
            conditions.append(not_held_condition)
        conditions += [
            "bc.company_id IS NOT NULL",
            "bc.is_for_qualified_investors IS NOT TRUE",
            "bc.market_price IS NOT NULL",
            "bc.market_price > 0",
            "bc.market_price_updated_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'",
            "bc.issue_size * bc.nominal >= 500000000",
            "bc.floating_coupon_flag IS TRUE",
            "mc_floater.metric_value::numeric IS NOT NULL",
            "bc.coupon_spread IS NOT NULL",
            "bc.risk_level <= %s",
            "(bc.maturity_date IS NULL OR bc.maturity_date > CURRENT_DATE)",
        ]
        params: List[Any] = [max_risk]

        if coupon_freq is not None and coupon_freq > 0:
            conditions.append("bc.coupon_quantity_per_year = %s")
            params.append(coupon_freq)

        if entity_type_filter in ('only_ofz', 'ofz_only', 'sovereign'):
            conditions.append("co.entity_type = 'sovereign'")
        elif entity_type_filter in ('exclude_ofz', 'no_ofz', 'exclude_sovereign'):
            conditions.append("co.entity_type IS NOT NULL AND co.entity_type != 'sovereign'")

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT
                bc.isin,
                bc.ticker,
                bc.name,
                bc.company_id,
                co.name as company,
                co.entity_type,
                'FLOAT' as coupon_type,
                mc_floater.metric_value::numeric as calculated_coupon_rate,
                mc_floater.metric_value::numeric as coupon_rate,
                bc.coupon_spread,
                bc.risk_level,
                bc.list_level,
                bc.coupon_quantity_per_year as coupon_freq,
                bc.coupon_quantity_per_year,
                bc.maturity_date,
                bc.offer_date,

                -- Купонный платеж на 1 бумагу
                ROUND(
                    (mc_floater.metric_value::numeric * bc.nominal / 100) / NULLIF(bc.coupon_quantity_per_year, 0),
                    2
                ) as coupon_payment_per_bond,

                -- Средний месячный купонный доход на 1 облигацию
                ROUND(
                    (mc_floater.metric_value::numeric * bc.nominal / 100) / 12.0,
                    2
                ) as average_monthly_coupon_per_bond,

                ROUND(
                    (mc_floater.metric_value::numeric * bc.nominal / 100) / 12.0,
                    2
                ) as monthly_coupon_income_per_bond,

                -- Рыночная цена
                bc.market_price,
                bc.market_price_updated_at,

                -- Текущая купонная доходность
                ROUND(
                    ((mc_floater.metric_value::numeric * bc.nominal / 100) / bc.market_price) * 100,
                    2
                ) as current_coupon_yield,

                -- До погашения (месяцев)
                ROUND(
                    (bc.maturity_date - CURRENT_DATE) / 30.0,
                    1
                ) as months_to_maturity,

                rh.rating_code as rating,
                rh.rating_date
                {held_select}

            FROM bonds_catalog bc
            {held_join}
            JOIN companies co ON co.id = bc.company_id
            LEFT JOIN (
                SELECT DISTINCT ON (isin) isin, rating_code, rating_date
                FROM rating_history
                ORDER BY isin, rating_date DESC, id DESC
            ) rh ON rh.isin = bc.isin
            {_LATEST_FLOATER_RATE_JOIN}
            WHERE {where_clause}
            ORDER BY calculated_coupon_rate DESC NULLS LAST, bc.coupon_spread DESC NULLS LAST, bc.isin
            LIMIT %s
        """
        params.append(limit)
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_portfolio_comparison_candidates(self, max_risk: int = 1, limit: int = 25,
                                            include_held: bool = False) -> List[Dict[str, Any]]:
        """Бумаги с потенциальной доходностью против средней доходности
        портфеля (режим compare команды analyze-buy-candidates).

        include_held=False — позиции портфеля исключены (прежнее поведение);
        include_held=True — докупаемые позиции остаются в выдаче с бейджем
        held (доля N%) (задача 002.2).
        """
        cursor = self._cursor()
        held_join, not_held_condition, held_select = _portfolio_exclusion_fragments(include_held)
        if not_held_condition is not None:
            not_held_sql = f"AND {not_held_condition}  -- Нет в портфеле"
        else:
            not_held_sql = "-- Исключение портфеля снято (include_held, 002.2)"
        query = f"""
            WITH portfolio_avg_yield AS (
                SELECT
                    AVG((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / pp.average_price * 100) as avg_portfolio_yield
                FROM bonds_catalog bc
                JOIN portfolio_positions pp ON bc.isin = pp.isin
                {_LATEST_FLOATER_RATE_JOIN}
                WHERE bc.currency = 'rub'
                    AND pp.current_price > 0
                    AND pp.quantity > 0
            )
            SELECT
                bc.isin,
                bc.ticker,
                bc.name,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) as coupon_rate_percent,
                bc.risk_level,

                -- Потенциальная годовая доходность (если купить по рыночной цене)
                ROUND(
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.market_price * 100,
                    2
                ) as potential_yield_percent,

                -- Сравнение с портфелем
                ROUND(
                    ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.market_price * 100) - pavg.avg_portfolio_yield,
                    2
                ) as yield_vs_portfolio,

                -- Рекомендация
                CASE
                    WHEN ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.market_price * 100) > pavg.avg_portfolio_yield + 2 THEN 'ПОКУПАТЬ'
                    WHEN ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.market_price * 100) > pavg.avg_portfolio_yield THEN 'РАССМОТРЕТЬ'
                    ELSE 'НЕ РЕКОМЕНДУЕТСЯ'
                END as recommendation
                {held_select}

            FROM bonds_catalog bc
            {held_join}
            {_LATEST_FLOATER_RATE_JOIN}
            CROSS JOIN portfolio_avg_yield pavg
            WHERE bc.currency = 'rub'
                AND (bc.is_trade_available IS TRUE OR bc.is_trade_available IS NULL)
                AND bc.perpetual_flag IS NOT TRUE
                {not_held_sql}
                AND COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) IS NOT NULL
                AND bc.market_price IS NOT NULL  -- Есть рыночная цена
                AND bc.risk_level <= %s  -- Только низкорисковые
            ORDER BY yield_vs_portfolio DESC
            LIMIT %s
        """
        cursor.execute(query, (max_risk, limit))
        return [dict(row) for row in cursor.fetchall()]

    def get_monthly_profit_candidates(self, min_profit: float, limit: int) -> List[Dict[str, Any]]:
        """Позиции портфеля с ежемесячной прибылью не ниже min_profit
        (режим detailed команды calculate-monthly-profit)."""
        cursor = self._cursor()
        query = f"""
            SELECT
                bc.isin,
                bc.ticker,
                bc.name,
                bc.nominal,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) as coupon_rate_percent,
                bc.coupon_quantity_per_year,
                bc.risk_level,
                pp.average_price,
                pp.current_price,
                pp.quantity,
                pp.broker_name,

                -- Купонный доход за месяц
                ROUND(
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity,
                    2
                ) as monthly_coupon_income,

                -- Прибыль от изменения цены
                ROUND(
                    (pp.current_price - pp.average_price) * pp.quantity,
                    2
                ) as price_change_profit,

                -- Общая ежемесячная прибыль
                ROUND(
                    ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                    ((pp.current_price - pp.average_price) * pp.quantity),
                    2
                ) as total_monthly_profit,

                -- Процентная доходность (годовая)
                ROUND(
                    ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / pp.average_price) * 100,
                    2
                ) as annual_yield_percent

            FROM bonds_catalog bc
            JOIN portfolio_positions pp ON bc.isin = pp.isin
            {_LATEST_FLOATER_RATE_JOIN}
            WHERE bc.currency = 'rub'
                AND pp.current_price > 0
                AND pp.quantity > 0
                AND ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                    ((pp.current_price - pp.average_price) * pp.quantity) >= %s
            ORDER BY total_monthly_profit DESC
            LIMIT %s
        """
        cursor.execute(query, (min_profit, limit))
        return [dict(row) for row in cursor.fetchall()]

    def get_portfolio_profit_summary(self) -> Dict[str, Any]:
        """Сводная ежемесячная прибыль портфеля (режим summary команды
        calculate-monthly-profit)."""
        cursor = self._cursor()
        query = f"""
            SELECT
                COUNT(DISTINCT bc.isin) as bonds_count,
                SUM(pp.quantity) as total_quantity,

                -- Общий купонный доход за месяц
                ROUND(
                    SUM((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity),
                    2
                ) as total_monthly_coupon_income,

                -- Общая прибыль от изменения цены
                ROUND(
                    SUM((pp.current_price - pp.average_price) * pp.quantity),
                    2
                ) as total_price_change_profit,

                -- Общая ежемесячная прибыль
                ROUND(
                    SUM(((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                        ((pp.current_price - pp.average_price) * pp.quantity)),
                    2
                ) as total_monthly_profit,

                -- Средняя годовая доходность портфеля
                ROUND(
                    AVG((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / pp.average_price * 100),
                    2
                ) as avg_annual_yield_percent

            FROM bonds_catalog bc
            JOIN portfolio_positions pp ON bc.isin = pp.isin
            {_LATEST_FLOATER_RATE_JOIN}
            WHERE bc.currency = 'rub'
                AND pp.current_price > 0
                AND pp.quantity > 0
        """
        cursor.execute(query)
        return dict(cursor.fetchone())

    def get_monthly_profit_by_broker(self) -> List[Dict[str, Any]]:
        """Ежемесячная прибыль в разрезе брокеров (режим by-broker команды
        calculate-monthly-profit)."""
        cursor = self._cursor()
        query = f"""
            SELECT
                pp.broker_name,
                COUNT(DISTINCT bc.isin) as bonds_count,

                -- Купонный доход за месяц
                ROUND(
                    SUM((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity),
                    2
                ) as monthly_coupon_income,

                -- Прибыль от изменения цены
                ROUND(
                    SUM((pp.current_price - pp.average_price) * pp.quantity),
                    2
                ) as price_change_profit,

                -- Общая ежемесячная прибыль
                ROUND(
                    SUM(((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                        ((pp.current_price - pp.average_price) * pp.quantity)),
                    2
                ) as total_monthly_profit

            FROM bonds_catalog bc
            JOIN portfolio_positions pp ON bc.isin = pp.isin
            {_LATEST_FLOATER_RATE_JOIN}
            WHERE bc.currency = 'rub'
                AND pp.current_price > 0
                AND pp.quantity > 0
            GROUP BY pp.broker_name
            ORDER BY total_monthly_profit DESC
        """
        cursor.execute(query)
        return [dict(row) for row in cursor.fetchall()]

    def get_monthly_profit_by_risk(self) -> List[Dict[str, Any]]:
        """Ежемесячная прибыль в разрезе уровней риска (режим by-risk команды
        calculate-monthly-profit)."""
        cursor = self._cursor()
        query = f"""
            SELECT
                bc.risk_level,
                COUNT(DISTINCT bc.isin) as bonds_count,

                -- Купонный доход за месяц
                ROUND(
                    SUM((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity),
                    2
                ) as monthly_coupon_income,

                -- Прибыль от изменения цены
                ROUND(
                    SUM((pp.current_price - pp.average_price) * pp.quantity),
                    2
                ) as price_change_profit,

                -- Общая ежемесячная прибыль
                ROUND(
                    SUM(((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                        ((pp.current_price - pp.average_price) * pp.quantity)),
                    2
                ) as total_monthly_profit,

                -- Средняя годовая доходность
                ROUND(
                    AVG((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / pp.average_price * 100),
                    2
                ) as avg_annual_yield_percent

            FROM bonds_catalog bc
            JOIN portfolio_positions pp ON bc.isin = pp.isin
            {_LATEST_FLOATER_RATE_JOIN}
            WHERE bc.currency = 'rub'
                AND pp.current_price > 0
                AND pp.quantity > 0
            GROUP BY bc.risk_level
            ORDER BY bc.risk_level
        """
        cursor.execute(query)
        return [dict(row) for row in cursor.fetchall()]

    def get_top_profitable_bonds(self, limit: int) -> List[Dict[str, Any]]:
        """Топ позиций портфеля по ежемесячной прибыли (режим top-10 команды
        calculate-monthly-profit)."""
        cursor = self._cursor()
        query = f"""
            SELECT
                bc.isin,
                bc.ticker,
                bc.name,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) as coupon_rate_percent,
                bc.risk_level,
                pp.quantity,
                pp.broker_name,

                -- Ежемесячная прибыль
                ROUND(
                    ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                    ((pp.current_price - pp.average_price) * pp.quantity),
                    2
                ) as monthly_profit,

                -- Купонная часть
                ROUND(
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity,
                    2
                ) as coupon_part,

                -- Прибыль от цены
                ROUND(
                    (pp.current_price - pp.average_price) * pp.quantity,
                    2
                ) as price_part

            FROM bonds_catalog bc
            JOIN portfolio_positions pp ON bc.isin = pp.isin
            {_LATEST_FLOATER_RATE_JOIN}
            WHERE bc.currency = 'rub'
                AND pp.current_price > 0
                AND pp.quantity > 0
            ORDER BY monthly_profit DESC
            LIMIT %s
        """
        cursor.execute(query, (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def get_bonds_yield_table(self, mode: str, min_maturity: str, max_maturity: str,
                              max_risk: int, no_amortization: bool = False,
                              monthly_coupons: bool = False, fixed_coupon: bool = False,
                              floating_coupon: bool = False, limit: int = 50,
                              max_listlevel: int = 2, max_ytm: float = 35.0,
                              include_held: bool = False) -> List[Dict[str, Any]]:
        """Строки таблицы доходностей для команды calculate-ytm.

        mode='buy' — кандидаты на покупку (цена рыночная),
        mode='portfolio' — позиции портфеля (цена средняя покупки). Сам расчёт
        YTM остаётся в use-case'е. Динамические фильтры — статические строки;
        даты, уровень риска и лимит передаются параметрами.

        В buy-режиме дополнительно отсекаются бумаги третьего эшелона
        (list_level > max_listlevel, NULL проходит) и аномальные доходности
        (ytm > max_ytm, % годовых — маркер ВДО/дистресса; NULL проходит).

        018: buy-кандидат обязан иметь реальную свежую market_price
        (IS NOT NULL, > 0, market_price_updated_at не старше 24 часов) —
        nominal цену не подменяет, current_price и real_yield_percent
        считаются от market_price. mode='portfolio' не изменился.
        market_price_updated_at — время записи цены в БД (ограничение из 009).

        include_held (только mode='buy'): False — позиции портфеля исключены
        (прежнее поведение); True — докупаемые позиции остаются в выдаче
        с бейджем held (доля N%) (задача 002.2).

        Окно погашения (002.3/P9.5): min_maturity/max_maturity — явные даты
        'YYYY-MM-DD' или None. max_maturity=None — верхней границы нет
        (широкое окно по умолчанию; прежний дефолт '2030-12-31' отсекал
        топ-фиксы 2030+). min_maturity=None — нижняя граница = CURRENT_DATE
        (уже погашенные бумаги не показываются; прежний дефолт '2025-01-01'
        был в прошлом).

        023: buy-выборка несёт последний кредитный рейтинг кандидата
        (credit_rating + rating_score из rating_history, join _LATEST_RATING_JOIN)
        — строгий фильтр --min-credit-rating в rebalance-report идёт по
        настоящему рейтингу, risk_level не подменяет его. mode='portfolio'
        не изменился.

        030.6: structural-условия WHERE buy-ветки (currency, is_trade_available,
        perpetual, широкие пределы пула, coupon, market_price, окно свежести)
        НЕ дублируются здесь — они собираются из единственной таблицы
        structural buy gates (src/use_cases/bond_filters.py), той же, по которой
        eligibility-гейт планирования оценивает canonical raw-строку; правило
        «extract, не копия»: гейт меняется в одном месте.
        """
        if mode not in ('buy', 'portfolio'):
            raise ValueError(f"Неизвестный режим таблицы доходностей: {mode!r}")

        # 002.3/P9.5: окно погашения собирается динамически — None не
        # подставляется параметром (сравнение с NULL отсекло бы все строки):
        # без нижней границы берём только непогашенные, верхняя отсутствует.
        # 030.6: floor без явного min_maturity — canonical фрагмент таблицы
        # structural buy gates (единственный источник с Python-оценкой).
        maturity_filters = []
        if min_maturity:
            maturity_filters.append(("bc.maturity_date >= %s", min_maturity))
        else:
            maturity_filters.append((BUY_MATURITY_FLOOR_SQL, None))
        if max_maturity:
            maturity_filters.append(("bc.maturity_date <= %s", max_maturity))
        maturity_filter_sql = "".join(
            f"\n                AND {clause}" for clause, _ in maturity_filters
        )
        maturity_params = [value for _, value in maturity_filters if value is not None]

        # 002.2: жёсткое исключение портфеля только при include_held=False
        # (запрос идентичен прежнему); при True позиции портфеля показываются
        # с бейджем held (N%) — агрегат по ISIN через view оценки (см. A3).
        if mode == 'buy':
            held_join = _HELD_SHARE_JOIN if include_held else ""
            held_select = _HELD_BADGE_SELECT if include_held else ""
            not_held_filter = "" if include_held else """
                AND bc.isin NOT IN (SELECT isin FROM portfolio_positions)"""

        if mode == 'buy':
            body = f"""
            SELECT
                bc.isin,
                bc.ticker,
                bc.name,
                bc.nominal,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) as coupon_rate_percent,
                bc.coupon_quantity_per_year,
                bc.maturity_date,
                bc.risk_level,
                bc.list_level,
                bc.amortization_flag,
                -- 018: цена кандидата — только реальная свежая market_price
                -- (guard в WHERE ниже), nominal подменой не служит.
                bc.market_price as current_price,
                (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.market_price * 100 as real_yield_percent,
                bc.market_price,
                bc.ytm,
                bc.ytm_null_reason,
                bc.ytm_updated_at,
                bc.floating_coupon_flag,
                bc.perpetual_flag,
                -- Общая сумма купонных выплат в месяц по всем бумагам в строке
                ROUND(
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * 1,
                    2
                ) as total_monthly_coupon_payment,
                -- Тип купона (фиксированный/плавающий)
                CASE
                    WHEN bc.floating_coupon_flag IS TRUE THEN 'Плавающий'
                    ELSE 'Фиксированный'
                END as coupon_type,
                -- 005.1 (rebalance-report): эмитент/флаг КУ в каждой строке —
                -- человекочитаемые поля и фильтры тулкита; добавка колонок
                -- существующий вывод calculate-ytm не меняет.
                bc.is_for_qualified_investors as ku,
                co.name as issuer,
                co.entity_type,
                -- 023: настоящий кредитный рейтинг кандидата (latest
                -- rating_history по ISIN) — canonical полю screener-строки
                -- credit_rating и строгому фильтру --min-credit-rating;
                -- risk_level остаётся отдельным фильтром.
                lr.credit_rating,
                lr.rating_score{held_select}
            FROM bonds_catalog bc
            LEFT JOIN companies co ON co.id = bc.company_id
            {_LATEST_FLOATER_RATE_JOIN}
            {_LATEST_RATING_JOIN}
            {held_join}
            WHERE {STRUCTURAL_BUY_WHERE_SQL}{maturity_filter_sql}{not_held_filter}
                AND (bc.ytm IS NULL OR bc.ytm <= %s)
        """
        else:
            body = f"""
            SELECT
                pp.isin,
                bc.ticker,
                bc.name,
                bc.nominal,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) as coupon_rate_percent,
                bc.coupon_quantity_per_year,
                bc.maturity_date,
                bc.risk_level,
                bc.list_level,
                bc.amortization_flag,
                pp.average_price,
                pp.current_price,
                pp.quantity,
                bc.market_price,
                bc.ytm,
                bc.ytm_null_reason,
                bc.ytm_updated_at,
                bc.floating_coupon_flag,
                bc.perpetual_flag,
                CASE
                    WHEN pp.average_price IS NOT NULL AND pp.average_price > 0
                    THEN (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / pp.average_price * 100
                    ELSE NULL
                END as real_yield_percent,
                CASE
                    WHEN pp.current_price IS NOT NULL AND pp.average_price IS NOT NULL
                    AND pp.average_price > 0
                    THEN ((pp.current_price - pp.average_price) / pp.average_price) * 100
                    ELSE NULL
                END as price_change_percent,
                -- Общая сумма купонных выплат в месяц по всем бумагам в строке
                ROUND(
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity,
                    2
                ) as total_monthly_coupon_payment,
                -- Тип купона (фиксированный/плавающий)
                CASE
                    WHEN bc.floating_coupon_flag IS TRUE THEN 'Плавающий'
                    ELSE 'Фиксированный'
                END as coupon_type,
                -- 005.3 (calculate-ytm): эмитент/флаг КУ/тип сущности в каждой
                -- строке — фильтр --exclude-sovereign и JSON-выдача; добавка
                -- колонок существующий вывод команды не меняет.
                bc.is_for_qualified_investors as ku,
                co.name as issuer,
                co.entity_type
            FROM portfolio_positions pp
            JOIN bonds_catalog bc ON pp.isin = bc.isin
            LEFT JOIN companies co ON co.id = bc.company_id
            {_LATEST_FLOATER_RATE_JOIN}
            WHERE bc.currency = 'rub'
                AND pp.current_price > 0
                AND pp.quantity > 0{maturity_filter_sql}
                AND bc.risk_level <= %s
                AND COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) IS NOT NULL
        """

        filters = ""
        if no_amortization:
            filters += " AND bc.amortization_flag IS NOT TRUE"
        if monthly_coupons:
            filters += " AND bc.coupon_quantity_per_year = 12"
        if fixed_coupon:
            filters += " AND bc.floating_coupon_flag IS NOT TRUE"
        if floating_coupon:
            filters += " AND bc.floating_coupon_flag IS TRUE"

        query = body + filters + " ORDER BY bc.ytm DESC NULLS LAST, real_yield_percent DESC NULLS LAST LIMIT %s"
        # 005.4 (миграция 013): bc.ytm хранится в процентах годовых (24.01 =
        # 24.01%), порог CLI --max-ytm тоже в процентах — сравнение 1:1;
        # параметры окна погашения — динамические (002.3/P9.5).
        # 030.6: параметры следуют порядку %s в собранном WHERE: в buy-режиме
        # structural-блок (risk, listlevel) идёт до окна погашения, ytm —
        # последним; режим portfolio не изменился.
        params: List[Any] = []
        if mode == 'buy':
            params += [max_risk, max_listlevel, *maturity_params, max_ytm]
        else:
            params += [*maturity_params, max_risk]
        params += [limit]

        cursor = self._cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_rebalance_portfolio_rows(self) -> List[Dict[str, Any]]:
        """Позиции портфеля, агрегированные по ISIN, для rebalance-report (005.1).

        Каждому ISIN — одна строка: количество/стоимость просуммированы по всем
        счетам, цена — фактическая (взвешенная по количеству current_price,
        фолбэк market_price каталога; для занулённых брокером позиций это
        честный 0). Стоимость — каноническая оценка view
        v_portfolio_positions_valuation (контракт A3/002.2). Имя/тикер/эмитент
        проставляются из каталога (анти-P-D: человекочитаемые поля даёт
        хранилище, агент ISIN по памяти не придумывает). YTM хранится в
        процентах годовых (005.4, миграция 013) и отдаётся как есть —
        расчётный движок use-case'а читает её без конвертации.

        030.7 (аддитивно): строка несёт valuation_source — источник оценки
        наибольшей позиции ISIN (008; та же семантика, что в
        get_chatgpt_portfolio_rows). Потребителю high-level планирования это
        позволяет показывать рядом «чем оценена позиция» (valuation_source)
        и «по какой цене считается план» (planning_price/planning_price_basis
        движка) — разные понятия. Legacy-потребители лишнее поле игнорируют.
        """
        cursor = self._cursor()
        cursor.execute(f"""
            SELECT
                a.isin,
                COALESCE(bc.ticker, a.pos_ticker) AS ticker,
                COALESCE(bc.name, a.pos_name) AS name,
                bc.nominal,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) AS coupon_rate_percent,
                bc.coupon_quantity_per_year,
                bc.maturity_date,
                bc.offer_date,
                bc.risk_level,
                bc.list_level,
                bc.amortization_flag,
                bc.floating_coupon_flag,
                bc.perpetual_flag,
                bc.is_for_qualified_investors AS ku,
                bc.market_price,
                bc.ytm,
                bc.ytm_null_reason,
                bc.ytm_updated_at,
                co.id AS company_id,
                co.name AS issuer,
                co.entity_type,
                -- 024: последний рейтинг позиции (был null — join был только
                -- у buy-кандидатов); нужен валидатору --min-credit-rating
                -- по целевому портфелю сценария. Тот же паттерн DISTINCT ON.
                lr.credit_rating,
                lr.rating_score,
                a.quantity,
                a.value_rub,
                a.valuation_source,
                COALESCE(
                    CASE WHEN a.quantity > 0 AND a.price_x_qty IS NOT NULL
                         THEN ROUND(a.price_x_qty / a.quantity, 4) END,
                    bc.market_price
                ) AS price
            FROM ({_AGGREGATED_PORTFOLIO_SUBQUERY}) a
            LEFT JOIN bonds_catalog bc ON bc.isin = a.isin
            LEFT JOIN companies co ON co.id = bc.company_id
            {_LATEST_RATING_JOIN}
            {_LATEST_FLOATER_RATE_JOIN}
            ORDER BY a.value_rub DESC NULLS LAST, a.isin
        """)
        return [dict(row) for row in cursor.fetchall()]

    def get_chatgpt_portfolio_rows(self) -> List[Dict[str, Any]]:
        """Активный портфель для P0 Google Sheets -> ChatGPT.

        Одна строка на ISIN. Переиспользует те же поля, что rebalance-report,
        и добавляет последний кредитный рейтинг, дюрацию и агрегированную
        потерю ликвидности. Финансовые расчёты остаются в fin_bonds.
        valuation_source (008) — источник оценки позиции с наибольшим
        position_value_rub внутри ISIN: несколько счетов/брокеров могут
        оценивать один выпуск разными ветками canonical valuation.
        """
        cursor = self._cursor()
        cursor.execute(f"""
            WITH latest_ratings AS (
                SELECT DISTINCT ON (isin)
                       isin, rating_code
                FROM rating_history
                WHERE rating_code IS NOT NULL
                ORDER BY isin, rating_date DESC, id DESC
            ),
            aggregated AS (
                SELECT
                    v.isin,
                    MAX(v.ticker) AS pos_ticker,
                    MAX(v.name) AS pos_name,
                    SUM(v.quantity) AS quantity,
                    SUM(v.position_value_rub) AS value_rub,
                    SUM(v.current_price * v.quantity) AS price_x_qty,
                    -- Источник оценки наибольшей позиции ISIN (008): не
                    -- восстанавливаем его постфактум из округлённых значений.
                    (array_agg(v.valuation_source
                               ORDER BY v.position_value_rub DESC NULLS LAST))[1]
                        AS valuation_source,
                    CASE
                        WHEN SUM(CASE WHEN v.liquidity_loss_ratio IS NOT NULL
                                      THEN v.position_value_rub ELSE 0 END) > 0
                        THEN
                            SUM(CASE WHEN v.liquidity_loss_ratio IS NOT NULL
                                     THEN v.position_value_rub * v.liquidity_loss_ratio
                                     ELSE 0 END)
                            / NULLIF(SUM(CASE WHEN v.liquidity_loss_ratio IS NOT NULL
                                              THEN v.position_value_rub ELSE 0 END), 0)
                        ELSE NULL
                    END AS liquidity_loss_ratio
                FROM v_portfolio_positions_valuation v
                WHERE v.position_status = 'active'
                GROUP BY v.isin
            )
            SELECT
                a.isin,
                COALESCE(bc.ticker, a.pos_ticker) AS ticker,
                COALESCE(bc.name, a.pos_name) AS name,
                bc.nominal,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric)
                    AS coupon_rate_percent,
                bc.coupon_quantity_per_year,
                bc.maturity_date,
                bc.offer_date,
                bc.risk_level,
                bc.list_level,
                bc.amortization_flag,
                bc.floating_coupon_flag,
                bc.perpetual_flag,
                bc.is_for_qualified_investors AS ku,
                bc.market_price,
                bc.ytm,
                bc.ytm_null_reason,
                bc.ytm_updated_at,
                bc.duration_macaulay,
                bc.duration_modified,
                bc.duration_null_reason,
                bc.duration_updated_at,
                lr.rating_code AS credit_rating,
                co.id AS company_id,
                co.name AS issuer,
                co.entity_type,
                a.quantity,
                a.value_rub,
                a.valuation_source,
                a.liquidity_loss_ratio,
                COALESCE(
                    CASE WHEN a.quantity > 0 AND a.price_x_qty IS NOT NULL
                         THEN ROUND(a.price_x_qty / a.quantity, 4) END,
                    bc.market_price
                ) AS price
            FROM aggregated a
            LEFT JOIN bonds_catalog bc ON bc.isin = a.isin
            LEFT JOIN companies co ON co.id = bc.company_id
            LEFT JOIN latest_ratings lr ON lr.isin = a.isin
            {_LATEST_FLOATER_RATE_JOIN}
            ORDER BY a.value_rub DESC NULLS LAST, a.isin
        """)
        return [dict(row) for row in cursor.fetchall()]

    def get_scenario_universe_rows(self, isins: List[str]) -> List[Dict[str, Any]]:
        """Строки каталога для ISIN, НЕ лежащих в портфеле — покупки сценария (005.2).

        Форма строки повторяет get_rebalance_portfolio_rows (quantity/value_rub
        = 0 — позиции ещё нет), чтобы сценарный расчёт в use-case'е шёл по одним
        ключам для держимых и покупаемых бумаг. company_id нужен сценарию для
        ошибки NO_COMPANY_LINK (блок 0 артефакта A2/002.4). Фильтров скрининга
        нет — поиск по явному списку ISIN из scenario.json (анти-P-D: ISIN
        приходит из вывода тулкита, не из памяти агента).

        030.6: выборка дополнена полями structural BUY gate (currency,
        is_trade_available, market_price_updated_at) — аддитивно; по этой
        permissive-строке eligibility-гейт планирования классифицирует причины
        отказа для ISIN, не попавшего в buy-пул (BUY_NOT_ELIGIBLE reasons).
        """
        if not isins:
            return []
        cursor = self._cursor()
        cursor.execute(f"""
            SELECT
                bc.isin,
                bc.ticker,
                bc.name,
                bc.nominal,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value::numeric) AS coupon_rate_percent,
                bc.coupon_quantity_per_year,
                bc.maturity_date,
                bc.offer_date,
                bc.risk_level,
                bc.list_level,
                bc.amortization_flag,
                bc.floating_coupon_flag,
                bc.perpetual_flag,
                -- 030.6: поля structural BUY gate для построчной оценки
                bc.currency,
                bc.is_trade_available,
                bc.market_price_updated_at,
                bc.is_for_qualified_investors AS ku,
                bc.market_price,
                bc.ytm,
                bc.ytm_null_reason,
                bc.ytm_updated_at,
                co.id AS company_id,
                co.name AS issuer,
                co.entity_type,
                -- 024: рейтинг кандидата на покупку для валидатора ограничений
                lr.credit_rating,
                lr.rating_score,
                0 AS quantity,
                0 AS value_rub
            FROM bonds_catalog bc
            LEFT JOIN companies co ON co.id = bc.company_id
            {_LATEST_RATING_JOIN}
            {_LATEST_FLOATER_RATE_JOIN}
            WHERE bc.isin = ANY(%s)
        """, (list(isins),))
        return [dict(row) for row in cursor.fetchall()]

    def get_portfolio_redemption_events(self, months: int = 6) -> List[Dict[str, Any]]:
        """Позиции портфеля с погашением/офертой на горизонте months месяцев (005.1).

        Сырьё для календаря redemptions_6m (пул реинвеста): по одной строке на
        ISIN с канонической оценкой позиции и обеими датами (maturity/offer);
        выбор ближайшего события и суммы — в use-case'е. Погашенные позиции
        (дата в прошлом) в окно не попадают автоматически.
        """
        if months <= 0:
            raise ValueError("months должен быть положительным числом")
        cursor = self._cursor()
        cursor.execute(f"""
            SELECT
                a.isin,
                COALESCE(bc.name, a.pos_name) AS name,
                COALESCE(bc.ticker, a.pos_ticker) AS ticker,
                co.name AS issuer,
                a.quantity,
                a.value_rub AS position_value_rub,
                bc.maturity_date,
                bc.offer_date
            FROM ({_AGGREGATED_PORTFOLIO_SUBQUERY}) a
            JOIN bonds_catalog bc ON bc.isin = a.isin
            LEFT JOIN companies co ON co.id = bc.company_id
            WHERE (bc.maturity_date IS NOT NULL
                   AND bc.maturity_date >= CURRENT_DATE
                   AND bc.maturity_date < CURRENT_DATE + make_interval(months => %s))
               OR (bc.offer_date IS NOT NULL
                   AND bc.offer_date >= CURRENT_DATE
                   AND bc.offer_date < CURRENT_DATE + make_interval(months => %s))
            ORDER BY a.isin
        """, (months, months))
        return [dict(row) for row in cursor.fetchall()]

    def get_last_risk_listlevel(self, limit: int) -> List[Dict[str, Any]]:
        """Последние уровни риска и листинга по каждому ISIN (команда check-db)."""
        cursor = self._cursor()
        cursor.execute("""
            WITH LatestRisk AS (
                SELECT
                    isin,
                    risk_level as risk_rating,
                    risk_date as last_update,
                    ROW_NUMBER() OVER(PARTITION BY isin ORDER BY risk_date DESC) as rn
                FROM risk_history
            ),
            LatestListLevel AS (
                SELECT
                    isin,
                    list_level,
                    check_date,
                    ROW_NUMBER() OVER(PARTITION BY isin ORDER BY check_date DESC) as rn
                FROM listlevel_history
            )
            SELECT
                lr.isin,
                lr.risk_rating,
                ll.list_level,
                lr.last_update
            FROM LatestRisk lr
            LEFT JOIN LatestListLevel ll ON lr.isin = ll.isin AND ll.rn = 1
            WHERE lr.rn = 1
            ORDER BY lr.last_update DESC
            LIMIT %s;
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def execute_debug_query(self, sql: str) -> List[Dict[str, Any]]:
        """Выполняет произвольный SQL-запрос (ветка --query команды check-db).

        Только для ручной диагностики из CLI: обходит семантику DTO,
        поэтому вне check-db не используется.
        """
        cursor = self._cursor()
        cursor.execute(sql)
        return [dict(row) for row in cursor.fetchall()]
