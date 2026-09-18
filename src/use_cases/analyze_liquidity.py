import logging
from typing import List, Dict, Any, Optional
import argparse
from decimal import Decimal

from src.use_cases.base import UseCase
from src.monitoring.adapters.factory import AdapterFactory
from src.monitoring.storage import Database
from src.data_models import Bond, PortfolioPosition
from src.utils import get_trading_session_info

logger = logging.getLogger(__name__)


class AnalyzeLiquidityUseCase(UseCase):
    """
    Use case для анализа ликвидности облигаций в портфеле.
    
    Этот UseCase анализирует ликвидность облигаций путем расчета 
    потенциальных потерь при выходе из позиции по рыночной цене.
    """

    def __init__(self, config: Dict[str, Any], db: Database, adapter_factory: AdapterFactory) -> None:
        """
        Инициализирует UseCase.
        
        Args:
            config: Конфигурация приложения
            db: Соединение с БД
            adapter_factory: Фабрика для создания адаптеров мониторинга
        """
        self.config = config
        self.db = db
        self.adapter_factory = adapter_factory
        self.adapter_name = 'liquidity_analyzer'
        self.target_amount = Decimal(config.get('LIQUIDITY_TARGET_AMOUNT', 60000))

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser) -> None:
        """Добавляет аргументы командной строки для этого use case."""
        parser.add_argument(
            '--isin',
            help='ISIN облигации для анализа. Если не указан, анализируются все облигации в портфеле.'
        )
        parser.add_argument(
            '--amount',
            type=float,
            help='Целевая сумма для расчета потерь (рублей). По умолчанию используется значение из конфигурации.'
        )
        parser.add_argument(
            '--depth',
            type=int,
            default=20,
            help='Глубина стакана для анализа (от 1 до 20).'
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Анализировать все облигации в каталоге, а не только в портфеле.'
        )

    @classmethod
    def create(cls, factory) -> 'AnalyzeLiquidityUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(
            config=factory.config,
            db=factory.get_db_connection(),
            adapter_factory=factory.get_adapter_factory()
        )

    def execute(self, args: argparse.Namespace) -> None:
        """Запускает анализ ликвидности."""
        # Проверяем торговое время и предупреждаем пользователя
        trading_info = get_trading_session_info()
        if not trading_info['is_trading_hours']:
            warning_msg = trading_info.get('warning_message', 'Сейчас нерабочее время торгов')
            next_session = trading_info.get('next_session_start')
            
            logger.warning("⏰ ВНИМАНИЕ: Анализ ликвидности вне торгового времени!")
            logger.warning(f"⏰ {warning_msg}")
            if next_session:
                logger.warning(f"⏰ Следующая торговая сессия: {next_session.strftime('%d.%m.%Y %H:%M МСК')}")
            logger.warning("⏰ Результаты могут не отражать реальную ликвидность!")
            print()  # Пустая строка для читаемости
        
        # Обновляем конфигурацию, если указаны аргументы командной строки
        if args.amount:
            self.config['LIQUIDITY_TARGET_AMOUNT'] = args.amount
            self.target_amount = Decimal(args.amount)
        
        if args.depth:
            self.config['LIQUIDITY_DEPTH'] = args.depth
        
        # Определяем, какие облигации анализировать
        if args.isin:
            # Анализируем конкретную облигацию
            bonds_to_analyze = [self.db.get_bond_by_isin(args.isin)]
            if not bonds_to_analyze[0]:
                logger.error(f"Облигация с ISIN {args.isin} не найдена в каталоге.")
                return
        elif args.all:
            # Анализируем все облигации в каталоге
            bonds_to_analyze = self.db.get_full_catalog()
            logger.info(f"Анализ всех облигаций в каталоге ({len(bonds_to_analyze)} шт.)")
        else:
            # Анализируем только облигации в портфеле
            portfolio = self.db.get_portfolio_positions()
            isins = [pos.isin for pos in portfolio if pos.isin]
            bonds_to_analyze = [self.db.get_bond_by_isin(isin) for isin in isins]
            bonds_to_analyze = [bond for bond in bonds_to_analyze if bond is not None]
            logger.info(f"Анализ облигаций в портфеле ({len(bonds_to_analyze)} шт.)")
        
        # Выполняем анализ
        results = self._analyze_bonds(bonds_to_analyze)
        
        # Выводим результаты
        self._print_results(results)

    def _analyze_bonds(self, bonds: List[Bond]) -> List[Dict[str, Any]]:
        """
        Анализирует ликвидность для списка облигаций.
        
        Args:
            bonds: Список облигаций для анализа
            
        Returns:
            Список с результатами анализа
        """
        results = []
        
        for bond in bonds:
            if not bond.isin:
                continue
                
            logger.info(f"Анализ ликвидности для {bond.isin} ({bond.name})")
            
            # Создаем адаптер для анализа
            adapter = self.adapter_factory.get_adapter(self.adapter_name, bond)
            if not adapter:
                logger.error(f"Не удалось создать адаптер {self.adapter_name} для {bond.isin}")
                continue
                
            # Выполняем анализ
            result = adapter.fetch()
            
            # Сохраняем результат
            results.append({
                'isin': bond.isin,
                'name': bond.name,
                'status': result.status,
                'message': result.message,
                'value': result.value,
                'is_significant': result.is_significant
            })
            
            if result.is_significant:
                logger.warning(f"ВНИМАНИЕ! {result.message}")
            else:
                logger.info(f"Результат: {result.message}")
        
        return results

    def _print_results(self, results: List[Dict[str, Any]]) -> None:
        """
        Выводит результаты анализа в консоль.
        """
        logger.info(f"Результаты анализа ликвидности для целевой суммы {self.target_amount} руб.:")
        
        # Сортируем по значению коэффициента потерь (от большего к меньшему)
        sorted_results = sorted(
            results, 
            key=lambda x: float(x['value']) if x['value'] is not None else -1, 
            reverse=True
        )
        
        for result in sorted_results:
            if result['value'] is not None:
                loss_value = float(result['value'])
                risk_level = "КРИТИЧЕСКИЙ" if loss_value >= 50 else \
                            "ВЫСОКИЙ" if loss_value >= 20 else \
                            "УМЕРЕННЫЙ" if loss_value >= 10 else \
                            "НИЗКИЙ"
            else:
                loss_value = 0
                risk_level = "НЕ ОПРЕДЕЛЕН"
            
            if result['value'] is not None:
                message = f"{result['isin']} ({result['name']}): {result['value']}% потери - {risk_level} риск"
            else:
                message = f"{result['isin']} ({result['name']}): Анализ не выполнен - {risk_level}"
            
            if result['is_significant']:
                logger.warning(f"⚠️  {message}")
            else:
                logger.info(f"   {message}") 