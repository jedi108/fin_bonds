"""
Use Case для обновления рыночных цен облигаций.
"""

import argparse
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from .base import UseCase
from ..tbank.api_client import TbankApiClient

logger = logging.getLogger(__name__)


class UpdateMarketPrices(UseCase):
    """Обновляет рыночные цены облигаций через API Tinkoff."""
    
    def __init__(self, factory):
        super().__init__(factory)
        self.tbank_client = factory.get_tbank_api_client()
    
    def execute(self, args: argparse.Namespace) -> None:
        """Выполняет обновление рыночных цен."""
        logger.info("🔄 Начинаю обновление рыночных цен облигаций...")
        
        # Получаем список облигаций для обновления
        bonds = self._get_bonds_for_update(args)
        
        if not bonds:
            logger.info("ℹ️ Нет облигаций для обновления цен.")
            return
        
        logger.info(f"📊 Найдено {len(bonds)} облигаций для обновления цен")
        
        # Ограничиваем количество облигаций
        if args.limit and len(bonds) > args.limit:
            bonds = bonds[:args.limit]
            logger.info(f"📊 Ограничиваем до {args.limit} облигаций")
        
        # Обрабатываем облигации пакетами
        batch_size = 50  # Размер пакета
        total_updated = 0
        total_errors = 0
        
        for i in range(0, len(bonds), batch_size):
            batch = bonds[i:i + batch_size]
            logger.info(f"📦 Обрабатываю пакет {i//batch_size + 1}/{(len(bonds) + batch_size - 1)//batch_size} ({len(batch)} облигаций)")
            
            # Обновляем FIGI для облигаций без FIGI
            for bond in batch:
                if not bond.get('figi'):
                    figi = self._get_figi_by_isin(bond['isin'])
                    if figi:
                        bond['figi'] = figi
            
            # Обновляем цены пакетно
            updated_count = self._update_bond_prices_batch(batch)
            total_updated += updated_count
            total_errors += len(batch) - updated_count
        
        logger.info(f"✅ Обновление завершено: {total_updated} успешно, {total_errors} ошибок")
    
    def _get_bonds_for_update(self, args: argparse.Namespace) -> List[Dict[str, Any]]:
        """Получает список облигаций для обновления цен."""
        if args.isin:
            # Обновляем только конкретный ISIN
            query = """
                SELECT isin, ticker, name, figi
                FROM bonds_catalog 
                WHERE isin = ?
                ORDER BY isin
            """
            with self.storage.conn:
                cursor = self.storage.conn.cursor()
                cursor.execute(query, [args.isin])
                return [dict(row) for row in cursor.fetchall()]
        else:
            # Обновляем все подходящие облигации
            query = """
                SELECT isin, ticker, name, figi
                FROM bonds_catalog 
                WHERE currency = 'rub'
                    AND (is_trade_available = 1 OR is_trade_available IS NULL)
                    AND perpetual_flag = 0
                    AND maturity_date >= date('now')
                ORDER BY isin
            """
            
            with self.storage.conn:
                cursor = self.storage.conn.cursor()
                cursor.execute(query)
                return [dict(row) for row in cursor.fetchall()]
    
    def _update_bond_price(self, bond: Dict[str, Any]) -> bool:
        """Обновляет цену для одной облигации."""
        try:
            # Получаем FIGI если его нет
            figi = bond.get('figi')
            if not figi:
                figi = self._get_figi_by_isin(bond['isin'])
                if not figi:
                    logger.warning(f"⚠️ Не удалось найти FIGI для {bond['isin']}")
                    return False
            
            # Получаем текущую цену через API
            current_price = self._get_current_price(figi)
            if current_price is None:
                logger.warning(f"⚠️ Не удалось получить цену для {bond['isin']}")
                return False
            
            # Обновляем цену в БД
            self._save_market_price(bond['isin'], current_price)
            
            logger.debug(f"✅ Обновлена цена для {bond['isin']}: {current_price}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка при обновлении цены для {bond['isin']}: {e}")
            return False
    
    def _get_figi_by_isin(self, isin: str) -> Optional[str]:
        """Получает FIGI по ISIN через API."""
        try:
            # Ищем в локальной БД сначала
            query = "SELECT figi FROM bonds_catalog WHERE isin = ?"
            with self.storage.conn:
                cursor = self.storage.conn.cursor()
                cursor.execute(query, [isin])
                result = cursor.fetchone()
                if result and result['figi']:
                    return result['figi']
            
            # Если нет в БД, запрашиваем через API
            instruments = self.tbank_client.find_instrument(isin)
            if instruments:
                figi = instruments[0].figi
                # Сохраняем FIGI в БД
                self._save_figi(isin, figi)
                return figi
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка при получении FIGI для {isin}: {e}")
            return None
    
    def _get_current_price(self, figi: str) -> Optional[float]:
        """Получает текущую цену по FIGI."""
        try:
            prices = self.tbank_client.get_market_prices([figi])
            if figi in prices:
                return float(prices[figi])
            return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка при получении цены для FIGI {figi}: {e}")
            return None
    
    def _save_market_price(self, isin: str, price: float) -> None:
        """Сохраняет рыночную цену в БД."""
        query = "UPDATE bonds_catalog SET market_price = ? WHERE isin = ?"
        with self.storage.conn:
            cursor = self.storage.conn.cursor()
            cursor.execute(query, [price, isin])
    
    def _save_figi(self, isin: str, figi: str) -> None:
        """Сохраняет FIGI в БД."""
        query = "UPDATE bonds_catalog SET figi = ? WHERE isin = ?"
        with self.storage.conn:
            cursor = self.storage.conn.cursor()
            cursor.execute(query, [figi, isin])
    
    def _update_bond_prices_batch(self, bonds: List[Dict[str, Any]]) -> int:
        """Обновляет цены для группы облигаций."""
        try:
            # Получить все FIGI
            figi_list = [bond.get('figi') for bond in bonds if bond.get('figi')]
            
            if not figi_list:
                logger.warning("⚠️ Нет FIGI для обновления цен")
                return 0
            
            # Получить цены пакетно
            prices = self.tbank_client.get_market_prices(figi_list)
            
            # Обновить цены в БД
            updated_count = 0
            for bond in bonds:
                figi = bond.get('figi')
                if figi and figi in prices:
                    self._save_market_price(bond['isin'], float(prices[figi]))
                    updated_count += 1
                    logger.debug(f"✅ Обновлена цена для {bond['isin']}: {prices[figi]}")
            
            logger.info(f"📊 Обновлено цен для {updated_count} из {len(bonds)} облигаций")
            return updated_count
            
        except Exception as e:
            logger.error(f"❌ Ошибка при пакетном обновлении цен: {e}")
            return 0
    
    @classmethod
    def setup_parser(cls, parser: argparse.ArgumentParser) -> None:
        """Настраивает парсер аргументов для команды."""
        parser.add_argument(
            '--limit', 
            type=int, 
            default=100, 
            help='Максимальное количество облигаций для обновления (по умолчанию: 100)'
        )
        parser.add_argument(
            '--isin', 
            type=str, 
            help='Обновить цену только для конкретного ISIN'
        )
    
    @classmethod
    def create(cls, factory) -> 'UpdateMarketPrices':
        """Создает экземпляр use case с необходимыми зависимостями."""
        return cls(factory)
