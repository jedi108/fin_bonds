from src.use_cases.interfaces import ICbrApiClient
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import logging
from decimal import Decimal

from src.constants import CBR_RUONIA_RATE_CODE

logger = logging.getLogger(__name__)

# URL для запроса динамики ставок межбанковского кредитного рынка
CBR_MKR_URL = "http://www.cbr.ru/scripts/xml_mkr.asp"


class CbrApiClient(ICbrApiClient):
    """
    Клиент для взаимодействия с API Центрального Банка России.
    Позволяет получать данные по ставке RUONIA.
    """

    def __init__(self, session: requests.Session = None):
        """
        Инициализирует клиент.

        Args:
            session: Опциональная сессия requests для переиспользования соединений.
        """
        self.session = session or requests.Session()

    def get_ruonia_rate_on_date(self, target_date: datetime.date) -> Decimal:
        """
        Получает значение ставки RUONIA на конкретную дату.
        Если на указанную дату ставки нет (выходной), ищет последнюю доступную
        ставку за предшествующие 7 дней.

        Args:
            target_date: Дата, на которую нужно получить ставку.

        Returns:
            Ставка RUONIA в процентах в виде Decimal.
        
        Raises:
            ValueError: Если ставка не найдена за указанный период.
        """
        # Ищем ставку в диапазоне [target_date - 7 дней, target_date]
        end_date = target_date
        start_date = end_date - timedelta(days=7)

        params = {
            "date_req1": start_date.strftime('%d/%m/%Y'),
            "date_req2": end_date.strftime('%d/%m/%Y')
        }

        try:
            response = self.session.get(CBR_MKR_URL, params=params)
            response.raise_for_status()
            response.encoding = 'windows-1251'
            root = ET.fromstring(response.content)

            # Ищем последнюю ставку RUONIA в полученном диапазоне
            latest_rate = None
            latest_date = None

            for record in root.findall('./Record'):
                # Код 7 соответствует ставке RUONIA
                if record.attrib.get('Code') == CBR_RUONIA_RATE_CODE:
                    rate_val_tag = record.find('C1')
                    # Проверяем, что тег существует, содержит текст и это не прочерк
                    if rate_val_tag is not None and rate_val_tag.text and rate_val_tag.text != '-':
                        record_date = datetime.strptime(record.attrib['Date'], '%d/%m/%Y').date()
                        # Мы ищем самую последнюю (максимальную) дату в диапазоне
                        if latest_date is None or record_date > latest_date:
                            latest_date = record_date
                            latest_rate = Decimal(rate_val_tag.text.replace(',', '.'))
            
            if latest_rate is not None:
                logger.info(f"Найдена ставка RUONIA {latest_rate}% на {latest_date.strftime('%d.%m.%Y')} для целевой даты {target_date.strftime('%d.%m.%Y')}.")
                return latest_rate

            raise ValueError(f"Не удалось найти ставку RUONIA для даты {target_date.strftime('%d.%m.%Y')}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при запросе к API ЦБ РФ для даты {target_date}: {e}")
            raise
        except ET.ParseError as e:
            logger.error(f"Ошибка парсинга XML ответа от API ЦБ РФ: {e}")
            raise
        except (ValueError, KeyError) as e:
            logger.error(f"Ошибка при обработке ответа от API ЦБ РФ: {e}")
            raise

    def get_latest_ruonia_rate(self) -> Decimal:
        """
        Получает самое последнее доступное значение ставки RUONIA.
        Это обертка над get_ruonia_rate_on_date для получения ставки на сегодня.

        Returns:
            Ставка RUONIA в процентах в виде Decimal.

        Raises:
            Exception: Если не удалось получить или распарсить данные.
        """
        # Просто вызываем новый метод с сегодняшней датой
        return self.get_ruonia_rate_on_date(datetime.now().date()) 