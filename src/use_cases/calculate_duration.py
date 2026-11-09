"""
Use Case для расчёта дюрации позиций и портфеля облигаций (004_duration_metric).
"""

import argparse
from collections import defaultdict
from decimal import Decimal
import logging
from typing import List, Dict, Any, Optional

from .base import UseCase

logger = logging.getLogger(__name__)


class CalculateDurationUseCase(UseCase):
    """Рассчитывает дюрацию Маколея и модифицированную дюрацию для облигаций и портфеля."""

    def __init__(self, factory):
        super().__init__(factory)

    @classmethod
    def create(cls, factory) -> 'CalculateDurationUseCase':
        return cls(factory)

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--mode',
            type=str,
            default='portfolio',
            choices=['portfolio', 'bond'],
            help='Режим расчёта: portfolio (портфель) или bond (одна бумага).'
        )
        parser.add_argument(
            '--isin',
            type=str,
            default=None,
            help='ISIN облигации (обязателен при --mode bond).'
        )
        parser.add_argument(
            '--recalculate',
            action='store_true',
            help='Принудительно пересчитать дюрацию в базе данных перед выводом.'
        )

    def execute(self, args: argparse.Namespace) -> None:
        if args.recalculate:
            logger.info("🔄 Пересчёт дюраций в каталоге облигаций...")
            if args.mode == 'bond' and args.isin:
                count = self.storage.update_bonds_duration([args.isin])
            else:
                count = self.storage.update_bonds_duration()
            logger.info(f"✅ Пересчитано дюраций для {count} бумаг.")

        if args.mode == 'bond':
            self._handle_bond_mode(args)
        else:
            self._handle_portfolio_mode(args)

    def _handle_bond_mode(self, args: argparse.Namespace) -> None:
        if not args.isin:
            print("❌ Ошибка: для режима --mode bond параметр --isin обязателен.")
            return

        bond = self.storage.get_bond_by_isin(args.isin)
        if not bond:
            print(f"❌ Облигация с ISIN {args.isin} не найдена в каталоге.")
            return

        print(f"\n{'='*70}")
        print(f"📊 Анализ дюрации облигации: {bond.isin}")
        print(f"{'='*70}")
        print(f"Название:             {bond.name or 'N/A'}")
        print(f"Тикер:                {bond.ticker or 'N/A'}")
        print(f"Номинал:              {bond.nominal or 'N/A'} {bond.currency or 'RUB'}")
        print(f"Рыночная цена:        {bond.market_price or 'N/A'} руб.")
        print(f"Купонная ставка:      {bond.coupon_rate_percent or 'N/A'}%")
        print(f"Выплат в год:         {bond.coupon_quantity_per_year or 'N/A'}")
        print(f"Дата погашения:       {bond.maturity_date or 'N/A'}")
        print(f"{'-'*70}")

        if bond.duration_modified is not None:
            print(f"Дюрация Маколея:      {bond.duration_macaulay:.4f} года")
            print(f"Модифицированная дюрация: {bond.duration_modified:.4f} года")
            print(f"Статус:               Рассчитана")
        else:
            print(f"Дюрация:              NULL")
            print(f"Причина исключения:   {bond.duration_null_reason or 'не рассчитана'}")

        print(f"Метка обновления:     {bond.duration_updated_at or 'N/A'}")
        print(f"{'='*70}\n")

    def _handle_portfolio_mode(self, args: argparse.Namespace) -> None:
        positions = self.storage.get_portfolio_durations()
        if not positions:
            print("ℹ️ В портфеле нет позиций.")
            return

        # Разделение на покрытые и исключённые
        total_positions = len(positions)
        total_value = sum(float(p.get('current_value') or 0) for p in positions)

        covered = [
            p for p in positions
            if p.get('duration_modified') is not None and float(p.get('current_value') or 0) > 0
        ]
        covered_value = sum(float(p.get('current_value') or 0) for p in covered)
        coverage_pct = (covered_value / total_value * 100.0) if total_value > 0 else 0.0

        excluded = [p for p in positions if p not in covered]
        excluded_value = sum(float(p.get('current_value') or 0) for p in excluded)

        # Средневзвешенная дюрация
        if covered_value > 0:
            weighted_mod = sum(
                float(p['current_value']) * float(p['duration_modified'])
                for p in covered
            ) / covered_value
            weighted_mac = sum(
                float(p['current_value']) * float(p['duration_macaulay'])
                for p in covered
            ) / covered_value
        else:
            weighted_mod = None
            weighted_mac = None

        # Вывод таблицы
        print(f"\n{'='*125}")
        print("📊 Дюрация позиций портфеля")
        print(f"{'='*125}")
        print(f"{'ISIN':<12} | {'Тикер':<8} | {'Название':<24} | {'Стоимость ₽':<12} | {'Цена ₽':<8} | {'D_mac (г)':<9} | {'D_mod (г)':<9} | {'Статус/Причина'}")
        print(f"{'-'*125}")

        for p in positions:
            isin = p.get('isin') or 'N/A'
            ticker = (p.get('ticker') or 'N/A')[:8]
            name = (p.get('name') or 'N/A')[:24]
            val = float(p.get('current_value') or 0)
            price = float(p.get('market_price') or p.get('current_price') or 0)
            d_mac = f"{float(p['duration_macaulay']):.2f}" if p.get('duration_macaulay') is not None else "-"
            d_mod = f"{float(p['duration_modified']):.2f}" if p.get('duration_modified') is not None else "-"
            reason = p.get('duration_null_reason') or ('OK' if p.get('duration_modified') is not None else 'not_calculated')

            print(f"{isin:<12} | {ticker:<8} | {name:<24} | {val:<12,.0f} | {price:<8.1f} | {d_mac:<9} | {d_mod:<9} | {reason}")

        print(f"{'='*125}")
        print("📈 ИТОГИ ПО ДЮРАЦИИ ПОРТФЕЛЯ:")
        print(f"- Всего позиций:                      {total_positions} (на сумму {total_value:,.0f} ₽)")
        print(f"- Покрыто расчётом дюрации:           {len(covered)} позиций ({coverage_pct:.1f}% от стоимости портфеля, {covered_value:,.0f} ₽)")
        print(f"- Исключено из расчёта:               {len(excluded)} позиций (на сумму {excluded_value:,.0f} ₽)")

        if excluded:
            reasons_breakdown = defaultdict(lambda: {'count': 0, 'value': 0.0})
            for p in excluded:
                r = p.get('duration_null_reason') or 'not_calculated'
                reasons_breakdown[r]['count'] += 1
                reasons_breakdown[r]['value'] += float(p.get('current_value') or 0)

            print("  Причины исключения:")
            for r, stats in sorted(reasons_breakdown.items(), key=lambda x: x[1]['value'], reverse=True):
                r_pct = (stats['value'] / total_value * 100.0) if total_value > 0 else 0.0
                print(f"    * {r:<30}: {stats['count']} поз. ({stats['value']:,.0f} ₽, {r_pct:.1f}%)")

        if weighted_mod is not None:
            print(f"- Средневзвешенная мод. дюрация:      {weighted_mod:.2f} года (по покрытой части: {coverage_pct:.1f}%)")
            print(f"- Средневзвешенная Маколей дюрация:   {weighted_mac:.2f} года")
        else:
            print("- Средневзвешенная мод. дюрация:      Н/Д (нет позиций с рассчитанной дюрацией)")
        print(f"{'='*125}\n")
