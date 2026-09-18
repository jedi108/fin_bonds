"""
Use case для расчета ежемесячной прибыли по облигациям.
"""
import logging
import argparse
from typing import TYPE_CHECKING, List, Dict, Any
from decimal import Decimal
import sqlite3

from src.use_cases.base import UseCase
from src.monitoring.storage import Database

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

class CalculateMonthlyProfitUseCase(UseCase):
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument('--mode', type=str, choices=['detailed', 'summary', 'by-broker', 'by-risk', 'top-10'], 
                          default='summary', help='Режим отображения: detailed, summary, by-broker, by-risk, top-10')
        parser.add_argument('--limit', type=int, default=20, help='Максимальное количество результатов')
        parser.add_argument('--min-profit', type=float, default=0.0, help='Минимальная ежемесячная прибыль для фильтрации')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'CalculateMonthlyProfitUseCase':
        return cls(db=factory.get_db_connection())

    def execute(self, args: argparse.Namespace):
        logger.info(f"🚀 Запуск расчета ежемесячной прибыли в режиме: {args.mode}")
        try:
            if args.mode == 'detailed':
                self._show_detailed_profit(args)
            elif args.mode == 'summary':
                self._show_summary_profit(args)
            elif args.mode == 'by-broker':
                self._show_profit_by_broker(args)
            elif args.mode == 'by-risk':
                self._show_profit_by_risk(args)
            elif args.mode == 'top-10':
                self._show_top_profitable_bonds(args)
        except Exception as e:
            logger.exception(f"Ошибка при расчете ежемесячной прибыли: {e}")

    def _show_detailed_profit(self, args: argparse.Namespace):
        """Показывает детальную ежемесячную прибыль по каждой бумаге."""
        query = """
            SELECT 
                bc.isin,
                bc.ticker,
                bc.name,
                bc.nominal,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) as coupon_rate_percent,
                bc.coupon_quantity_per_year,
                bc.risk_level,
                pp.average_price,
                pp.current_price,
                pp.quantity,
                pp.broker_name,
                
                -- Купонный доход за месяц
                ROUND(
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity, 
                    2
                ) as monthly_coupon_income,
                
                -- Прибыль от изменения цены
                ROUND(
                    (pp.current_price - pp.average_price) * pp.quantity, 
                    2
                ) as price_change_profit,
                
                -- Общая ежемесячная прибыль
                ROUND(
                    ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                    ((pp.current_price - pp.average_price) * pp.quantity), 
                    2
                ) as total_monthly_profit,
                
                -- Процентная доходность (годовая)
                ROUND(
                    ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / pp.average_price) * 100, 
                    2
                ) as annual_yield_percent
                
            FROM bonds_catalog bc
            JOIN portfolio_positions pp ON bc.isin = pp.isin
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
                AND ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                    ((pp.current_price - pp.average_price) * pp.quantity) >= ?
            ORDER BY total_monthly_profit DESC
            LIMIT ?
        """
        
        with self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute(query, [args.min_profit, args.limit])
            rows = cursor.fetchall()
            
        if not rows:
            logger.info("ℹ️ Не найдено облигаций, соответствующих критериям.")
            return
            
        logger.info(f"📈 Найдено {len(rows)} облигаций для анализа")
        self._print_detailed_table(rows)

    def _show_summary_profit(self, args: argparse.Namespace):
        """Показывает суммарную ежемесячную прибыль по всем бумагам."""
        query = """
            SELECT 
                COUNT(DISTINCT bc.isin) as bonds_count,
                SUM(pp.quantity) as total_quantity,
                
                -- Общий купонный доход за месяц
                ROUND(
                    SUM((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity), 
                    2
                ) as total_monthly_coupon_income,
                
                -- Общая прибыль от изменения цены
                ROUND(
                    SUM((pp.current_price - pp.average_price) * pp.quantity), 
                    2
                ) as total_price_change_profit,
                
                -- Общая ежемесячная прибыль
                ROUND(
                    SUM(((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                        ((pp.current_price - pp.average_price) * pp.quantity)), 
                    2
                ) as total_monthly_profit,
                
                -- Средняя годовая доходность портфеля
                ROUND(
                    AVG((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / pp.average_price * 100), 
                    2
                ) as avg_annual_yield_percent
                
            FROM bonds_catalog bc
            JOIN portfolio_positions pp ON bc.isin = pp.isin
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
        """
        
        with self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute(query)
            row = cursor.fetchone()
            
        if not row:
            logger.info("ℹ️ Не найдено данных для анализа.")
            return
            
        self._print_summary_table(row)

    def _show_profit_by_broker(self, args: argparse.Namespace):
        """Показывает ежемесячную прибыль по брокерам."""
        query = """
            SELECT 
                pp.broker_name,
                COUNT(DISTINCT bc.isin) as bonds_count,
                
                -- Купонный доход за месяц
                ROUND(
                    SUM((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity), 
                    2
                ) as monthly_coupon_income,
                
                -- Прибыль от изменения цены
                ROUND(
                    SUM((pp.current_price - pp.average_price) * pp.quantity), 
                    2
                ) as price_change_profit,
                
                -- Общая ежемесячная прибыль
                ROUND(
                    SUM(((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                        ((pp.current_price - pp.average_price) * pp.quantity)), 
                    2
                ) as total_monthly_profit
                
            FROM bonds_catalog bc
            JOIN portfolio_positions pp ON bc.isin = pp.isin
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
            GROUP BY pp.broker_name
            ORDER BY total_monthly_profit DESC
        """
        
        with self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
            
        if not rows:
            logger.info("ℹ️ Не найдено данных по брокерам.")
            return
            
        self._print_broker_table(rows)

    def _show_profit_by_risk(self, args: argparse.Namespace):
        """Показывает ежемесячную прибыль по уровням риска."""
        query = """
            SELECT 
                bc.risk_level,
                COUNT(DISTINCT bc.isin) as bonds_count,
                
                -- Купонный доход за месяц
                ROUND(
                    SUM((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity), 
                    2
                ) as monthly_coupon_income,
                
                -- Прибыль от изменения цены
                ROUND(
                    SUM((pp.current_price - pp.average_price) * pp.quantity), 
                    2
                ) as price_change_profit,
                
                -- Общая ежемесячная прибыль
                ROUND(
                    SUM(((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                        ((pp.current_price - pp.average_price) * pp.quantity)), 
                    2
                ) as total_monthly_profit,
                
                -- Средняя годовая доходность
                ROUND(
                    AVG((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / pp.average_price * 100), 
                    2
                ) as avg_annual_yield_percent
                
            FROM bonds_catalog bc
            JOIN portfolio_positions pp ON bc.isin = pp.isin
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
            GROUP BY bc.risk_level
            ORDER BY bc.risk_level
        """
        
        with self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
            
        if not rows:
            logger.info("ℹ️ Не найдено данных по уровням риска.")
            return
            
        self._print_risk_table(rows)

    def _show_top_profitable_bonds(self, args: argparse.Namespace):
        """Показывает топ-10 облигаций по ежемесячной прибыли."""
        query = """
            SELECT 
                bc.isin,
                bc.ticker,
                bc.name,
                COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) as coupon_rate_percent,
                bc.risk_level,
                pp.quantity,
                pp.broker_name,
                
                -- Ежемесячная прибыль
                ROUND(
                    ((COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity) +
                    ((pp.current_price - pp.average_price) * pp.quantity), 
                    2
                ) as monthly_profit,
                
                -- Купонная часть
                ROUND(
                    (COALESCE(bc.coupon_rate_percent, mc_floater.metric_value) * bc.nominal / 100) / bc.coupon_quantity_per_year * pp.quantity, 
                    2
                ) as coupon_part,
                
                -- Прибыль от цены
                ROUND(
                    (pp.current_price - pp.average_price) * pp.quantity, 
                    2
                ) as price_part
                
            FROM bonds_catalog bc
            JOIN portfolio_positions pp ON bc.isin = pp.isin
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
            ORDER BY monthly_profit DESC
            LIMIT ?
        """
        
        with self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute(query, [args.limit])
            rows = cursor.fetchall()
            
        if not rows:
            logger.info("ℹ️ Не найдено облигаций для анализа.")
            return
            
        self._print_top_table(rows)

    def _print_detailed_table(self, rows: List[sqlite3.Row]):
        """Выводит детальную таблицу прибыли."""
        print(f"\n{'='*120}")
        print(f"📊 Детальная ежемесячная прибыль по облигациям")
        print(f"{'='*120}")
        print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<25} | {'Купон %':<6} | {'Риск':<4} | {'Кол-во':<6} | "
              f"{'Брокер':<6} | {'Купон':<8} | {'Цена':<8} | {'Итого':<8} | {'Доходн. %':<9}")
        print("-" * 120)
        
        for row in rows:
            try:
                coupon_rate = float(row['coupon_rate_percent']) if row['coupon_rate_percent'] else 0
                quantity = float(row['quantity']) if row['quantity'] else 0
                coupon_income = float(row['monthly_coupon_income']) if row['monthly_coupon_income'] else 0
                price_profit = float(row['price_change_profit']) if row['price_change_profit'] else 0
                total_profit = float(row['total_monthly_profit']) if row['total_monthly_profit'] else 0
                annual_yield = float(row['annual_yield_percent']) if row['annual_yield_percent'] else 0
                
                print(f"{row['isin']:<12} | "
                      f"{row['ticker'] or 'N/A':<8} | "
                      f"{row['name'][:25] if row['name'] else 'N/A':<25} | "
                      f"{coupon_rate:<6.1f} | "
                      f"{row['risk_level'] or 'N/A':<4} | "
                      f"{quantity:<6.0f} | "
                      f"{row['broker_name'] or 'N/A':<6} | "
                      f"{coupon_income:<8.0f} | "
                      f"{price_profit:<8.0f} | "
                      f"{total_profit:<8.0f} | "
                      f"{annual_yield:<9.1f}")
            except Exception as e:
                logger.error(f"Ошибка при форматировании строки: {e}")
                continue
                
        print(f"{'='*120}")
        print(f"💡 Купон - купонный доход за месяц, Цена - прибыль от изменения цены, Итого - общая прибыль")
        print(f"{'='*120}\n")

    def _print_summary_table(self, row: sqlite3.Row):
        """Выводит сводную таблицу прибыли."""
        print(f"\n{'='*80}")
        print(f"📊 Сводка ежемесячной прибыли портфеля")
        print(f"{'='*80}")
        
        bonds_count = int(row['bonds_count']) if row['bonds_count'] else 0
        total_quantity = float(row['total_quantity']) if row['total_quantity'] else 0
        coupon_income = float(row['total_monthly_coupon_income']) if row['total_monthly_coupon_income'] else 0
        price_profit = float(row['total_price_change_profit']) if row['total_price_change_profit'] else 0
        total_profit = float(row['total_monthly_profit']) if row['total_monthly_profit'] else 0
        avg_yield = float(row['avg_annual_yield_percent']) if row['avg_annual_yield_percent'] else 0
        
        print(f"📈 Количество облигаций: {bonds_count}")
        print(f"📦 Общее количество: {total_quantity:.0f}")
        print(f"💰 Купонный доход за месяц: {coupon_income:,.0f} ₽")
        print(f"📊 Прибыль от изменения цены: {price_profit:,.0f} ₽")
        print(f"🎯 Общая ежемесячная прибыль: {total_profit:,.0f} ₽")
        print(f"📈 Средняя годовая доходность: {avg_yield:.1f}%")
        print(f"💡 Ежемесячная доходность: {avg_yield/12:.1f}%")
        print(f"{'='*80}\n")

    def _print_broker_table(self, rows: List[sqlite3.Row]):
        """Выводит таблицу прибыли по брокерам."""
        print(f"\n{'='*80}")
        print(f"📊 Ежемесячная прибыль по брокерам")
        print(f"{'='*80}")
        print(f"{'Брокер':<8} | {'Облигаций':<9} | {'Купон':<8} | {'Цена':<8} | {'Итого':<8}")
        print("-" * 80)
        
        for row in rows:
            try:
                bonds_count = int(row['bonds_count']) if row['bonds_count'] else 0
                coupon_income = float(row['monthly_coupon_income']) if row['monthly_coupon_income'] else 0
                price_profit = float(row['price_change_profit']) if row['price_change_profit'] else 0
                total_profit = float(row['total_monthly_profit']) if row['total_monthly_profit'] else 0
                
                print(f"{row['broker_name'] or 'N/A':<8} | "
                      f"{bonds_count:<9} | "
                      f"{coupon_income:<8.0f} | "
                      f"{price_profit:<8.0f} | "
                      f"{total_profit:<8.0f}")
            except Exception as e:
                logger.error(f"Ошибка при форматировании строки: {e}")
                continue
                
        print(f"{'='*80}\n")

    def _print_risk_table(self, rows: List[sqlite3.Row]):
        """Выводит таблицу прибыли по уровням риска."""
        print(f"\n{'='*90}")
        print(f"📊 Ежемесячная прибыль по уровням риска")
        print(f"{'='*90}")
        print(f"{'Риск':<4} | {'Облигаций':<9} | {'Купон':<8} | {'Цена':<8} | {'Итого':<8} | {'Доходн. %':<9}")
        print("-" * 90)
        
        for row in rows:
            try:
                bonds_count = int(row['bonds_count']) if row['bonds_count'] else 0
                coupon_income = float(row['monthly_coupon_income']) if row['monthly_coupon_income'] else 0
                price_profit = float(row['price_change_profit']) if row['price_change_profit'] else 0
                total_profit = float(row['total_monthly_profit']) if row['total_monthly_profit'] else 0
                avg_yield = float(row['avg_annual_yield_percent']) if row['avg_annual_yield_percent'] else 0
                
                print(f"{row['risk_level'] or 'N/A':<4} | "
                      f"{bonds_count:<9} | "
                      f"{coupon_income:<8.0f} | "
                      f"{price_profit:<8.0f} | "
                      f"{total_profit:<8.0f} | "
                      f"{avg_yield:<9.1f}")
            except Exception as e:
                logger.error(f"Ошибка при форматировании строки: {e}")
                continue
                
        print(f"{'='*90}\n")

    def _print_top_table(self, rows: List[sqlite3.Row]):
        """Выводит таблицу топ-облигаций."""
        print(f"\n{'='*100}")
        print(f"📊 Топ-{len(rows)} облигаций по ежемесячной прибыли")
        print(f"{'='*100}")
        print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<20} | {'Купон %':<6} | {'Риск':<4} | {'Кол-во':<6} | "
              f"{'Брокер':<6} | {'Купон':<8} | {'Цена':<8} | {'Итого':<8}")
        print("-" * 100)
        
        for row in rows:
            try:
                coupon_rate = float(row['coupon_rate_percent']) if row['coupon_rate_percent'] else 0
                quantity = float(row['quantity']) if row['quantity'] else 0
                coupon_part = float(row['coupon_part']) if row['coupon_part'] else 0
                price_part = float(row['price_part']) if row['price_part'] else 0
                monthly_profit = float(row['monthly_profit']) if row['monthly_profit'] else 0
                
                print(f"{row['isin']:<12} | "
                      f"{row['ticker'] or 'N/A':<8} | "
                      f"{row['name'][:20] if row['name'] else 'N/A':<20} | "
                      f"{coupon_rate:<6.1f} | "
                      f"{row['risk_level'] or 'N/A':<4} | "
                      f"{quantity:<6.0f} | "
                      f"{row['broker_name'] or 'N/A':<6} | "
                      f"{coupon_part:<8.0f} | "
                      f"{price_part:<8.0f} | "
                      f"{monthly_profit:<8.0f}")
            except Exception as e:
                logger.error(f"Ошибка при форматировании строки: {e}")
                continue
                
        print(f"{'='*100}\n")
