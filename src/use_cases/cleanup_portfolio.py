import argparse
import logging
from typing import TYPE_CHECKING, Any, Dict, List

from src.storage import PortfolioStorage
from src.use_cases.base import UseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class CleanupPortfolioUseCase(UseCase):
    """
    Сценарий: чистка зомби-позиций портфеля (задача 002.1).

    Зомби — активные (status='active') строки portfolio_positions, которые
    блокируют гейт свежести фантомной оценкой:
      - бумага погашена (maturity_date < CURRENT_DATE), но позиция всё ещё
        в портфеле (кейс МОНОП 1P02: 35 шт после погашения) → status='matured';
      - нулевое/NULL количество → status='closed'.

    Позиции с ненулевым количеством и нулевой оценкой на НЕпогашенных бумагах
    (кейс ГлобалФ: цена 0 от брокера, торговля через оферту) НЕ трогаются —
    это не ошибка синка; они выводятся в отчёте справочно, а гейт свежести
    считает их через WARN_ZOMBIE_ROWS.
    """

    def __init__(self, config: Dict[str, Any], db: PortfolioStorage):
        self.config = config
        self.db = db
        self.logger = logging.getLogger(self.__class__.__name__)

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Только показать, что будет помечено (matured/closed), ничего не менять.'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'CleanupPortfolioUseCase':
        return cls(config=factory.config, db=factory.get_db_connection())

    def execute(self, args: argparse.Namespace):
        logger.info(f"Запуск чистки зомби-позиций (dry_run={args.dry_run})...")

        zombies = self.db.find_zombies()
        self._print_report(zombies)

        if args.dry_run:
            to_mature = len(zombies['matured'])
            to_close = len(zombies['zero_quantity'])
            print(f"DRY-RUN: было бы помечено status='matured': {to_mature}, "
                  f"status='closed': {to_close}. Изменения не применены.")
            logger.info("Dry-run завершён: изменения не применены.")
            return

        # Порядок важен: closed сначала, затем matured — у пересечения
        # («погашена И нулевое количество») остаётся более информативный matured.
        closed = self.db.mark_closed_positions()
        matured = self.db.mark_matured_positions()

        print(f"Готово: помечено status='closed': {len(closed)}, "
              f"status='matured': {len(matured)}.")
        if zombies['zero_value_live']:
            print(f"Пропущено (живые позиции с нулевой оценкой от брокера, не зомби): "
                  f"{len(zombies['zero_value_live'])} — их учитывает гейт как WARN_ZOMBIE_ROWS.")
        logger.info("Чистка зомби-позиций завершена.")

    def _print_report(self, zombies: Dict[str, List[Dict[str, Any]]]):
        def rows(items: List[Dict[str, Any]]):
            return (
                f"  {r['isin']} | {r['broker_name']} | {r['account_id'] or ''} | "
                f"qty: {r['quantity']} | value: {r['current_value']}"
                for r in items
            )

        print(f"Зомби-кандидаты (status='active' сейчас):")
        print(f"1) Погашенные бумаги (будут помечены status='matured'): {len(zombies['matured'])}")
        for line in rows(zombies['matured']):
            print(line)
        print(f"2) Нулевое количество (будут помечены status='closed'): {len(zombies['zero_quantity'])}")
        for line in rows(zombies['zero_quantity']):
            print(line)
        print(f"3) НЕ зомби — живые позиции с нулевой/NULL оценкой на непогашенных "
              f"бумагах (не чистятся; гейт: WARN_ZOMBIE_ROWS): {len(zombies['zero_value_live'])}")
        for line in rows(zombies['zero_value_live']):
            print(line)
