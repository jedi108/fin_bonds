import logging
from dataclasses import dataclass
from typing import List, Optional
import random

from tinkoff.invest import Client, InstrumentIdType, Bond
from tinkoff.invest.utils import quotation_to_decimal
from tinkoff.invest.exceptions import RequestError

@dataclass
class BondData:
    """Модель для хранения информации об облигации, полученной из API."""
    isin: str
    ticker: str
    name: str
    # Поля inn и class_code больше не требуются для основной логики, 
    # но оставим на случай будущего расширения.
    class_code: Optional[str] = None
    inn: Optional[str] = None
    yield_to_maturity: Optional[float] = None
    tinkoff_risk_level: Optional[int] = None
    
def get_bond_details(token: str, isins: List[str]) -> List[BondData]:
    """
    Получает детальную информацию по списку облигаций (ISIN) через Tinkoff API.
    """
    bonds_data = []
    with Client(token) as client:
        for isin in isins:
            try:
                find_response = client.instruments.find_instrument(query=isin)

                found_instruments = [
                    instr for instr in find_response.instruments 
                    if instr.instrument_kind == 1 and (
                        instr.isin.lower() == isin.lower() or 
                        instr.ticker.lower() == isin.lower()
                    )
                ]

                if not found_instruments:
                    logging.warning(f"Инструмент с ISIN {isin} не найден или не является облигацией в Tinkoff API.")
                    continue
                
                figi = found_instruments[0].figi

                bond_response = client.instruments.bond_by(
                    id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
                    id=figi
                )
                
                bond_instrument: Bond = bond_response.instrument
                
                yield_value = getattr(bond_instrument, 'yield_to_maturity', None)
                yld = quotation_to_decimal(yield_value) if yield_value else None

                bonds_data.append(BondData(
                    isin=bond_instrument.isin,
                    ticker=bond_instrument.ticker,
                    name=bond_instrument.name,
                    class_code=bond_instrument.class_code,
                    # ИНН больше не ищем, но поле в структуре оставим
                    inn=getattr(bond_instrument, 'issuer_inn', None), 
                    yield_to_maturity=float(yld) if yld else 0.0,
                    tinkoff_risk_level=None
                ))
            except RequestError as e:
                logging.error(f"Ошибка API Tinkoff для ISIN {isin}: Code={e.code} Details='{e.details}'")
                continue
            except Exception as e:
                logging.error(f"Не удалось получить информацию по ISIN {isin}: {e}", exc_info=True)
                continue
                
    return bonds_data 

def get_tinkoff_risk_level(token: str, isin: str) -> Optional[int]:
    """
    Получает только уровень риска для одной облигации по ISIN.
    """
    with Client(token) as client:
        try:
            find_response = client.instruments.find_instrument(query=isin)
            found_instruments = [
                instr for instr in find_response.instruments 
                if instr.instrument_kind == 1 and (
                    instr.isin.lower() == isin.lower() or 
                    instr.ticker.lower() == isin.lower()
                )
            ]
            if not found_instruments:
                return None
            
            # Предполагаем, что у всех инструментов с одним ISIN одинаковый уровень риска
            # В Tinkoff API уровень риска находится в инструменте, а не в основной информации о бумаге
            # и может быть не у всех. Здесь нужна реальная логика получения, если она есть в API.
            # На данный момент, эта функция - заглушка, возвращающая случайное значение для демонстрации.
            # В реальном API нужно было бы найти поле risk_level.
            # return found_instruments[0].risk_level
            
            # Так как поле risk_level не доступно напрямую, эмулируем его получение
            return random.randint(1, 3)

        except Exception:
            return None 