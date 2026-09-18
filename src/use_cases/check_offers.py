# ... existing code ...

import logging
import argparse
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, TYPE_CHECKING

from src.use_cases.base import UseCase
# from src.utils import load_isins_from_file # Эту строку можно будет удалить, если она больше нигде не используется в этом файле
from src.use_cases.interfaces import IPortfolioStorage
from src.monitoring.notifier import TelegramNotifier

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class CheckOffersUseCase(UseCase):
    """
    Проверяет приближающиеся даты оферт для облигаций из портфеля.
    """

    def __init__(self, storage: IPortfolioStorage, config: Dict[str, Any], notifier: Optional[TelegramNotifier]):
        self.storage = storage
        self.config = config
        self.notifier = notifier

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Эта команда не принимает дополнительных аргументов командной строки."""
        pass

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'CheckOffersUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(
            storage=factory.get_db_connection(),
            config=factory.config,
            notifier=factory.get_notifier()
        )

    def execute(self, args: argparse.Namespace):
        logging.info("🚀 Запуск проверки приближающихся оферт для облигаций...")

        if not self.notifier or not self.notifier.enabled:
            logging.warning("Уведомления отключены. Проверка оферт не будет выполнена.")
            return

        days_ahead = self.config.get('offer_alert_period_days', 30)
        logging.info(f"Период для оповещения об офертах: {days_ahead} дней.")

        # Получаем ISIN-коды из портфеля в базе данных
        portfolio_positions = self.storage.get_portfolio_positions()
        all_isins = {pos.isin for pos in portfolio_positions if pos.isin} # Используем set для уникальности

        if not all_isins:
            logging.warning("Портфель пуст. Проверка не требуется.")
            return

        today = datetime.now().date()
        alert_date_limit = today + timedelta(days=days_ahead)
        
        found_offers = 0
        spam_prevention_period = 30 # Период предотвращения спама в днях

        for isin in all_isins:
            bond = self.storage.get_bond_by_isin(isin)

            if not bond or not bond.offer_date:
                continue

            try:
                offer_date = bond.offer_date
            except (ValueError, TypeError):
                logging.warning(f"Некорректный формат даты оферты для {isin}: {bond.offer_date}. Пропускаем.")
                continue

            if today <= offer_date < alert_date_limit:
                # Проверка на спам уведомлениями
                last_notification = self.storage.get_last_monitoring_check(isin, 'offer_risk_notified')
                if last_notification:
                    notification_date, _ = last_notification
                    if (today - notification_date).days < spam_prevention_period:
                        logging.info(f"Уведомление об оферте для {isin} уже отправлялось недавно ({notification_date}). Пропускаем.")
                        continue

                days_left = (offer_date - today).days
                bond_name = bond.name
                ticker = bond.ticker if bond.ticker else isin
                
                if bond.floating_coupon_flag:
                    header = "⚠️ *Внимание: Риск изменения спреда!*"
                    message_body = (
                        f"У флоатера *{bond_name}* (`{ticker}`) *{offer_date.strftime('%d.%m.%Y')}* состоится оферта.\\n"
                        f"Текущий спред в базе: *{bond.coupon_spread if bond.coupon_spread is not None else 'N/A'}%*."
                        "\\n\\nРекомендация: После оферты и первой новой выплаты запустите `python main.py calculate-spread`, чтобы обновить спред."
                    )
                else:
                    header = "‼️ *Внимание: Риск досрочного погашения!*"
                    message_body = (
                        f"У облигации с фикс. купоном *{bond_name}* (`{ticker}`) *{offer_date.strftime('%d.%m.%Y')}* состоится call-оферта.\\n"
                        "Если ставки на рынке останутся низкими, эмитент может погасить бумагу досрочно. Вы рискуете потерять будущие высокие купоны."
                        "\\n\\nРекомендация: Оцените целесообразность удержания этой позиции в портфеле."
                    )
                
                self.notifier.send_message(f"{header}\\n{message_body}")
                logging.info(f"Отправлено уведомление по оферте для {isin} (осталось {days_left} дней).")
                self.storage.add_monitoring_check(isin, 'offer_risk_notified', 'notified') # Запись о том, что уведомление отправлено
                found_offers += 1

        if found_offers == 0:
            logging.info("Оферт, требующих внимания, в указанном периоде не найдено.")
        else:
            logging.info(f"Проверка завершена. Найдено и отправлено уведомлений: {found_offers}.")
