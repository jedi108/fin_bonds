#!/usr/bin/env python3
"""
Use case для получения и обновления полного каталога облигаций.
Сохраняет данные в базу SQLite и опционально выгружает в CSV.
"""
import logging
import os
import csv
from decimal import Decimal
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from dataclasses import dataclass, asdict
import argparse

from tinkoff.invest import Client, Bond, MoneyValue, InstrumentStatus
from tinkoff.invest.utils import quotation_to_decimal

from src.use_cases.interfaces import IMoexApiClient
from src.use_cases.interfaces import IPortfolioStorage
from src.use_cases.base import UseCase
from src.data_models import Bond as BondDTO

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


def money_value_to_decimal(money_value: Optional[MoneyValue]) -> Decimal:
    """Конвертирует MoneyValue в Decimal, возвращая 0, если значение None."""
    if not money_value:
        return Decimal(0)
    return quotation_to_decimal(money_value)


def fetch_and_prepare_bond_details(bond: Bond) -> BondDTO:
    """Извлекает данные из объекта Bond API и подготавливает наш DTO."""
    maturity_dt = bond.maturity_date
    offer_dt = getattr(bond, 'buy_back_date', None)
    
    maturity_date_obj = maturity_dt.date() if maturity_dt else None
    offer_date_obj = offer_dt.date() if offer_dt else None

    return BondDTO(
        figi=bond.figi,
        isin=bond.isin,
        name=bond.name,
        ticker=bond.ticker,
        currency=bond.currency,
        maturity_date=maturity_date_obj,
        offer_date=offer_date_obj,
        nominal=float(money_value_to_decimal(bond.nominal)),
        coupon_quantity_per_year=bond.coupon_quantity_per_year,
        floating_coupon_flag=bond.floating_coupon_flag,
        perpetual_flag=bond.perpetual_flag,
        amortization_flag=bond.amortization_flag,
        issue_size=bond.issue_size,
        risk_level=bond.risk_level,
        is_trade_available=bond.buy_available_flag and bond.sell_available_flag,
        is_for_qualified_investors=bond.for_qual_investor_flag,
        list_level=None,  # Будет заполнено из MOEX
        coupon_rate_percent=None,  # Будет заполнено из MOEX
        coupon_spread=None
    )


def _write_bond_to_console(bond: BondDTO):
    """Выводит детальную информацию об одной облигации в консоль."""
    logger.info(
        f"  {bond.name:<50} | "
        f"{bond.ticker:<15} | "
        f"{bond.risk_level:<5} | "
        f"Листинг: {bond.list_level or 'N/A':<1} | "
        f"Погашение: {bond.maturity_date} | "
        f"Купонов/год: {bond.coupon_quantity_per_year:<2} | "
        f"Номинал: {bond.nominal:>7.2f} {bond.currency}"
    )


class UpdateBondsCatalogUseCase(UseCase):
    """
    Основная функция для получения и сохранения каталога облигаций.
    Загружает полный список доступных облигаций с Tinkoff API.
    """
    def __init__(self, factory: 'UseCaseFactory'):
        super().__init__(factory)
        self.db = self.factory.get_db_connection()
        self.tbank_client: TbankApiClient = self.factory.get_tbank_api_client()
        self.moex_client: MoexApiClient = self.factory.get_moex_api_client()
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--output', type=str, default='none', choices=['none', 'csv', 'gsheets', 'console'],
            help='Формат вывода данных: none (по умолчанию), csv, gsheets, console'
        )
        parser.add_argument(
            '--source', type=str, default='all', choices=['all', 'tbank', 'moex'],
            help='Источник данных для обновления: all (по умолчанию), tbank, moex'
        )
        parser.add_argument(
            '--update-prices', action='store_true',
            help='Обновить рыночные цены для облигаций в каталоге'
        )
        parser.add_argument(
            '--prices-only', action='store_true',
            help='Обновить только рыночные цены, без обновления каталога'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'UpdateBondsCatalogUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(factory)

    def execute(self, args: argparse.Namespace):
        self.logger.info(f"Запуск use case для обновления каталога облигаций (источник: {args.source})...")
        try:
            # Если только цены, пропускаем обновление каталога
            if not args.prices_only:
                if args.source in ['all', 'tbank']:
                    self._update_from_tbank()
                
                if args.source in ['all', 'moex']:
                    self._update_from_moex()
            
            # Обновляем рыночные цены если запрошено
            if args.update_prices or args.prices_only:
                self._update_market_prices()
            
            if not args.prices_only:
                self._handle_output(args)
        except Exception as e:
            self.logger.error(f"Произошла критическая ошибка в use case 'UpdateBondsCatalog': {e}", exc_info=True)
        finally:
            self.logger.info("✅ Use case завершил работу.")

    def _update_from_tbank(self):
        """
        Получает полный список облигаций с TBank API,
        находит новые и добавляет их в локальную базу данных.
        """
        # Шаг 1: Получаем все облигации из Tinkoff API
        self.logger.info("--- Обновление каталога из TBank API ---")
        with Client(self.tbank_client.token) as client:
            all_tinkoff_bonds = self._get_all_bonds_from_tinkoff(client)

        if not all_tinkoff_bonds:
            self.logger.error("Не удалось получить список облигаций из TBank API. Прерывание.")
            return
        self.logger.info(f"Успешно загружено {len(all_tinkoff_bonds)} облигаций.")

        # Шаг 2: Получаем существующие ISIN из нашей БД
        self.logger.info("Фильтрация новых облигаций для добавления в каталог...")
        existing_isins = self.db.get_all_isins_from_catalog()
        self.logger.info(f"Получено {len(existing_isins)} существующих ISIN из БД")
        if existing_isins:
            self.logger.info(f"Примеры существующих ISIN: {list(existing_isins)[:5]}")

        # Шаг 3: Определяем, какие облигации новые и добавляем их
        self.logger.info(f"Существующих ISIN в БД: {len(existing_isins)}")
        new_bonds_to_add = [bond for bond in all_tinkoff_bonds if bond.isin not in existing_isins]
        if new_bonds_to_add:
            self.logger.info(f"Найдено {len(new_bonds_to_add)} новых облигаций. Добавление в каталог...")
            self.db.add_bonds_to_catalog(new_bonds_to_add)
            self.logger.info(f"Добавлено {len(new_bonds_to_add)} облигаций в каталог.")
        else:
            self.logger.info("Новых облигаций для добавления не найдено.")
            
        # Принудительно обновляем все облигации для заполнения is_trade_available
        self.logger.info("Принудительное обновление всех облигаций для заполнения is_trade_available...")
        self.db.add_bonds_to_catalog(all_tinkoff_bonds)
        self.logger.info(f"Обновлено {len(all_tinkoff_bonds)} облигаций в каталоге.")

    def _update_from_moex(self):
        """
        Обогащает существующие записи в каталоге данными с MOEX
        (уровень листинга, ставка купона и т.д.).
        """
        # Шаг 4: Обновляем данные из MOEX
        self.logger.info("--- Обновление данных с MOEX (листинг, купон) ---")
        bonds_to_update = self.db.get_bonds_for_moex_update()
        self.logger.info(f"Найдено {len(bonds_to_update)} облигаций для обновления.")
        
        if not bonds_to_update:
            self.logger.info("Облигаций для обновления данными с MOEX не найдено.")
            return

        updated_count = 0
        for bond in bonds_to_update:
            self.logger.debug(f"Обновление данных с MOEX для ISIN: {bond.isin}")
            moex_data = self.moex_client.get_bond_data(bond.isin, bond.nominal)
            if moex_data:
                self.db.update_bond_moex_data(
                    isin=bond.isin,
                    list_level=moex_data.list_level,
                    coupon_rate=moex_data.coupon_rate,
                    offer_date=moex_data.offer_date
                )
                updated_count += 1
        self.logger.info(f"Сверка завершена. Обновлено записей с MOEX: {updated_count}")

    def _update_market_prices(self):
        """
        Обновляет рыночные цены для облигаций в каталоге.
        """
        self.logger.info("--- Обновление рыночных цен ---")
        
        # Получаем облигации без цен или с устаревшими ценами
        bonds_without_prices = self.db.get_bonds_without_market_prices()
        bonds_with_old_prices = self.db.get_bonds_with_old_market_prices()
        
        all_bonds_to_update = list(set(bonds_without_prices + bonds_with_old_prices))
        
        if not all_bonds_to_update:
            self.logger.info("Нет облигаций для обновления цен.")
            return
            
        self.logger.info(f"Найдено {len(all_bonds_to_update)} облигаций для обновления цен.")
        
        # Получаем FIGI для облигаций
        figi_by_isin = {}
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT isin, figi FROM bonds_catalog WHERE isin IN ({})".format(
            ','.join(['?' for _ in all_bonds_to_update])
        ), all_bonds_to_update)
        
        for row in cursor.fetchall():
            if row['figi']:  # Пропускаем облигации без FIGI
                figi_by_isin[row['isin']] = row['figi']
        
        if not figi_by_isin:
            self.logger.warning("Не найдено FIGI для облигаций. Невозможно получить цены.")
            return
            
        self.logger.info(f"Получено {len(figi_by_isin)} FIGI для запроса цен.")
        
        # Получаем цены через TBank API
        figi_list = list(figi_by_isin.values())
        prices_by_figi = self.tbank_client.get_market_prices(figi_list)
        
        if not prices_by_figi:
            self.logger.warning("Не удалось получить рыночные цены.")
            return
            
        # Подготавливаем данные для обновления
        prices_data = []
        for isin, figi in figi_by_isin.items():
            if figi in prices_by_figi:
                price = prices_by_figi[figi]
                # Конвертируем цену из процентов в абсолютное значение
                bond_data = self.db.get_bond_by_isin(isin)
                if bond_data and bond_data.nominal:
                    absolute_price = price * bond_data.nominal / Decimal('100')
                    prices_data.append((isin, absolute_price, 'tbank'))
        
        # Обновляем цены в базе данных
        updated_count = self.db.update_market_prices(prices_data)
        self.logger.info(f"Обновлено рыночных цен: {updated_count}")

    def _handle_output(self, args: argparse.Namespace):
        """Обрабатывает вывод данных в консоль или файл."""
        if args.output == "none":
            return
        
        self.logger.info(f"--- Вывод данных в формате: {args.output} ---")
        full_catalog = self.db.get_full_catalog()

        if not full_catalog:
            self.logger.info("Нет данных для вывода.")
            return

        if args.output == "console":
            header = (
                f"  {'Название':<50} | {'Тикер':<15} | {'Риск':<5} | "
                f"{'Листинг':<9} | {'Дата погашения':<21} | "
                f"{'Купонов/год':<13} | {'Номинал'}"
            )
            self.logger.info(header)
            self.logger.info("-" * (len(header) + 5))
            for bond_dto in full_catalog:
                _write_bond_to_console(bond_dto)
            self.logger.info("-" * (len(header) + 5))
            self.logger.info(f"Выведено {len(full_catalog)} облигаций в консоль.")

        elif args.output == "csv":
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"_output_/bonds_catalog_{timestamp}.csv"
            self.logger.info(f"Подготовка к сохранению данных в файл: {output_filename}")
            
            os.makedirs(os.path.dirname(output_filename), exist_ok=True)
            
            data_to_write = [asdict(bond) for bond in full_catalog]
            if data_to_write:
                try:
                    fieldnames = list(data_to_write[0].keys())
                    with open(output_filename, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerows(data_to_write)
                    
                    self.logger.info(f"Данные успешно сохранены в файл: {output_filename}")
                    self.logger.info(f"Полный путь к файлу: {os.path.abspath(output_filename)}")
                except Exception as e:
                    self.logger.error(f"Ошибка при сохранении в CSV: {e}")

    def _get_all_bonds_from_tinkoff(self, client: Client) -> List[BondDTO]:
        """Вспомогательный метод для получения всех облигаций из Tinkoff API."""
        raw_bonds = client.instruments.bonds().instruments
        bonds_dto_list = []
        for bond in raw_bonds:
            if bond.currency.lower() != 'rub':
                continue
            
            # Пропускаем облигации без ISIN или maturity_date
            if not bond.isin or not bond.maturity_date:
                continue

            bonds_dto_list.append(fetch_and_prepare_bond_details(bond))
        return bonds_dto_list

    # Этот метод не используется и будет удален
    # def _check_and_notify_coupon_change(self, bond: BondDTO, new_rate: Optional[float]):
    #     ... 