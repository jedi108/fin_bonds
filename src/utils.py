import logging
import os
from datetime import date, datetime, time, timedelta
from typing import List, Any, Dict
from pathlib import Path

import yaml
import pytz


def setup_logging(level=logging.INFO):
    """
    Настраивает базовую конфигурацию логирования.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(module)s - %(message)s",
        handlers=[logging.StreamHandler()]
    )


# Корень проекта: utils.py лежит в src/, parents[1] — каталог проекта.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Относительные пути верхнего уровня config.yaml, привязываемые к корню проекта.
_PROJECT_ROOTED_KEYS = ("portfolio_file", "watchlist_file", "portfolio_isins_txt_file")


def _anchor_to_project_root(value: Any) -> Any:
    """Перепривязывает относительный путь к корню проекта; абсолютный не меняет."""
    if isinstance(value, str) and value and not Path(value).is_absolute():
        return str(PROJECT_ROOT / value)
    return value


def load_config() -> Dict[str, Any]:
    """
    Загружает конфигурационный файл config.yaml из корня проекта
    (независимо от текущей директории запуска) и привязывает относительные
    пути данных из него к корню проекта.
    """
    config_path = PROJECT_ROOT / "config.yaml"
    if not config_path.is_file():
        # Эта ситуация обрабатывается в main.py, но дублируем для безопасности
        raise FileNotFoundError(f"Файл конфигурации '{config_path}' не найден.")

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    for key in _PROJECT_ROOTED_KEYS:
        if key in config:
            config[key] = _anchor_to_project_root(config[key])

    plotter_config = config.get('plotter')
    if isinstance(plotter_config, dict) and 'output_dir' in plotter_config:
        plotter_config['output_dir'] = _anchor_to_project_root(plotter_config['output_dir'])

    export_config = config.get('export_portfolio')
    if isinstance(export_config, dict) and 'gcreds_filename' in export_config:
        export_config['gcreds_filename'] = _anchor_to_project_root(export_config['gcreds_filename'])

    return config


def load_isins_from_file(file_path: str) -> List[str]:
    """
    Загружает список ISIN-кодов из текстового файла.
    Каждый ISIN должен быть на новой строке.
    Пустые строки и лишние пробелы игнорируются.
    """
    if not file_path:
        logging.warning("Путь к файлу с ISIN не указан.")
        return []
    
    path = Path(file_path)
    if not path.is_file():
        logging.warning(f"Файл с ISIN '{path.resolve()}' не найден. Возвращен пустой список.")
        return []

    try:
        with open(path, 'r', encoding='utf-8') as f:
            isins = [line.strip() for line in f if line.strip()]
            logging.info(f"Загружено {len(isins)} ISIN-кодов из файла '{path.name}'.")
            return isins
    except Exception as e:
        logging.error(f"Ошибка при чтении файла с ISIN '{path.resolve()}': {e}")
        return []


def is_trading_hours() -> bool:
    """
    Проверяет, является ли текущее время торговым временем на Московской бирже.
    
    Returns:
        bool: True, если сейчас торговое время.
    """
    moscow_tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(moscow_tz)
    current_time = now.time()
    weekday = now.weekday()  # 0 = понедельник, 6 = воскресенье
    
    # Проверяем, что это рабочий день (понедельник-пятница)
    if weekday >= 5:  # суббота или воскресенье
        return False
    
    # Основная торговая сессия: 10:00 - 18:50
    main_session_start = time(10, 0)
    main_session_end = time(18, 50)
    
    # Вечерняя торговая сессия: 19:05 - 23:50
    evening_session_start = time(19, 5)
    evening_session_end = time(23, 50)
    
    # Проверяем попадание в торговые сессии
    return (main_session_start <= current_time <= main_session_end) or \
           (evening_session_start <= current_time <= evening_session_end)


def get_trading_session_info() -> Dict[str, Any]:
    """
    Возвращает информацию о текущей торговой сессии.
    
    Returns:
        Dict с информацией о торговой сессии.
    """
    moscow_tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(moscow_tz)
    current_time = now.time()
    weekday = now.weekday()
    
    # Основная торговая сессия: 10:00 - 18:50
    main_session_start = time(10, 0)
    main_session_end = time(18, 50)
    
    # Вечерняя торговая сессия: 19:05 - 23:50
    evening_session_start = time(19, 5)
    evening_session_end = time(23, 50)
    
    result = {
        "current_time": now,
        "is_weekend": weekday >= 5,
        "is_trading_hours": False,
        "session_type": None,
        "next_session_start": None,
        "warning_message": None
    }
    
    if weekday >= 5:
        result["warning_message"] = "Выходной день. Торги не проводятся."
        # Следующая сессия - понедельник 10:00
        days_until_monday = 7 - weekday
        next_monday = now.replace(hour=10, minute=0, second=0, microsecond=0)
        next_monday += timedelta(days=days_until_monday)
        result["next_session_start"] = next_monday
        return result
    
    if main_session_start <= current_time <= main_session_end:
        result["is_trading_hours"] = True
        result["session_type"] = "main"
    elif evening_session_start <= current_time <= evening_session_end:
        result["is_trading_hours"] = True
        result["session_type"] = "evening"
    else:
        # Определяем следующую сессию
        if current_time < main_session_start:
            # До основной сессии
            next_session = now.replace(hour=10, minute=0, second=0, microsecond=0)
            result["next_session_start"] = next_session
            result["warning_message"] = "Торги начнутся в 10:00 МСК"
        elif main_session_end < current_time < evening_session_start:
            # Между сессиями
            next_session = now.replace(hour=19, minute=5, second=0, microsecond=0)
            result["next_session_start"] = next_session
            result["warning_message"] = "Вечерняя торговая сессия начнется в 19:05 МСК"
        else:
            # После вечерней сессии
            next_day = now + timedelta(days=1)
            next_session = next_day.replace(hour=10, minute=0, second=0, microsecond=0)
            result["next_session_start"] = next_session
            result["warning_message"] = "Торги завершены. Следующая сессия завтра в 10:00 МСК"
    
    return result 