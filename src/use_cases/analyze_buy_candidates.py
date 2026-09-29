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
    def _add_risk_arguments(parser: argparse.ArgumentParser, default: int = 1):
        """Добавляет взаимоисключаемые аргументы для риска: --max-risk (основной) и --min-risk (deprecated)."""
        risk_group = parser.add_mutually_exclusive_group()
        risk_group.add_argument(
            '--max-risk',
            type=int,
            choices=[1, 2],
            default=None,
            help=f'Максимальный уровень риска (1 или 2, по умолчанию: {default})',
        )
        risk_group.add_argument(
            '--min-risk',
            type=int,
            choices=[1, 2],
            default=None,
            help='Устаревший алиас для --max-risk',
        )

    @staticmethod
    def _add_filter_arguments(parser: argparse.ArgumentParser):
        """Добавляет фильтры частоты купонов и ОФЗ."""
        freq_group = parser.add_mutually_exclusive_group()
        freq_group.add_argument(
            '--coupon-freq',
            choices=['12', '4', '2', 'all'],
            default=None,
            help='Частота выплат купона в год (12, 4, 2, all)',
        )
        freq_group.add_argument(
            '--monthly',
            action='store_true',
            help='Только ежемесячный купон (12 раз в год)',
        )
        freq_group.add_argument(
            '--quarterly',
            action='store_true',
            help='Только квартальный купон (4 раза в год)',
        )

        ofz_group = parser.add_mutually_exclusive_group()
        ofz_group.add_argument(
            '--only-ofz',
            '--ofz-only',
            action='store_true',
            dest='only_ofz',
            help='Только ОФЗ (государственные бумаги)',
        )
        ofz_group.add_argument(
            '--exclude-ofz',
            '--no-ofz',
            action='store_true',
            dest='exclude_ofz',
            help='Исключить ОФЗ (только корпоративные эмитенты)',
        )

    @staticmethod
    def _add_include_held_argument(parser: argparse.ArgumentParser):
        """Флаг --include-held: показывать бумаги портфеля (докупки) с бейджем held (N%) (002.2)."""
        parser.add_argument(
            '--include-held',
            dest='include_held',
            action='store_true',
            help='Показывать и бумаги, уже лежащие в портфеле (докупки), '
                 'с бейджем held (доля N%%). По умолчанию позиции портфеля исключены',
        )

    @classmethod
    def setup_parser(cls, subparser: argparse.ArgumentParser):
        """Настройка парсера аргументов."""
        subparsers = subparser.add_subparsers(dest='mode', help='Режим анализа')

        # Режим: топ по доходности
        top_parser = subparsers.add_parser('top', help='Топ бумаг по доходности')
        top_parser.add_argument('--limit', type=int, default=20, help='Количество бумаг (по умолчанию: 20)')
        cls._add_risk_arguments(top_parser, default=1)
        top_parser.add_argument('--min-months', type=int, default=1, help='Минимум месяцев до погашения (по умолчанию: 1)')
        top_parser.add_argument('--fixed-coupon', action='store_true', help='Только облигации с фиксированным купоном')
        top_parser.add_argument('--floating-coupon', action='store_true', help='Только флоатеры')
        top_parser.add_argument('--amortization', action='store_true', help='Только амортизируемые облигации')
        top_parser.add_argument('--no-amortization', action='store_true', help='Исключить амортизируемые облигации')
        cls._add_filter_arguments(top_parser)
        cls._add_include_held_argument(top_parser)

        # Режим: флоатеры
        floater_parser = subparsers.add_parser('floaters', help='Анализ флоатеров для покупки')
        floater_parser.add_argument('--limit', type=int, default=15, help='Количество бумаг (по умолчанию: 15)')
        cls._add_risk_arguments(floater_parser, default=1)
        cls._add_filter_arguments(floater_parser)
        cls._add_include_held_argument(floater_parser)

        # Режим: сравнение с портфелем
        compare_parser = subparsers.add_parser('compare', help='Сравнение с текущим портфелем')
        compare_parser.add_argument('--limit', type=int, default=25, help='Количество бумаг (по умолчанию: 25)')
        cls._add_risk_arguments(compare_parser, default=1)
        cls._add_include_held_argument(compare_parser)

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

    @staticmethod
    def _resolve_max_risk(args: argparse.Namespace, default: int = 1) -> int:
        if getattr(args, 'max_risk', None) is not None:
            return args.max_risk
        if getattr(args, 'min_risk', None) is not None:
            return args.min_risk
        return default

    @staticmethod
    def _resolve_coupon_freq(args: argparse.Namespace) -> Any:
        if getattr(args, 'monthly', False):
            return 12
        if getattr(args, 'quarterly', False):
            return 4
        freq = getattr(args, 'coupon_freq', None)
        if freq and freq != 'all':
            return int(freq)
        return None

    @staticmethod
    def _resolve_entity_type_filter(args: argparse.Namespace) -> Any:
        if getattr(args, 'only_ofz', False):
            return 'only_ofz'
        if getattr(args, 'exclude_ofz', False):
            return 'exclude_ofz'
        return None

    def _show_top_candidates(self, args: argparse.Namespace):
        """Показывает топ бумаг по доходности."""
        max_risk = self._resolve_max_risk(args, default=1)
        coupon_freq = self._resolve_coupon_freq(args)
        entity_type_filter = self._resolve_entity_type_filter(args)

        rows = self.db.get_top_buy_candidates(
            max_risk=max_risk,
            min_days_to_maturity=args.min_months * 30,
            only_fixed_coupon=args.fixed_coupon,
            only_floating_coupon=args.floating_coupon,
            only_amortization=args.amortization,
            exclude_amortization=args.no_amortization,
            limit=args.limit,
            coupon_freq=coupon_freq,
            entity_type_filter=entity_type_filter,
            include_held=getattr(args, 'include_held', False),
        )

        if not rows:
            logger.info("ℹ️ Не найдено подходящих бумаг для покупки.")
            return

        logger.info(f"📈 Найдено {len(rows)} бумаг для анализа")
        self._print_top_candidates_table(rows)

    def _show_floater_candidates(self, args: argparse.Namespace):
        """Показывает флоатеры для покупки."""
        max_risk = self._resolve_max_risk(args, default=1)
        coupon_freq = self._resolve_coupon_freq(args)
        entity_type_filter = self._resolve_entity_type_filter(args)

        rows = self.db.get_floater_buy_candidates(
            limit=args.limit,
            max_risk=max_risk,
            coupon_freq=coupon_freq,
            entity_type_filter=entity_type_filter,
            include_held=getattr(args, 'include_held', False),
        )

        if not rows:
            logger.info("ℹ️ Не найдено флоатеров для покупки.")
            return

        logger.info(f"📈 Найдено {len(rows)} флоатеров для анализа")
        self._print_floater_candidates_table(rows)

    def _show_portfolio_comparison(self, args: argparse.Namespace):
        """Показывает сравнение с текущим портфелем."""
        max_risk = self._resolve_max_risk(args, default=1)
        rows = self.db.get_portfolio_comparison_candidates(
            max_risk=max_risk,
            limit=args.limit,
            include_held=getattr(args, 'include_held', False),
        )

        if not rows:
            logger.info("ℹ️ Не найдено бумаг для сравнения с портфелем.")
            return

        logger.info(f"📈 Найдено {len(rows)} бумаг для сравнения")
        self._print_comparison_table(rows)

    def _print_top_candidates_table(self, rows: List[Dict[str, Any]]):
        """Выводит таблицу топ кандидатов."""
        print(f"\n{'='*155}")
        print(f"📊 Топ бумаг для покупки по текущей купонной доходности")
        print(f"{'='*155}")
        print(f"{'ISIN':<12} | {'Тикер':<8} | {'Эмитент':<22} | {'Купон %':<7} | {'Выплат':<6} | {'Купон ₽':<7} | {'₽/мес':<6} | {'Цена':<7} | {'Куп.дох %':<9} | {'Риск':<4} | {'До погаш.':<9} | {'Тип':<12} | {'Портфель':<15}")
        print("-" * 173)

        for row in rows:
            try:
                coupon_rate = float(row.get('coupon_rate') or row.get('coupon_rate_percent') or 0)
                freq = row.get('coupon_freq') or row.get('coupon_quantity_per_year') or 0
                coupon_payment = float(row.get('coupon_payment_per_bond') or 0)
                monthly_coupon = float(row.get('average_monthly_coupon_per_bond') or row.get('monthly_coupon_income_per_bond') or 0)
                market_price = float(row.get('market_price') or 0)
                coupon_yield = float(row.get('current_coupon_yield') or row.get('annual_yield_percent') or 0)
                months_to_maturity = float(row.get('months_to_maturity') or 0)
                company = (row.get('company') or row.get('name') or 'N/A')[:22]

                print(f"{row.get('isin', 'N/A'):<12} | "
                      f"{(row.get('ticker') or 'N/A')[:8]:<8} | "
                      f"{company:<22} | "
                      f"{coupon_rate:<7.2f} | "
                      f"{freq:<6} | "
                      f"{coupon_payment:<7.2f} | "
                      f"{monthly_coupon:<6.2f} | "
                      f"{market_price:<7.0f} | "
                      f"{coupon_yield:<9.2f} | "
                      f"{row.get('risk_level') or 'N/A':<4} | "
                      f"{months_to_maturity:<9.1f} | "
                      f"{row.get('bond_type', 'Обычная'):<12} | "
                      f"{row.get('held_badge') or '':<15}")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка при выводе строки: {e}")
                continue
        
        print(f"{'='*155}")

    def _print_floater_candidates_table(self, rows: List[Dict[str, Any]]):
        """Выводит таблицу флоатеров для покупки."""
        print(f"\n{'='*155}")
        print(f"📊 Флоатеры для покупки")
        print(f"{'='*155}")
        print(f"{'ISIN':<12} | {'Тикер':<8} | {'Эмитент':<22} | {'Ставка %':<8} | {'Спред':<6} | {'Выплат':<6} | {'Купон ₽':<7} | {'₽/мес':<6} | {'Цена':<7} | {'Куп.дох %':<9} | {'Риск':<4} | {'До погаш.':<9} | {'Портфель':<15}")
        print("-" * 173)

        for row in rows:
            try:
                calculated_rate = float(row.get('calculated_coupon_rate') or row.get('coupon_rate') or 0)
                spread = float(row.get('coupon_spread') or 0)
                freq = row.get('coupon_freq') or row.get('coupon_quantity_per_year') or 0
                coupon_payment = float(row.get('coupon_payment_per_bond') or 0)
                monthly_coupon = float(row.get('average_monthly_coupon_per_bond') or row.get('monthly_coupon_income_per_bond') or 0)
                market_price = float(row.get('market_price') or 0)
                coupon_yield = float(row.get('current_coupon_yield') or 0)
                months_to_maturity = float(row.get('months_to_maturity') or 0)
                company = (row.get('company') or row.get('name') or 'N/A')[:22]

                print(f"{row.get('isin', 'N/A'):<12} | "
                      f"{(row.get('ticker') or 'N/A')[:8]:<8} | "
                      f"{company:<22} | "
                      f"{calculated_rate:<8.2f} | "
                      f"{spread:<6.2f} | "
                      f"{freq:<6} | "
                      f"{coupon_payment:<7.2f} | "
                      f"{monthly_coupon:<6.2f} | "
                      f"{market_price:<7.0f} | "
                      f"{coupon_yield:<9.2f} | "
                      f"{row.get('risk_level') or 'N/A':<4} | "
                      f"{months_to_maturity:<9.1f} | "
                      f"{row.get('held_badge') or '':<15}")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка при выводе строки: {e}")
                continue
        
        print(f"{'='*155}")

    def _print_comparison_table(self, rows: List[Dict[str, Any]]):
        """Выводит таблицу сравнения с портфелем."""
        print(f"\n{'='*120}")
        print(f"📊 Сравнение с текущим портфелем")
        print(f"{'='*120}")
        print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<25} | {'Купон %':<6} | {'Риск':<4} | {'Доходн. %':<9} | {'Разница':<7} | {'Рекомендация':<15} | {'Портфель':<15}")
        print("-" * 138)

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
                      f"{row['recommendation']:<15} | "
                      f"{row.get('held_badge') or '':<15}")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка при выводе строки: {e}")
                continue
        
        print(f"{'='*120}")
