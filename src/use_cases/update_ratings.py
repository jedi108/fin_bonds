import logging
import argparse
from typing import Dict, Any, Optional, TYPE_CHECKING, List
from tqdm import tqdm

from src.use_cases.base import UseCase
from src.use_cases.interfaces import IPortfolioStorage
from src.monitoring.notifier import TelegramNotifier
from src.monitoring.adapters.factory import AdapterFactory
from src.monitoring.adapters.base import MonitoringResult

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class UpdateRatingsUseCase(UseCase):
    """
    Проверяет и обновляет кредитные рейтинги, уровни листинга, риска
    и другие отслеживаемые метрики, уведомляя о значительных изменениях.
    """
    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды update_ratings."""
        parser.add_argument(
            '--isin',
            type=str,
            help='Указать ISIN конкретной облигации для проверки.'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'UpdateRatingsUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(
            storage=factory.get_db_connection(),
            notifier=factory.get_notifier(),
            use_case_factory=factory
        )

    def __init__(self, storage: IPortfolioStorage, notifier: Optional[TelegramNotifier], use_case_factory: 'UseCaseFactory'):
        self.storage = storage
        self.notifier = notifier
        self.adapter_factory = AdapterFactory(use_case_factory)
        self.config = use_case_factory.config

    def execute(self, args: argparse.Namespace):
        logger.info("🚀 Запуск use case: Обновление отслеживаемых метрик...")

        # 1. Получаем список адаптеров из конфига
        active_adapters = self.config.get('monitoring_adapters', [])
        if not active_adapters:
            logger.warning("В конфигурации 'monitoring_adapters' не указаны активные адаптеры. Пропускаем.")
            return
        
        logger.info(f"Адаптеры, загруженные из конфигурации: {active_adapters}")

        # 2. Получаем данные облигаций для мониторинга (объединенный запрос)
        if args.isin:
            portfolio_bond = self.storage.get_bond_data_for_monitoring(args.isin)
            if not portfolio_bond:
                logger.warning(f"Облигация {args.isin} не найдена в портфеле или каталоге.")
                return
            portfolio_bonds = [portfolio_bond]
            logger.info(f"Запрошена проверка для одной облигации: {args.isin}")
        else:
            portfolio_bonds = self.storage.get_portfolio_bonds_for_monitoring()
            if not portfolio_bonds:
                logger.warning("Портфель пуст или данные для мониторинга не найдены.")
                return
            logger.info(f"Найдено {len(portfolio_bonds)} облигаций в портфеле для проверки.")

        logger.info(f"Активные адаптеры: {', '.join(active_adapters)}")
        all_results = []

        # 3. Запускаем циклы проверок (теперь с оптимизированным потоком данных)
        for portfolio_bond in tqdm(portfolio_bonds, desc="Проверка облигаций"):
            bond_name = portfolio_bond.name or portfolio_bond.isin
            logger.debug(f"Проверка {bond_name} (ISIN: {portfolio_bond.isin})...")

            for adapter_name in active_adapters:
                adapter = self.adapter_factory.get_adapter(adapter_name, portfolio_bond)
                
                if not adapter:
                    logger.warning(f"Не удалось создать адаптер '{adapter_name}' для ISIN {portfolio_bond.isin}. Пропускаем.")
                    continue

                try:
                    # Для адаптера credit_rating вызываем fetch с bond_data
                    if adapter_name == 'credit_rating':
                        result = adapter.fetch(portfolio_bond)
                    else:
                        result = adapter.fetch()
                    all_results.append(result)
                    if result is not None:
                        if result.status == 'changed':
                            logger.info(f"[{adapter_name}] Результат для {portfolio_bond.isin}: {result.status} - {result.message}")
                        else:
                            logger.debug(f"[{adapter_name}] Результат для {portfolio_bond.isin}: {result.status} - {result.message}")  
                except Exception as e:
                    logger.error(f"Критическая ошибка при выполнении адаптера '{adapter_name}' для {portfolio_bond.isin}: {e}", exc_info=True)
        
        # 4. Отправляем уведомления о значимых изменениях
        significant_changes = [res for res in all_results if res.is_significant]

        if significant_changes and self.notifier:
            logger.info(f"Найдено {len(significant_changes)} значительных изменений. Отправка уведомления...")
            report_text = self._format_changes_report(significant_changes)
            self.notifier.send_message(report_text)
        else:
            logger.info("Значительных изменений не найдено или уведомитель не настроен.")

        logger.info("✅ Use case завершен: Обновление отслеживаемых метрик.")

    def _format_changes_report(self, changes: List[MonitoringResult]) -> str:
        """Форматирует список изменений в единый отчет для Telegram."""
        header = "📢 Обнаружены важные изменения в портфеле!\n"
        report_lines = [header]
        for change in changes:
            bond_info = self.storage.get_bond_by_isin(change.isin)
            bond_name = bond_info.name if bond_info and bond_info.name else change.isin
            report_lines.append(f"*{bond_name}* ({change.isin}):\n- {change.message}\n")
        
        return "\n".join(report_lines)