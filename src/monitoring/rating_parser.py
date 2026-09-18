import logging
import requests
from bs4 import BeautifulSoup
from typing import Optional, Dict, Any
import re
import json

from src.moex.api_client import MoexApiClient

logger = logging.getLogger(__name__)

RATING_SCALE = {
    "AAA(RU)": 15, "AAA.RU": 15, "RUAAA": 15,
    "AA+(RU)": 14, "AA+.RU": 14, "RUAA+": 14,
    "AA(RU)": 13, "AA.RU": 13, "RUAA": 13,
    "AA-(RU)": 12, "AA-.RU": 12, "RUAA-": 12,
    "A+(RU)": 11, "A+.RU": 11, "RUA+": 11,
    "A(RU)": 10, "A.RU": 10, "RUA": 10,
    "A-(RU)": 9, "A-.RU": 9, "RUA-": 9,
    "BBB+(RU)": 8, "BBB+.RU": 8, "RUBBB+": 8,
    "BBB(RU)": 7, "BBB.RU": 7, "RUBBB": 7,
    "BBB-(RU)": 6, "BBB-.RU": 6, "RUBBB-": 6,
    "BB+(RU)": 5, "BB+.RU": 5, "RUBB+": 5,
    "BB(RU)": 4, "BB.RU": 4, "RUBB": 4,
    "BB-(RU)": 3, "BB-.RU": 3, "RUBB-": 3,
    "B+(RU)": 2, "B+.RU": 2, "RUB+": 2,
    "B(RU)": 1, "B.RU": 1, "RUB": 1,
    "B-(RU)": 0, "B-.RU": 0, "RUB-": 0,
    # и другие возможные варианты, включая C-уровни
    "CCC(RU)": -1, "CCC.RU": -1, "RUCCC": -1,
    "CC(RU)": -2, "CC.RU": -2, "RUCC": -2,
    "C(RU)": -3, "C.RU": -3, "RUC": -3,
    "RD": -4, "D": -5,
    "AAA": 15, "AA+": 14, "AA": 13, "AA-": 12,
    "A+": 11, "A": 10, "A-": 9,
    "BBB+": 8, "BBB": 7, "BBB-": 6,
    "BB+": 5, "BB": 4, "BB-": 3,
    "B+": 2, "B": 1, "B-": 0
}

def get_rating_from_moex(isin: str, client: MoexApiClient) -> Optional[Dict[str, str]]:
    """
    Получает кредитный рейтинг с Московской биржи.
    Возвращает самый последний по дате присвоения рейтинг.
    """
    # Сначала нужен SECID
    sec_info = client._get_secid_and_board(isin)
    if not sec_info or not sec_info.get('secid'):
        logger.warning(f"MOEX: Не удалось получить SECID для {isin}")
        return None
    
    secid = sec_info['secid']
    ratings = client.get_security_rating(secid)

    if not ratings:
        return None

    # Рейтингов может быть несколько от разных агентств.
    # Логика: берем самый свежий по 'rating_date'
    # MOEX отдает даты в формате 'YYYY-MM-DD HH:MM:SS'
    latest_rating = max(ratings, key=lambda r: r['rating_date'])
    
    rating_value = latest_rating.get('rating_value_code') # 'AAA.ru'
    
    if rating_value:
        # Для совместимости с остальной системой, вернем словарь
        desc = client.get_security_description(isin)
        name = desc.get('NAME', '') if desc else ''
        return {"rating": rating_value, "name": name, "ticker": secid}
        
    return None


def get_rating_from_smartlab(isin: str, **kwargs) -> Optional[Dict[str, str]]:
    """
    Получает кредитный рейтинг с сайта smart-lab.ru.
    Ищет рейтинг в новой структуре страницы с прогресс-баром.
    """
    url = f"https://smart-lab.ru/q/bonds/{isin}/"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        # Ищем строки с классом quotes-simple-table__row
        rows = soup.find_all('div', class_='quotes-simple-table__row')
        for row in rows:
            items = row.find_all('div', class_='quotes-simple-table__item')
            if len(items) >= 2:
                # Проверяем, содержит ли первый элемент текст "Кредитный рейтинг"
                first_item_text = items[0].get_text(strip=True)
                if 'Кредитный рейтинг' in first_item_text:
                    # Ищем прогресс-бар с рейтингом
                    progress_bar = items[1].find('div', class_='linear-progress-bar')
                    if progress_bar:
                        # Ищем текст рейтинга внутри прогресс-бара
                        rating_text = progress_bar.find('div', class_='linear-progress-bar__text')
                        if rating_text:
                            rating_value = rating_text.get_text(strip=True)
                            if rating_value:
                                logger.debug(f"Smart-lab: найден рейтинг '{rating_value}' для {isin}")
                                return {"rating": rating_value, "name": isin, "ticker": isin}
        
        logger.debug(f"Smart-lab: рейтинг для {isin} не найден на странице.")
        return None

    except requests.RequestException as e:
        logger.warning(f"Smart-lab: Ошибка при запросе к {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"Smart-lab: Непредвиденная ошибка при парсинге {isin}: {e}", exc_info=True)
        return None



