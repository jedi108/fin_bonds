import argparse
from datetime import date, datetime, timezone
import json
import logging
from typing import Dict, Any, TYPE_CHECKING

import pandas as pd

from src.storage import PortfolioStorage
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

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
        query = getattr(args, 'query', None)

        if is_json and not is_freshness:
            raise ValueError("--json flag can only be used with --freshness")
        if is_freshness and query:
            raise ValueError("Cannot combine --freshness with --query")

        if is_freshness:
            freshness_data = self.db.get_db_freshness()

            generated_at = datetime.now(timezone.utc).isoformat()

            def format_dt(val):
                if val is None:
                    return None
                if isinstance(val, datetime):
                    if val.tzinfo is None:
                        val = val.replace(tzinfo=timezone.utc)
                    return val.isoformat()
                return str(val)

            def format_date(val):
                if val is None:
                    return None
                if isinstance(val, (date, datetime)):
                    return val.strftime('%Y-%m-%d')
                return str(val)

            report = {
                "generated_at": generated_at,
                "bonds_catalog_rows": int(freshness_data.get('bonds_catalog_rows') or 0),
                "bonds_catalog_max_updated_at": format_dt(freshness_data.get('bonds_catalog_max_updated_at')),
                "bonds_with_market_price": int(freshness_data.get('bonds_with_market_price') or 0),
                "market_price_max_updated_at": format_dt(freshness_data.get('market_price_max_updated_at')),
                "bonds_with_stale_market_price": int(freshness_data.get('bonds_with_stale_market_price') or 0),
                "portfolio_positions_rows": int(freshness_data.get('portfolio_positions_rows') or 0),
                "portfolio_max_updated_at": format_dt(freshness_data.get('portfolio_max_updated_at')),
                "monitoring_checks_max_check_date": format_date(freshness_data.get('monitoring_checks_max_check_date')),
            }

            if is_json:
                print(json.dumps(report, ensure_ascii=False))
            else:
                print(f"Database Freshness Report (at {report['generated_at']}):")
                print(f"- Bonds catalog: {report['bonds_catalog_rows']} rows (last updated: {report['bonds_catalog_max_updated_at'] or 'null'})")
                print(f"- Market prices: {report['bonds_with_market_price']} bonds (last updated: {report['market_price_max_updated_at'] or 'null'}, stale: {report['bonds_with_stale_market_price']})")
                print(f"- Portfolio positions: {report['portfolio_positions_rows']} rows (last updated: {report['portfolio_max_updated_at'] or 'null'})")
                print(f"- Monitoring checks: last check date {report['monitoring_checks_max_check_date'] or 'null'}")

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
 