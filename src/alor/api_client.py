import logging
from typing import List, Optional, Dict, Any
from decimal import Decimal
import requests
from src.use_cases.interfaces import IAlorApiClient
from src.data_models import PortfolioPosition, Bond

logger = logging.getLogger(__name__)

class AlorApiClient(IAlorApiClient):
    def __init__(self, token: str, refresh_token: str, client_id: str, client_secret: str, login: str = None, db_path: str = None):
        if not token:
            raise ValueError("Alor API token is required.")
        self.token = token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = "https://api.alor.ru"  # Боевой контур
        self.login = login
        self.db_path = db_path
        
    def _refresh_access_token(self) -> bool:
        """Обновляет access-токен через refresh-токен. Возвращает True если успешно."""
        refresh_url = self.base_url.replace('api.', 'oauth')
        if 'apidev' in self.base_url:
            refresh_url = 'https://oauthdev.alor.ru/refresh?token=' + self.refresh_token
        else:
            refresh_url = 'https://oauth.alor.ru/refresh?token=' + self.refresh_token
        try:
            logger.info(f"Обновление access-токена через {refresh_url}")
            resp = requests.post(refresh_url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            new_token = data.get('AccessToken')
            if new_token:
                self.token = new_token
                logger.info("Access-токен успешно обновлён.")
                return True
            else:
                logger.error(f"Не удалось получить AccessToken из ответа: {data}")
                return False
        except Exception as e:
            logger.error(f"Ошибка при обновлении access-токена: {e}")
            return False

    def _get_current_price_from_orderbook(self, isin: str) -> Optional[Decimal]:
        """Получает текущую цену из стакана заявок."""
        try:
            url = f"{self.base_url}/md/v2/orderbooks/MOEX/{isin}"
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json'
            }
            
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 401:
                logger.warning("Access-токен истёк, пробую обновить...")
                if self._refresh_access_token():
                    headers['Authorization'] = f'Bearer {self.token}'
                    resp = requests.get(url, headers=headers, timeout=10)
            
            if resp.status_code != 200:
                logger.warning(f"Не удалось получить стакан заявок для {isin}: {resp.status_code}")
                return None
                
            data = resp.json()
            
            # Получаем лучшую цену покупки и продажи
            best_bid = data.get('bids', [{}])[0].get('price') if data.get('bids') else None
            best_ask = data.get('asks', [{}])[0].get('price') if data.get('asks') else None
            
            if best_bid and best_ask:
                # Используем среднюю цену между лучшей покупкой и продажей
                current_price_percent = (best_bid + best_ask) / 2
                logger.debug(f"Цена для {isin}: bid={best_bid}, ask={best_ask}, avg={current_price_percent}")
                return Decimal(str(current_price_percent))
            elif best_bid:
                # Если есть только покупки, используем лучшую цену покупки
                logger.debug(f"Только покупки для {isin}: {best_bid}")
                return Decimal(str(best_bid))
            elif best_ask:
                # Если есть только продажи, используем лучшую цену продажи
                logger.debug(f"Только продажи для {isin}: {best_ask}")
                return Decimal(str(best_ask))
            else:
                logger.warning(f"Нет данных в стакане заявок для {isin}")
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при получении стакана заявок для {isin}: {e}")
            return None

    def get_portfolio_positions(self) -> List[PortfolioPosition]:
        """Получает позиции портфеля из Alor API (через md/v2/Clients/:exchange/:portfolio/positions)."""
        logger.info("Получение позиций портфеля из Alor API (md/v2)...")
        positions: List[PortfolioPosition] = []
        try:
            exchange = "MOEX"  # Для российских облигаций
            # Извлекаем только первую часть логина (до слеша)
            portfolio_id = self.login.split('/')[0] if self.login else "D88199"
            url = f"{self.base_url}/md/v2/Clients/{exchange}/{portfolio_id}/positions"
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json'
            }
            logger.info(f"Запрос позиций для логина {portfolio_id}: {url}")
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 401:
                logger.warning("Access-токен истёк или невалиден, пробую обновить...")
                if self._refresh_access_token():
                    headers['Authorization'] = f'Bearer {self.token}'
                    resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 404:
                logger.warning(f"Портфель {portfolio_id} не найден или нет доступа.")
                return []
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Ответ Alor API: {data}")
            for pos in data:
                try:
                    # Фильтруем только облигации (исключаем валюты и другие инструменты)
                    symbol = pos.get('symbol', '')
                    if symbol == 'RUB' or pos.get('isCurrency', False):
                        logger.debug(f"Пропускаем валютную позицию: {symbol}")
                        continue
                    
                    # Проверяем, что это облигация (ISIN начинается с RU)
                    if not symbol.startswith('RU'):
                        logger.debug(f"Пропускаем не-облигацию: {symbol}")
                        continue
                    
                    # Получаем номинал из БД для корректировки цены
                    bond_data = self._get_bond_nominal(symbol)
                    nominal = bond_data if bond_data else Decimal('1000')  # Дефолтный номинал
                    
                    # Корректируем среднюю цену покупки: avgPrice в Alor API это % от номинала
                    avg_price_percent = Decimal(str(pos.get('avgPrice', 0)))
                    avg_price_corrected = (avg_price_percent / Decimal('100')) * nominal
                    
                    # Получаем текущую цену из стакана заявок
                    current_price_percent = self._get_current_price_from_orderbook(symbol)
                    if current_price_percent:
                        current_price_corrected = (current_price_percent / Decimal('100')) * nominal
                        logger.debug(f"Текущая цена для {symbol}: {current_price_corrected} (из стакана)")
                    else:
                        current_price_corrected = Decimal('0')
                        logger.warning(f"Не удалось получить текущую цену для {symbol}")
                    
                    positions.append(PortfolioPosition(
                        isin=symbol,  # В Alor API symbol содержит ISIN
                        ticker=symbol,
                        name=pos.get('shortName') or symbol,
                        quantity=Decimal(str(pos.get('qty', 0))),
                        average_price=avg_price_corrected,
                        current_price=current_price_corrected,
                        broker_name="Alor",
                        currency=pos.get('currency'),
                        instrument_type="corp_bond" if symbol.startswith("RU") and len(symbol) == 12 else None,
                        liquidity_loss_ratio=None,
                    ))
                except Exception as e:
                    logger.error(f"Ошибка преобразования позиции Alor: {e}")
            logger.info(f"Получено {len(positions)} позиций из Alor API.")
            return positions
        except Exception as e:
            logger.error(f"Ошибка при получении портфеля из Alor: {e}")
            return []
    
    def enrich_bonds_by_tickers(self, tickers: List[str]) -> List[Dict[str, Any]]:
        logger.info(f"Обогащение данных для {len(tickers)} тикеров из Alor API...")
        return []
    
    def get_bonds(self) -> List[Bond]:
        logger.info("Получение списка облигаций из Alor API...")
        return []
    
    def _make_request(self, endpoint: str, method: str = 'GET', **kwargs) -> Dict[str, Any]:
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json'
        }
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.request(method, url, headers=headers, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка запроса к Alor API: {e}")
            raise
    
    def _refresh_token_if_needed(self) -> bool:
        return True
    
    def _get_bond_nominal(self, isin: str) -> Optional[Decimal]:
        """Получает номинал облигации из БД."""
        try:
            # Импортируем здесь, чтобы избежать циклических импортов
            from src.monitoring.storage import Database
            if self.db_path:
                db = Database(self.db_path)
                bond_data = db.get_bond_by_isin(isin)
                if bond_data and bond_data.nominal:
                    return Decimal(str(bond_data.nominal))
            else:
                logger.warning(f"db_path не установлен, не удалось получить номинал для {isin}")
        except Exception as e:
            logger.warning(f"Не удалось получить номинал для {isin}: {e}")
        return None 