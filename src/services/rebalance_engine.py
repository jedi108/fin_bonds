"""
Canonical calculation/validation engine ребалансировки (задача 030.2).

Единственный расчётчик целевого (target) портфеля: сделки (isin, qty_delta) ->
состояние «после» с summary (стоимость/бюджет/купоны), лимитами эмитента 15%,
структурными ошибками сценария, финальным валидатором формальных ограничений
(constraint_checks), feasible и целевым портфелем в канонической форме строк.
Один и тот же вызов обслуживает legacy `rebalance-report --scenario`
(сборка JSON-блока scenario остаётся в use case) и будущие high-level
потребители (rebalance-plan/compare, проекты 030.7+): полный результат
доступен одним вызовом `evaluate_scenario` как dict, без shell-парсинга.

Правило 030 «не писать второй rebalance calculator рядом с существующим»:
математика issuer share, cashflow, budget и constraints живёт ТОЛЬКО здесь —
извлечена из `RebalanceReportUseCase` (005.2/024) без изменения поведения.

Planning price (030.2): для каждого ISIN вселенной сценария движок явно
отдаёт цену расчёта и её basis (`planning_prices`) — фундамент правила
«одна цена на ISIN внутри plan» (030.7):
- held позиция — каноническая оценка view на штуку (value_rub/qty);
- новая позиция — market_price каталога;
- без market_price — номинал (legacy fallback; для high-level planner'а
  basis делает его ЯВНЫМ — молчаливого фолбэка наружу больше нет);
- погашенная бумага — 0 (анти-фантомный P1-guard).

Anti-ловушки:
- Engine не знает ни о CLI (argparse/intents), ни о high-level planning-слое:
  ограничения приходят как ScenarioConstraints (плоские значения), сделки —
  как список {'isin', 'qty_delta'}; чтение scenario.json остаётся в use case.
- YTM — от единого расчётного движка (CalculateYtmUseCase, 005.1); второго
  пересчёта нет. Денежные величины остаются float (как в legacy report);
  Decimal вводится только в новом planning-слое (030.7).
"""
from dataclasses import dataclass
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.storage import PortfolioStorage
from src.use_cases.bond_filters import is_sovereign as _is_sovereign_rule
from src.use_cases.calculate_ytm import CalculateYtmUseCase
from src.utils import rating_code_to_score

# Лимит концентрации эмитента, % портфеля (план ребалансировки 001/002).
# Каноническое место с 030.2: движок считает лимиты и constraint ISSUER_LIMIT;
# rebalance_report реэкспортирует имя для совместимости импортов.
ISSUER_CONCENTRATION_LIMIT_PCT = 15.0

# ---------------------------------------------------------------------------
# Сценарный контракт (005.2): явные ошибки сценария (аналог блока 0
# scenario_summary.sql / A2 из 002.4).
# ---------------------------------------------------------------------------

SCENARIO_ERROR_UNKNOWN_ISIN = 'UNKNOWN_ISIN'
SCENARIO_ERROR_NEGATIVE_QTY_AFTER = 'NEGATIVE_QTY_AFTER'
SCENARIO_ERROR_NO_COMPANY_LINK = 'NO_COMPANY_LINK'

# Статусы лимита эмитента — строки как в блоке 3 A2 (совместимость контракта).
SCENARIO_LIMIT_STATUS_PASS = 'PASS'
SCENARIO_LIMIT_STATUS_VIOLATION = 'LIMIT_VIOLATION (>15%)'

# 024: violation types финального валидатора формальных ограничений
# (scenario.constraint_checks). Это НЕ structural errors (scenario.errors):
# нарушение не делает сценарий «неготовым», оно делает его неосуществимым.
CONSTRAINT_POSITION_VALUE_LIMIT = 'POSITION_VALUE_LIMIT'
CONSTRAINT_COUPON_FREQUENCY_BELOW_MIN = 'COUPON_FREQUENCY_BELOW_MIN'
CONSTRAINT_COUPON_FREQUENCY_ABOVE_MAX = 'COUPON_FREQUENCY_ABOVE_MAX'
CONSTRAINT_COUPON_FREQUENCY_NOT_IN_LIST = 'COUPON_FREQUENCY_NOT_IN_LIST'
CONSTRAINT_COUPON_FREQUENCY_MISSING = 'COUPON_FREQUENCY_MISSING'
CONSTRAINT_SOVEREIGN_NOT_ALLOWED = 'SOVEREIGN_NOT_ALLOWED'
CONSTRAINT_CREDIT_RATING_BELOW_MIN = 'CREDIT_RATING_BELOW_MIN'
CONSTRAINT_CREDIT_RATING_MISSING = 'CREDIT_RATING_MISSING'
CONSTRAINT_ISSUER_LIMIT = 'ISSUER_LIMIT'
CONSTRAINT_INSUFFICIENT_CASH_ESTIMATE = 'INSUFFICIENT_CASH_ESTIMATE'

# Ключи строки бумаги в ответе (portfolio.positions, screener.fixed/floater,
# scenario.positions_after). 023: credit_rating; 025: share_pct рядом с
# issuer_pct («Доля выпуска, %» ≠ «Доля эмитента, %»).
BOND_ROW_KEYS = (
    'isin', 'name', 'ticker', 'qty', 'price', 'value', 'share_pct',
    'ytm_pct', 'ytm_reason', 'coupon_pct', 'freq',
    'coupon_kind', 'risk_level', 'list_level', 'maturity', 'offer',
    'amort', 'ku', 'issuer', 'issuer_pct', 'held_badge', 'credit_rating',
)

# Ключи строк блока scenario.
SCENARIO_TRADE_KEYS = (
    'isin', 'name', 'ticker', 'issuer', 'qty_delta', 'action',
    'qty_before', 'qty_after', 'price', 'value_delta',
)
SCENARIO_POSITION_KEYS = BOND_ROW_KEYS + ('coupon_month',)
SCENARIO_LEG_KEYS = (
    'leg', 'isin', 'name', 'ticker', 'issuer', 'qty_delta',
    'trade_value', 'issuer_pct_after_leg', 'limit_status',
)
SCENARIO_SUMMARY_KEYS = (
    'portfolio_before', 'portfolio_after',
    # 024: budget — securities/cash до и после; денежные поля «после» —
    # оценки (qty * eff_price, без НКД и комиссий settlement'а).
    'securities_before', 'securities_after',
    'cash_before', 'sales_value', 'purchases_value', 'cash_after_estimate',
    'investable_total_before', 'investable_total_after',
    'coupon_month_before', 'coupon_month_after',
    'coupon_year_before', 'coupon_year_after',
    'delta_month', 'delta_year',
)

# 030.2: basis planning-цены ISIN (правило «одна цена на ISIN внутри plan»).
# market_price/nominal_fallback — та же лексика, что у valuation_source
# view'а позиций (миграция 014); held_valuation — каноническая оценка view
# на штуку; matured_zero — анти-фантомный P1-guard погашенных бумаг.
PLANNING_PRICE_BASIS_HELD_VALUATION = 'held_valuation'
PLANNING_PRICE_BASIS_MARKET = 'market_price'
PLANNING_PRICE_BASIS_NOMINAL_FALLBACK = 'nominal_fallback'
PLANNING_PRICE_BASIS_MATURED_ZERO = 'matured_zero'


def _to_float(value: Any) -> Optional[float]:
    """Decimal/int/str -> float, None проходит (JSON не умеет Decimal)."""
    if value is None:
        return None
    return float(value)


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _to_iso_day(value: Any) -> Optional[str]:
    """date/datetime -> 'YYYY-MM-DD', None проходит."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _fmt_qty(value: float) -> str:
    """Количество в штуках без хвостового '.0' (для бейджей и сообщений)."""
    qty = float(value)
    if qty.is_integer():
        return str(int(qty))
    return f"{qty:.4f}".rstrip('0').rstrip('.')


def _issuer_limit_status(issuer_pct: float) -> str:
    """Статус лимита эмитента 15% — строки как в блоке 3 A2 (002.4)."""
    if issuer_pct > ISSUER_CONCENTRATION_LIMIT_PCT:
        return SCENARIO_LIMIT_STATUS_VIOLATION
    return SCENARIO_LIMIT_STATUS_PASS


def issuer_shares(rows: List[Dict[str, Any]], total_value: float) -> Dict[str, float]:
    """Доля эмитента в портфеле, % (для issuer_pct и концентраций)."""
    totals: Dict[str, float] = {}
    for row in rows:
        issuer = row.get('issuer') or row.get('name') or row.get('isin')
        totals[issuer] = totals.get(issuer, 0.0) + (_to_float(row.get('value_rub')) or 0.0)
    if total_value <= 0:
        return {}
    return {issuer: round(value / total_value * 100, 2) for issuer, value in totals.items()}


def value_mix(
    items: List[Tuple[Any, float]], total_value: float, key: str
) -> List[Dict[str, Any]]:
    """Стоимостный микс по группировочному признаку (риск/частота).

    Единая арифметика для concentrations ядра и частотного микса
    «до/после» сценария (005.2): доля группы — от стоимости
    соответствующего состояния портфеля; группы без значения (None) —
    в конце списка.
    """
    totals: Dict[Any, float] = {}
    for group, value in items:
        totals[group] = totals.get(group, 0.0) + value

    def pct(value: float) -> float:
        return round(value / total_value * 100, 2) if total_value > 0 else 0.0

    return [
        {key: group, 'value': round(value, 2), 'pct': pct(value)}
        for group, value in sorted(totals.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
    ]


def bond_to_json(
    row: Dict[str, Any],
    total_value: float,
    issuer_shares_map: Dict[str, float],
    held_default: bool = False,
) -> Dict[str, Any]:
    """Строка бумаги в каноническом контракте (portfolio, screener и
    scenario.positions_after) — одна форма строки для всего тулкита."""
    value = round(_to_float(row.get('value_rub')) or 0.0, 2)
    share_pct = round(value / total_value * 100, 2) if total_value > 0 else 0.0

    # Бейдж held: в скринере приходит из SQL (002.2), в портфеле строится
    # по доле ISIN — контракт бейджа единый: 'held (N%)'.
    badge = row.get('held_badge')
    if badge is None and held_default and total_value > 0:
        badge = f"held ({share_pct:.2f}%)"

    issuer = row.get('issuer') or row.get('name') or row.get('isin')
    # price: у позиций портфеля — из выборки; у кандидатов скринера —
    # market_price каталога (018: buy-выборка без fallback на nominal).
    price_raw = row.get('price')
    if price_raw is None:
        price_raw = row.get('current_price')
    return {
        'isin': row.get('isin'),
        'name': row.get('name') or row.get('isin'),
        'ticker': row.get('ticker'),
        'qty': round(_to_float(row.get('quantity')) or 0.0, 4),
        'price': round(_to_float(price_raw), 4) if price_raw is not None else None,
        'value': value,
        # 025: доля выпуска — раньше считалась, но не возвращалась, из-за
        # чего CSV писал issuer_pct под заголовком, похожим на долю выпуска.
        'share_pct': share_pct,
        'ytm_pct': round(_to_float(row.get('ytm_percent')), 2)
        if row.get('ytm_percent') is not None else None,
        'ytm_reason': row.get('ytm_reason'),
        'coupon_pct': round(_to_float(row.get('coupon_rate_percent')), 2)
        if row.get('coupon_rate_percent') is not None else None,
        'freq': _to_int(row.get('coupon_quantity_per_year')),
        'coupon_kind': 'float' if row.get('floating_coupon_flag') else 'fix',
        'risk_level': _to_int(row.get('risk_level')),
        'credit_rating': row.get('credit_rating') or None,
        'list_level': _to_int(row.get('list_level')),
        'maturity': _to_iso_day(row.get('maturity_date')),
        'offer': _to_iso_day(row.get('offer_date')),
        'amort': bool(row.get('amortization_flag')),
        'ku': bool(row.get('ku')),
        'issuer': issuer,
        'issuer_pct': issuer_shares_map.get(issuer),
        'held_badge': badge,
    }


@dataclass(frozen=True)
class ScenarioConstraints:
    """Формальные ограничения целевого портфеля (024) без знаний о CLI.

    Заполняется из canonical args отчёта (rebalance-report) или напрямую
    high-level planner'ом (030.7); None/False = ограничение не проверяется.
    """

    freq_min: Optional[int] = None
    freq_max: Optional[int] = None
    freq_in: Optional[Sequence[int]] = None
    exclude_sovereign: bool = False
    min_credit_rating: Optional[str] = None
    max_position_value: Optional[float] = None


class RebalanceEngine:
    """Канонический расчёт/валидация целевого портфеля (030.2).

    Хранилище — то же, что у use case'ов (SQL только в src/storage.py);
    YTM — тот же расчётный движок calculate-ytm; шкала рейтингов — из
    конфига (порог --min-credit-rating переводится существующей
    rating_code_to_score). Поведение полностью наследует
    RebalanceReportUseCase._build_scenario (005.2/024) — извлечение без
    изменения математики.
    """

    def __init__(self, db: PortfolioStorage, rating_scale: Optional[Dict[str, int]] = None):
        self.db = db
        # Единый расчётный движок YTM (005.1): единицы YTM — только из одного
        # источника; собственного пересчёта в engine нет.
        self._ytm_engine = CalculateYtmUseCase(db)
        self.rating_scale = rating_scale or {}

    # ------------------------------------------------------------------
    # Каноническая YTM
    # ------------------------------------------------------------------

    def enrich_with_ytm(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Каноническая YTM в процентах — только через расчётный движок calculate-ytm.

        Единый путь для портфеля/скринера отчёта и positions_after сценария:
        второй обвязки над CalculateYtmUseCase не существует.
        """
        # Интерфейс движка calculate-ytm читает только min_ytm; CLI-объект
        # сюда не протягивается (engine не зависит от argparse).
        return self._ytm_engine._enrich_bonds_with_ytm(
            rows, SimpleNamespace(min_ytm=None)
        )

    # ------------------------------------------------------------------
    # Сценарий: trades -> целевой портфель (005.2), валидация (024)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_scenario_state(
        row: Dict[str, Any], qty_before: float, value_now: Optional[float],
        today: date, is_held: bool,
    ) -> Dict[str, Any]:
        """Состояние одного ISIN вселенной сценария (аналог base-CTE A2).

        Цена «после» (eff_price) и её planning basis (030.2): погашенные —
        0 (P1-guard: без номинального фолбэка и фантомного купона);
        держимые — каноническая оценка view на штуку (value_rub/qty);
        покупки — market_price, без неё — номинал (fallback ЯВНО помечен
        basis'ом nominal_fallback). Купон ₽/мес =
        qty * номинал * ставка% / 1200 (амортизация не моделируется —
        то же ограничение, что у A2/002-P8).
        """
        maturity = row.get('maturity_date')
        is_matured = maturity is not None and maturity < today
        nominal = _to_float(row.get('nominal')) or 0.0
        coupon_rate = 0.0 if is_matured else (_to_float(row.get('coupon_rate_percent')) or 0.0)

        if is_matured:
            eff_price, value_before = 0.0, 0.0
            planning_price_basis = PLANNING_PRICE_BASIS_MATURED_ZERO
        elif qty_before > 0 and value_now is not None and value_now > 0:
            eff_price = value_now / qty_before
            value_before = value_now
            planning_price_basis = PLANNING_PRICE_BASIS_HELD_VALUATION
        else:
            market_price = _to_float(row.get('market_price'))
            if market_price:
                eff_price = market_price
                planning_price_basis = PLANNING_PRICE_BASIS_MARKET
            else:
                eff_price = nominal
                planning_price_basis = PLANNING_PRICE_BASIS_NOMINAL_FALLBACK
            value_before = round(qty_before * eff_price, 2)

        return {
            'row': row,
            'is_held': is_held,
            'is_matured': is_matured,
            'qty_before': qty_before,
            'value_before': value_before,
            'eff_price': eff_price,
            'planning_price_basis': planning_price_basis,
            'nominal': nominal,
            'coupon_rate': coupon_rate,
            'coupon_month_before': round(qty_before * nominal * coupon_rate / 1200.0, 2),
            'freq': _to_int(row.get('coupon_quantity_per_year')),
            'name': row.get('name') or row['isin'],
            'ticker': row.get('ticker'),
            'issuer': row.get('issuer') or row.get('name') or row['isin'],
            'company_id': row.get('company_id'),
        }

    def evaluate_scenario(
        self,
        trades: List[Dict[str, Any]],
        constraints: Optional[ScenarioConstraints] = None,
    ) -> Dict[str, Any]:
        """Полный расчёт сценария одним вызовом (Python API 030.2).

        trades — уже валидированный список {'isin': str, 'qty_delta': float}
        (чтение/структурная валидация scenario.json — забота вызывающего,
        у rebalance-report это _load_scenario_trades); constraints —
        формальные ограничения целевого портфеля (024).

        Возвращает dict со всеми блоками расчёта: valid / feasible / errors /
        trades (эхо) / summary / budget_check / constraint_checks /
        freq_mix_before|after / issuer_limits / positions_after — плюс
        planning_prices: {isin: {price, basis}} для КАЖДОГО ISIN вселенной
        сценария. Calculator и validator внутри прогона используют одну и ту
        же цену (state['eff_price']); в legacy JSON-блоке scenario ключ
        planning_prices не публикуется — контракт команды не меняется.

        При структурных ошибках (блок 0 A2) блоки «после» = null: числа
        невалидного сценария нельзя интерпретировать; feasible = null —
        осуществимость не определена (это не investment-отказ).
        """
        if constraints is None:
            constraints = ScenarioConstraints()
        today = date.today()

        held_rows = self.db.get_rebalance_portfolio_rows()
        held_by_isin = {row['isin']: row for row in held_rows}

        # Каталог для ISIN вне портфеля (покупки) — те же колонки, что у позиций.
        missing = sorted({t['isin'] for t in trades} - set(held_by_isin))
        catalog_by_isin = {
            row['isin']: row
            for row in (self.db.get_scenario_universe_rows(missing) if missing else [])
        }

        # Вселенная сценария: текущий портфель + все ISIN из сделок.
        universe: Dict[str, Dict[str, Any]] = {}
        for isin, row in held_by_isin.items():
            universe[isin] = self._make_scenario_state(
                row,
                qty_before=_to_float(row.get('quantity')) or 0.0,
                value_now=_to_float(row.get('value_rub')),
                today=today,
                is_held=True,
            )
        for isin, row in catalog_by_isin.items():
            if isin not in universe:
                universe[isin] = self._make_scenario_state(
                    row, qty_before=0.0, value_now=None, today=today, is_held=False,
                )

        # Planning-цены: одна цена и её basis на ISIN (030.2) — тот же
        # eff_price, которым считаются value/cashflow/лимиты/валидатор.
        planning_prices = {
            isin: {
                'price': state['eff_price'],
                'basis': state['planning_price_basis'],
            }
            for isin, state in sorted(universe.items())
        }

        # Суммарная дельта по ISIN (аналог GROUP BY isin в planned из A2).
        deltas: Dict[str, float] = {}
        for trade in trades:
            deltas[trade['isin']] = deltas.get(trade['isin'], 0.0) + trade['qty_delta']
        for isin, state in universe.items():
            delta = deltas.get(isin, 0.0)
            state['qty_delta'] = delta
            state['qty_after'] = max(state['qty_before'] + delta, 0.0)
            state['value_after'] = round(state['qty_after'] * state['eff_price'], 2)
            state['coupon_month_after'] = round(
                state['qty_after'] * state['nominal'] * state['coupon_rate'] / 1200.0, 2
            )

        # Блок 0: явные ошибки сценария (штатно — пустой список).
        errors: List[Dict[str, Any]] = []
        for isin in sorted({t['isin'] for t in trades} - set(universe)):
            errors.append({
                'type': SCENARIO_ERROR_UNKNOWN_ISIN,
                'isin': isin,
                'message': f"{isin}: ISIN не найден ни в портфеле, ни в каталоге — сделка не применена",
            })
        for isin, state in universe.items():
            remainder = state['qty_before'] + state['qty_delta']
            if remainder < 0:
                errors.append({
                    'type': SCENARIO_ERROR_NEGATIVE_QTY_AFTER,
                    'isin': isin,
                    'message': (
                        f"{isin}: остаток после сделок {_fmt_qty(remainder)} шт "
                        f"(позиция {_fmt_qty(state['qty_before'])} шт, "
                        f"сделки {_fmt_qty(state['qty_delta'])} шт)"
                    ),
                })
            elif state['qty_after'] > 0 and state['company_id'] is None:
                errors.append({
                    'type': SCENARIO_ERROR_NO_COMPANY_LINK,
                    'isin': isin,
                    'message': f"{isin}: бумага не связана с эмитентом (company_id IS NULL)",
                })
        valid = not errors

        # Целевое состояние (блок 2 A2): непогашенные позиции после сделок.
        target_states = [
            state for state in universe.values()
            if state['qty_after'] > 0 and not state['is_matured']
        ]
        total_before = round(sum(s['value_before'] for s in universe.values()), 2)
        total_after = round(sum(s['value_after'] for s in target_states), 2)

        # Сделки с именами из каталога (анти-P-D) + лимит по каждой ноге
        # кумулятивно (анти-P6: в 002 суммарную долю РЕСО считали вручную).
        running = {isin: state['qty_before'] for isin, state in universe.items()}
        trades_echo: List[Dict[str, Any]] = []
        legs: List[Dict[str, Any]] = []
        for leg_no, trade in enumerate(trades, start=1):
            isin, delta = trade['isin'], trade['qty_delta']
            state = universe.get(isin)
            if state is None:
                trades_echo.append({
                    'isin': isin, 'name': isin, 'ticker': None, 'issuer': None,
                    'qty_delta': delta,
                    'action': 'buy' if delta > 0 else ('sell' if delta < 0 else 'noop'),
                    'qty_before': None, 'qty_after': None,
                    'price': None, 'value_delta': None,
                })
                continue
            qty_before_leg = running.get(isin, 0.0)
            running[isin] = qty_before_leg + delta
            trades_echo.append({
                'isin': isin,
                'name': state['name'],
                'ticker': state['ticker'],
                'issuer': state['issuer'],
                'qty_delta': delta,
                'action': 'buy' if delta > 0 else ('sell' if delta < 0 else 'noop'),
                'qty_before': round(qty_before_leg, 4),
                'qty_after': round(running[isin], 4),
                'price': round(state['eff_price'], 4),
                'value_delta': round(delta * state['eff_price'], 2),
            })
            issuer_value = sum(
                max(running[other], 0.0) * other_state['eff_price']
                for other, other_state in universe.items()
                if other_state['issuer'] == state['issuer']
            )
            issuer_pct = issuer_value / total_after * 100 if total_after > 0 else 0.0
            legs.append({
                'leg': leg_no,
                'isin': isin,
                'name': state['name'],
                'ticker': state['ticker'],
                'issuer': state['issuer'],
                'qty_delta': delta,
                'trade_value': round(delta * state['eff_price'], 2),
                'issuer_pct_after_leg': round(issuer_pct, 2),
                'limit_status': _issuer_limit_status(issuer_pct),
            })

        # Лимиты эмитента по сумме всех ног (блок 3 A2) — по целевому портфелю.
        issuer_totals: Dict[str, float] = {}
        for state in target_states:
            issuer = state['issuer']
            issuer_totals[issuer] = issuer_totals.get(issuer, 0.0) + state['value_after']
        after_all = [
            {
                'issuer': issuer,
                'value': round(value, 2),
                'pct': round(value / total_after * 100, 2) if total_after > 0 else 0.0,
                'limit_status': _issuer_limit_status(
                    value / total_after * 100 if total_after > 0 else 0.0
                ),
            }
            for issuer, value in issuer_totals.items()
        ]
        after_all.sort(key=lambda item: (-item['value'], item['issuer']))

        coupon_month_before = round(
            sum(s['coupon_month_before'] for s in universe.values()), 2
        )
        coupon_month_after = round(
            sum(s['coupon_month_after'] for s in target_states), 2
        )
        delta_month = round(coupon_month_after - coupon_month_before, 2)

        # 024: budget — canonical cash из 022 (PortfolioStorage
        # .get_available_cash_rub; broker API из отчёта запрещён, unknown
        # cash не превращается в 0). Все денежные поля «после» — оценки:
        # qty * eff_price не учитывает НКД и комиссии расчётов, поэтому
        # cash_after — именно estimate, а не точный settlement.
        cash = self.db.get_available_cash_rub()
        cash_known = bool(cash['cash_known'])
        cash_before = (
            round(_to_float(cash['cash_available_rub']), 2) if cash_known else None
        )
        sales_value = round(float(abs(sum(
            leg['value_delta'] for leg in trades_echo
            if leg['action'] == 'sell' and leg['value_delta'] is not None
        ))), 2)
        purchases_value = round(float(sum(
            leg['value_delta'] for leg in trades_echo
            if leg['action'] == 'buy' and leg['value_delta'] is not None
        )), 2)
        cash_after_estimate = (
            round(cash_before + sales_value - purchases_value, 2)
            if cash_known else None
        )
        investable_total_before = (
            round(total_before + cash_before, 2) if cash_known else None
        )
        investable_total_after = (
            round(total_after + cash_after_estimate, 2) if cash_known else None
        )
        budget_check = {
            'known': cash_known,
            # unknown cash: passed=null — неизвестность не маскируется под PASS.
            'passed': (cash_after_estimate >= 0) if cash_known else None,
        }

        summary = {
            'portfolio_before': total_before,
            'portfolio_after': total_after,
            'securities_before': total_before,
            'securities_after': total_after,
            'cash_before': cash_before,
            'sales_value': sales_value,
            'purchases_value': purchases_value,
            'cash_after_estimate': cash_after_estimate,
            'investable_total_before': investable_total_before,
            'investable_total_after': investable_total_after,
            'coupon_month_before': coupon_month_before,
            'coupon_month_after': coupon_month_after,
            'coupon_year_before': round(coupon_month_before * 12, 2),
            'coupon_year_after': round(coupon_month_after * 12, 2),
            'delta_month': delta_month,
            'delta_year': round(delta_month * 12, 2),
        }

        # Частотный микс «до/после»: доля частот в стоимости соответствующего
        # состояния (до — текущий портфель, после — целевой портфель).
        freq_mix_before = value_mix(
            [(s['freq'], s['value_before']) for s in universe.values() if s['is_held']],
            total_before, 'freq',
        )
        freq_mix_after = value_mix(
            [(s['freq'], s['value_after']) for s in target_states],
            total_after, 'freq',
        )

        # 024: финальный валидатор формальных ограничений — весь целевой
        # портфель (positions_after), а не только покупки. Structural
        # validity (valid) отделена от investment feasibility (feasible):
        # нарушение ограничения НЕ удаляет summary/positions_after.
        violations = (
            self._constraint_violations(
                constraints, target_states, after_all, budget_check, cash_after_estimate,
            )
            if valid else None
        )
        if not valid:
            # Целевого портфеля нет — осуществимость не определена (не false).
            feasible = None
        elif violations:
            feasible = False
        elif not cash_known:
            # Единственная неизвестность — budget из-за unknown cash.
            feasible = None
        else:
            feasible = True

        # Целевой портфель в канонической форме строк ядра (+ coupon_month):
        # YTM — от того же расчётного движка, имена/эмитенты — из каталога.
        after_rows: List[Dict[str, Any]] = []
        for state in target_states:
            raw = dict(state['row'])
            raw['quantity'] = state['qty_after']
            raw['value_rub'] = state['value_after']
            raw['price'] = state['eff_price']
            after_rows.append(raw)
        after_rows = self.enrich_with_ytm(after_rows)
        issuer_shares_after = issuer_shares(after_rows, total_after)

        positions_after: List[Dict[str, Any]] = []
        for raw in after_rows:
            pos = bond_to_json(raw, total_after, issuer_shares_after, held_default=True)
            pos['coupon_month'] = universe[raw['isin']]['coupon_month_after']
            if raw['isin'] in deltas:
                sign = '+' if deltas[raw['isin']] > 0 else ''
                pos['held_badge'] = f"сделка {sign}{_fmt_qty(deltas[raw['isin']])} шт"
            positions_after.append(pos)
        positions_after.sort(key=lambda pos: (-pos['value'], pos['isin']))

        return {
            'valid': valid,
            # 024: feasible=true только если сценарий структурно валиден И все
            # известные формальные проверки пройдены; unknown cash -> null.
            'feasible': feasible,
            'errors': errors,
            'trades': trades_echo,
            # Блоки «после» валидны только при пустом блоке ошибок (как в A2);
            # constraint_checks=null — проверки не выполнялись (не «нет нарушений»).
            'summary': summary if valid else None,
            'budget_check': budget_check if valid else None,
            'constraint_checks': violations if valid else None,
            'freq_mix_before': freq_mix_before if valid else None,
            'freq_mix_after': freq_mix_after if valid else None,
            'issuer_limits': {
                'limit_pct': ISSUER_CONCENTRATION_LIMIT_PCT,
                'legs': legs,
                'after_all': after_all,
                'has_violations': any(
                    item['limit_status'] != SCENARIO_LIMIT_STATUS_PASS
                    for item in after_all
                ),
            } if valid else None,
            'positions_after': positions_after if valid else [],
            # 030.2: planning-цена и её basis для каждого ISIN вселенной
            # (фундамент «одна цена на ISIN внутри plan», 030.7). В legacy
            # JSON-блок не публикуется — контракт команды не меняется.
            'planning_prices': planning_prices,
        }

    def _constraint_violations(
        self,
        constraints: ScenarioConstraints,
        target_states: List[Dict[str, Any]],
        after_all: List[Dict[str, Any]],
        budget_check: Dict[str, Any],
        cash_after_estimate: Optional[float],
    ) -> List[Dict[str, Any]]:
        """Финальный валидатор формальных ограничений (024).

        Проверяет ВЕСЬ целевой портфель (positions_after) по
        ScenarioConstraints: freq_min/freq_max/freq_in, exclude_sovereign,
        min_credit_rating, max_position_value и лимит концентрации эмитента
        15% (переиспользуется уже посчитанный issuer_limits.after_all —
        второго расчёта концентрации нет). Бюджет входит проверкой
        INSUFFICIENT_CASH_ESTIMATE только при known cash: unknown не
        превращается в violation.

        Каждая violation машиночитаема: type + isin (где применимо) + actual
        и limit (где применимо) + человекочитаемый message.
        """
        violations: List[Dict[str, Any]] = []

        def add(vtype: str, isin: Optional[str], actual: Any,
                limit: Any, message: str) -> None:
            violations.append({
                'type': vtype,
                'isin': isin,
                'actual': actual,
                'limit': limit,
                'message': message,
            })

        freq_constrained = (
            constraints.freq_min is not None or constraints.freq_max is not None
            or constraints.freq_in is not None
        )
        # Порог рейтинга один раз переводится в балл существующей шкалой
        # (валидация кода — на границе CLI; второго сравнения рейтингов нет).
        min_rating_score = (
            rating_code_to_score(constraints.min_credit_rating, self.rating_scale)
            if constraints.min_credit_rating is not None else None
        )

        for state in target_states:
            isin = state['row']['isin']

            if (constraints.max_position_value is not None
                    and state['value_after'] > constraints.max_position_value):
                add(
                    CONSTRAINT_POSITION_VALUE_LIMIT, isin,
                    state['value_after'], constraints.max_position_value,
                    f"{isin}: стоимость позиции после сделок "
                    f"{state['value_after']:.2f} ₽ превышает лимит "
                    f"{constraints.max_position_value:.2f} ₽",
                )

            freq = state['freq']
            if freq_constrained:
                if freq is None:
                    add(
                        CONSTRAINT_COUPON_FREQUENCY_MISSING, isin,
                        None, None,
                        f"{isin}: частота купонов неизвестна (NULL) — "
                        "формальный фильтр частоты не подтверждён",
                    )
                elif constraints.freq_min is not None and freq < constraints.freq_min:
                    add(
                        CONSTRAINT_COUPON_FREQUENCY_BELOW_MIN, isin,
                        freq, constraints.freq_min,
                        f"{isin}: частота {freq}/год ниже минимальной "
                        f"{constraints.freq_min}/год",
                    )
                elif constraints.freq_max is not None and freq > constraints.freq_max:
                    add(
                        CONSTRAINT_COUPON_FREQUENCY_ABOVE_MAX, isin,
                        freq, constraints.freq_max,
                        f"{isin}: частота {freq}/год выше максимальной "
                        f"{constraints.freq_max}/год",
                    )
                elif (constraints.freq_in is not None
                        and freq not in constraints.freq_in):
                    add(
                        CONSTRAINT_COUPON_FREQUENCY_NOT_IN_LIST, isin,
                        freq, constraints.freq_in,
                        f"{isin}: частота {freq}/год вне списка "
                        f"{constraints.freq_in}",
                    )

            if constraints.exclude_sovereign and _is_sovereign_rule(state['row']):
                add(
                    CONSTRAINT_SOVEREIGN_NOT_ALLOWED, isin,
                    None, None,
                    f"{isin}: суверенный эмитент ({state['issuer']}) "
                    "исключён флагом --exclude-sovereign",
                )

            if min_rating_score is not None:
                score = _to_int(state['row'].get('rating_score'))
                code = state['row'].get('credit_rating') or None
                if score is None:
                    add(
                        CONSTRAINT_CREDIT_RATING_MISSING, isin,
                        None, constraints.min_credit_rating,
                        f"{isin}: нет рейтинга в rating_history — порог "
                        f"{constraints.min_credit_rating} не подтверждён",
                    )
                elif score < min_rating_score:
                    add(
                        CONSTRAINT_CREDIT_RATING_BELOW_MIN, isin,
                        code, constraints.min_credit_rating,
                        f"{isin}: рейтинг {code} ниже порога "
                        f"{constraints.min_credit_rating}",
                    )

        # Лимит эмитента 15% — из уже посчитанного issuer_limits
        # (024, п.7: второй расчёт концентрации не создаётся).
        for item in after_all:
            if item['limit_status'] != SCENARIO_LIMIT_STATUS_PASS:
                add(
                    CONSTRAINT_ISSUER_LIMIT, None,
                    item['pct'], ISSUER_CONCENTRATION_LIMIT_PCT,
                    f"Эмитент {item['issuer']}: доля {item['pct']}% в целевом "
                    f"портфеле превышает лимит "
                    f"{ISSUER_CONCENTRATION_LIMIT_PCT}%",
                )

        # Бюджет: violation только при known cash (unknown = null, не 0).
        if budget_check['known'] and budget_check['passed'] is False:
            add(
                CONSTRAINT_INSUFFICIENT_CASH_ESTIMATE, None,
                cash_after_estimate, 0.0,
                f"Оценка cash после сделок {cash_after_estimate:.2f} ₽ < 0: "
                "покупки не покрываются cash_before + sales_value",
            )

        return violations
