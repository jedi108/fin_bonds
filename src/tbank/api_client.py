from src.use_cases.interfaces import ITbankApiClient
import logging
import pprint
from typing import List, Optional, Dict, Any
from decimal import Decimal
from functools import lru_cache

from tinkoff.invest import Client, PortfolioResponse, Instrument, GetBondCouponsResponse
from tinkoff.invest.utils import quotation_to_decimal
from src.data_models import PortfolioPosition, InstrumentInfo

logger = logging.getLogger(__name__)


class TbankApiClient(ITbankApiClient):
    def __init__(self, token: str):
        if not token:
            raise ValueError("TBank API token is required.")
        self.token = token

    def get_portfolio_positions(self) -> List[PortfolioPosition]:
        """Получает и преобразует все позиции по всем счетам в DTO."""
        positions = []
        with Client(self.token) as client:
            accounts = client.users.get_accounts().accounts
            for account in accounts:
                logger.info(f"Загрузка портфеля для счета: {account.name} ({account.id})")
                portfolio: PortfolioResponse = client.operations.get_portfolio(account_id=account.id)
                
                # Получаем информацию по каждому инструменту отдельно
                figi_list = [p.figi for p in portfolio.positions]
                instruments_info = self._get_instruments_by_figi_list(client, figi_list)

                for pos in portfolio.positions:
                    instrument = instruments_info.get(pos.figi)
                    if not instrument:
                        logger.warning(
                            f"Не удалось найти информацию по FIGI: '{pos.figi}'. "
                            f"Пропуск позиции: Type='{pos.instrument_type}'."
                        )
                        continue
                    
                    # Пропускаем все, что не является облигацией
                    if instrument.instrument_type != 'bond':
                        logger.debug(f"Пропуск инструмента {instrument.ticker} (тип: {instrument.instrument_type}).")
                        continue

                    positions.append(PortfolioPosition(
                        isin=instrument.isin,
                        ticker=instrument.ticker,
                        name=instrument.name,
                        quantity=self._convert_money_value(pos.quantity),
                        average_price=self._convert_money_value(pos.average_position_price),
                        current_price=self._convert_money_value(pos.current_price),
                        yield_to_maturity=self._convert_money_value(pos.expected_yield),
                        broker_name="TBank",
                        portfolio_percent=Decimal(0),
                        current_value=Decimal(0),
                        instrument_type=instrument.instrument_type, # Добавлено: передаем instrument_type
                    ))
        return positions

    def get_trading_status(self, figi: str) -> Optional[Dict[str, Any]]:
        """
        Получает информацию о торговом статусе инструмента.
        
        Args:
            figi: Идентификатор инструмента.
            
        Returns:
            Dict[str, Any]: Информация о торговом статусе или None при ошибке.
        """
        if not figi:
            logger.warning("Попытка запросить торговый статус с пустым FIGI. Возвращаем None.")
            return None
            
        with Client(self.token) as client:
            try:
                status = client.market_data.get_trading_status(figi=figi)
                
                return {
                    "figi": status.figi,
                    "trading_status": status.trading_status,
                    "limit_order_available_flag": status.limit_order_available_flag,
                    "market_order_available_flag": status.market_order_available_flag,
                    "api_trade_available_flag": status.api_trade_available_flag
                }
                
            except Exception as e:
                logger.error(f"Ошибка при получении торгового статуса для FIGI {figi}: {e}")
                return None

    def get_order_book(self, figi: str, depth: int = 20) -> Dict[str, Any]:
        """
        Получает стакан заявок (ордербук) по FIGI инструмента.
        
        Args:
            figi: Идентификатор инструмента.
            depth: Глубина стакана (от 1 до 20).
            
        Returns:
            Dict[str, Any]: Словарь с информацией о стакане заявок.
        """
        if not figi:
            logger.warning("Попытка запросить стакан с пустым FIGI. Возвращаем None.")
            return None
        
        # Ограничиваем глубину стакана
        if depth < 1:
            depth = 1
        elif depth > 20:
            depth = 20
            
        with Client(self.token) as client:
            try:
                # Получаем данные стакана
                order_book = client.market_data.get_order_book(figi=figi, depth=depth)
                
                # Преобразуем в более удобный формат для работы
                result = {
                    "figi": order_book.figi,
                    "depth": depth,
                    "bids": [],  # Заявки на покупку
                    "asks": [],  # Заявки на продажу
                    "last_price": self._convert_money_value(order_book.last_price) if order_book.last_price else None,
                    "close_price": self._convert_money_value(order_book.close_price) if order_book.close_price else None,
                    "time": getattr(order_book, 'time', None),
                    "limit_up": self._convert_money_value(order_book.limit_up) if order_book.limit_up else None,
                    "limit_down": self._convert_money_value(order_book.limit_down) if order_book.limit_down else None,
                }
                
                # Обработка заявок на покупку
                for bid in order_book.bids:
                    result["bids"].append({
                        "price": self._convert_money_value(bid.price),
                        "quantity": bid.quantity
                    })
                
                # Обработка заявок на продажу
                for ask in order_book.asks:
                    result["asks"].append({
                        "price": self._convert_money_value(ask.price),
                        "quantity": ask.quantity
                    })
                
                return result
                
            except Exception as e:
                logger.error(f"Ошибка при получении стакана заявок для FIGI {figi}: {e}", exc_info=True)
                return None

    def get_market_prices(self, figi_list: List[str]) -> Dict[str, Decimal]:
        """
        Получает рыночные цены для списка FIGI.
        
        Args:
            figi_list: Список FIGI для получения цен
            
        Returns:
            Dict[str, Decimal]: Словарь {figi: price} с рыночными ценами
        """
        if not figi_list:
            return {}
            
        prices = {}
        with Client(self.token) as client:
            try:
                # Получаем последние цены для всех FIGI
                response = client.market_data.get_last_prices(figi=figi_list)
                
                for price_info in response.last_prices:
                    if price_info.figi and price_info.price:
                        # Конвертируем цену из quotation в Decimal
                        price = quotation_to_decimal(price_info.price)
                        prices[price_info.figi] = price
                        
            except Exception as e:
                logger.error(f"Ошибка при получении рыночных цен: {e}", exc_info=True)
                
        return prices

    @lru_cache(maxsize=1)
    def get_instrument_by_figi(self, figi: str) -> Optional[InstrumentInfo]:
        """
        Получает информацию об одном инструменте по FIGI с кэшированием.
        
        Args:
            figi: Идентификатор FIGI инструмента. Не должен быть пустым.
            
        Returns:
            Optional[InstrumentInfo]: Информация об инструменте или None, если инструмент не найден
                                    или передан пустой FIGI.
        """
        if not figi:
            logger.warning("Попытка запросить инструмент с пустым FIGI. Возвращаем None.")
            return None
            
        with Client(self.token) as client:
            try:
                # Шаг 1: Получаем базовую информацию
                instrument_response = client.instruments.get_instrument_by(id_type=1, id=figi)
                logger.debug(f"RAW get_instrument_by response:\n{pprint.pformat(instrument_response)}")
                instrument = instrument_response.instrument
                if not instrument:
                    logger.warning(f"Не удалось найти инструмент по FIGI: {figi}")
                    return None
                
                risk_level = 0
                # Шаг 2: Если это облигация, запрашиваем полную информацию для нее, чтобы получить risk_level
                if instrument.instrument_type == 'bond':
                    bond_response = client.instruments.bond_by(id_type=1, id=figi)
                    logger.debug(f"RAW bond_by response:\n{pprint.pformat(bond_response)}")
                    # risk_level в Bond - это enum, берем его числовое значение (.value)
                    if bond_response and bond_response.instrument and bond_response.instrument.risk_level:
                        risk_level = bond_response.instrument.risk_level.value
                
                # Шаг 3: Собираем DTO из базовой информации и обогащенных данных
                return self._instrument_to_dto(instrument, risk_level_override=risk_level)
                
            except Exception as e:
                # Логируем с exc_info=True для полного стектрейса
                logger.error(f"Tbank API Client: Ошибка при получении инструмента по FIGI {figi}: {e}", exc_info=True)
                return None

    def _get_instruments_by_figi_list(self, client: Client, figi_list: List[str]) -> Dict[str, InstrumentInfo]:
        """
        Получает информацию о нескольких инструментах по списку FIGI.
        Делает индивидуальные запросы для каждого FIGI.
        """
        if not figi_list:
            return {}
            
        instruments_info = {}
        for figi in figi_list:
            if not figi:
                logger.warning("Обнаружена позиция с пустым FIGI. Пропускаем.")
                continue

            # Используем уже существующий метод, который кэширует результаты
            # Но для этого ему нужен отдельный клиент, создадим его здесь
            # или лучше переделать get_instrument_by_figi, чтобы он принимал клиент
            
            # Временное решение: вызываем напрямую, чтобы избежать рекурсии и лишних клиентов
            try:
                instrument_response = client.instruments.get_instrument_by(id_type=1, id=figi) # 1 = FIGI
                instrument = instrument_response.instrument
                if instrument and instrument.figi:
                    instruments_info[instrument.figi] = self._instrument_to_dto(instrument)
                else:
                    logger.warning(f"Не получена полная информация для FIGI: '{figi}'")
            except Exception as e:
                # Часто API может вернуть ошибку для "технических" FIGI типа валюты, которые есть в портфеле
                logger.warning(f"Не удалось получить информацию для FIGI '{figi}': {e}")
        
        return instruments_info

    def _instrument_to_dto(self, instrument: Any, risk_level_override: Optional[int] = None) -> Any:
        """
        ПРЕОБРАЗОВАНИЕ В DTO ВРЕМЕННО ОТКЛЮЧЕНО.
        Возвращает "сырой" объект инструмента.
        """
        risk_level = risk_level_override if risk_level_override is not None else getattr(instrument, 'risk_level', 0)
        return InstrumentInfo(
            figi=instrument.figi,
            ticker=instrument.ticker,
            name=instrument.name,
            isin=instrument.isin,
            currency=instrument.currency,
            risk_level=risk_level,
            instrument_type=instrument.instrument_type
        )

    @staticmethod
    def _convert_money_value(money_value) -> Decimal:
        """Конвертирует денежное значение из формата Tinkoff API в Decimal."""
        if money_value is None:
            return Decimal(0)
        return Decimal(money_value.units) + Decimal(money_value.nano) / Decimal("1e9") 

    def _get_full_instrument_by_ticker(self, client: Client, ticker: str) -> Optional[Instrument]:
        """
        Ищет инструмент по тикеру, а затем получает полную информацию о нем по FIGI.
        Возвращает полный объект Instrument. Принимает существующий клиент.
        """
        try:
            # Шаг 1: Найти инструмент по тикеру
            find_response = client.instruments.find_instrument(query=ticker)
            if not find_response.instruments:
                logger.warning(f"Тикер '{ticker}' не найден через find_instrument.")
                return None

            found_instrument_short = None
            for instrument_short in find_response.instruments:
                if instrument_short.ticker.upper() == ticker.upper() and instrument_short.instrument_type == 'bond':
                    found_instrument_short = instrument_short
                    break
            
            if not found_instrument_short:
                logger.warning(f"Точное совпадение для тикера-облигации '{ticker}' не найдено.")
                return None
            
            # Шаг 2: Получить полную информацию по FIGI
            figi = found_instrument_short.figi
            full_instrument_response = client.instruments.get_instrument_by(id_type=1, id=figi) # 1 = FIGI
            return full_instrument_response.instrument

        except Exception as e:
            logger.error(f"Ошибка при полном поиске инструмента по тикеру {ticker}: {e}")
            return None

    def enrich_bonds_by_tickers(self, tickers: List[str]) -> List[Dict[str, Any]]:
        """
        Принимает список тикеров, находит их через TBank API и возвращает
        минимально необходимые данные (isin, ticker, name) для мержа в DataFrame.
        Использует быстрый метод find_instrument.
        """
        enriched_list = []
        with Client(self.token) as client:
            for ticker in tickers:
                try:
                    find_response = client.instruments.find_instrument(query=ticker)
                    if not find_response.instruments:
                        continue

                    found_bond = None
                    for instrument in find_response.instruments:
                        if instrument.ticker.upper() == ticker.upper() and instrument.instrument_type == 'bond':
                            found_bond = instrument
                            break

                    if found_bond:
                        enriched_list.append({
                            'isin': found_bond.isin,
                            'ticker': found_bond.ticker,
                            'name': found_bond.name,
                            'figi': found_bond.figi,
                            'currency': found_bond.currency,
                            'instrument_type': found_bond.instrument_type
                        })

                except Exception as e:
                    logger.error(f"Ошибка при поиске инструмента по тикеру {ticker}: {e}")

        return enriched_list

    def get_figi_by_isin(self, isin: str) -> Optional[str]:
        """
        Находит FIGI инструмента по ISIN.
        
        Args:
            isin: ISIN код инструмента.
            
        Returns:
            Optional[str]: FIGI инструмента или None, если инструмент не найден.
        """
        if not isin:
            return None
            
        with Client(self.token) as client:
            try:
                # Используем метод поиска по ISIN
                instruments = client.instruments.find_instrument(query=isin).instruments
                
                # Ищем точное совпадение по ISIN
                for instrument in instruments:
                    if instrument.isin == isin:
                        return instrument.figi
                        
                # Если не нашли
                logger.warning(f"Не удалось найти инструмент по ISIN: {isin}")
                return None
                
            except Exception as e:
                logger.error(f"Ошибка при поиске инструмента по ISIN {isin}: {e}")
                return None

    def get_all_bonds_with_risk(self) -> Dict[str, int]:
        """
        Получает список всех облигаций с их уровнем риска.
        
        Returns:
            Dict[str, int]: Словарь, где ключ - ISIN, значение - уровень риска.
        """
        result = {}
        with Client(self.token) as client:
            try:
                # Получаем все облигации
                bonds = client.instruments.bonds().instruments
                
                for bond in bonds:
                    if bond.isin:
                        result[bond.isin] = bond.risk_level.value
                
                return result
                
            except Exception as e:
                logger.error(f"Ошибка при получении всех облигаций: {e}")
                return {}

    def get_portfolio(self, account_id: str) -> Optional[PortfolioResponse]:
        """Получает портфель по ID аккаунта."""
        with Client(self.token) as client:
            try:
                return client.operations.get_portfolio(account_id=account_id)
            except Exception as e:
                logger.error(f"Ошибка при получении портфеля для аккаунта {account_id}: {e}")
                return None

    def find_instrument(self, query: str) -> List[Dict[str, Any]]:
        """
        Ищет инструмент по запросу (тикер, FIGI или название).
        
        Args:
            query: Строка поиска.
            
        Returns:
            List[Dict[str, Any]]: Список найденных инструментов.
        """
        with Client(self.token) as client:
            try:
                instruments = client.instruments.find_instrument(query=query).instruments
                
                result = []
                for instrument in instruments:
                    result.append({
                        'figi': instrument.figi,
                        'ticker': instrument.ticker,
                        'isin': instrument.isin,
                        'name': instrument.name,
                        'type': instrument.instrument_type,
                        'currency': instrument.currency
                    })
                
                return result
                
            except Exception as e:
                logger.error(f"Ошибка при поиске инструмента по запросу {query}: {e}")
                return []

    def get_bond_coupons(self, figi: str) -> Optional[Dict[str, Any]]:
        """
        Получает информацию о купонах облигации.
        
        Args:
            figi: FIGI облигации.
            
        Returns:
            Optional[Dict[str, Any]]: Информация о купонах или None в случае ошибки.
        """
        if not figi:
            return None
            
        with Client(self.token) as client:
            try:
                # Получаем информацию о купонах за последний и следующий год
                from datetime import datetime, timedelta
                from tinkoff.invest import GetBondCouponsRequest
                
                now = datetime.now()
                from_date = now - timedelta(days=365)
                to_date = now + timedelta(days=365)
                
                request = GetBondCouponsRequest(
                    figi=figi,
                    from_=from_date,
                    to=to_date
                )
                
                response: GetBondCouponsResponse = client.instruments.get_bond_coupons(request)
                
                # Преобразуем данные в более удобный формат
                result = {
                    'figi': figi,
                    'coupons': []
                }
                
                for coupon in response.events:
                    coupon_data = {
                        'date': coupon.date,
                        'value': self._convert_money_value(coupon.pay_one_bond),
                        'coupon_type': str(coupon.coupon_type),
                        'coupon_period': coupon.coupon_period
                    }
                    result['coupons'].append(coupon_data)
                
                return result
                
            except Exception as e:
                logger.error(f"Ошибка при получении купонов для FIGI {figi}: {e}")
                return None     # --- Interface compliance additions ---
    def get_bonds(self):
        """Возвращает список облигаций (заглушка для интерфейса)."""
        try:
            return list(self.get_all_bonds_with_risk().keys())  # simple placeholder
        except Exception:
            return []
