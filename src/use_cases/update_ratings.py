import logging
import argparse
import time
from typing import Dict, Any, Optional, TYPE_CHECKING, List, Tuple, Union
from tqdm import tqdm

from src.use_cases.base import UseCase
from src.storage import PortfolioStorage
from src.monitoring.notifier import TelegramNotifier
from src.monitoring.adapters.factory import AdapterFactory
from src.monitoring.adapters.base import MonitoringResult
from src.data_models import RatingTarget

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

# Скоупы обновления (002.9, Р1/Р3):
# portfolio — бумаги портфеля, все адаптеры monitoring_adapters (ночной прогон);
# missing/catalog — дозаполнение рейтингов по каталогу, только credit_rating:
# остальные адаптеры требуют позицию/figi и бьют в T-Bank/лимитные API
# (Р5 оценивает catalog-прогон как 2-3 запроса по ISS и smart-lab на бумагу).
SCOPES = ('portfolio', 'missing', 'catalog')
RATINGS_ONLY_SCOPES = ('missing', 'catalog')


class UpdateRatingsUseCase(UseCase):
    """
    Проверяет и обновляет кредитные рейтинги, уровни листинга, риска
    и другие отслеживаемые метрики, уведомляя о значительных изменениях.

    Скоупы (002.9): portfolio — портфель (по умолчанию), missing — живые
    бумаги каталога с последней проверкой 'N/A' или без проверок,
    catalog — все живые бумаги каталога. --isin проверяет одну бумагу
    каталога (портфель не обязателен) и приоритетнее скоупа.
    """
    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды update_ratings."""
        parser.add_argument(
            '--isin',
            type=str,
            help='Указать ISIN конкретной облигации для проверки (по каталогу, портфель не обязателен).'
        )
        parser.add_argument(
            '--scope',
            type=str,
            choices=SCOPES,
            default='portfolio',
            help='Какие бумаги проверять: portfolio (по умолчанию), missing (без рейтинга/N/A), catalog (весь каталог).'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Ограничить число бумаг (для скоупов missing/catalog; для portfolio игнорируется).'
        )
        parser.add_argument(
            '--delay',
            type=float,
            default=0.5,
            help='Пауза между бумагами в секундах для скоупов missing/catalog (по умолчанию 0.5).'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'UpdateRatingsUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(
            storage=factory.get_db_connection(),
            notifier=factory.get_notifier(),
            use_case_factory=factory
        )

    def __init__(self, storage: PortfolioStorage, notifier: Optional[TelegramNotifier], use_case_factory: 'UseCaseFactory'):
        self.storage = storage
        self.notifier = notifier
        self.adapter_factory = AdapterFactory(use_case_factory)
        self.config = use_case_factory.config

    def _collect_targets(self, args: argparse.Namespace) -> Tuple[str, List[Union[RatingTarget, Any]]]:
        """Собирает список бумаг для проверки по --isin/скоупу.

        Возвращает (метка скоупа для логов/сводки, список таргетов).
        Таргеты: PortfolioBond для portfolio, RatingTarget для остальных.
        """
        if args.isin:
            if getattr(args, 'scope', 'portfolio') != 'portfolio':
                logger.info(f"Задан --isin: скоуп '{args.scope}' игнорируется (Р2, 002.9).")
            target = self.storage.get_bond_for_rating_refresh(args.isin)
            if not target:
                logger.warning(f"Облигация {args.isin} не найдена в каталоге.")
                return f"isin:{args.isin}", []
            return f"isin:{args.isin}", [target]

        scope = getattr(args, 'scope', 'portfolio') or 'portfolio'
        if scope == 'portfolio':
            if getattr(args, 'limit', None) is not None:
                logger.warning("--limit неприменим к скоупу portfolio — игнорируется (см. doc/commands/update-ratings.md).")
            targets = self.storage.get_portfolio_bonds_for_monitoring()
            if not targets:
                logger.warning("Портфель пуст или данные для мониторинга не найдены.")
            return scope, targets

        targets = self.storage.get_isins_for_rating_refresh(scope, limit=args.limit)
        if not targets:
            logger.info(f"Скоуп '{scope}': бумаг для проверки не найдено.")
        return scope, targets

    def _format_summary(self, scope_label: str, checked: int, rated: int, na: int, errors: int) -> str:
        """Итоговая сводка одной строкой (002.9, Р7) — её читает скилл finbonds-ratings."""
        return f"Итог: scope={scope_label}; checked={checked}; rated={rated}; na={na}; errors={errors}"

    def execute(self, args: argparse.Namespace):
        logger.info("🚀 Запуск use case: Обновление отслеживаемых метрик...")

        # 1. Получаем список адаптеров из конфига
        active_adapters = self.config.get('monitoring_adapters', [])
        if not active_adapters:
            logger.warning("В конфигурации 'monitoring_adapters' не указаны активные адаптеры. Пропускаем.")
            return

        scope_label, bonds = self._collect_targets(args)
        if not bonds:
            logger.info("✅ Use case завершен: нечего проверять.")
            return

        # Таргеты вне портфеля (RatingTarget) поддерживают только credit_rating:
        # остальные адаптеры завязаны на позицию/figi и бьют в T-Bank/лимитные
        # API (Р4/Р5, 002.9). Портфельный прогон — полный набор адаптеров.
        ratings_only = all(isinstance(b, RatingTarget) for b in bonds)
        if ratings_only and active_adapters != ['credit_rating']:
            adapters_to_run = ['credit_rating'] if 'credit_rating' in active_adapters else []
            logger.info("Скоуп вне портфеля: выполняется только адаптер credit_rating "
                        "(остальные адаптеры monitoring_adapters работают по портфелю).")
        else:
            adapters_to_run = active_adapters

        logger.info(f"Скоуп '{scope_label}': {len(bonds)} бумаг для проверки; адаптеры: {', '.join(adapters_to_run)}")

        # Пауза между бумагами для скоупов missing/catalog (Р5): без неё
        # 2-3 запроса × N бумаг по ISS и smart-lab попадают в лимиты/антибот.
        delay = float(getattr(args, 'delay', 0.0) or 0.0)
        throttled = scope_label in RATINGS_ONLY_SCOPES and delay > 0

        all_results = []
        errors = 0

        # 2. Запускаем циклы проверок (теперь с оптимизированным потоком данных)
        for idx, portfolio_bond in enumerate(tqdm(bonds, desc="Проверка облигаций")):
            if throttled and idx > 0:
                time.sleep(delay)

            bond_name = getattr(portfolio_bond, 'name', None) or portfolio_bond.isin
            logger.debug(f"Проверка {bond_name} (ISIN: {portfolio_bond.isin})...")

            for adapter_name in adapters_to_run:
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
                    errors += 1
                    logger.error(f"Критическая ошибка при выполнении адаптера '{adapter_name}' для {portfolio_bond.isin}: {e}", exc_info=True)

        # 3. Итоговая сводка (Р7): счётчики считаются по credit_rating
        rating_results = [r for r in all_results if r is not None and r.adapter_name == 'credit_rating']
        rated = sum(1 for r in rating_results if r.value and r.value != 'N/A')
        na = sum(1 for r in rating_results if r.value == 'N/A')
        logger.info(self._format_summary(scope_label, len(bonds), rated, na, errors))

        # 4. Отправляем уведомления о значительных изменениях — только для
        # портфельного скоупа и --isin (Р6): missing/catalog пишут в БД без
        # Telegram-спама. Сегодня credit_rating всегда is_significant=False,
        # но политика зафиксирована кодом, а не случаем.
        notifications_enabled = scope_label == 'portfolio' or scope_label.startswith('isin:')
        significant_changes = [res for res in all_results if res.is_significant]

        if notifications_enabled and significant_changes and self.notifier:
            logger.info(f"Найдено {len(significant_changes)} значительных изменений. Отправка уведомления...")
            report_text = self._format_changes_report(significant_changes)
            self.notifier.send_message(report_text)
        else:
            logger.info("Значительных изменений не найдено, уведомитель не настроен или скоуп без уведомлений.")

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
