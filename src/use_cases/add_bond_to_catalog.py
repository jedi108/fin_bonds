"""
Use case для ручного добавления одной облигации в каталог.
"""
import logging
import argparse
from decimal import Decimal
from typing import TYPE_CHECKING, Optional
from tinkoff.invest import Client, InstrumentIdType
from src.moex.api_client import MoexApiClient
from src.monitoring.storage import Database
from src.use_cases.base import UseCase
from src.data_models import Bond
from src.use_cases.update_bonds_catalog import fetch_and_prepare_bond_details
from src.constants import FLOATING_COUPON_INDICATORS
from datetime import datetime
from dataclasses import asdict

# Предотвращаем циклический импорт для type hints
if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

def _parse_date(date_str: Optional[str]) -> Optional[datetime.date]:
    """Парсит строку с датой в объект date."""
    if not date_str or date_str == '0000-00-00':
        return None
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return None

class AddBondToCatalogUseCase(UseCase):
    """
    Добавляет одну облигацию в каталог по ISIN.
    
    1. Проверяет, нет ли уже такой облигации в БД.
    2. Получает основные данные с MOEX.
    3. Пытается обогатить их данными из Tinkoff API (FIGI, риск-уровень и т.д.).
    4. Сохраняет результат в БД.
    """
    def __init__(self, config, db: Database, tinkoff_token: str, moex_client: MoexApiClient):
        self.config = config
        self.db = db
        self.tinkoff_token = tinkoff_token
        self.moex_client = moex_client

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        """Добавляет аргументы для команды добавления облигации."""
        parser.add_argument('--isin', type=str, required=True, help='ISIN-код облигации для добавления в каталог.')
        parser.add_argument('--is_floater', action='store_true', help='Принудительно установить флаг флоатера.')

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'AddBondToCatalogUseCase':
        """Создает экземпляр с зависимостями из фабрики."""
        return cls(
            config=factory.config, 
            db=factory.get_db_connection(), 
            tinkoff_token=factory.tinkoff_token, 
            moex_client=factory.get_moex_api_client()
        )

    def execute(self, args: argparse.Namespace):
        isin = args.isin
        logger.info(f"Запуск use case для добавления облигации {isin} в каталог...")

        if self.db.get_bond_by_isin(isin):
            logger.info(f"Облигация с ISIN {isin} уже существует в каталоге. Пропуск.")
            return

        # Шаг 1: Получение данных с MOEX
        logger.info(f"Запрос данных с MOEX для ISIN: {isin}")
        moex_data = self.moex_client.get_security_description(isin)
        if not moex_data:
            logger.error(f"Не удалось найти данные для ISIN {isin} на MOEX. Добавление невозможно.")
            return
        logger.info("Данные с MOEX получены.")

        # Шаг 2: Создание базового объекта Bond на основе данных MOEX
        try:
            coupon_period = int(moex_data.get('COUPONPERIOD', 0))
            coupon_quantity = 365 / coupon_period if coupon_period > 0 else 0
        except (ValueError, TypeError):
            coupon_quantity = 0

        bond_dto = Bond(
            isin=isin,
            ticker=moex_data.get('SECID') or isin,
            name=moex_data.get('SHORTNAME') or isin,
            list_level=int(moex_data.get('LISTLEVEL', 0)),
            currency=moex_data.get('CURRENCYID', 'SUR'),
            nominal=float(Decimal(str(moex_data.get('FACEVALUE', '0')))),
            coupon_rate_percent=float(Decimal(str(moex_data.get('COUPONPERCENT', '0')))),
            coupon_quantity_per_year=int(coupon_quantity),
            maturity_date=_parse_date(moex_data.get('MATDATE')),
            aci_value=float(Decimal(str(moex_data.get('ACCRUEDINT', '0')))),
            offer_date=_parse_date(moex_data.get('OFFERDATE')),
            # Значения по умолчанию, которые могут быть перезаписаны
            figi=None,
            floating_coupon_flag=False,
            perpetual_flag=False,
            amortization_flag=False,
            class_code='N/A',
            issue_size=0,
            risk_level=0,
            coupon_spread=None
        )

        # Шаг 3: Обогащение данными из Tinkoff
        logger.info("Попытка обогатить данные через Tinkoff API...")
        try:
            with Client(self.tinkoff_token) as tinkoff_client:
                find_response = tinkoff_client.instruments.find_instrument(query=isin)
                found_instrument = next((instr for instr in find_response.instruments if instr.isin == isin), None)

                if found_instrument:
                    logger.info(f"Найден FIGI: {found_instrument.figi}. Запрос полной информации.")
                    bond_response = tinkoff_client.instruments.bond_by(
                        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
                        id=found_instrument.figi
                    )
                    if bond_response and bond_response.instrument:
                        tinkoff_details = fetch_and_prepare_bond_details(bond_response.instrument)
                        # Обновляем наш DTO
                        bond_dto.figi = tinkoff_details.figi
                        bond_dto.floating_coupon_flag = tinkoff_details.floating_coupon_flag
                        bond_dto.perpetual_flag = tinkoff_details.perpetual_flag
                        bond_dto.amortization_flag = tinkoff_details.amortization_flag
                        bond_dto.class_code = tinkoff_details.class_code
                        bond_dto.issue_size = tinkoff_details.issue_size
                        bond_dto.risk_level = tinkoff_details.risk_level
                        if tinkoff_details.nominal > 0:
                            bond_dto.nominal = float(tinkoff_details.nominal)
                        logger.info(f"Данные из Tinkoff успешно получены для '{tinkoff_details.name}'.")
                else:
                    logger.warning("Не удалось найти инструмент в Tinkoff API. Будут сохранены только данные MOEX.")
        except Exception as e:
            logger.warning(f"Произошла ошибка при запросе к Tinkoff API: {e}. Будут сохранены только данные MOEX.")

        # Шаг 4: Эвристики и флаги
        if args.is_floater:
            bond_dto.floating_coupon_flag = True
            logger.info("Флаг флоатера установлен принудительно.")

        # Шаг 5: Сохранение в БД
        logger.info(f"Сохранение облигации {isin} в базу данных...")
        self.db.add_bonds_to_catalog([bond_dto])
        logger.info("Облигация успешно добавлена в каталог.")
        logger.info("Use case завершил работу.")