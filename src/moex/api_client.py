from src.use_cases.interfaces import IMoexApiClient
import logging
import requests
from typing import Optional, Dict, Any, List
import pprint
from decimal import Decimal
from datetime import datetime
from dataclasses import dataclass
from datetime import date
from src.data_models import Bond

logger = logging.getLogger(__name__)

@dataclass
class MoexBondData:
    isin: str
    list_level: Optional[int] = None
    coupon_rate: Optional[Decimal] = None
    offer_date: Optional[date] = None

class MoexApiClient(IMoexApiClient):
    """
    Клиент для взаимодействия с MOEX ISS API.
    """
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.base_url = "https://iss.moex.com/iss"

    def _make_request(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Отправляет запрос к MOEX API и обрабатывает базовые ошибки."""
        try:
            # url уже должен быть полным
            response = self.session.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as err:
            if err.response.status_code == 404:
                logger.warning(f"Ресурс не найден: {url}")
            else:
                logger.error(f"Ошибка HTTP при запросе к {url}: {err.response.status_code} - {err.response.reason}")
            return None
        except requests.exceptions.RequestException as err:
            logger.error(f"Ошибка подключения при запросе к {url}: {err}")
            return None

    def get_security_description(self, isin: str) -> Optional[Dict[str, Any]]:
        """
        Получает описание ценной бумаги по её ISIN.
        """
        endpoint = f"/securities/{isin}.json"
        full_url = f"{self.base_url}{endpoint}"
        params = {
            'iss.meta': 'off',
            'iss.only': 'description',
            'description.columns': 'name,value'
        }
        data = self._make_request(full_url, params)
        if not data or 'description' not in data or not data['description']['data']:
            return None
        
        description_data = {row[0]: row[1] for row in data['description']['data']}
        description_data['ISIN'] = isin
        return description_data

    def get_raw_security_description(self, isin: str) -> Optional[Dict[str, Any]]:
        """
        Получает сырое, необработанное описание ценной бумаги по её ISIN.
        """
        endpoint = f"/securities/{isin}.json"
        params = {
            'iss.meta': 'off',
            'iss.only': 'description'
        }
        return self._make_request(endpoint, params)

    def get_market_data(self, isin: str) -> Optional[Dict[str, Any]]:
        """
        Получает рыночные данные для указанного ISIN.
        """
        logger.debug(f"get_market_data: {isin}")
        sec_info = self._get_secid_and_board(isin)
        if not sec_info:
            logger.error(f"get_market_data: _get_secid_and_board for {isin} returned None")
            return None
        
        secid, boardid, engine, market = sec_info['secid'], sec_info['boardid'], sec_info['engine'], sec_info['market']

        endpoint = f"/engines/{engine}/markets/{market}/securities/{secid}.json"
        full_url = f"{self.base_url}{endpoint}"
        params = {
            'iss.meta': 'off',
            'iss.only': 'marketdata,securities',
            'securities.columns': 'SECID,SECNAME,FACEVALUE,CURRENCYID,LISTLEVEL',
            'marketdata.columns': 'SECID,LAST,WAPRICE,LCURRENTPRICE,NUMTRADES'
        }
        data = self._make_request(full_url, params)

        if not data:
            return None
        
        securities_data = self._format_iss_response(data.get('securities', {}))
        marketdata_data = self._format_iss_response(data.get('marketdata', {}))

        if not securities_data or not marketdata_data:
            return None

        full_data = {**securities_data[0], **marketdata_data[0]}
        logger.debug(f"get_market_data: returning {full_data}")
        return full_data

    def get_bond_data(self, isin: str, nominal: Optional[Decimal] = None) -> Optional[MoexBondData]:
        """
        Получает основные данные по облигации с MOEX.
        """
        logger.debug(f"get_bond_data: {isin}")
        data = self.get_all_data_on_market_for_bond(isin)
        if not data:
            return None

        securities = self._format_iss_response(data.get('securities', {}))
        marketdata = self._format_iss_response(data.get('marketdata', {}))

        if not securities:
            return None

        sec_data = securities[0]
        mkt_data = marketdata[0] if marketdata else {}

        try:
            list_level = int(sec_data.get('LISTLEVEL', 0))
        except (ValueError, TypeError):
            list_level = 0
        
        offer_date_str = sec_data.get('OFFERDATE')
        offer_date = datetime.strptime(offer_date_str, '%Y-%m-%d').date() if offer_date_str and '0000-00-00' not in offer_date_str else None

        coupon_rate = None
        try:
            coupon_value = Decimal(str(mkt_data.get('COUPONVALUE', sec_data.get('COUPONVALUE', '0'))))
            coupon_period = int(sec_data.get('COUPONPERIOD', 0))
            
            if coupon_value > 0 and coupon_period > 0 and nominal and nominal > 0:
                # Расчет годовой ставки купона
                coupon_rate = (coupon_value / nominal) * (Decimal('365') / Decimal(coupon_period)) * 100
        except (ValueError, TypeError, KeyError):
            pass

        # Если расчет не удался, пробуем взять готовое значение
        if not coupon_rate or coupon_rate <= 0:
            try:
                coupon_rate_val = sec_data.get('COUPONPERCENT')
                if coupon_rate_val:
                    coupon_rate = Decimal(str(coupon_rate_val))
            except (ValueError, TypeError):
                pass
        
        logger.debug(f"get_bond_data: returning MoexBondData")
        return MoexBondData(
            isin=isin,
            list_level=list_level,
            coupon_rate=coupon_rate,
            offer_date=offer_date
        )

    def find_securities(self, query: str) -> Optional[List[Dict[str, Any]]]:
        """
        Ищет ценные бумаги по строке запроса.
        """
        logger.debug(f"find_securities: {query}")
        endpoint = "/securities.json"
        full_url = f"{self.base_url}{endpoint}"
        params = {
            'q': query,
            'iss.meta': 'off',
            'iss.only': 'securities',
            'securities.columns': 'secid,name,shortname,isin,regnumber,type,group,primary_boardid'
        }
        data = self._make_request(full_url, params)
        if not data:
            logger.error(f"find_securities: _make_request for {query} returned None")
            return None
        result = self._format_iss_response(data.get('securities', {}))
        logger.debug(f"find_securities: returning {len(result)} items")
        return result

    def raw_find_securities(self, query: str) -> Optional[Dict[str, Any]]:
        """
        Ищет ценные бумаги по строке запроса и возвращает сырой, необработанный ответ.
        """
        endpoint = "/securities.json"
        full_url = f"{self.base_url}{endpoint}"
        params = {
            'q': query,
            'iss.meta': 'off'
        }
        data = self._make_request(full_url, params)
        logger.debug(f"Получены сырые данные по инструменту: {pprint.pformat(data)}")
        return data

    def get_full_security_spec(self, secid: str) -> Optional[Dict[str, Any]]:
        """
        Получает полную спецификацию для указанного secid.
        ВНИМАНИЕ: может не работать для всех инструментов, используйте с осторожностью.
        """
        endpoint = f"/securities/{secid}.json"
        full_url = f"{self.base_url}{endpoint}"
        params = {'iss.meta': 'off'}
        return self._make_request(full_url, params)

    def get_candles(self, secid: str, from_date: str = "", interval: int = 24, engine: str = "stock", market: str = "bonds") -> Optional[List[Dict[str, Any]]]:
        """
        Получает исторические данные (свечи) для инструмента.
        """
        endpoint = f"/history/engines/{engine}/markets/{market}/securities/{secid}/candles.json"
        full_url = f"{self.base_url}{endpoint}"
        params = {'iss.meta': 'off'}
        if from_date:
            params['from'] = from_date
        if interval:
            params['interval'] = interval
            
        data = self._make_request(full_url, params)
        return self._format_iss_response(data.get('history', {})) if data else None

    def get_engines(self) -> Optional[List[Dict[str, Any]]]:
        """Получает список торговых систем."""
        endpoint = "/engines.json"
        full_url = f"{self.base_url}{endpoint}"
        params = {'iss.meta': 'off', 'iss.only': 'engines', 'engines.columns': 'name,title'}
        data = self._make_request(full_url, params)
        return self._format_iss_response(data.get('engines', {})) if data else None

    def get_markets(self, engine: str) -> Optional[List[Dict[str, Any]]]:
        """Получает список рынков для торговой системы."""
        endpoint = f"/engines/{engine}/markets.json"
        full_url = f"{self.base_url}{endpoint}"
        params = {'iss.meta': 'off', 'iss.only': 'markets', 'markets.columns': 'name,title,marketplace'}
        data = self._make_request(full_url, params)
        return self._format_iss_response(data.get('markets', {})) if data else None

    def get_all_data_on_market_for_bond(self, isin: str) -> Optional[Dict[str, Any]]:
        """
        Получает всю доступную информацию (все блоки) для облигации с учетом рынка.
        """
        logger.debug(f"get_all_data_on_market_for_bond: {isin}")
        sec_info = self._get_secid_and_board(isin)
        if not sec_info:
            logger.error(f"Не удалось получить secid и board для ISIN '{isin}'.")
            return None

        secid, engine, market = sec_info['secid'], sec_info['engine'], sec_info['market']

        endpoint = f"/engines/{engine}/markets/{market}/securities/{secid}.json"
        full_url = f"{self.base_url}{endpoint}"
        params = {
            'iss.meta': 'off',
        }
        data = self._make_request(full_url, params)
        logger.debug(f"get_all_data_on_market_for_bond: returning data")
        return data

    def _get_secid_and_board(self, isin: str) -> Optional[Dict[str, str]]:
        """Вспомогательный метод для получения SECID и основной торговой доски по ISIN, используя поиск."""
        logger.debug(f"_get_secid_and_board: {isin}")
        
        # Шаг 1: Найти бумагу по ISIN
        securities = self.find_securities(isin)
        if not securities:
            logger.error(f"MOEX API: find_securities для '{isin}' вернул пустой результат.")
            return None

        # ISIN - уникальный идентификатор, так что первая запись должна быть нашей.
        security = securities[0]
        
        secid = security.get('secid')
        boardid = security.get('primary_boardid')

        if not secid or not boardid:
            logger.error(f"_get_secid_and_board: secid или boardid не найдены для isin='{isin}' в ответе MOEX: {security}")
            return None

        # Для облигаций engine='stock' и market='bonds' являются стандартными.
        # Это допущение, но оно покрывает 99% случаев для этого проекта.
        result = {
            'secid': secid,
            'boardid': boardid,
            'engine': 'stock',
            'market': 'bonds'
        }
        logger.debug(f"_get_secid_and_board: returning {result}")
        return result

    def _format_iss_response(self, response_block: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Форматирует стандартный ответ от MOEX ISS API.
        """
        if not response_block or 'columns' not in response_block or 'data' not in response_block:
            return []
        
        columns = response_block['columns']
        data = response_block['data']
        
        return [dict(zip(columns, row)) for row in data]

    def fetch_all_bonds_data(self) -> Dict[str, Dict[str, Any]]:
        """
        Загружает данные по всем облигациям с основных торговых площадок MOEX.
        """
        all_bonds = {}
        boards = ['TQOB', 'TQCB', 'TQIR']
        columns = [
            'SECID', 'ISIN', 'LISTLEVEL', 'COUPONVALUE', 
            'COUPONPERIOD', 'ACCRUEDINT', 'NEXTCOUPON', 'COUPONPERCENT', 'OFFERDATE'
        ]

        logger.info("Загрузка всех облигаций с MOEX API. Это может занять некоторое время...")

        for board in boards:
            start = 0
            while True:
                endpoint = f"/engines/stock/markets/bonds/boards/{board}/securities.json"
                params = {
                    'iss.meta': 'off',
                    'iss.only': 'securities',
                    'securities.columns': ','.join(columns),
                    'limit': 100,
                    'start': start
                }
                
                data = self._make_request(endpoint, params)
                securities = self._format_iss_response(data.get('securities', {})) if data else []

                if not securities:
                    break

                for bond in securities:
                    isin = bond.get('ISIN')
                    if isin and isin not in all_bonds:
                        all_bonds[isin] = bond
                
                start += 100
            
            logger.info(f"Загружено {len(all_bonds)} уникальных облигаций после сканирования доски {board}.")

        logger.info(f"Всего загружено {len(all_bonds)} уникальных облигаций с MOEX.")
        return all_bonds

    def get_all_security_info_on_market(self, isin: str) -> Optional[Dict[str, Any]]:
        """
        Получает всю доступную информацию по инструменту на его основном рынке.
        """
        sec_info = self._get_secid_and_board(isin)
        if not sec_info:
            return None

        secid, engine, market = sec_info['secid'], sec_info['engine'], sec_info['market']

        endpoint = f"/engines/{engine}/markets/{market}/securities/{secid}.json"
        params = {
            'iss.meta': 'off',
        }
        return self._make_request(endpoint, params)

    def get_bonds_data_by_tickers(self, tickers: List[str]) -> List[Bond]:
        """
        Получает и обновляет данные по облигациям из MOEX по списку тикеров.
        """
        if not tickers:
            return []

        tickers_query = ",".join(tickers)
        url = (
            f"https://iss.moex.com/iss/securities.json"
            f"?q={tickers_query}&iss.meta=off&securities.columns=SECID,ISIN,SHORTNAME,FACEUNIT,FACEVALUE,MATDATE,LISTLEVEL,STATUS"
        )

        try:
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()
            
            bonds_data = data['securities']['data']
            if not bonds_data:
                logger.warning(f"Не найдено данных для тикеров: {tickers_query}")
                return []

            bonds_list = []
            for item in bonds_data:
                if len(item) < 8:
                    logger.warning(f"Неполные данные от MOEX для элемента: {item}. Пропуск.")
                    continue
                bond = Bond(
                    ticker=item[0],
                    isin=item[1],
                    name=item[2],
                    currency=item[3],
                    nominal=Decimal(str(item[4])) if item[4] else None,
                    maturity_date=datetime.strptime(item[5], '%Y-%m-%d').date() if item[5] else None,
                    list_level=item[6],
                    is_trade_available=item[7] == 'A'
                )
                bonds_list.append(bond)
            
            return bonds_list

        except requests.RequestException as e:
            logger.error(f"Ошибка сети при запросе данных по тикерам: {e}")
            return []
        except (ValueError, KeyError) as e:
            logger.error(f"Ошибка парсинга ответа от MOEX для тикеров: {e}")
            return []

    def get_bond_by_isin(self, isin: str) -> Optional[Dict[str, Any]]:
        """
        Получает основную информацию по облигации по ее ISIN.
        Возвращает словарь с данными или None, если бумага не найдена.
        """
        logger.debug(f"get_bond_by_isin: {isin}")
        data = self.get_all_data_on_market_for_bond(isin)
        if not data:
            logger.error(f"get_bond_by_isin: get_all_data_on_market_for_bond for {isin} returned None")
            return None

        securities = self._format_iss_response(data.get('securities', {}))
        marketdata = self._format_iss_response(data.get('marketdata', {}))

        if not securities:
            logger.error(f"get_bond_by_isin: 'securities' block is missing for {isin}")
            return None

        security_data = securities[0]
        market_data = marketdata[0] if marketdata else {}

        # Объединяем информацию
        full_data = {**security_data, **market_data}
        
        # Преобразуем некоторые значения для консистентности
        if 'LISTLEVEL' in full_data:
            try:
                full_data['list_level'] = int(full_data['LISTLEVEL'])
            except (ValueError, TypeError):
                full_data['list_level'] = None

        logger.debug(f"get_bond_by_isin: returning full_data")
        return full_data

    @staticmethod
    def _parse_date(date_str: str) -> Optional[date]:
        """Преобразует строку в дату, обрабатывая некорректные форматы."""
        try:
            return datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return None

    def get_security_rating(self, secid: str) -> Optional[Dict[str, Any]]:
        """
        Получает кредитный рейтинг для указанного secid.
        """
        endpoint = f"/securities/{secid}/credit_ratings.json"
        full_url = f"{self.base_url}{endpoint}"
        params = {
            'iss.meta': 'off',
        }
        data = self._make_request(full_url, params)
        return self._format_iss_response(data.get('ratings', {})) if data else None

# --- Старая функция больше не нужна, так как мы переходим на массовую загрузку ---
# def get_bond_data_from_moex(isin: str) -> Optional[Dict[str, Any]]:
#     """
#     Получает данные по облигации с Московской биржи по её ISIN.
#     Оставлено для обратной совместимости. Рекомендуется использовать MoexApiClient.
#     """
#     client = MoexApiClient()
#     return client.get_security_description(isin) 