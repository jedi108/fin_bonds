import logging
from typing import Any, Callable, Dict, Optional

from src.moex.api_client import MoexApiClient
from src.utils import rating_code_to_score

logger = logging.getLogger(__name__)

def get_rating_from_moex(
    isin: str,
    client: MoexApiClient,
    rating_scale: Optional[Dict[str, int]] = None,
    **kwargs,
) -> Optional[Dict[str, str]]:
    """
    Получает кредитный рейтинг с MOEX ISS через эндпоинт /iss/cci/rating
    (002.9, Р10): emitent_id по ISIN → карточка рейтингов выпуска.

    Старый источник /iss/securities/<secid>/credit_ratings.json не существует
    (MOEX отвечал карточкой бумаги без блока ratings — провайдер возвращал
    None для любой облигации). Ответ /cci содержит все три агентства
    (АКРА/Эксперт РА/НКР) и их историю.

    Выбор рейтинга (worst-of): по каждому агентству берётся его последняя
    запись; агентства, чья последняя запись 'Отозван' (или пустая), не
    учитываются. Из действующих берётся худший по rating_scale — иначе
    «изменение» зависело бы от того, какое агентство обновилось последним.
    Коды вне шкалы используются, только если распознанных нет (тогда —
    самый свежий). Имя агентства — для сообщения пользователю.
    """
    records = client.get_security_rating_cci(isin)
    if not records:
        logger.warning(f"MOEX cci: рейтинги для {isin} не получены")
        return None

    latest_by_agency: Dict[Any, Dict[str, Any]] = {}
    for r in records:
        agency = r.get('agency_name_short_ru') or r.get('agency_id')
        current = latest_by_agency.get(agency)
        if current is None or str(r.get('rating_date') or '') > str(current.get('rating_date') or ''):
            latest_by_agency[agency] = r

    active = [
        r for r in latest_by_agency.values()
        if r.get('rating_level_name_short_ru') and r.get('rating_level_name_short_ru') != 'Отозван'
    ]
    if not active:
        logger.info(f"MOEX cci: действующих рейтингов {isin} нет (отозваны или пусты)")
        return None

    scored = [
        (rating_code_to_score(r['rating_level_name_short_ru'], rating_scale), r) for r in active
    ]
    known = [(score, r) for score, r in scored if score is not None]
    if known:
        # худший балл; min стабилен, поэтому при равенстве — самая свежая запись
        known.sort(key=lambda item: str(item[1].get('rating_date') or ''), reverse=True)
        chosen = min(known, key=lambda item: item[0])[1]
    else:
        chosen = max(active, key=lambda r: str(r.get('rating_date') or ''))

    return {
        "rating": chosen['rating_level_name_short_ru'],
        "agency": chosen.get('agency_name_short_ru') or '',
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
