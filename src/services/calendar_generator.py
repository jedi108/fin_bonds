import os
from typing import List, Union
from datetime import datetime
from ics import Calendar, Event

class CalendarGenerator:
    """
    Сервис для генерации .ics файлов с датами оферт по облигациям.
    """
    def __init__(self, output_dir: str = "."):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def generate_calendar(self, bonds: List[dict], filename: str = None) -> str:
        """
        Принимает список облигаций (dict с полями 'isin', 'name', 'offer_date') и создает .ics файл.
        Возвращает путь к созданному файлу.
        """
        cal = Calendar()
        for bond in bonds:
            offer_date = bond.get('offer_date')
            if not offer_date:
                continue
            # offer_date может быть datetime/date/str
            if isinstance(offer_date, str):
                try:
                    offer_date = datetime.fromisoformat(offer_date)
                except Exception:
                    continue
            elif not isinstance(offer_date, datetime):
                offer_date = datetime.combine(offer_date, datetime.min.time())
            event = Event()
            event.name = f"Оферта: {bond.get('name', bond.get('isin', ''))}"
            event.begin = offer_date.strftime('%Y-%m-%d')
            event.make_all_day()
            event.description = f"ISIN: {bond.get('isin', '')}"
            cal.events.add(event)
        if not filename:
            filename = f"offers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ics"
        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.writelines(cal.serialize_iter())
        return filepath 