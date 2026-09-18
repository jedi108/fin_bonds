import logging
import sys
import argparse
import csv
from pathlib import Path
from decimal import Decimal
from typing import Dict, List, Tuple, Any, Optional
from datetime import datetime

import pandas as pd
from tinkoff.invest import Client, RequestError
from tinkoff.invest.utils import quotation_to_decimal
from tabulate import tabulate

from src.data_models import PortfolioPosition, Portfolio
from src.use_cases.interfaces import IPortfolioStorage

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None

if 'TYPE_CHECKING' in sys.modules:
    from src.use_cases.factory import UseCaseFactory

class ExportPortfolioUseCase:
    """
    Сценарий: экспорт данных о портфеле из локальной БД в различные форматы.
    """
    def __init__(self, config: dict, storage: IPortfolioStorage, tinkoff_token: str):
        self.config = config
        self.storage = storage
        self.tinkoff_token = tinkoff_token
        self.client: Optional[Client] = None

        export_config = self.config.get('export_portfolio', {})
        self.GCREDS_FILENAME = export_config.get('gcreds_filename', 'gcreds.json')
        self.GOOGLE_SHEET_NAME = export_config.get('google_sheet_name', 'My Finance')
        self.SHEET_MAPPING = export_config.get('sheet_mapping', {
            "Акции": "Акции", "ОФЗ": "ОФЗ", "Корп. облигации": "Корп. облигации", "Фонды": "Фонды",
        })
        self.ETF_TYPES = set(export_config.get('etf_types', ["etf", "share_etf", "bond_etf"]))

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument('--format', type=str, choices=['gsheets', 'xlsx', 'csv', 'console'], required=True, help='Формат вывода.')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'ExportPortfolioUseCase':
        return cls(
            config=factory.config,
            storage=factory.get_db_connection(),
            tinkoff_token=factory.tinkoff_token
        )

    def execute(self, args: argparse.Namespace):
        output_format = args.format
        logging.info(f"🚀 Запуск экспорта портфеля в формат: {output_format}")

        positions = self.storage.get_portfolio_positions()
        if not positions:
            logging.info("ℹ️ В базе данных нет позиций для экспорта. Завершение работы.")
            return

        with Client(self.tinkoff_token) as client:
            self.client = client
            enriched_portfolio = self._enrich_positions_with_market_data(positions)
        
        if not enriched_portfolio.positions:
            logging.warning("Не удалось обогатить позиции рыночными данными. Экспорт невозможен.")
            return

        categorized_positions = self._classify_positions(enriched_portfolio.positions)
        export_data = self._prepare_export_data(categorized_positions)

        if not export_data:
            logging.info("ℹ️ Нет данных для экспорта после подготовки.")
            return

        if output_format == 'gsheets':
            self._export_to_gsheets(export_data)
        elif output_format == 'xlsx':
            self._export_to_xls(export_data)
        elif output_format == 'csv':
            self._export_to_csv(export_data)
        elif output_format == 'console':
            self._export_to_console(export_data)

        logging.info(f"\n🏁 Экспорт портфеля в {output_format} завершен.")

    def _enrich_positions_with_market_data(self, positions: List[PortfolioPosition]) -> Portfolio:
        """Обогащает позиции текущими ценами и рассчитывает доли."""
        all_figis = [p.figi for p in positions if p.figi]
        if not all_figis:
            logging.warning("Нет FIGI для запроса рыночных данных.")
            return Portfolio(positions=positions)

        try:
            prices_response = self.client.market_data.get_last_prices(figi=all_figis)
            last_prices = {p.figi: quotation_to_decimal(p.price) for p in prices_response.last_prices}
        except RequestError as e:
            logging.error(f"Ошибка API при получении последних цен: {e}. Экспорт будет без текущих цен.")
            last_prices = {}

        total_value = Decimal(0)
        for pos in positions:
            # Получаем номинал из bonds_catalog
            bond_data = self.storage.get_bond_by_isin(pos.isin)
            nominal = bond_data.nominal if bond_data and bond_data.nominal is not None else Decimal('1000') # Дефолтное значение 1000, если номинал не найден

            # Используем цену из БД, если API недоступно, иначе берем из last_prices
            price_from_api = last_prices.get(pos.figi)
            if price_from_api is not None:
                # Корректируем цену из API: умножаем на номинал и делим на 100 (так как API возвращает % от номинала)
                pos.current_price = price_from_api * nominal / Decimal('100')
            else:
                pos.current_price = pos.current_price # Используем цену из БД, если API не вернуло данных

            total_value += pos.quantity * pos.current_price

        if total_value > 0:
            for pos in positions:
                pos.share_in_portfolio = (pos.quantity * pos.current_price) / total_value
        
        # Получаем данные о потерях ликвидности
        # ВАЖНО: Ликвидность является биржевым свойством ISIN, а не свойством брокера.
        # Стакан заявок одинаков для всех брокеров на MOEX, поэтому результаты анализа
        # ликвидности применимы ко всем брокерам для данного ISIN.
        for pos in positions:
            if pos.isin:
                # Для Alor не устанавливаем liquidity_loss_ratio, так как стакан не анализируется
                if pos.broker_name == "Alor":
                    pos.liquidity_loss_ratio = None
                    continue
                    
                loss_data = self.storage.get_last_monitoring_check(pos.isin, 'liquidity_analyzer')
                if loss_data and loss_data[1]: # loss_data[1] содержит metric_value (коэффициент потерь)
                    try:
                        pos.liquidity_loss_ratio = Decimal(loss_data[1])
                    except Exception as e:
                        logging.warning(f"Не удалось преобразовать значение ликвидности '{loss_data[1]}' для {pos.isin}: {e}")

        # Получаем данные о купонной доходности
        for pos in positions:
            if pos.isin:
                bond_data = self.storage.get_bond_by_isin(pos.isin)
                if bond_data and bond_data.coupon_rate_percent is not None:
                    pos.coupon_rate_percent = bond_data.coupon_rate_percent
                else:
                    # Если coupon_rate_percent не найден в bonds_catalog (например, для флоатеров),
                    # пытаемся получить его из monitoring_checks (если он был рассчитан floater_coupon_calculator)
                    floater_coupon_data = self.storage.get_last_monitoring_check(pos.isin, 'floater_coupon_calculator')
                    if floater_coupon_data and floater_coupon_data[1]:
                        try:
                            pos.coupon_rate_percent = Decimal(floater_coupon_data[1])
                        except Exception as e:
                            logging.warning(f"Не удалось преобразовать рассчитанную купонную доходность '{floater_coupon_data[1]}' для {pos.isin}: {e}")

        # Получаем данные о кредитном рейтинге
        for pos in positions:
            if pos.isin:
                rating_data = self.storage.get_last_monitoring_check(pos.isin, 'credit_rating')
                if rating_data and rating_data[1]:
                    pos.rating = rating_data[1]
                else:
                    pos.rating = None

        return Portfolio(positions=positions, total_value_rub=total_value)

    def _classify_positions(self, all_positions: List[PortfolioPosition]) -> Dict[str, List[PortfolioPosition]]:
        categorized = { "Акции": [], "ОФЗ": [], "Корп. облигации": [], "Фонды": [], "Прочее": [] }
        etf_tickers = {pos.ticker for pos in all_positions if pos.instrument_type in self.ETF_TYPES}

        for pos in all_positions:
            # Сначала обрабатываем фонды (ETFs) по их типу, так как это самый надежный признак
            if pos.ticker in etf_tickers:
                categorized["Фонды"].append(pos)
                continue

            # Проверяем, является ли инструмент облигацией
            is_bond_by_type = pos.instrument_type in ['bond', 'corp_bond']
            # Убедимся, что pos.name существует перед вызовом .lower()
            is_bond_by_name = (pos.name and 'облигация' in pos.name.lower()) if pos.name else False

            if is_bond_by_type or is_bond_by_name:
                # Если pos.name существует, проверяем на ОФЗ
                if pos.name and "ОФЗ" in pos.name.upper():
                    categorized["ОФЗ"].append(pos)
                else:
                    categorized["Корп. облигации"].append(pos)
                continue

            # Если инструмент не был классифицирован как фонд или облигация,
            # и его тип 'share' или он неизвестен, но не является облигацией по имени,
            # тогда классифицируем как акцию.
            if pos.instrument_type == 'share' or (pos.instrument_type is None and not is_bond_by_name):
                 categorized["Акции"].append(pos)
                 continue

            # Если мы дошли до сюда, значит тип актива действительно неизвестен
            logging.warning(f"️️Не удалось классифицировать актив: {pos.name} ({pos.ticker}). Помещен в 'Прочее'.")
            categorized["Прочее"].append(pos)
            
        return categorized

    def _prepare_export_data(self, categorized_positions: Dict[str, List[PortfolioPosition]]) -> Dict[str, Tuple[List[str], List[Any]]]:
        export_data = {}
        headers = ["Тикер", "ISIN", "Название", "Количество", "Средняя", "Текущая цена", "Доля", "Брокер", "Потери при выходе", "Купонная доходность", "Рейтинг"]
        for category, assets in categorized_positions.items():
            sheet_name = self.SHEET_MAPPING.get(category)
            if not assets or not sheet_name:
                continue
            
            data_rows = []
            for asset in assets:
                row = [
                    asset.ticker, asset.isin, asset.name, asset.quantity,
                    asset.average_price, asset.current_price,
                    asset.share_in_portfolio, asset.broker_name,
                    'N/A' if asset.liquidity_loss_ratio is None else asset.liquidity_loss_ratio,
                    asset.coupon_rate_percent,
                    asset.rating if hasattr(asset, 'rating') and asset.rating is not None else 'N/A',
                ]
                data_rows.append(row)
            export_data[sheet_name] = (headers, data_rows)
        return export_data

    def _export_to_gsheets(self, export_data):
        if gspread is None:
            logging.error("Библиотека gspread не установлена. Выполните: pip install gspread google-auth-oauthlib")
            return
        logging.info("--- Начало выгрузки в Google Sheets ---")
        gspread_client = self._get_gspread_client()
        if not gspread_client: return
        try:
            spreadsheet = gspread_client.open(self.GOOGLE_SHEET_NAME)
            for sheet_name, (header, data_rows) in export_data.items():
                self._update_worksheet(spreadsheet, sheet_name, header, data_rows)
        except gspread.exceptions.GSpreadException as e:
            logging.error(f"Ошибка при работе с Google Sheets: {e}", exc_info=True)

    def _update_worksheet(self, sheet, sheet_name, header, data_rows):
        logging.info(f"🔄 Обновляю лист '{sheet_name}'...")
        try:
            worksheet = sheet.worksheet(sheet_name)
            worksheet.clear()
            worksheet.update([header] + data_rows, 'A1')
            logging.info(f"✅ Лист '{sheet_name}' успешно обновлен. Строк: {len(data_rows)}.")
        except gspread.exceptions.WorksheetNotFound:
            logging.warning(f"Лист '{sheet_name}' не найден. Создаю новый...")
            worksheet = sheet.add_worksheet(title=sheet_name, rows=len(data_rows)+1, cols=len(header))
            worksheet.update([header] + data_rows, 'A1')
            logging.info(f"✅ Лист '{sheet_name}' создан и обновлен.")
        except gspread.exceptions.GSpreadException as e:
            logging.error(f"Ошибка при обновлении листа '{sheet_name}': {e}", exc_info=True)

    def _export_to_console(self, export_data: Dict[str, Tuple[List[str], List[Any]]]):
        logging.info("--- Начало выгрузки в консоль ---")
        for category, (header, data_rows) in export_data.items():
            print(f"\nКатегория: {category}")
            if not data_rows:
                print("Нет данных для отображения.")
                continue
            
            # Форматируем данные для вывода в консоль
            formatted_data_rows = []
            for row in data_rows:
                formatted_row = []
                for i, item in enumerate(row):
                    if i == 4 and isinstance(item, Decimal): # Средняя
                        formatted_row.append(f"{item:.2f}")
                    elif i == 5 and isinstance(item, Decimal): # Текущая цена
                        formatted_row.append(f"{item:.2f}")
                    elif i == 6 and isinstance(item, Decimal): # Доля
                        formatted_row.append(f"{item:.2%}")
                    elif i == 8: # Потери при выходе
                        if item is None:
                            formatted_row.append("N/A")
                        elif isinstance(item, Decimal):
                            formatted_row.append(f"{item:.2f}%")
                        else:
                            formatted_row.append(str(item))
                    elif i == 9 and isinstance(item, Decimal): # Купонная доходность
                        formatted_row.append(f"{item:.2f}%" if item is not None else "N/A")
                    elif i == 10: # Рейтинг
                        formatted_row.append(str(item) if item is not None else "N/A")
                    else:
                        formatted_row.append(str(item))
                formatted_data_rows.append(formatted_row)
            
            print(tabulate(formatted_data_rows, headers=header, tablefmt="grid", stralign="right"))
        logging.info("✅ Данные выведены в консоль.")

    def _export_to_xls(self, export_data):
        output_dir = Path("_output_")
        output_dir.mkdir(exist_ok=True)
        filename = output_dir / f"portfolio_export_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        
        try:
            with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                for sheet_name, (header, data_rows) in export_data.items():
                    df = pd.DataFrame(data_rows, columns=header)
                    df.to_excel(writer, sheet_name=sheet_name, index=False)
            logging.info(f"✅ Портфель успешно экспортирован в файл: {filename}")
        except Exception as e:
            logging.error(f"Ошибка при сохранении в XLSX: {e}", exc_info=True)

    def _export_to_csv(self, export_data: Dict[str, Tuple[List[str], List[Any]]]):
        output_dir = Path("_output_")
        output_dir.mkdir(exist_ok=True)
        filename = output_dir / f"portfolio_export_{datetime.now():%Y%m%d_%H%M%S}.csv"
        
        all_rows = []
        header = []
        for sheet_name, (hdr, data_rows) in export_data.items():
            if not header:
                header = hdr + ['Категория']
            for row in data_rows:
                all_rows.append(row + [sheet_name])

        if not all_rows: return
            
        try:
            with open(filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(header)
                writer.writerows(all_rows)
            logging.info(f"✅ Портфель успешно экспортирован в файл: {filename}")
        except Exception as e:
            logging.error(f"Ошибка при сохранении в CSV: {e}", exc_info=True)

    def _get_gspread_client(self) -> Optional['gspread.Client']:
        creds_path = Path(self.GCREDS_FILENAME)
        if not creds_path.exists():
            logging.error(f"Файл {self.GCREDS_FILENAME} не найден.")
            return None
        try:
            scopes = ['https://www.googleapis.com/auth/spreadsheets']
            creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
            return gspread.authorize(creds)
        except Exception as e:
            logging.error(f"Ошибка аутентификации в Google Sheets: {e}", exc_info=True)
            return None 