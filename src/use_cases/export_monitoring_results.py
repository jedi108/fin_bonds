"""
Use case для экспорта результатов мониторинга в консоль и Excel файл.
"""
import logging
import argparse
import pandas as pd
from datetime import datetime
from typing import TYPE_CHECKING, List, Dict, Any

from src.monitoring.storage import Database
from src.use_cases.base import UseCase

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class ExportMonitoringResultsUseCase(UseCase):
    """
    Экспортирует результаты мониторинга в консоль и Excel файл.
    """
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды экспорта результатов мониторинга."""
        parser.add_argument(
            '--output-file', 
            type=str, 
            default=f'monitoring_results_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx',
            help='Путь к Excel файлу для сохранения (по умолчанию: monitoring_results_YYYYMMDD_HHMMSS.xlsx)'
        )
        parser.add_argument(
            '--isin', 
            type=str, 
            help='ISIN конкретной облигации для экспорта (если не указан, экспортируются все)'
        )
        parser.add_argument(
            '--adapter', 
            type=str, 
            help='Конкретный адаптер для экспорта (например: credit_rating, moex_listlevel)'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'ExportMonitoringResultsUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(db=factory.get_db_connection())

    def execute(self, args: argparse.Namespace):
        logger.info("🚀 Запуск экспорта результатов мониторинга...")
        
        try:
            # Получаем данные мониторинга
            monitoring_data = self._get_monitoring_data(args.isin, args.adapter)
            
            if not monitoring_data:
                logger.warning("Не найдено данных мониторинга для экспорта.")
                return
            
            # Выводим в консоль
            self._print_to_console(monitoring_data)
            
            # Сохраняем в Excel
            self._save_to_excel(monitoring_data, args.output_file)
            
            logger.info(f"✅ Экспорт завершен. Файл сохранен: {args.output_file}")
            
        except Exception as e:
            logger.error(f"Ошибка при экспорте: {e}", exc_info=True)

    def _get_monitoring_data(self, isin: str = None, adapter: str = None) -> List[Dict[str, Any]]:
        """Получает данные мониторинга из БД."""
        try:
            # Получаем все записи мониторинга
            if isin:
                # Для конкретного ISIN
                data = self.db.get_monitoring_checks_by_isin(isin)
            elif adapter:
                # Для конкретного адаптера
                data = self.db.get_monitoring_checks_by_adapter(adapter)
            else:
                # Все данные
                data = self.db.get_all_monitoring_checks()
            
            # Обогащаем данными об облигациях
            enriched_data = []
            for record in data:
                bond_info = self.db.get_bond_by_isin(record[0])  # record[0] - это ISIN
                enriched_record = {
                    'isin': record[0],
                    'bond_name': bond_info.name if bond_info else 'Неизвестно',
                    'adapter': record[1],
                    'metric_value': record[2],
                    'check_date': record[3] if len(record) > 3 else None
                }
                enriched_data.append(enriched_record)
            
            return enriched_data
            
        except Exception as e:
            logger.error(f"Ошибка при получении данных мониторинга: {e}")
            return []

    def _print_to_console(self, data: List[Dict[str, Any]]):
        """Выводит результаты в консоль в табличном виде."""
        if not data:
            return
        
        print("\n" + "="*80)
        print("РЕЗУЛЬТАТЫ МОНИТОРИНГА")
        print("="*80)
        
        # Группируем по ISIN для удобного отображения
        grouped_data = {}
        for record in data:
            isin = record['isin']
            if isin not in grouped_data:
                grouped_data[isin] = []
            grouped_data[isin].append(record)
        
        for isin, records in grouped_data.items():
            print(f"\n📊 {records[0]['bond_name']} ({isin})")
            print("-" * 60)
            
            for record in records:
                adapter_name = record['adapter']
                metric_value = record['metric_value']
                check_date = record['check_date']
                
                # Красивое отображение в зависимости от типа адаптера
                if adapter_name == 'credit_rating':
                    print(f"  🏆 Кредитный рейтинг: {metric_value}")
                elif adapter_name == 'moex_listlevel':
                    print(f"  📈 Уровень листинга MOEX: {metric_value}")
                elif adapter_name == 'tinkoff.risk_level':
                    print(f"  ⚠️  Уровень риска TBank: {metric_value}")
                elif adapter_name == 'moex_coupon':
                    print(f"  💰 Ставка купона MOEX: {metric_value}%")
                elif adapter_name == 'floater_coupon_calculator':
                    print(f"  🧮 Расчетный купон флоатера: {metric_value}%")
                elif adapter_name == 'liquidity_analyzer':
                    print(f"  💧 Анализ ликвидности: {metric_value}")
                else:
                    print(f"  📋 {adapter_name}: {metric_value}")
                
                if check_date:
                    print(f"      📅 Проверено: {check_date}")
        
        print("\n" + "="*80)

    def _save_to_excel(self, data: List[Dict[str, Any]], output_file: str):
        """Сохраняет результаты в Excel файл."""
        if not data:
            return
        
        try:
            # Создаем DataFrame
            df = pd.DataFrame(data)
            
            # Сортируем по ISIN и дате
            df = df.sort_values(['isin', 'check_date'], ascending=[True, False])
            
            # Создаем Excel writer
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                # Основной лист с данными
                df.to_excel(writer, sheet_name='Мониторинг', index=False)
                
                # Создаем сводный лист
                self._create_summary_sheet(df, writer)
            
            logger.info(f"📊 Данные сохранены в Excel: {output_file}")
            
        except Exception as e:
            logger.error(f"Ошибка при сохранении в Excel: {e}")

    def _create_summary_sheet(self, df: pd.DataFrame, writer):
        """Создает сводный лист в Excel."""
        try:
            # Группируем по ISIN
            summary_data = []
            
            for isin in df['isin'].unique():
                isin_data = df[df['isin'] == isin]
                bond_name = isin_data.iloc[0]['bond_name']
                
                summary_row = {
                    'ISIN': isin,
                    'Название': bond_name
                }
                
                # Добавляем данные по каждому адаптеру
                for _, row in isin_data.iterrows():
                    adapter = row['adapter']
                    value = row['metric_value']
                    
                    if adapter == 'credit_rating':
                        summary_row['Кредитный рейтинг'] = value
                    elif adapter == 'moex_listlevel':
                        summary_row['Уровень листинга'] = value
                    elif adapter == 'tinkoff.risk_level':
                        summary_row['Уровень риска'] = value
                    elif adapter == 'moex_coupon':
                        summary_row['Ставка купона (%)'] = value
                    elif adapter == 'floater_coupon_calculator':
                        summary_row['Расчетный купон (%)'] = value
                    elif adapter == 'liquidity_analyzer':
                        summary_row['Ликвидность'] = value
                
                summary_data.append(summary_row)
            
            # Создаем сводный DataFrame
            summary_df = pd.DataFrame(summary_data)
            summary_df.to_excel(writer, sheet_name='Сводка', index=False)
            
        except Exception as e:
            logger.warning(f"Не удалось создать сводный лист: {e}") 