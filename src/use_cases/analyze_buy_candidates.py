"""
Use Case для анализа облигаций для покупки.
"""

import argparse
import logging
from typing import Any, Dict, List

from src.use_cases.base import UseCase
from src.storage import PortfolioStorage

logger = logging.getLogger(__name__)


class AnalyzeBuyCandidatesUseCase(UseCase):
    """Анализ облигаций для покупки."""

    def __init__(self, db: PortfolioStorage):
        self.db = db

    @staticmethod
    def setup_parser(subparser: argparse.ArgumentParser):
        """Настройка парсера аргументов."""
        subparsers = subparser.add_subparsers(dest='mode', help='Режим анализа')
        
        # Режим: топ по доходности
        top_parser = subparsers.add_parser('top', help='Топ бумаг по доходности')
        top_parser.add_argument('--limit', type=int, default=20, help='Количество бумаг (по умолчанию: 20)')
        top_parser.add_argument('--min-risk', type=int, default=1, help='Максимальный уровень риска (по умолчанию: 1)')
        top_parser.add_argument('--min-months', type=int, default=1, help='Минимум месяцев до погашения (по умолчанию: 1)')
        top_parser.add_argument('--fixed-coupon', action='store_true', help='Только облигации с фиксированным купоном')
        top_parser.add_argument('--floating-coupon', action='store_true', help='Только флоатеры')
        top_parser.add_argument('--amortization', action='store_true', help='Только амортизируемые облигации')
        top_parser.add_argument('--no-amortization', action='store_true', help='Исключить амортизируемые облигации')
        
        # Режим: флоатеры
        floater_parser = subparsers.add_parser('floaters', help='Анализ флоатеров для покупки')
        floater_parser.add_argument('--limit', type=int, default=15, help='Количество бумаг (по умолчанию: 15)')
        
        # Режим: сравнение с портфелем
        compare_parser = subparsers.add_parser('compare', help='Сравнение с текущим портфелем')
        compare_parser.add_argument('--limit', type=int, default=25, help='Количество бумаг (по умолчанию: 25)')
        compare_parser.add_argument('--min-risk', type=int, default=2, help='Максимальный уровень риска (по умолчанию: 2)')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'AnalyzeBuyCandidatesUseCase':
        return cls(db=factory.get_db_connection())

    def execute(self, args: argparse.Namespace):
        """Выполнение команды."""
        logger.info(f"🚀 Запуск анализа бумаг для покупки в режиме: {args.mode}")
        
        if args.mode == 'top':
            self._show_top_candidates(args)
        elif args.mode == 'floaters':
            self._show_floater_candidates(args)
        elif args.mode == 'compare':
            self._show_portfolio_comparison(args)
        else:
            logger.error("❌ Неизвестный режим анализа")

    def _show_top_candidates(self, args: argparse.Namespace):
        """Показывает топ бумаг по доходности."""
        rows = self.db.get_top_buy_candidates(
            max_risk=args.min_risk,
            min_days_to_maturity=args.min_months * 30,
            only_fixed_coupon=args.fixed_coupon,
            only_floating_coupon=args.floating_coupon,
            only_amortization=args.amortization,
            exclude_amortization=args.no_amortization,
            limit=args.limit,
        )

        if not rows:
            logger.info("ℹ️ Не найдено подходящих бумаг для покупки.")
            return

        logger.info(f"📈 Найдено {len(rows)} бумаг для анализа")
        self._print_top_candidates_table(rows)

    def _show_floater_candidates(self, args: argparse.Namespace):
        """Показывает флоатеры для покупки."""
        rows = self.db.get_floater_buy_candidates(limit=args.limit)

        if not rows:
            logger.info("ℹ️ Не найдено флоатеров для покупки.")
            return

        logger.info(f"📈 Найдено {len(rows)} флоатеров для анализа")
        self._print_floater_candidates_table(rows)

    def _show_portfolio_comparison(self, args: argparse.Namespace):
        """Показывает сравнение с текущим портфелем."""
        rows = self.db.get_portfolio_comparison_candidates(
            max_risk=args.min_risk,
            limit=args.limit,
        )

        if not rows:
            logger.info("ℹ️ Не найдено бумаг для сравнения с портфелем.")
            return

        logger.info(f"📈 Найдено {len(rows)} бумаг для сравнения")
        self._print_comparison_table(rows)

    def _print_top_candidates_table(self, rows: List[Dict[str, Any]]):
        """Выводит таблицу топ кандидатов."""
        print(f"\n{'='*140}")
        print(f"📊 Топ бумаг для покупки по доходности к погашению")
        print(f"{'='*140}")
        print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<25} | {'Купон %':<6} | {'Риск':<4} | {'Цена':<8} | {'Доходность %':<12} | {'До погаш.':<9} | {'Тип':<12}")
        print("-" * 140)
        
        for row in rows:
            try:
                coupon_rate = float(row['coupon_rate_percent']) if row['coupon_rate_percent'] else 0
                market_price = float(row['market_price']) if row['market_price'] else 0
                annual_yield = float(row['annual_yield_percent']) if row['annual_yield_percent'] else 0
                months_to_maturity = float(row['months_to_maturity']) if row['months_to_maturity'] else 0
                
                print(f"{row['isin']:<12} | "
                      f"{row['ticker'] or 'N/A':<8} | "
                      f"{row['name'][:25] if row['name'] else 'N/A':<25} | "
                      f"{coupon_rate:<6.1f} | "
                      f"{row['risk_level'] or 'N/A':<4} | "
                      f"{market_price:<8.0f} | "
                      f"{annual_yield:<12.2f} | "
                      f"{months_to_maturity:<9.1f} | "
                      f"{row['bond_type']:<12}")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка при выводе строки: {e}")
                continue
        
        print(f"{'='*120}")

    def _print_floater_candidates_table(self, rows: List[Dict[str, Any]]):
        """Выводит таблицу флоатеров для покупки."""
        print(f"\n{'='*120}")
        print(f"📊 Флоатеры для покупки")
        print(f"{'='*120}")
        print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<25} | {'Ставка %':<7} | {'Спред':<6} | {'Риск':<4} | {'Купон/мес':<8} | {'До погаш.':<9}")
        print("-" * 120)
        
        for row in rows:
            try:
                calculated_rate = float(row['calculated_coupon_rate']) if row['calculated_coupon_rate'] else 0
                spread = float(row['coupon_spread']) if row['coupon_spread'] else 0
                monthly_income = float(row['monthly_coupon_income_per_bond']) if row['monthly_coupon_income_per_bond'] else 0
                months_to_maturity = float(row['months_to_maturity']) if row['months_to_maturity'] else 0
                
                print(f"{row['isin']:<12} | "
                      f"{row['ticker'] or 'N/A':<8} | "
                      f"{row['name'][:25] if row['name'] else 'N/A':<25} | "
                      f"{calculated_rate:<7.1f} | "
                      f"{spread:<6.1f} | "
                      f"{row['risk_level'] or 'N/A':<4} | "
                      f"{monthly_income:<8.0f} | "
                      f"{months_to_maturity:<9.1f}")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка при выводе строки: {e}")
                continue
        
        print(f"{'='*120}")

    def _print_comparison_table(self, rows: List[Dict[str, Any]]):
        """Выводит таблицу сравнения с портфелем."""
        print(f"\n{'='*120}")
        print(f"📊 Сравнение с текущим портфелем")
        print(f"{'='*120}")
        print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<25} | {'Купон %':<6} | {'Риск':<4} | {'Доходн. %':<9} | {'Разница':<7} | {'Рекомендация':<15}")
        print("-" * 120)
        
        for row in rows:
            try:
                coupon_rate = float(row['coupon_rate_percent']) if row['coupon_rate_percent'] else 0
                potential_yield = float(row['potential_yield_percent']) if row['potential_yield_percent'] else 0
                yield_diff = float(row['yield_vs_portfolio']) if row['yield_vs_portfolio'] else 0
                
                # Отладочная информация для RU000A10B7T7
                if row['isin'] == 'RU000A10B7T7':
                    logger.debug(f"DEBUG: {row['isin']} - coupon_rate: {coupon_rate}, potential_yield: {potential_yield}")
                
                print(f"{row['isin']:<12} | "
                      f"{row['ticker'] or 'N/A':<8} | "
                      f"{row['name'][:25] if row['name'] else 'N/A':<25} | "
                      f"{coupon_rate:<6.1f} | "
                      f"{row['risk_level'] or 'N/A':<4} | "
                      f"{potential_yield:<9.1f} | "
                      f"{yield_diff:<7.1f} | "
                      f"{row['recommendation']:<15}")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка при выводе строки: {e}")
                continue
        
        print(f"{'='*120}")
