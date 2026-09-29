"""
Use case для расчета и отображения YTM (доходности к погашению) облигаций.
"""
import argparse
from datetime import date, datetime
from decimal import Decimal
import logging
from typing import TYPE_CHECKING, List, Dict, Any, Optional

from src.services.cashflow import calculate_bond_cashflow_metrics
from src.storage import PortfolioStorage
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class CalculateYtmUseCase(UseCase):
    """
    Рассчитывает и отображает YTM (доходность к погашению) для облигаций.
    Поддерживает два режима:
    1. buy: кандидаты на покупку по рыночной цене каталога (mark-to-market)
    2. portfolio: позиции портфеля по рыночной цене каталога (mark-to-market),
       при этом средняя цена покупки остаётся справочной колонкой.
    """

    def __init__(self, db: PortfolioStorage):
        self.db = db

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды расчета YTM."""
        parser.add_argument('--mode', type=str, choices=['buy', 'portfolio'], required=True,
                           help='Режим анализа: buy - для покупки, portfolio - уже купленные')
        parser.add_argument('--min-ytm', type=float, default=None,
                           help='Минимальная YTM для фильтрации (в процентах)')
        parser.add_argument('--max-risk', type=int, default=3,
                           help='Максимальный уровень риска (1-5)')
        parser.add_argument('--min-maturity', type=str, default='2025-01-01',
                           help='Минимальная дата погашения (YYYY-MM-DD)')
        parser.add_argument('--max-maturity', type=str, default='2030-12-31',
                           help='Максимальная дата погашения (YYYY-MM-DD)')
        parser.add_argument('--no-amortization', action='store_true',
                           help='Исключить облигации с амортизацией')
        parser.add_argument('--monthly-coupons', action='store_true',
                           help='Только облигации с ежемесячными купонами (12 раз в год)')
        parser.add_argument('--fixed-coupon', action='store_true',
                           help='Только облигации с фиксированным купоном')
        parser.add_argument('--floating-coupon', action='store_true',
                           help='Только облигации с плавающим купоном')
        parser.add_argument('--limit', type=int, default=50,
                           help='Максимальное количество результатов')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'CalculateYtmUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(db=factory.get_db_connection())

    def execute(self, args: argparse.Namespace):
        logger.info(f"🚀 Запуск расчета YTM в режиме: {args.mode}")

        try:
            if args.mode == 'buy':
                self._analyze_bonds_for_buying(args)
            elif args.mode == 'portfolio':
                self._analyze_portfolio_bonds(args)

        except Exception as e:
            logger.exception(f"Ошибка при расчете YTM: {e}")

    def _analyze_bonds_for_buying(self, args: argparse.Namespace):
        """Анализ облигаций для покупки по текущей рыночной цене."""
        logger.info("📊 Анализ облигаций для покупки...")

        bonds = self._get_bonds_for_buying(args)
        if not bonds:
            logger.info("ℹ️ Не найдено облигаций, соответствующих критериям.")
            return

        logger.info(f"📈 Найдено {len(bonds)} облигаций для анализа")
        self._print_bonds_table(bonds, "Облигации для покупки (по текущей цене)")

    def _analyze_portfolio_bonds(self, args: argparse.Namespace):
        """Анализ уже купленных облигаций по средней цене покупки."""
        logger.info("💼 Анализ облигаций в портфеле...")

        bonds = self._get_portfolio_bonds(args)
        if not bonds:
            logger.info("ℹ️ В портфеле нет облигаций, соответствующих критериям.")
            return

        logger.info(f"📈 Найдено {len(bonds)} облигаций в портфеле")
        self._print_bonds_table(bonds, "Облигации в портфеле (mark-to-market YTM)")

    def _enrich_bonds_with_ytm(self, bonds: List[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
        """
        Обогащает строки канонической YTM:
        - Если ytm_updated_at задан: берёт сохранённый bc.ytm или bc.ytm_null_reason.
        - Если ytm_updated_at IS NULL (до backfill): временный расчёт через общий cashflow-решатель.
        - Переводит долю единицы в проценты (100 * y).
        - Фильтрует по args.min_ytm без приведения NULL к 0.
        """
        enriched = []
        for bond in bonds:
            if bond.get('ytm_updated_at') is not None:
                if bond.get('ytm') is not None:
                    bond['ytm_percent'] = float(bond['ytm']) * 100.0
                    bond['ytm_reason'] = None
                else:
                    bond['ytm_percent'] = None
                    bond['ytm_reason'] = bond.get('ytm_null_reason') or 'not_applicable'
            else:
                # Временная совместимость до выполнения общего backfill
                price = bond.get('market_price') or bond.get('current_price')
                metrics = calculate_bond_cashflow_metrics(
                    valuation_date=date.today(),
                    nominal=bond.get('nominal'),
                    maturity_date=bond.get('maturity_date'),
                    coupon_quantity_per_year=bond.get('coupon_quantity_per_year'),
                    coupon_rate_percent=bond.get('coupon_rate_percent'),
                    market_price=price,
                    perpetual_flag=bond.get('perpetual_flag'),
                    floating_coupon_flag=bond.get('floating_coupon_flag'),
                    amortization_flag=bond.get('amortization_flag'),
                )
                if metrics.ytm is not None:
                    bond['ytm_percent'] = float(metrics.ytm) * 100.0
                    bond['ytm_reason'] = None
                else:
                    bond['ytm_percent'] = None
                    bond['ytm_reason'] = metrics.null_reason

            if args.min_ytm is not None:
                if bond['ytm_percent'] is None or bond['ytm_percent'] < args.min_ytm:
                    continue

            enriched.append(bond)
        return enriched

    def _get_bonds_for_buying(self, args: argparse.Namespace) -> List[Dict[str, Any]]:
        """Получает облигации для покупки с канонической YTM по рыночной цене."""
        bonds = self.db.get_bonds_yield_table(
            mode='buy',
            min_maturity=args.min_maturity,
            max_maturity=args.max_maturity,
            max_risk=args.max_risk,
            no_amortization=args.no_amortization,
            monthly_coupons=args.monthly_coupons,
            fixed_coupon=args.fixed_coupon,
            floating_coupon=args.floating_coupon,
            limit=args.limit,
        )
        return self._enrich_bonds_with_ytm(bonds, args)

    def _get_portfolio_bonds(self, args: argparse.Namespace) -> List[Dict[str, Any]]:
        """Получает облигации из портфеля с канонической mark-to-market YTM."""
        bonds = self.db.get_bonds_yield_table(
            mode='portfolio',
            min_maturity=args.min_maturity,
            max_maturity=args.max_maturity,
            max_risk=args.max_risk,
            no_amortization=args.no_amortization,
            monthly_coupons=args.monthly_coupons,
            fixed_coupon=args.fixed_coupon,
            floating_coupon=args.floating_coupon,
            limit=args.limit,
        )
        return self._enrich_bonds_with_ytm(bonds, args)

    def _print_bonds_table(self, bonds: List[Dict[str, Any]], title: str):
        """Выводит таблицу с облигациями в консоль."""
        if not bonds:
            return

        print(f"\n{'='*80}")
        print(f"📊 {title}")
        print(f"{'='*80}")

        if 'average_price' in bonds[0]:
            # Режим портфеля
            print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<26} | {'Номинал':<7} | {'Купон %':<6} | "
                  f"{'Частота':<7} | {'Погашение':<10} | {'Риск':<4} | {'Лист':<4} | {'Амортиз.':<8} | "
                  f"{'Ср.цена':<8} | {'Тек.цена':<8} | {'Кол-во':<6} | {'YTM %':<24} | {'Измен.цены %':<11} | {'Купон/мес':<10} | {'Тип купона':<12}")
            print("-" * 180)

            for i, bond in enumerate(bonds):
                try:
                    nominal = float(bond['nominal']) if bond.get('nominal') else 0
                    coupon_rate = float(bond['coupon_rate_percent']) if bond.get('coupon_rate_percent') else 0
                    coupon_freq = int(float(bond['coupon_quantity_per_year'])) if bond.get('coupon_quantity_per_year') else 0
                    avg_price = float(bond['average_price']) if bond.get('average_price') else 0
                    curr_price = float(bond.get('market_price') or bond.get('current_price') or 0)
                    quantity = float(bond['quantity']) if bond.get('quantity') else 0
                    price_change = float(bond['price_change_percent']) if bond.get('price_change_percent') else 0
                    total_monthly_coupon = float(bond['total_monthly_coupon_payment']) if bond.get('total_monthly_coupon_payment') else 0

                    ytm = bond.get('ytm_percent')
                    if ytm is not None:
                        ytm_str = f"{ytm:.2f}%"
                    else:
                        reason = bond.get('ytm_reason') or 'Н/Д'
                        ytm_str = f"— ({reason})"

                    print(f"{bond['isin']:<12} | "
                          f"{bond['ticker'] or 'N/A':<8} | "
                          f"{(bond['name'] or 'N/A')[:26]:<26} | "
                          f"{nominal:<7.0f} | "
                          f"{coupon_rate:<6.1f} | "
                          f"{coupon_freq:<7} | "
                          f"{str(bond['maturity_date']) if bond.get('maturity_date') else 'N/A':<10} | "
                          f"{bond['risk_level'] if bond.get('risk_level') is not None else 'N/A':<4} | "
                          f"{bond['list_level'] if bond.get('list_level') is not None else 'N/A':<4} | "
                          f"{'Да' if bond.get('amortization_flag') else 'Нет':<8} | "
                          f"{avg_price:<8.2f} | "
                          f"{curr_price:<8.2f} | "
                          f"{quantity:<6.0f} | "
                          f"{ytm_str:<24} | "
                          f"{price_change:<11.2f} | "
                          f"{total_monthly_coupon:<10.0f} | "
                          f"{bond.get('coupon_type', 'N/A'):<12}")
                except Exception as e:
                    print(f"Ошибка при форматировании строки {i}: {e}")
                    print(f"Данные: {bond}")
                    continue
        else:
            # Режим покупки
            print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<26} | {'Номинал':<7} | {'Купон %':<6} | "
                  f"{'Частота':<7} | {'Погашение':<10} | {'Риск':<4} | {'Лист':<4} | {'Амортиз.':<8} | "
                  f"{'Тек.цена':<8} | {'YTM %':<24} | {'Купон/мес':<10} | {'Тип купона':<12}")
            print("-" * 155)

            for bond in bonds:
                try:
                    nominal = float(bond['nominal']) if bond.get('nominal') else 0
                    coupon_rate = float(bond['coupon_rate_percent']) if bond.get('coupon_rate_percent') else 0
                    coupon_freq = int(float(bond['coupon_quantity_per_year'])) if bond.get('coupon_quantity_per_year') else 0
                    curr_price = float(bond.get('market_price') or bond.get('current_price') or 0)
                    total_monthly_coupon = float(bond['total_monthly_coupon_payment']) if bond.get('total_monthly_coupon_payment') else 0

                    ytm = bond.get('ytm_percent')
                    if ytm is not None:
                        ytm_str = f"{ytm:.2f}%"
                    else:
                        reason = bond.get('ytm_reason') or 'Н/Д'
                        ytm_str = f"— ({reason})"

                    print(f"{bond['isin']:<12} | "
                          f"{bond['ticker'] or 'N/A':<8} | "
                          f"{(bond['name'] or 'N/A')[:26]:<26} | "
                          f"{nominal:<7.0f} | "
                          f"{coupon_rate:<6.1f} | "
                          f"{coupon_freq:<7} | "
                          f"{str(bond['maturity_date']) if bond.get('maturity_date') else 'N/A':<10} | "
                          f"{bond['risk_level'] if bond.get('risk_level') is not None else 'N/A':<4} | "
                          f"{bond['list_level'] if bond.get('list_level') is not None else 'N/A':<4} | "
                          f"{'Да' if bond.get('amortization_flag') else 'Нет':<8} | "
                          f"{curr_price:<8.2f} | "
                          f"{ytm_str:<24} | "
                          f"{total_monthly_coupon:<10.0f} | "
                          f"{bond.get('coupon_type', 'N/A'):<12}")
                except Exception as e:
                    print(f"Ошибка при форматировании строки: {e}")
                    print(f"Данные: {bond}")
                    continue

        print(f"{'='*80}")
        print(f"📊 Всего облигаций: {len(bonds)}")
        print(f"💡 YTM % — каноническая доходность к погашению (mark-to-market по рыночной чистой цене).")
        print(f"💡 Для флоатеров, бессрочных и амортизируемых бумаг отображается «— (причина)» без упрощенных формул.")
        if 'average_price' in bonds[0]:
            print(f"💡 Ср.цена — средняя цена покупки портфеля (справочная колонка, не подменяет YTM).")
        print(f"{'='*80}\n")
