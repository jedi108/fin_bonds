#!/usr/bin/env python3
"""
Тестовый скрипт для проверки обработки пустого FIGI в TbankApiClient.
"""

import os
import logging
from dotenv import load_dotenv

from src.tbank.api_client import TbankApiClient

# Настройка логирования
logging.basicConfig(level=logging.DEBUG, 
                   format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

def test_empty_figi():
    """
    Проверяет обработку пустого FIGI в методе get_instrument_by_figi.
    """
    # Загрузка токена из переменных окружения
    load_dotenv()
    token = os.getenv('INVEST_TOKEN')
    if not token:
        logger.error("Токен API (INVEST_TOKEN) не найден в .env файле")
        return

    # Создание экземпляра клиента
    client = TbankApiClient(token)

    # Тестовые значения
    test_cases = [
        ("", "пустая строка"),
        (None, "None"),
        ("   ", "строка с пробелами"),
    ]

    # Проверка каждого тестового случая
    for figi, case_name in test_cases:
        logger.info(f"Тестовый случай: {case_name}")
        result = client.get_instrument_by_figi(figi)
        logger.info(f"Результат: {result}")

    logger.info("Все тесты выполнены.")

if __name__ == "__main__":
    test_empty_figi()
