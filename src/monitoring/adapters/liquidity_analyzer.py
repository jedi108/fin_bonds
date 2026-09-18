import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .base import BaseAdapter, MonitoringResult
from src.use_cases.interfaces import ITbankApiClient
from src.data_models import Bond
from src.utils import get_trading_session_info

logger = logging.getLogger(__name__)


class LiquidityAnalyzerAdapter(BaseAdapter):
    """
    Адаптер для анализа ликвидности облигаций на основе стакана заявок.
    
    Анализирует, насколько будет снижена стоимость при выходе из позиции 
    по рыночной цене для заданного объёма (например, 60 тыс. рублей).
    
    ВАЖНО: Ликвидность является биржевым свойством ISIN, а не свойством брокера.
    Стакан заявок одинаков для всех брокеров, торгующих на одной бирже (MOEX).
    Поэтому результаты анализа ликвидности применимы ко всем брокерам для данного ISIN.
    """

    @property
    def name(self) -> str:
        return 'liquidity_analyzer'

    def __init__(self, db, config, bond_data, **kwargs):
        super().__init__(db, config, bond_data, **kwargs)
        self.tbank_api_client = kwargs.get('tbank_api_client')
        if not self.tbank_api_client:
            token = kwargs.get('token') or config.get('TINVEST_TOKEN')
            if not token:
                raise ValueError("TBank API token is required for LiquidityAnalyzerAdapter")
            self.tbank_api_client = ITbankApiClient(token)

        # Целевая сумма для расчёта потерь (по умолчанию 60 000 рублей)
        self.target_amount = Decimal(config.get('LIQUIDITY_TARGET_AMOUNT', 60000))
        
        # Глубина стакана для анализа
        self.depth = int(config.get('LIQUIDITY_DEPTH', 20))
        
        self.verbose_name = "Анализ ликвидности"

    def fetch(self) -> MonitoringResult:
        """
        Получает и анализирует стакан заявок для облигации по её ISIN.
        Рассчитывает потенциальные потери при выходе из позиции по рыночной цене.
        """
        isin = self.bond_data.isin
        bond = self.bond_data

        if not bond:
            return self._handle_error(isin, f"Облигация с ISIN {isin} не найдена в БД")
        
        if not bond.figi:
            # Если FIGI не указан, пробуем получить его
            figi = self.tbank_api_client.get_figi_by_isin(isin)
            if not figi:
                return self._handle_error(isin, f"Не удалось получить FIGI для облигации {isin}")
            bond.figi = figi
        
        try:
            # Проверяем торговое время
            trading_info = get_trading_session_info()
            
            # Проверяем торговый статус конкретного инструмента
            trading_status = self.tbank_api_client.get_trading_status(bond.figi)
            
            # Получаем стакан заявок
            order_book = self.tbank_api_client.get_order_book(figi=bond.figi, depth=self.depth)
            if not order_book:
                return self._handle_error(isin, f"Не удалось получить стакан заявок для {isin}")
                
            # Рассчитываем потенциальные потери при выходе по рынку
            loss_ratio, market_exit_value, trading_warning = self._calculate_market_exit_loss(order_book, bond, trading_info, trading_status)
            
            if loss_ratio is None or market_exit_value is None:
                # Если торги не ведутся или стакан пуст, и это не ошибка, просто фиксируем недоступность
                self.db.add_monitoring_check(
                    isin, 
                    self.name, 
                    "N/A" # Сохраняем как N/A, если торги не ведутся
                )
                return MonitoringResult(
                    adapter_name=self.name,
                    isin=isin,
                    status='unavailable',
                    message=f"Анализ ликвидности недоступен: {trading_warning or 'Нет данных стакана или торги не проводятся'}"
                )

            # Сохраняем результат в БД как метрику мониторинга
            self.db.add_monitoring_check(
                isin, 
                self.name, 
                f"{loss_ratio:.4f}"  # Сохраняем коэффициент потерь с 4 знаками после запятой
            )
            
            # Сохраняем детальную информацию в историю ликвидности
            self._save_to_history(isin, self.target_amount, market_exit_value, loss_ratio)
            
            # Проверяем предыдущее значение для определения тренда
            last_check = self.db.get_last_monitoring_check(isin, self.name)
            previous_ratio = None
            # Здесь мы должны учитывать, что last_check[1] может быть "N/A"
            if last_check and last_check[1] != f"{loss_ratio:.4f}" and last_check[1] != "N/A":
                try:
                    previous_ratio = Decimal(last_check[1])
                except (ValueError, TypeError):
                    logger.warning(f"Невозможно преобразовать предыдущее значение '{last_check[1]}' в Decimal")
            
            # Формируем сообщение с учётом тренда и торговых ограничений
            # Собираем информацию о торговых ограничениях
            trading_limitations = {
                'trading_info': trading_info,
                'trading_status': trading_status,
                'warning': trading_warning
            }
            message = self._format_message(loss_ratio, previous_ratio, market_exit_value, trading_limitations)
            
            # Определяем значимость изменений
            is_significant = self._is_significant_change(loss_ratio, previous_ratio)
            
            return MonitoringResult(
                adapter_name=self.name,
                isin=isin,
                status='changed' if is_significant else 'success',
                message=message,
                value=str(loss_ratio),
                is_significant=is_significant
            )

        except Exception as e:
            logger.error(f"Ошибка при анализе ликвидности для {isin}: {e}", exc_info=True)
            return self._handle_error(isin, f"Ошибка анализа ликвидности: {e}")

    def _calculate_market_exit_loss(self, order_book: Dict, bond: Bond, trading_info: Dict = None, trading_status: Dict = None) -> Tuple[Decimal, Decimal, Optional[str]]:
        """
        Рассчитывает потенциальные потери при выходе из позиции по рыночной цене.
        
        Args:
            order_book: Стакан заявок
            bond: Объект облигации
            trading_info: Информация о торговых сессиях
            trading_status: Торговый статус конкретного инструмента
            
        Returns:
            Tuple[Decimal, Decimal, Optional[str]]: (коэффициент потерь в %, фактическая выручка, предупреждение о торговых ограничениях)
        """
        # Проверяем торговое время и статус инструмента
        instrument_not_trading = False
        trading_warning = None
        
        # Проверяем общее время торгов
        if trading_info and not trading_info.get('is_trading_hours'):
            trading_warning = trading_info.get('warning_message', 'Сейчас нерабочее время')
            instrument_not_trading = True
        # Проверяем торговый статус конкретного инструмента    
        elif trading_status:
            # Если API торги недоступны или торговля приостановлена
            if not trading_status.get('api_trade_available_flag') or not trading_status.get('market_order_available_flag'):
                if trading_info and trading_info.get('session_type') == 'evening':
                    trading_warning = "Инструмент недоступен в вечернюю торговую сессию"
                else:
                    trading_warning = "Торговля инструментом приостановлена"
                instrument_not_trading = True
        
        if instrument_not_trading and trading_warning:
            logger.warning(f"Анализ ликвидности для {bond.isin}: {trading_warning}")
        
        if not order_book or not order_book.get('bids'):
            # Если нет заявок, проверяем - возможно это из-за ограничений торговли
            if instrument_not_trading:
                logger.info(f"Пустой стакан для {bond.isin} может быть связан с ограничениями торговли: {trading_warning}. "
                            f"Возвращаем None для потерь ликвидности.")
                return None, None, trading_warning  # Возвращаем None, если торги не ведутся
            
            return Decimal(100), Decimal(0), trading_warning  # 100% потери, если нет ни одной заявки и торги ведутся
        
        # Номинал облигации
        nominal = bond.nominal or Decimal(1000)
        
        # Получаем заявки на покупку (bids)
        bids = order_book['bids']
        
        # Если нет заявок на покупку, возвращаем 100% потери
        if not bids:
            if instrument_not_trading:
                logger.info(f"Отсутствие заявок на покупку для {bond.isin} может быть связано с ограничениями торговли: {trading_warning}")
            return Decimal(100), Decimal(0), trading_warning
        
        # Получаем текущую цену (last_price)
        # Цена хранится в процентах от номинала (например, 90.5 означает 90.5% от номинала)
        last_price = order_book.get('last_price')
        if not last_price or last_price <= 0:
            # Если нет последней цены, берём лучшую цену покупки
            last_price = bids[0]['price'] if bids else Decimal(100)
        
        # Расчёт количества лотов для продажи
        # Переводим проценты в абсолютную цену: например, 90.5% от 1000 = 905 руб
        price_per_lot = nominal * last_price / Decimal(100)
        
        # Количество лотов, которые нужно продать для достижения целевой суммы
        # Например, для 60 000 руб при цене 905 руб за лот, необходимо 66.3 лотов (округляем вверх)
        lots_to_sell = self.target_amount / price_per_lot
        lots_to_sell = lots_to_sell.quantize(Decimal('1'), rounding='ROUND_UP')
        
        # Теоретическая выручка при продаже по текущей цене
        theoretical_value = lots_to_sell * price_per_lot
        
        # Фактическая выручка при продаже через стакан
        market_exit_value = Decimal(0)
        remaining_lots = lots_to_sell
        
        # Проходимся по стакану и рассчитываем фактическую выручку
        for bid in bids:
            if remaining_lots <= 0:
                break
                
            bid_price = bid['price']  # Цена в % от номинала
            bid_quantity = Decimal(str(bid['quantity']))  # Количество лотов в заявке
            
            # Сколько лотов мы можем продать по этой цене
            lots_sold_at_this_price = min(remaining_lots, bid_quantity)
            
            # Рассчитываем выручку от продажи по этой цене
            revenue_at_this_price = lots_sold_at_this_price * nominal * bid_price / Decimal(100)
            market_exit_value += revenue_at_this_price
            
            remaining_lots -= lots_sold_at_this_price
        
        # Если не хватило глубины стакана, оставшееся количество лотов
        # продаём по последней (наихудшей) доступной цене
        if remaining_lots > 0:
            worst_price = bids[-1]['price']
            revenue_at_worst_price = remaining_lots * nominal * worst_price / Decimal(100)
            market_exit_value += revenue_at_worst_price
            
            # Логируем предупреждение о недостаточной глубине стакана
            logger.warning(
                f"Недостаточная глубина стакана для {bond.isin}. "
                f"Не хватает заявок для продажи {remaining_lots} лотов из {lots_to_sell}. "
                f"Используется худшая цена {worst_price}%."
            )
        
        # Расчёт потерь и коэффициента потерь
        loss = theoretical_value - market_exit_value
        loss_ratio = (loss / theoretical_value * Decimal(100)) if theoretical_value > 0 else Decimal(100)
        
        logger.debug(
            f"Расчет потерь для {bond.isin}: "
            f"Ном.={nominal}, Цена={last_price}%, "
            f"Лотов={lots_to_sell}, "
            f"Теор.={theoretical_value}, "
            f"Факт.={market_exit_value}, "
            f"Потери={loss} ({loss_ratio:.2f}%)"
        )
        
        return loss_ratio, market_exit_value, trading_warning

    def _save_to_history(self, isin: str, base_volume: Decimal, 
                         market_exit_value: Decimal, loss_ratio: Decimal) -> None:
        """
        Сохраняет данные о ликвидности в таблицу liquidity_history.
        
        Args:
            isin: ISIN облигации
            base_volume: Базовый объем (целевая сумма)
            market_exit_value: Фактическая выручка при выходе по рынку
            loss_ratio: Коэффициент потерь в процентах
        """
        try:
            cursor = self.db.conn.cursor()
            cursor.execute("""
                INSERT INTO liquidity_history (timestamp, isin, base_volume, market_exit_value, loss_ratio, depth_level, data_source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (datetime.now().isoformat(), isin, str(base_volume), str(market_exit_value), str(loss_ratio), self.depth, 'TBank_API'))
            self.db.conn.commit()
            logger.debug(f"Сохранены данные о ликвидности для {isin}")
        except Exception as e:
            logger.error(f"Ошибка при сохранении истории ликвидности для {isin}: {e}", exc_info=True)

    def _format_message(self, loss_ratio: Decimal, previous_ratio: Optional[Decimal], 
                       market_exit_value: Decimal, trading_limitations: Dict = None) -> str:
        """
        Форматирует сообщение о ликвидности.
        """
        message = f"Потери при выходе из позиции на {self.target_amount} ₽: {loss_ratio:.2f}% (выручка ~{market_exit_value:,.0f} ₽)."
        
        if previous_ratio is not None and loss_ratio != previous_ratio:
            trend = "📈" if loss_ratio < previous_ratio else "📉"
            message += f" (ранее {previous_ratio:.2f}%) {trend}"
        
        # Добавляем информацию о торговых ограничениях
        if trading_limitations:
            # Предупреждение о сессии или статусе инструмента
            if trading_limitations.get('warning'):
                message += f"\n*Предупреждение:* {trading_limitations['warning']}."
            # Дополнительная информация о торговом статусе, если доступна
            elif trading_limitations.get('trading_status'):
                status_info = trading_limitations['trading_status']
                if not status_info.get('buy_available_flag'):
                    message += "\n*Запрет покупок.*"
                if not status_info.get('sell_available_flag'):
                    message += "\n*Запрет продаж.*"

        return message

    def _is_significant_change(self, current_ratio: Decimal, previous_ratio: Optional[Decimal]) -> bool:
        """
        Определяет, является ли изменение ликвидности значимым.
        """
        # Порог значимости - 5% потерь
        significance_threshold = Decimal(self.config.get('LIQUIDITY_SIGNIFICANCE_THRESHOLD', '5.0'))
        
        # Если потерь больше порога, изменение всегда значимо
        if current_ratio > significance_threshold:
            return True
            
        # Если было предыдущее значение, проверяем ухудшение
        if previous_ratio is not None:
            # Если раньше потерь было меньше порога, а теперь стало больше - это значимо
            if previous_ratio <= significance_threshold < current_ratio:
                return True
        
        return False

    def _handle_error(self, isin: str, message: str) -> MonitoringResult:
        """
        Обрабатывает ошибки, возникшие в адаптере.
        """
        logger.error(f"Ошибка в LiquidityAnalyzerAdapter для {isin}: {message}")
        return MonitoringResult(
            adapter_name=self.name,
            isin=isin,
            status='error',
            message=message
        )
    
