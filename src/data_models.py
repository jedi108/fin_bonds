from dataclasses import dataclass, asdict, field, fields
from typing import Optional, Dict, Any, List
from datetime import date, datetime
from decimal import Decimal
from tinkoff.invest import Bond as TinkoffBond, PortfolioPosition as TinkoffPortfolioPosition, Instrument as TinkoffInstrument

@dataclass
class Bond:
    """
    Класс данных (DTO) для представления облигации.
    Используется для передачи данных между слоями приложения.
    """
    isin: str
    figi: Optional[str] = None
    ticker: Optional[str] = None
    name: Optional[str] = None
    currency: Optional[str] = None
    nominal: Optional[Decimal] = None
    maturity_date: Optional[date] = None
    coupon_quantity_per_year: Optional[int] = None
    coupon_type: Optional[str] = None
    coupon_spread: Optional[Decimal] = None
    list_level: Optional[int] = None
    is_trade_available: Optional[bool] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    is_for_qualified_investors: Optional[bool] = None
    risk_level: Optional[int] = None
    offer_date: Optional[date] = None
    coupon_rate_percent: Optional[Decimal] = None
    issue_size: Optional[int] = None
    # Флаги, определяющие тип и поведение облигации
    floating_coupon_flag: bool = False
    perpetual_flag: bool = False
    amortization_flag: bool = False
    
    # Рыночные данные (новые поля)
    market_price: Optional[Decimal] = None
    market_price_source: Optional[str] = None  # 'tbank', 'moex', 'alor'
    market_price_updated_at: Optional[datetime] = None

    @classmethod
    def from_tinkoff_api(cls, tinkoff_bond: TinkoffBond) -> "Bond":
        """Фабричный метод для создания DTO из ответа Tinkoff API."""
        # Пропускаем облигации без ISIN или maturity_date
        if not tinkoff_bond.isin or not tinkoff_bond.maturity_date:
            return None
        
        return cls(
            isin=tinkoff_bond.isin,
            figi=tinkoff_bond.figi,
            ticker=tinkoff_bond.ticker,
            name=tinkoff_bond.name,
            currency=tinkoff_bond.currency,
            maturity_date=tinkoff_bond.maturity_date.date() if tinkoff_bond.maturity_date else None,
            coupon_quantity_per_year=tinkoff_bond.coupon_quantity_per_year,
            risk_level=tinkoff_bond.risk_level.value,
            is_for_qualified_investors=tinkoff_bond.for_qual_investor_flag,
            is_trade_available=tinkoff_bond.buy_available_flag and tinkoff_bond.sell_available_flag,
            coupon_type='Постоянный' if tinkoff_bond.floating_coupon_flag is False else 'Переменный',
            issue_size=tinkoff_bond.issue_size,
            floating_coupon_flag=tinkoff_bond.floating_coupon_flag,
            perpetual_flag=tinkoff_bond.perpetual_flag,
            amortization_flag=tinkoff_bond.amortization_flag,
        )
    
    def to_db_dict(self) -> Dict[str, Any]:
        """Преобразует объект в словарь, пригодный для записи в БД."""
        db_dict = self.to_dict()
        for key, value in db_dict.items():
            if isinstance(value, Decimal):
                db_dict[key] = str(value)
            elif isinstance(value, date):
                db_dict[key] = value.strftime('%Y-%m-%d')
            elif isinstance(value, bool):
                db_dict[key] = int(value)
        return db_dict

    def to_dict(self) -> Dict[str, Any]:
        """Преобразует объект в словарь."""
        return asdict(self)

@dataclass(frozen=True)
class CalculatedCoupon:
    """
    Класс данных для представления рассчитанного купона флоатера.
    """
    isin: str
    check_date: date
    calculated_rate: Decimal

# --- DTOs for Portfolio Sync ---

@dataclass
class InstrumentInfo:
    """DTO для базовой информации об инструменте из TBank API."""
    figi: str
    ticker: str
    name: str
    isin: str
    currency: str
    risk_level: int
    instrument_type: str

    def to_dict(self) -> Dict[str, Any]:
        """Преобразует объект в словарь, корректно обрабатывая Decimal."""
        # Преобразуем Decimal в строку для JSON-сериализации
        data = asdict(self)
        if isinstance(data.get('nominal'), Decimal):
            data['nominal'] = str(data['nominal'])
        return data

@dataclass
class PortfolioPosition:
    """DTO для позиции в портфеле."""
    isin: str
    figi: Optional[str] = None
    ticker: Optional[str] = None
    name: Optional[str] = None
    quantity: Optional[Decimal] = None
    average_price: Optional[Decimal] = None
    current_price: Optional[Decimal] = None
    currency: Optional[str] = None
    yield_to_maturity: Optional[Decimal] = None
    portfolio_percent: Optional[Decimal] = None
    current_value: Optional[Decimal] = None
    broker_name: Optional[str] = None
    instrument_type: Optional[str] = None
    liquidity_loss_ratio: Optional[Decimal] = None # Новое поле для коэффициента потерь ликвидности
    coupon_rate_percent: Optional[Decimal] = None # Новое поле для купонной доходности

    @classmethod
    def from_tinkoff_api(cls, position: TinkoffPortfolioPosition, instrument: TinkoffInstrument) -> "PortfolioPosition":
        """Фабричный метод для создания DTO из ответа Tinkoff API."""
        # Пропускаем облигации без ISIN или maturity_date
        if not position.isin or not instrument.maturity_date:
            return None
        
        return cls(
            isin=position.isin,
            figi=position.figi,
            ticker=instrument.ticker,
            name=instrument.name,
            quantity=position.quantity,
            average_price=position.average_price,
            current_price=position.current_price,
            currency=instrument.currency,
            yield_to_maturity=position.yield_to_maturity,
            portfolio_percent=position.portfolio_percent,
            current_value=position.current_value,
            broker_name=position.broker_name,
            instrument_type=instrument.instrument_type,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Преобразует объект в словарь."""
        return asdict(self)

    def to_db_dict(self) -> Dict[str, Any]:
        """Преобразует объект в словарь, пригодный для записи в БД."""
        db_dict = self.to_dict()
        for key, value in db_dict.items():
            if isinstance(value, Decimal):
                db_dict[key] = str(value)
        return db_dict

@dataclass
class Portfolio:
    """DTO для представления всего портфеля пользователя."""
    positions: List[PortfolioPosition] = field(default_factory=list)
    total_value_rub: Decimal = field(default=Decimal(0))

    def to_dict(self) -> Dict[str, Any]:
        """Преобразует объект в словарь."""
        return asdict(self)

@dataclass
class MoexBondData:
    """DTO для данных по облигации, полученных из MOEX API."""
    isin: str
    # ... existing code ...

@dataclass
class PortfolioBond:
    """
    DTO для представления облигации из портфеля с объединенными данными 
    из таблиц portfolio_positions и bonds_catalog.
    Используется для оптимизации запросов в UpdateRatingsUseCase.
    """
    # Основные идентификаторы
    isin: str
    figi: Optional[str] = None
    
    # Данные из bonds_catalog
    ticker: Optional[str] = None
    name: Optional[str] = None
    currency: Optional[str] = None
    nominal: Optional[Decimal] = None
    maturity_date: Optional[date] = None
    offer_date: Optional[date] = None
    coupon_quantity_per_year: Optional[int] = None
    coupon_rate_percent: Optional[Decimal] = None
    coupon_type: Optional[str] = None
    coupon_spread: Optional[Decimal] = None
    list_level: Optional[int] = None
    risk_level: Optional[int] = None
    issue_size: Optional[int] = None
    is_trade_available: Optional[bool] = None
    is_for_qualified_investors: Optional[bool] = None
    floating_coupon_flag: bool = False
    perpetual_flag: bool = False
    amortization_flag: bool = False
    
    # Данные из portfolio_positions  
    quantity: Optional[Decimal] = None
    average_price: Optional[Decimal] = None
    current_price: Optional[Decimal] = None
    yield_to_maturity: Optional[Decimal] = None
    portfolio_percent: Optional[Decimal] = None
    current_value: Optional[Decimal] = None
    broker_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Преобразует объект в словарь."""
        return asdict(self)

    def to_db_dict(self) -> Dict[str, Any]:
        """Преобразует объект в словарь, пригодный для записи в БД."""
        db_dict = self.to_dict()
        for key, value in db_dict.items():
            if isinstance(value, Decimal):
                db_dict[key] = str(value)
            elif isinstance(value, date):
                db_dict[key] = value.strftime('%Y-%m-%d')
            elif isinstance(value, bool):
                db_dict[key] = int(value)
        return db_dict