import logging
from typing import Callable, Dict, Optional

from src.moex.api_client import MoexApiClient

logger = logging.getLogger(__name__)

def get_rating_from_moex(isin: str, client: MoexApiClient) -> Optional[Dict[str, str]]:
    """
    Получает кредитный рейтинг с MOEX ISS через эндпоинт /iss/cci/rating
    (002.9, Р10): emitent_id по ISIN → карточка рейтингов выпуска.

    Старый источник /iss/securities/<secid>/credit_ratings.json не существует
    (MOEX отвечал карточкой бумаги без блока ratings — провайдер возвращал
    None для любой облигации). Ответ /cci содержит все три агентства
    (АКРА/Эксперт РА/НКР) и имя агентства — используется для сообщения
    пользователю, в БД не пишется (колонки agency в схеме нет).
    """
    records = client.get_security_rating_cci(isin)
    if not records:
        logger.warning(f"MOEX cci: рейтинги для {isin} не получены")
        return None

    # В эмитентском справочнике есть исторические записи 'Отозван' —
    # их исключаем и берём свежую по rating_date (даты в формате 'YYYY-MM-DD',
    # строки сравниваются лексикографически корректно).
    active = [r for r in records if r.get('rating_level_name_short_ru') != 'Отозван']
    if not active:
        logger.info(f"MOEX cci: все рейтинги {isin} отозваны")
        return None

    latest = max(active, key=lambda r: str(r.get('rating_date') or ''))
    rating_value = latest.get('rating_level_name_short_ru')
    if not rating_value:
        logger.warning(f"MOEX cci: у свежей записи {isin} пустой rating_level_name_short_ru")
        return None

    return {
        "rating": rating_value,
        "agency": latest.get('agency_name_short_ru') or '',
        "name": isin,
        "ticker": isin,
    }


def load_rating_providers() -> Dict[str, Callable[..., Optional[Dict[str, str]]]]:
    """Провайдеры рейтингов в порядке fallback: встроенный MOEX ISS, затем
    опциональные внешние из модуля `private_plugins.rating_providers`
    (словарь RATING_PROVIDERS той же сигнатуры: (isin, **kwargs) -> dict | None).
    Без модуля проект работает только на MOEX.
    """
    providers: Dict[str, Callable[..., Optional[Dict[str, str]]]] = {'moex': get_rating_from_moex}
    try:
        from private_plugins.rating_providers import RATING_PROVIDERS
    except ModuleNotFoundError as e:
        if e.name not in ('private_plugins', 'private_plugins.rating_providers'):
            # модуль есть, но сломан (нет зависимости) — не молчим
            logger.warning(f"private_plugins.rating_providers не загружен: {e}")
        else:
            logger.debug("Дополнительные провайдеры рейтингов не подключены (нет private_plugins).")
        return providers
    for name, provider in RATING_PROVIDERS.items():
        providers.setdefault(name, provider)
    logger.debug(f"Провайдеры рейтингов: {', '.join(providers)}")
    return providers
