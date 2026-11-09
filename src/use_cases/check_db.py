import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import sys
from typing import Dict, Any, List, Optional, Tuple, TYPE_CHECKING

import pandas as pd

from src.storage import PortfolioStorage
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory


def _format_dt(val):
    """Метка времени -> ISO-строка с таймзоной (NULL/пусто -> None)."""
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.isoformat()
    return str(val)


def _format_date(val):
    """Дата/метка времени -> 'YYYY-MM-DD' (NULL/пусто -> None)."""
    if val is None:
        return None
    if isinstance(val, (date, datetime)):
        return val.strftime('%Y-%m-%d')
    return str(val)


def classify_freshness(
    freshness_data: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Tuple[List[str], List[str], str, bool]:
    """Гейт свежести данных (контракт 001/002; WARN vs CRITICAL по P1/002).

    Общая точка классификации для check-db и rebalance-report (005.1):
    одинаковые пороги и тексты инцидентов. Инцидент 2026-09-29: пустой каталог
    цен давал «0 устаревших из 0» и считался OK — поэтому пустота цен/портфеля
    трактуется как критическое нарушение, а не как отсутствие проблем.

    Returns:
        (critical_issues, warning_issues, gate_status, is_fresh).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    bonds_with_market_price = int(freshness_data.get('bonds_with_market_price') or 0)
    tradeable_count = int(freshness_data.get('bonds_tradeable_count') or 0)
    stale_or_missing = int(freshness_data.get('bonds_tradeable_stale_or_missing') or 0)
    portfolio_rows = int(freshness_data.get('portfolio_positions_rows') or 0)
    zero_value_positions = int(freshness_data.get('portfolio_zero_value_positions') or 0)
    # Правило A1 (002.1): подозрительные = zero-строки на НЕпогашенных
    # бумагах (класс бага синка из 001); зомби-строки погашенных бумаг
    # подозрительными не считаются и CRITICAL не дают.
    zero_rows_suspicious = int(freshness_data.get('portfolio_zero_rows_suspicious') or 0)
    zero_rows_isins = freshness_data.get('portfolio_zero_value_isins') or ''

    last_sync_time = freshness_data.get('last_sync_time')
    if isinstance(last_sync_time, datetime) and last_sync_time.tzinfo is None:
        last_sync_time = last_sync_time.replace(tzinfo=timezone.utc)
    # Порог «3 дня» — точным сравнением меток, как в A1
    # (last_sync_time < NOW() - INTERVAL '3 days'), а не целыми сутками
    # через (.days): floor расходился бы с SQL-гейтом в окне 3–4 дня.
    sync_stale = last_sync_time is None or last_sync_time < now - timedelta(days=3)

    stale_share_limit = tradeable_count * 0.2
    stale_share_ok = stale_or_missing <= stale_share_limit

    critical_issues: List[str] = []
    warning_issues: List[str] = []

    if bonds_with_market_price == 0:
        critical_issues.append(
            "Каталог рыночных цен пуст: ни у одной облигации не заполнен market_price"
        )
    if not stale_share_ok:
        warning_issues.append(
            f"Без актуальной рыночной цены {stale_or_missing} из {tradeable_count} "
            f"торгуемых облигаций — превышен порог 20%"
        )
    elif stale_or_missing > 0:
        warning_issues.append(
            f"Без актуальной рыночной цены {stale_or_missing} из {tradeable_count} "
            f"торгуемых облигаций (в пределах порога 20%)"
        )
    if portfolio_rows == 0:
        critical_issues.append("Портфель пуст: в portfolio_positions нет ни одной позиции")
    if zero_rows_suspicious > 2:
        critical_issues.append(
            f"Подозрительных позиций с нулевой/отсутствующей оценкой на "
            f"НЕпогашенных бумагах: {zero_rows_suspicious} (порог >2) — массовый "
            f"сбой синка; ISIN: {zero_rows_isins or 'n/a'}"
        )
    elif zero_value_positions > 0:
        # WARN_ZOMBIE_ROWS: данные свежие, ребалансировке не мешает,
        # вручную не разбирать (002.1).
        warning_issues.append(
            f"WARN_ZOMBIE_ROWS: zero-строк в портфеле {zero_value_positions}, "
            f"из них подозрительных (непогашенные) {zero_rows_suspicious} (в пределах "
            f"порога); это зомби/легитимно занулённые брокером строки — "
            f"разбирать вручную не нужно. ISIN: {zero_rows_isins or 'n/a'}"
        )
    if last_sync_time is None:
        critical_issues.append("Нет метки последней синхронизации портфеля (last_sync_time = NULL)")
    elif sync_stale:
        critical_issues.append(
            f"Портфель не синхронизировался дольше 3 дней "
            f"(последняя синхронизация: {_format_dt(last_sync_time)})"
        )

    is_fresh = (
        bonds_with_market_price > 0
        and stale_share_ok
        and portfolio_rows > 0
        and zero_rows_suspicious <= 2
        and last_sync_time is not None
        and not sync_stale
    )

    # Канонический статус гейта v2 (артефакт A1, freshness_check.sql):
    # тот же порядок проверок, что в SQL — обе точки гейта обязаны
    # возвращать согласованное значение.
    if portfolio_rows == 0:
        gate_status = "EMPTY_PORTFOLIO"
    elif zero_rows_suspicious > 2:
        gate_status = "CRITICAL_ZERO_VALUATION"
    elif tradeable_count == 0 or bonds_with_market_price == 0:
        gate_status = "CRITICAL_NO_PRICES"
    elif not stale_share_ok:
        gate_status = "STALE_PRICES"
    elif sync_stale:
        gate_status = "STALE_PORTFOLIO"
    elif zero_value_positions > 0:
        gate_status = "WARN_ZOMBIE_ROWS"
    else:
        gate_status = "OK"

    return critical_issues, warning_issues, gate_status, is_fresh


def build_freshness_report(
    freshness_data: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Собирает канонический JSON-отчёт свежести (команда check-db --freshness --json).

    Использует classify_freshness — тот же гейт, что и rebalance-report (005.1).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    generated_at = now.isoformat()

    critical_issues, warning_issues, gate_status, is_fresh = classify_freshness(freshness_data, now)

    issues = critical_issues + warning_issues
    status = "CRITICAL" if critical_issues else ("WARNING" if warning_issues else "OK")

    return {
        "generated_at": generated_at,
        "status": status,
        "gate_status": gate_status,
        "is_fresh": is_fresh,
        "issues": issues,
        "bonds_catalog_rows": int(freshness_data.get('bonds_catalog_rows') or 0),
        "bonds_catalog_max_updated_at": _format_dt(freshness_data.get('bonds_catalog_max_updated_at')),
        "bonds_with_market_price": int(freshness_data.get('bonds_with_market_price') or 0),
        "market_price_max_updated_at": _format_dt(freshness_data.get('market_price_max_updated_at')),
        "bonds_with_stale_market_price": int(freshness_data.get('bonds_with_stale_market_price') or 0),
        "bonds_tradeable_count": int(freshness_data.get('bonds_tradeable_count') or 0),
        "bonds_tradeable_stale_or_missing": int(freshness_data.get('bonds_tradeable_stale_or_missing') or 0),
        "portfolio_positions_rows": int(freshness_data.get('portfolio_positions_rows') or 0),
        "portfolio_zero_value_positions": int(freshness_data.get('portfolio_zero_value_positions') or 0),
        "portfolio_zero_rows_suspicious": int(freshness_data.get('portfolio_zero_rows_suspicious') or 0),
        "portfolio_zero_rows_isins": freshness_data.get('portfolio_zero_value_isins') or '',
        "portfolio_total_value": float(Decimal(str(freshness_data.get('portfolio_total_value') or 0))),
        "portfolio_max_updated_at": _format_dt(freshness_data.get('portfolio_max_updated_at')),
        "monitoring_checks_max_check_date": _format_date(freshness_data.get('monitoring_checks_max_check_date')),
    }

class CheckDbUseCase(UseCase):
    """
    Сценарий: Проверка последних записей в базе данных и меток свежести.
    """
    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды проверки БД."""
        parser.add_argument(
            '--limit', type=int, default=10,
            help='Ограничивает количество выводимых записей (по умолчанию 10).'
        )
        parser.add_argument(
            '--query', type=str, default=None,
            help='Выполняет произвольный SQL-запрос вместо запроса по умолчанию.'
        )
        parser.add_argument(
            '--freshness', action='store_true', default=False,
            help='Показывает метки свежести данных в БД.'
        )
        parser.add_argument(
            '--json', action='store_true', default=False,
            help='Выводит результат проверки свежести в формате JSON.'
        )
        parser.add_argument(
            '--fail-stale', action='store_true', default=False,
            help='Завершает процесс с кодом 1, если данные не свежие (is_fresh=false).'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'CheckDbUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(config=factory.config, db=factory.get_db_connection())

    def __init__(self, config: Dict[str, Any], db: PortfolioStorage):
        self.config = config
        self.db = db
        self.logger = logging.getLogger(self.__class__.__name__)

    def execute(self, args: argparse.Namespace):
        self.logger.info("Executing CheckDbUseCase")

        is_freshness = getattr(args, 'freshness', False)
        is_json = getattr(args, 'json', False)
        fail_stale = getattr(args, 'fail_stale', False)
        query = getattr(args, 'query', None)

        if is_json and not is_freshness:
            raise ValueError("--json flag can only be used with --freshness")
        if is_freshness and query:
            raise ValueError("Cannot combine --freshness with --query")

        if is_freshness:
            freshness_data = self.db.get_db_freshness()
            report = build_freshness_report(freshness_data)

            if is_json:
                print(json.dumps(report, ensure_ascii=False))
            else:
                print(f"Database Freshness Report (at {report['generated_at']}):")
                print(f"- Status: {report['status']} / gate: {report['gate_status']} (is_fresh: {str(report['is_fresh']).lower()})")
                for issue in report['issues']:
                    print(f"  ! {issue}")
                print(f"- Bonds catalog: {report['bonds_catalog_rows']} rows (last updated: {report['bonds_catalog_max_updated_at'] or 'null'})")
                print(f"- Market prices: {report['bonds_with_market_price']} bonds (last updated: {report['market_price_max_updated_at'] or 'null'}, stale: {report['bonds_with_stale_market_price']})")
                print(f"- Tradeable bonds: {report['bonds_tradeable_count']} (stale or missing price: {report['bonds_tradeable_stale_or_missing']})")
                print(f"- Portfolio positions: {report['portfolio_positions_rows']} rows (last updated: {report['portfolio_max_updated_at'] or 'null'})")
                print(f"- Portfolio valuation: total {report['portfolio_total_value']} (zero/NULL value positions: {report['portfolio_zero_value_positions']}, suspicious (unmatured): {report['portfolio_zero_rows_suspicious']}, ISINs: {report['portfolio_zero_rows_isins'] or 'n/a'})")
                print(f"- Monitoring checks: last check date {report['monitoring_checks_max_check_date'] or 'null'}")

            if fail_stale and not report['is_fresh']:
                self.logger.error("Freshness gate failed (is_fresh=false) — exit 1 (--fail-stale)")
                sys.exit(1)

            self.logger.info("CheckDbUseCase finished")
            return

        try:
            if query:
                rows = self.db.execute_debug_query(query)
            else:
                rows = self.db.get_last_risk_listlevel(args.limit)

            df = pd.DataFrame(rows)

            if df.empty:
                self.logger.info("No data found in the database matching the criteria.")
                print("No data found.")
            else:
                self.logger.info(f"Successfully fetched {len(df)} records.")
                print("Query result:")
                print(df.to_string())

        except Exception as e:
            self.logger.error(f"An unexpected error occurred: {e}")
            print(f"An unexpected error occurred: {e}")
            raise

        self.logger.info("CheckDbUseCase finished")
 