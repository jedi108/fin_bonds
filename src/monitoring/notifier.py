import logging
import requests
import os
from dotenv import load_dotenv
from typing import Dict, Optional

logger = logging.getLogger(__name__)

class TelegramNotifier:
    """Класс для отправки уведомлений в Telegram."""

    def __init__(self, config: Dict[str, any]):
        """
        Инициализирует уведомитель.
        Токен и ID чата загружаются из переменных окружения.
        """
        load_dotenv()
        self.enabled = config.get('enabled', False)
        self.bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.environ.get('TELEGRAM_CHAT_ID')
        
        if self.enabled and (not self.bot_token or not self.chat_id):
            logger.warning("Telegram-уведомления включены, но TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не найдены в .env. Уведомления будут отключены.")
            self.enabled = False

        if self.enabled:
            self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        else:
            self.api_url = ""

    def send_message(self, text: str) -> bool:
        """
        Отправляет текстовое сообщение в заданный чат.

        Args:
            text (str): Текст сообщения.

        Returns:
            bool: True, если сообщение успешно отправлено, иначе False.
        """
        if not self.enabled:
            # print("ℹ️ Уведомления отключены, пропуск отправки сообщения.")
            return False

        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': 'Markdown'
        }

        try:
            response = requests.post(self.api_url, data=payload, timeout=10)
            response.raise_for_status()
            if response.json().get("ok"):
                logger.info("Уведомление успешно отправлено в Telegram.")
                return True
            else:
                logger.error(f"Ошибка от API Telegram: {response.text}")
                return False
        except requests.RequestException as e:
            logger.error(f"Не удалось отправить уведомление в Telegram: {e}")
            return False 

    def send_document(self, file_path: str, caption: Optional[str] = None):
        """
        Отправляет файл (например, .ics) в Telegram.
        """
        if not self.enabled:
            return
        if not self.bot_token or not self.chat_id:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        with open(file_path, 'rb') as f:
            files = {'document': f}
            data = {'chat_id': self.chat_id}
            if caption:
                data['caption'] = caption
            response = requests.post(url, data=data, files=files)
            if response.status_code != 200:
                logging.error(f"Ошибка отправки файла в Telegram: {response.text}") 