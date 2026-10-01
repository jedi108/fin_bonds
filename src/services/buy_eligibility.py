"""
BUY eligibility gate — structural + policy одной реализацией (задача 030.6).

Три семантики планирования не смешиваются (правило 030 №5):

1. **structural BUY gate** — бумагу вообще можно рассматривать для покупки:
   фактические gates канонического buy-path `get_bonds_yield_table(mode='buy')`
   (перечень 030.1). Здесь они НЕ скопированы: SQL-фрагменты и построчные
   Python-предикаты — одна таблица STRUCTURAL_BUY_GATES
   (src/use_cases/bond_filters.py), из которой собран и WHERE buy-выборки
   хранилища, и структурная оценка строки. ISIN из canonical raw-universe
   контекста уже прошёл эти gates в SQL на frozen as_of среза.
2. **policy eligibility** — проходит ли бумага фильтры конкретного запроса
   (CandidateFilters, контракт 030.5): построчные правила скринера
   (screener_policy_rejection / screener_ytm_rejection из bond_filters) — та
   же реализация, что у отчёта; отчёт не получает второго набора почти
   одинаковых условий. YTM-потолок — релей canonical float (правило 030 №3),
   денег в gate нет.
3. **final portfolio constraints** — canonical validator движка (024,
   030.7/030.8); сюда не входит: freq/sovereign/rating — не candidate-фильтры
   и в CandidateFilters отсутствуют, в policy-вызове они не задаются
   (не смешивать lifecycle-проверки).

Eligibility проверяется только для BUY/increase (buy_gate_applies):
qty_delta > 0 → structural и policy обязательны; qty_delta < 0 → sell/reduce
разрешён независимо от eligibility (плохая/погашенная/непокупаемая
удерживаемая бумага сокращаема/продаваема); qty_delta == 0 → no-op не
блокируется. Held increase проходит тот же gate, что и новая позиция:
оценка зависит только от (canonical raw-строка, as_of, filters) — «льгот»
для удерживаемых нет, как и зависимости от попадания ISIN в published
top-screener_limit отчёта.

Все time-sensitive причины (MATURED, MARKET_PRICE_STALE, окно погашения)
вычисляются относительно frozen PlanningContext.as_of (as_of_date), не
wall-clock subcommand'а: один и тот же (row, as_of, filters) даёт один и тот
же результат — replay не меняет reasons.

BUY rejection ≠ auto-fit (правило «нет silent auto-fit»): если requested
target требует докупку, но eligibility не пройдена — requested BUY не
уменьшается тихо до qty_before и не выдаётся за успешный fit; planner
возвращает BUY_NOT_ELIGIBLE с structured reasons[] (code/actual/limit), в
canonical plan нет BUY leg по этой бумаге. Auto-fit вправе резать BUY только
по количественным ограничениям (position/budget/issuer — 030.7). Публикация
отказа в ответе planner'а — контракт 030.7/030.8; здесь — canonical
source метки и reasons.

Row-контракт: canonical raw-строка каталога, как во всех canonical выборках
(buy-пул PlanningContext.universe_rows либо permissive-строка
get_scenario_universe_rows): coupon_rate_percent — уже COALESCE с последней
ставкой флоатера; ytm_percent — обогащённая расчётная YTM движка (та же
enrich_with_ytm, что у скринера); плюс поля gates (currency,
is_trade_available, perpetual_flag, maturity_date, risk_level, list_level,
market_price, market_price_updated_at). Номинал в structural gate не
участвует: нет свежей market_price — покупки нет, молчаливого nominal
fallback нет (018).

Не делается (границы 030.6): построение сделок/fitting (030.7), CLI (030.8),
второй валидатор целевого портфеля, применение policy-фильтров к untouched
holdings (candidate-only фильтры — только к оцениваемой бумаге).
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional, Tuple

from src.services.planning_intents import CandidateFilters
from src.use_cases.bond_filters import (
    ScreenerRejection,
    screener_policy_rejection,
    screener_ytm_rejection,
    structural_buy_violations,
)

# Машиночитаемая метка отказа покупки (контракт ответа planner'а 030.7/030.8):
# единственный источник строки — BUY_NOT_ELIGIBLE; ручных копий нет.
BUY_ELIGIBLE = 'BUY_ELIGIBLE'
BUY_NOT_ELIGIBLE = 'BUY_NOT_ELIGIBLE'

# Коды policy-причин окна погашения (policy-часть; structural-коды живут
# в bond_filters рядом со своими gates).
REASON_MIN_MATURITY = 'min_maturity'
REASON_MAX_MATURITY = 'max_maturity'


@dataclass(frozen=True)
class BuyEligibilityReason:
    """Машиночитаемая причина отказа BUY eligibility (контракт 030.6).

    code — canonical код (structural — таблица gates, policy — ключи
    excluded-счётчиков отчёта: 'risk'/'ku'/'fixed_max_ytm'/...); actual —
    фактическое значение строки; limit — порог/граница правила. Причины не
    зависят от текстовых сообщений; actual/limit = None, где неприменимы.
    """

    code: str
    actual: Any = None
    limit: Any = None


@dataclass(frozen=True)
class BuyEligibility:
    """Canonical eligibility-результат одной бумаги (reusable, 030.6).

    structural_eligible/policy_eligible раздельно (structural ≠ policy);
    `buy_eligible` — совокупный вердикт BUY-ноги; `reasons` — плоский список
    причин (structural + policy) для structured отказа. policy оценивается
    только у структурно покупаемой бумаги (семантика policy определена
    поверх structural-пула, шумные причины не смешиваются).
    """

    isin: str
    structural_eligible: bool
    policy_eligible: bool
    structural_reasons: Tuple[BuyEligibilityReason, ...] = ()
    policy_reasons: Tuple[BuyEligibilityReason, ...] = ()

    @property
    def buy_eligible(self) -> bool:
        return self.structural_eligible and self.policy_eligible

    @property
    def reasons(self) -> Tuple[BuyEligibilityReason, ...]:
        return self.structural_reasons + self.policy_reasons

    @property
    def status(self) -> str:
        return BUY_ELIGIBLE if self.buy_eligible else BUY_NOT_ELIGIBLE


def structural_buy_reasons(
    row: Mapping, as_of_date: date
) -> Tuple[BuyEligibilityReason, ...]:
    """Structural причины отказа для canonical raw-строки (030.6).

    Единственная реализация с buy-path: таблица gates в bond_filters
    (её SQL-фрагменты составляют WHERE get_bonds_yield_table mode='buy').
    as_of_date — frozen as_of PlanningContext.
    """
    return tuple(
        BuyEligibilityReason(code, actual, limit)
        for code, actual, limit in structural_buy_violations(row, as_of_date)
    )


def structural_buy_eligible(row: Mapping, as_of_date: date) -> bool:
    """Structural BUY gate одной canonical raw-строки относительно as_of."""
    return not structural_buy_violations(row, as_of_date)


def buy_gate_applies(qty_delta: float) -> bool:
    """Применяется ли BUY eligibility к сделке (lifecycle-правило 030.6).

    qty_delta > 0 (покупка/увеличение) — gate обязателен; qty_delta < 0
    (продажа/сокращение) — разрешена независимо от eligibility; qty_delta == 0
    — no-op не блокируется. Знак дельты planner нормализует относительно
    current qty (030.7): sell_all/target_value ниже текущей позиции — не BUY.
    """
    return qty_delta > 0


def _maturity_window_reason(
    row: Mapping, min_maturity: Optional[str], max_maturity: Optional[str]
) -> Optional[BuyEligibilityReason]:
    """Policy-окно погашения конкретного запроса (CandidateFilters).

    Семантика окна — та же, что у SQL-фильтра скринера (002.3/P9.5): нижняя
    и верхняя граница включительно. NULL-срок структурно отклонён gate'ом
    MATURED раньше — здесь он не рассматривается.
    """
    maturity = row.get('maturity_date')
    if maturity is None:
        return None
    day = maturity.date() if isinstance(maturity, datetime) else maturity
    if min_maturity is not None and day < date.fromisoformat(str(min_maturity)):
        return BuyEligibilityReason(
            REASON_MIN_MATURITY, day.isoformat(), str(min_maturity)
        )
    if max_maturity is not None and day > date.fromisoformat(str(max_maturity)):
        return BuyEligibilityReason(
            REASON_MAX_MATURITY, day.isoformat(), str(max_maturity)
        )
    return None


def _policy_reasons(
    row: Mapping, filters: CandidateFilters
) -> Tuple[BuyEligibilityReason, ...]:
    """Policy-причины одной бумаги по фильтрам запроса (030.6).

    Правила — построчные функции скринера (bond_filters): report и planner
    используют одну реализацию. freq/sovereign/rating сознательно не заданы:
    это final-portfolio constraints (PortfolioConstraints → canonical
    validator движка, 030.7), а не candidate-фильтры.
    """
    reasons = []
    rejection: Optional[ScreenerRejection] = screener_policy_rejection(
        row,
        include_ku=filters.include_ku,
        max_risk=filters.max_risk,
        max_listlevel=filters.max_list_level,
    )
    if rejection is not None:
        reasons.append(
            BuyEligibilityReason(rejection.key, rejection.actual, rejection.limit)
        )
    maturity_reason = _maturity_window_reason(
        row, filters.min_maturity, filters.max_maturity
    )
    if maturity_reason is not None:
        reasons.append(maturity_reason)
    ytm_rejection = screener_ytm_rejection(row, max_ytm=filters.max_ytm_pct)
    if ytm_rejection is not None:
        reasons.append(
            BuyEligibilityReason(ytm_rejection.key, ytm_rejection.actual,
                                 ytm_rejection.limit)
        )
    return tuple(reasons)


def evaluate_buy_eligibility(
    isin: str,
    row: Mapping,
    as_of_date: date,
    filters: CandidateFilters,
) -> BuyEligibility:
    """Полная BUY eligibility одной бумаги: structural + policy (030.6).

    isin — эхо запрошенного ISIN (row обязан существовать в canonical data:
    отсутствие ISIN — UNKNOWN_ISIN движка, а не eligibility-причина).
    row — canonical raw-строка (см. row-контракт модуля); as_of_date — frozen
    as_of PlanningContext; filters — CandidateFilters запроса (контракт
    030.5; defaults — default_candidate_filters(), без собственных копий).
    """
    structural = structural_buy_reasons(row, as_of_date)
    if structural:
        return BuyEligibility(
            isin=isin,
            structural_eligible=False,
            policy_eligible=False,
            structural_reasons=structural,
            policy_reasons=(),
        )
    policy = _policy_reasons(row, filters)
    return BuyEligibility(
        isin=isin,
        structural_eligible=True,
        policy_eligible=not policy,
        structural_reasons=(),
        policy_reasons=policy,
    )
