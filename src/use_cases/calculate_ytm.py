"""
Use case для расчета и отображения YTM (доходности к погашению) облигаций.
"""
import logging
import argparse
from typing import TYPE_CHECKING, List, Dict, Any, Optional
from decimal import Decimal
from datetime import date, datetime
import sqlite3
import math

from src.use_cases.base import UseCase
from src.monitoring.storage import Database

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class CalculateYtmUseCase(UseCase):
    """
    Рассчитывает и отображает YTM (доходность к погашению) для облигаций.
    Поддерживает два режима:
    1. Анализ облигаций для покупки (по текущей рыночной цене)
    2. Анализ уже купленных облигаций (по средней цене покупки)
    """
    
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды расчета YTM."""
        parser.add_argument('--mode', type=str, choices=['buy', 'portfolio'], required=True,
                           help='Режим анализа: buy - для покупки, portfolio - уже купленные')
        parser.add_argument('--min-ytm', type=float, default=0.0,
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
        
        # Выводим результаты в виде таблицы
        self._print_bonds_table(bonds, "Облигации для покупки (по текущей цене)")

    def _analyze_portfolio_bonds(self, args: argparse.Namespace):
        """Анализ уже купленных облигаций по средней цене покупки."""
        logger.info("💼 Анализ облигаций в портфеле...")
        
        bonds = self._get_portfolio_bonds(args)
        if not bonds:
            logger.info("ℹ️ В портфеле нет облигаций, соответствующих критериям.")
            return
            
        logger.info(f"📈 Найдено {len(bonds)} облигаций в портфеле")
        
        # Выводим результаты в виде таблицы
        self._print_bonds_table(bonds, "Облигации в портфеле (по средней цене покупки)")

    def _get_bonds_for_buying(self, args: argparse.Namespace) -> List[Dict[str, Any]]:
        """Получает облигации для покупки с расчетом YTM по текущей цене."""
        query = """
            SELECT 
                bc.isin,
                bc.ticker,
                bc.name,
                bc.nominal,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) as coupon_rate_percent,
                bc.coupon_quantity_per_year,
                bc.maturity_date,
                bc.risk_level,
                bc.list_level,
                bc.amortization_flag,
                COALESCE(bc.market_price, bc.nominal) as current_price,
                CASE 
                    WHEN COALESCE(bc.market_price, bc.nominal) IS NOT NULL AND COALESCE(bc.market_price, bc.nominal) > 0 
                    THEN (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / COALESCE(bc.market_price, bc.nominal) * 100
                    ELSE NULL 
                END as real_yield_percent,
                -- Общая сумма купонных выплат в месяц по всем бумагам в строке
                ROUND(
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * 1, 
                    2
                ) as total_monthly_coupon_payment,
                -- Тип купона (фиксированный/плавающий)
                CASE 
                    WHEN bc.floating_coupon_flag = 1 THEN 'Плавающий'
                    ELSE 'Фиксированный'
                END as coupon_type
            FROM bonds_catalog bc
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
            WHERE bc.currency = 'rub'
                AND (bc.is_trade_available = 1 OR bc.is_trade_available IS NULL)
                AND bc.perpetual_flag = 0
                AND bc.maturity_date >= ?
                AND bc.maturity_date <= ?
                AND bc.risk_level <= ?
                AND COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) IS NOT NULL
                AND bc.isin NOT IN (SELECT isin FROM portfolio_positions)
        """
        
        params = [args.min_maturity, args.max_maturity, args.max_risk]
        
        if args.no_amortization:
            query += " AND bc.amortization_flag = 0"
            
        if args.monthly_coupons:
            query += " AND bc.coupon_quantity_per_year = 12"
            
        if args.fixed_coupon:
            query += " AND bc.floating_coupon_flag = 0"
            
        if args.floating_coupon:
            query += " AND bc.floating_coupon_flag = 1"
            
        query += " ORDER BY real_yield_percent DESC NULLS LAST LIMIT ?"
        params.append(args.limit)
        
        with self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            bonds = [dict(row) for row in rows]
            
            # Добавляем расчет YTM для каждой облигации
            for bond in bonds:
                try:
                    nominal = float(bond['nominal'])
                    coupon_rate = float(bond['coupon_rate_percent'])
                    market_price = float(bond['current_price'])
                    maturity_date = bond['maturity_date']
                    coupon_frequency = int(bond['coupon_quantity_per_year'])
                    amortization_flag = bool(bond['amortization_flag'])
                    
                    ytm = self._calculate_ytm(
                        nominal, coupon_rate, market_price, 
                        maturity_date, coupon_frequency, amortization_flag
                    )
                    bond['ytm_percent'] = ytm
                    
                except Exception as e:
                    logger.error(f"Ошибка при расчете YTM для {bond.get('isin', 'Unknown')}: {e}")
                    bond['ytm_percent'] = None
            
            return bonds

    def _calculate_ytm(self, nominal: float, coupon_rate: float, market_price: float, 
                      maturity_date: str, coupon_frequency: int, amortization_flag: bool) -> Optional[float]:
        """
        Рассчитывает YTM (доходность к погашению) для облигации.
        
        Args:
            nominal: Номинал облигации
            coupon_rate: Годовая купонная ставка (%)
            market_price: Текущая рыночная цена
            maturity_date: Дата погашения (YYYY-MM-DD)
            coupon_frequency: Частота купонных выплат в год
            amortization_flag: Флаг наличия амортизации
            
        Returns:
            YTM в процентах или None если не удалось рассчитать
        """
        try:
            # Рассчитываем время до погашения в годах
            maturity = datetime.strptime(maturity_date, '%Y-%m-%d').date()
            today = date.today()
            years_to_maturity = (maturity - today).days / 365.25
            
            if years_to_maturity <= 0:
                return None
            
            # Годовой купон в рублях
            annual_coupon = nominal * coupon_rate / 100
            
            # Для простоты пока используем упрощенную формулу YTM
            # В будущем можно реализовать итеративный метод Ньютона-Рафсона
            
            if amortization_flag:
                # Для облигаций с амортизацией используем упрощенную формулу
                # Учитываем амортизацию и время до погашения
                current_yield = annual_coupon / market_price
                capital_gain = (nominal - market_price) / years_to_maturity
                
                # Корректируем на амортизацию (уменьшаем доходность)
                amortization_adjustment = 0.25  # 25% снижение из-за амортизации
                ytm = (current_yield + (capital_gain / market_price)) * (1 - amortization_adjustment)
            else:
                # Для обычных облигаций используем формулу текущей доходности + прирост капитала
                capital_gain = (nominal - market_price) / years_to_maturity
                current_yield = annual_coupon / market_price
                ytm = current_yield + (capital_gain / market_price)
            
            return ytm * 100  # Конвертируем в проценты
            
        except Exception as e:
            logger.error(f"Ошибка при расчете YTM: {e}")
            return None

    def _calculate_bond_price(self, nominal: float, coupon_rate: float, years_to_maturity: float,
                             coupon_frequency: int, ytm_rate: float, amortization_flag: bool) -> float:
        """
        Рассчитывает цену облигации при заданном YTM.
        
        Args:
            nominal: Номинал облигации
            coupon_rate: Годовая купонная ставка (%)
            years_to_maturity: Время до погашения в годах
            coupon_frequency: Частота купонных выплат в год
            ytm_rate: YTM в десятичном виде (например, 0.15 для 15%)
            amortization_flag: Флаг наличия амортизации
            
        Returns:
            Рассчитанная цена облигации
        """
        try:
            if amortization_flag:
                # Для облигаций с амортизацией
                # Предполагаем линейную амортизацию
                periods = int(years_to_maturity * coupon_frequency)
                price = 0
                remaining_nominal = nominal
                amortization_per_period = (nominal - nominal * 0.8) / periods  # Амортизация до 80% от номинала
                
                for i in range(periods):
                    period_rate = ytm_rate / coupon_frequency
                    coupon_payment = (remaining_nominal * coupon_rate / 100) / coupon_frequency
                    amortization_payment = amortization_per_period
                    total_payment = coupon_payment + amortization_payment
                    
                    price += total_payment / ((1 + period_rate) ** (i + 1))
                    remaining_nominal -= amortization_payment
                
                # Добавляем финальный платеж
                price += remaining_nominal / ((1 + ytm_rate / coupon_frequency) ** periods)
                
            else:
                # Для обычных облигаций
                periods = int(years_to_maturity * coupon_frequency)
                coupon_payment = (nominal * coupon_rate / 100) / coupon_frequency
                period_rate = ytm_rate / coupon_frequency
                
                # Текущая стоимость купонных платежей
                coupon_pv = coupon_payment * (1 - (1 + period_rate) ** (-periods)) / period_rate
                
                # Текущая стоимость номинала
                nominal_pv = nominal / ((1 + period_rate) ** periods)
                
                price = coupon_pv + nominal_pv
            
            return price
            
        except Exception as e:
            logger.error(f"Ошибка при расчете цены облигации: {e}")
            return 0

    def _get_portfolio_bonds(self, args: argparse.Namespace) -> List[Dict[str, Any]]:
        """Получает облигации из портфеля с расчетом YTM по средней цене покупки."""
        query = """
            SELECT 
                pp.isin,
                bc.ticker,
                bc.name,
                bc.nominal,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) as coupon_rate_percent,
                bc.coupon_quantity_per_year,
                bc.maturity_date,
                bc.risk_level,
                bc.list_level,
                bc.amortization_flag,
                pp.average_price,
                pp.current_price,
                pp.quantity,
                CASE 
                    WHEN pp.average_price IS NOT NULL AND pp.average_price > 0 
                    THEN (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / pp.average_price * 100
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
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity, 
                    2
                ) as total_monthly_coupon_payment,
                -- Тип купона (фиксированный/плавающий)
                CASE 
                    WHEN bc.floating_coupon_flag = 1 THEN 'Плавающий'
                    ELSE 'Фиксированный'
                END as coupon_type
            FROM portfolio_positions pp
            JOIN bonds_catalog bc ON pp.isin = bc.isin
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
            WHERE bc.currency = 'rub'
                AND pp.current_price > 0
                AND pp.quantity > 0
                AND bc.maturity_date >= ?
                AND bc.maturity_date <= ?
                AND bc.risk_level <= ?
                AND COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) IS NOT NULL
        """
        
        params = [args.min_maturity, args.max_maturity, args.max_risk]
        
        if args.no_amortization:
            query += " AND bc.amortization_flag = 0"
            
        if args.monthly_coupons:
            query += " AND bc.coupon_quantity_per_year = 12"
            
        if args.fixed_coupon:
            query += " AND bc.floating_coupon_flag = 0"
            
        if args.floating_coupon:
            query += " AND bc.floating_coupon_flag = 1"
            
        query += " ORDER BY real_yield_percent DESC NULLS LAST LIMIT ?"
        params.append(args.limit)
        
        with self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            return [dict(row) for row in rows]

    def _print_bonds_table(self, bonds: List[Dict[str, Any]], title: str):
        """Выводит таблицу с облигациями в консоль."""
        if not bonds:
            return
            
        print(f"\n{'='*80}")
        print(f"📊 {title}")
        print(f"{'='*80}")
        
        # Определяем заголовки в зависимости от режима
        if 'average_price' in bonds[0]:
            # Режим портфеля
            headers = [
                "ISIN", "Тикер", "Название", "Номинал", "Купон %", 
                "Частота", "Погашение", "Риск", "Лист", "Амортиз.",
                "Ср.цена", "Тек.цена", "Кол-во", "Доходн. %", "Измен.цены %", "Купон/мес", "Тип купона"
            ]
            
            # Выводим заголовки
            print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<30} | {'Номинал':<7} | {'Купон %':<6} | "
                  f"{'Частота':<7} | {'Погашение':<10} | {'Риск':<4} | {'Лист':<4} | {'Амортиз.':<8} | "
                  f"{'Ср.цена':<8} | {'Тек.цена':<8} | {'Кол-во':<6} | {'Доходн. %':<9} | {'Измен.цены %':<11} | {'Купон/мес':<10} | {'Тип купона':<12}")
            print("-" * 172)
            
            for i, bond in enumerate(bonds):
                try:
                    # Преобразуем строковые значения в числа
                    nominal = float(bond['nominal']) if bond['nominal'] else 0
                    coupon_rate = float(bond['coupon_rate_percent']) if bond['coupon_rate_percent'] else 0
                    coupon_freq = int(float(bond['coupon_quantity_per_year'])) if bond['coupon_quantity_per_year'] else 0
                    avg_price = float(bond['average_price']) if bond['average_price'] else 0
                    curr_price = float(bond['current_price']) if bond['current_price'] else 0
                    quantity = float(bond['quantity']) if bond['quantity'] else 0
                    real_yield = float(bond['real_yield_percent']) if bond['real_yield_percent'] else 0
                    price_change = float(bond['price_change_percent']) if bond['price_change_percent'] else 0
                    total_monthly_coupon = float(bond['total_monthly_coupon_payment']) if bond['total_monthly_coupon_payment'] else 0
                    
                    print(f"{bond['isin']:<12} | "
                          f"{bond['ticker'] or 'N/A':<8} | "
                          f"{bond['name'][:30] if bond['name'] else 'N/A':<30} | "
                          f"{nominal:<7.0f} | "
                          f"{coupon_rate:<6.1f} | "
                          f"{coupon_freq:<7} | "
                          f"{bond['maturity_date'] or 'N/A':<10} | "
                          f"{bond['risk_level'] or 'N/A':<4} | "
                          f"{bond['list_level'] or 'N/A':<4} | "
                          f"{'Да' if bond['amortization_flag'] else 'Нет':<8} | "
                          f"{avg_price:<8.2f} | "
                          f"{curr_price:<8.2f} | "
                          f"{quantity:<6.0f} | "
                          f"{real_yield:<9.2f} | "
                          f"{price_change:<11.2f} | "
                          f"{total_monthly_coupon:<10.0f} | "
                          f"{bond.get('coupon_type', 'N/A'):<12}")
                except Exception as e:
                    print(f"Ошибка при форматировании строки {i}: {e}")
                    print(f"Данные: {bond}")
                    continue
        else:
            # Режим покупки
            headers = [
                "ISIN", "Тикер", "Название", "Номинал", "Купон %", 
                "Частота", "Погашение", "Риск", "Лист", "Амортиз.",
                "Тек.цена", "Доходн. %", "YTM %", "Купон/мес", "Тип купона"
            ]
            
            # Выводим заголовки
            print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<30} | {'Номинал':<7} | {'Купон %':<6} | "
                  f"{'Частота':<7} | {'Погашение':<10} | {'Риск':<4} | {'Лист':<4} | {'Амортиз.':<8} | "
                  f"{'Тек.цена':<8} | {'Доходн. %':<9} | {'YTM %':<7} | {'Купон/мес':<10} | {'Тип купона':<12}")
            print("-" * 149)
            
            for bond in bonds:
                try:
                    # Преобразуем строковые значения в числа
                    nominal = float(bond['nominal']) if bond['nominal'] else 0
                    coupon_rate = float(bond['coupon_rate_percent']) if bond['coupon_rate_percent'] else 0
                    coupon_freq = int(float(bond['coupon_quantity_per_year'])) if bond['coupon_quantity_per_year'] else 0
                    curr_price = float(bond['current_price']) if bond['current_price'] else 0
                    real_yield = float(bond['real_yield_percent']) if bond['real_yield_percent'] else 0
                    total_monthly_coupon = float(bond['total_monthly_coupon_payment']) if bond['total_monthly_coupon_payment'] else 0
                    
                    # Получаем YTM
                    ytm = bond.get('ytm_percent')
                    ytm_str = f"{ytm:.2f}" if ytm is not None else "N/A"
                    
                    print(f"{bond['isin']:<12} | "
                          f"{bond['ticker'] or 'N/A':<8} | "
                          f"{bond['name'][:30] if bond['name'] else 'N/A':<30} | "
                          f"{nominal:<7.0f} | "
                          f"{coupon_rate:<6.1f} | "
                          f"{coupon_freq:<7} | "
                          f"{bond['maturity_date'] or 'N/A':<10} | "
                          f"{bond['risk_level'] or 'N/A':<4} | "
                          f"{bond['list_level'] or 'N/A':<4} | "
                          f"{'Да' if bond['amortization_flag'] else 'Нет':<8} | "
                          f"{curr_price:<8.2f} | "
                          f"{real_yield:<9.2f} | "
                          f"{ytm_str:<7} | "
                          f"{total_monthly_coupon:<10.0f} | "
                          f"{bond.get('coupon_type', 'N/A'):<12}")
                except Exception as e:
                    print(f"Ошибка при форматировании строки: {e}")
                    print(f"Данные: {bond}")
                    continue
        
        print(f"{'='*80}")
        print(f"�� Всего облигаций: {len(bonds)}")
        print(f"💡 Доходн. % = (Купонная ставка × Номинал) / Цена покупки × 100%")
        print(f"💡 YTM % = Доходность к погашению с учетом времени и амортизации")
        print(f"{'='*80}\n")
