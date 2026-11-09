"""
Auto-fit fitter: от high-level intents к normalized net-сделкам (задача 030.7).

Закрывает реальный класс ошибки: LLM считала qty по цене отчёта (951.30),
а scenario-валидатор проверял по другой (1103.06) → violations и ручной
decrement. Fitter сам переводит `target_value`/`sell_all` intents в целые
количества по ЕДИНОЙ planning-цене, укладывает их в бюджет/лимиты и
валидирует результат ТОЛЬКО каноническим движком (030.2) — второго
калькулятора/валидатора нет (правило 030 №1).

Конвейер (по файлу задачи):

    requested final value
      -> planning_price из PlanningContext (одна цена на ISIN на весь план)
      -> requested final qty (floor до целой бумаги)
      -> max-position cap (pre-size)
      -> explicit reductions/sells (sell_all, target ниже текущего)
      -> budget fit (canonical full-state валидация)
      -> issuer fit (canonical full-state валидация)
      -> одна net-сделка на ISIN
      -> полный canonical validation

Одна цена на ISIN (P0): sizing считается по `context.effective_prices[isin]`
— тем же canonical planning price/basis, что отдаёт движок (030.2/030.3).
После финальной валидации fitter сверяет `validation.planning_prices` с
`context.effective_prices`: расхождение (срез БД уехал между сборкой context
и планом) даёт диагностику CONTEXT_PRICE_MISMATCH и `feasible=null` — план,
посчитанный не по той цене, наружу не выдаётся. Это и есть механическая
гарантия «одна planning_price на sizing и валидацию».

Деньги (правило 030 №3): вся арифметика fitter'а — Decimal; цена —
ЕДИНСТВЕННЫЙ float-релей canonical planning_price, нормализованный через
`Decimal(str(price))` (не `Decimal(float)`); сравнений денежных лимитов
во float нет. Rounding policy:
- quantities — целые: requested final qty = floor(target / price); шаги
  урезания — ceil(необходимая сумма / price);
- fitted_target_value_rub — kopecks, ROUND_DOWN (отображение не завышает
  верхнюю границу);
- float появляется только в relay на границе float-домена движка
  (qty_delta в evaluate_scenario) и в эхе canonical planning_price.

Приоритет урезания (детерминированный, не optimizer):
1. sell_all и сокращения (target ниже текущего) применяются до увеличений —
   они не режутся никогда;
2. BUY increases входят в allocations по явному priority (меньше = раньше);
3. при нехватке бюджета режется lower-priority BUY первым;
4. при issuer violation режутся BUY того же эмитента, начиная с
   lower-priority; доля пересчитывается движком на ПОЛНОМ final state после
   каждого шага (никакого «headroom, посчитанного один раз»);
5. после каждого изменения — полная canonical валидация; в конце — ещё раз.

Сходимость: BUY qty только уменьшаются целыми шагами к полу `qty_before`
(урезание покупки не может продать больше держимого); каждый шаг либо
устраняет нарушение целиком (ceil по текущему canonical состоянию), либо
навсегда опускает один BUY до пола — число шагов ограничено числом
BUY-увеличений (+ страховочный bound).

Чего fitter НЕ делает:
- не продаёт и не сокращает позиции без intent (правило P0.3): violation,
  вызванный untouched holding и не исправимый допустимыми BUY-урезаниями,
  остаётся canonical violation (`feasible=false`);
- не подгоняет молча ineligible BUY: увеличение требует BUY eligibility
  (030.6); отказ → статус BUY_NOT_ELIGIBLE с structured reasons и без
  BUY-ноги в плане; direct target_value gate не обходит;
- не считает BUY sizing по номиналу: nominal_fallback (и нулевая цена
  matured_zero) не годится для BUY sizing в high-level path → статус
  PRICE_UNAVAILABLE (legacy low-level `--scenario` сохраняет свой fallback);
- не переписывает legacy `rebalance_report.py` и не трогает контракт
  `rebalance-report --scenario`.

Row-контракт: строки PlanningContext как есть (held_rows — агрегат
портфеля с quantity/value_rub/valuation_source; universe_rows — canonical
raw buy-пул с полями structural gates). BUY eligibility held-ISIN,
отсутствующего в buy-пуле, — NOT_IN_BUY_POOL: реальный buy-SQL не пропустил
бумагу ни по одному structural gate на frozen as_of (построчную
классификацию причин даёт permissive-выборка 030.6 — забота 030.8, fitter
БД вне context не читает).

Границы (не входит): CLI/factory-проводка (030.8), сравнение сценариев
(030.9), sandbox/adapter (030.10/030.11).
"""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from src.services.buy_eligibility import (
    BUY_NOT_ELIGIBLE,
    BuyEligibility,
    BuyEligibilityReason,
    evaluate_buy_eligibility,
)
from src.services.planning_context import PlanningContext
from src.services.planning_intents import (
    CandidateFilters,
    PortfolioConstraints,
    SellAllIntent,
    TargetValueIntent,
    allocation_order,
    default_candidate_filters,
)
from src.services.rebalance_engine import (
    CONSTRAINT_POSITION_VALUE_LIMIT,
    ISSUER_CONCENTRATION_LIMIT_PCT,
    PLANNING_PRICE_BASIS_NOMINAL_FALLBACK,
    SCENARIO_ERROR_UNKNOWN_ISIN,
    SCENARIO_LIMIT_STATUS_PASS,
    RebalanceEngine,
)

PlanningIntent = Union[SellAllIntent, TargetValueIntent]

# Статус подгонки одного target (машиночитаемый, контракт 030.7/030.8).
TARGET_FITTED = 'FITTED'
TARGET_STATUS_BUY_NOT_ELIGIBLE = BUY_NOT_ELIGIBLE  # единый источник строки (030.6)
TARGET_PRICE_UNAVAILABLE = 'PRICE_UNAVAILABLE'
TARGET_UNKNOWN_ISIN = SCENARIO_ERROR_UNKNOWN_ISIN  # код движка (блок 0 A2)

# Диагностика уровня плана (аналог scenario.errors движка).
DIAG_CONTEXT_PRICE_MISMATCH = 'CONTEXT_PRICE_MISMATCH'

# Причины подгонки (fit_reasons): почему final qty отличается от requested.
FIT_REASON_INTEGER_QTY = 'INTEGER_QTY'    # target округлён вниз до целой бумаги
FIT_REASON_POSITION_CAP = 'POSITION_CAP'  # max_position_value_rub (pre-size/fit)
FIT_REASON_BUDGET_CAP = 'BUDGET_CAP'      # нехватка cash (known cash)
FIT_REASON_ISSUER_CAP = 'ISSUER_CAP'      # лимит концентрации эмитента 15%

# Причина отказа BUY для ISIN, отсутствующего в canonical buy-пуле контекста.
REASON_NOT_IN_BUY_POOL = 'NOT_IN_BUY_POOL'

# Kopecks-квантование отображаемых денег (rounding policy, см. шапку).
_KOPECK = Decimal('0.01')

# Страховочный bound итераций fit-цикла (штатно хватает числа BUY-увеличений:
# каждый шаг либо устраняет нарушение целиком, либо опускает один BUY до пола).
_MAX_FIT_ITERATIONS = 100


def _price_decimal(context_price: Any) -> Decimal:
    """Canonical float planning_price -> Decimal (граница float→Decimal).

    Единственный relay доменов в fitter'е: `Decimal(str(value))` — shortest
    round-trip, без артефактов двоичного представления (не `Decimal(float)`).
    """
    return Decimal(str(float(context_price)))


def _floor_qty(value: Decimal, price: Decimal) -> int:
    """Максимальное целое количество бумаг на сумму value по цене price."""
    return int((value / price).to_integral_value(rounding=ROUND_FLOOR))


def _ceil_steps(amount: Decimal, price: Decimal) -> int:
    """Минимальное целое количество бумаг, покрывающее сумму amount."""
    steps = int((amount / price).to_integral_value(rounding=ROUND_CEILING))
    return max(steps, 1)


def _money(value: Decimal) -> Decimal:
    """Деньги наружу: kopecks, ROUND_DOWN (верхняя граница не завышается)."""
    return value.quantize(_KOPECK, rounding=ROUND_DOWN)


def _qty_out(value: Decimal) -> Union[int, float]:
    """Количество наружу: int, когда количество целое (fitted qty целые)."""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


@dataclass(frozen=True)
class FitDiagnostic:
    """Машиночитаемая диагностика уровня плана (аналог scenario.errors)."""

    code: str
    isin: Optional[str]
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {'code': self.code, 'isin': self.isin, 'message': self.message}


@dataclass(frozen=True)
class FitTargetResult:
    """Результат подгонки одного target (контракт «Result per target» 030.7).

    requested/fitted — деньги planning-слоя (Decimal; fitted — kopecks);
    price/price_basis — canonical planning цена и её basis (030.2) как есть
    из контекста; valuation_source — происхождение оценки удерживаемой
    позиции (008; у покупок None) — рядом с planning basis это РАЗНЫЕ
    понятия: чем оценена позиция vs по какой цене считается план.
    fit_reasons — почему final qty отличается от requested; buy_reasons —
    structured причины отказа eligibility (030.6), только у
    BUY_NOT_ELIGIBLE.
    """

    isin: str
    status: str
    requested_target_value_rub: Optional[Decimal]
    fitted_target_value_rub: Optional[Decimal]
    qty_before: Union[int, float]
    qty_after: Union[int, float]
    qty_delta: Union[int, float]
    price: Optional[float]
    price_basis: Optional[str]
    valuation_source: Optional[str] = None
    fit_reasons: Tuple[str, ...] = ()
    buy_reasons: Tuple[BuyEligibilityReason, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'isin': self.isin,
            'status': self.status,
            'requested_target_value_rub': self.requested_target_value_rub,
            'fitted_target_value_rub': self.fitted_target_value_rub,
            'qty_before': self.qty_before,
            'qty_after': self.qty_after,
            'qty_delta': self.qty_delta,
            'price': self.price,
            'price_basis': self.price_basis,
            'valuation_source': self.valuation_source,
            'fit_reasons': list(self.fit_reasons),
            'buy_reasons': [
                {'code': r.code, 'actual': r.actual, 'limit': r.limit}
                for r in self.buy_reasons
            ],
        }


@dataclass
class _TargetState:
    """Внутреннее рабочее состояние одного target'а во время fit-цикла."""

    isin: str
    intent: PlanningIntent
    echo_index: int
    qty_before: Decimal              # как есть из canonical портфеля
    floor_qty: Decimal               # ниже урезанием BUY опускаться нельзя
    desired_qty: Decimal             # текущий желаемый final qty
    requested_qty: Optional[int]     # floor(target/price) до корректировок
    price_dec: Optional[Decimal]     # Decimal-релей canonical цены
    price: Optional[float]           # canonical planning_price (эхо)
    price_basis: Optional[str]
    valuation_source: Optional[str]
    issuer: str
    status: str = TARGET_FITTED
    fit_reasons: List[str] = field(default_factory=list)
    eligibility: Optional[BuyEligibility] = None
    sizing_failed: bool = False      # PRICE_UNAVAILABLE / UNKNOWN_ISIN


class AutoFitFitter:
    """Fitter high-level intents в допустимые normalized сделки (030.7).

    Потребляет frozen `PlanningContext` (030.3) и валидирует результат
    исключительно каноническим движком `RebalanceEngine.evaluate_scenario`
    (030.2): весь sizing/netting/fit живёт здесь, вся математика лимитов,
    бюджета и валидация — в движке. `engine` можно передать явно
    (тесты/переиспользование); по умолчанию строится над тем же `db`.
    """

    def __init__(
        self,
        db: Any,
        context: PlanningContext,
        rating_scale: Optional[Dict[str, int]] = None,
        engine: Optional[RebalanceEngine] = None,
    ):
        self.db = db
        self.context = context
        self._engine = engine if engine is not None else RebalanceEngine(
            db, rating_scale=rating_scale
        )

    # ------------------------------------------------------------------
    # Публичный вход
    # ------------------------------------------------------------------

    def fit(
        self,
        intents: Sequence[PlanningIntent],
        constraints: Optional[PortfolioConstraints] = None,
        filters: Optional[CandidateFilters] = None,
    ) -> Dict[str, Any]:
        """Подогнать normalized intents (030.5) и провалидировать план.

        intents — typed DTO `SellAllIntent`/`TargetValueIntent` (выход
        `normalize_intents`, 030.5; повторный target одного ISIN там уже
        отклонён, дубликат здесь отклоняется громко). constraints —
        `PortfolioConstraints` (relay в canonical validator движка);
        filters — `CandidateFilters` policy eligibility покупки (030.6),
        defaults — canonical `default_candidate_filters()`.

        Возвращает dict: context_fingerprint/as_of, targets (эхо в порядке
        входа), trades (одна net-сделка на ISIN), diagnostics,
        price_consistent, полный canonical `validation` (результат движка)
        и plan-level `feasible` (= feasible движка; None при unknown cash
        или расхождении цен контекста и валидации).
        """
        constraints = constraints if constraints is not None else PortfolioConstraints()
        filters = filters if filters is not None else default_candidate_filters()

        targets = self._build_targets(intents, constraints, filters)

        # 1. Полный желаемый target vector; sells/reductions уже в нём.
        state: Dict[str, Decimal] = {t.isin: t.desired_qty for t in targets}
        trades = self._net_trades(targets, state)

        # 2. Bounded fit: каждое изменение — полная canonical валидация.
        validation = self._validate(trades, constraints)
        for _ in range(_MAX_FIT_ITERATIONS):
            fix = self._next_fix(targets, state, validation, constraints)
            if fix is None:
                break
            isin, new_qty = fix
            state[isin] = new_qty
            trades = self._net_trades(targets, state)
            validation = self._validate(trades, constraints)

        # 3. Гарантия «одна planning_price на sizing и валидацию».
        mismatch_diagnostics, price_consistent = self._check_price_consistency(
            targets, validation, trades
        )
        diagnostics = [
            FitDiagnostic(
                code=TARGET_UNKNOWN_ISIN,
                isin=target.isin,
                message=(
                    f"{target.isin}: ISIN не найден ни в портфеле, ни в "
                    "canonical universe среза контекста — target пропущен"
                ),
            )
            for target in targets if target.status == TARGET_UNKNOWN_ISIN
        ]
        diagnostics.extend(mismatch_diagnostics)
        for target in targets:
            target.desired_qty = state[target.isin]

        result_targets = tuple(self._target_result(t) for t in targets)
        feasible = validation['feasible'] if price_consistent else None

        return {
            'context_fingerprint': self.context.context_fingerprint,
            'as_of': self.context.as_of,
            'targets': result_targets,
            'trades': tuple(trades),
            'diagnostics': tuple(d.to_dict() for d in diagnostics),
            'price_consistent': price_consistent,
            'validation': validation,
            'feasible': feasible,
        }

    # ------------------------------------------------------------------
    # Классификация targets: requested qty, eligibility, pre-size caps
    # ------------------------------------------------------------------

    def _build_targets(
        self,
        intents: Sequence[PlanningIntent],
        constraints: PortfolioConstraints,
        filters: CandidateFilters,
    ) -> List[_TargetState]:
        """Классификация каждого intent'а в рабочее состояние (эхо-порядок).

        Sell/reduce разрешены без eligibility (030.6); любое увеличение
        проходит BUY gate; BUY sizing — только по planning цене контекста
        (nominal_fallback/нулевая цена запрещены), requested qty — floor.
        """
        if isinstance(intents, (str, bytes, dict)) or not isinstance(
            intents, (list, tuple)
        ):
            raise TypeError(
                "intents: ожидался список/кортеж normalized intents (030.5), "
                f"получен {type(intents).__name__}"
            )
        seen: set = set()
        for intent in intents:
            if not isinstance(intent, (SellAllIntent, TargetValueIntent)):
                raise TypeError(
                    "intents: ожидались normalized DTO SellAllIntent/"
                    f"TargetValueIntent, получен {type(intent).__name__}; "
                    "сырой вход нормализуйте normalize_intents() (030.5)"
                )
            if intent.isin in seen:
                raise ValueError(
                    f"{intent.isin}: повторный target одного ISIN в списке "
                    "intents — используйте normalize_intents() (030.5)"
                )
            seen.add(intent.isin)

        held_by_isin = {row['isin']: row for row in self.context.held_rows}
        universe_by_isin = {row['isin']: row for row in self.context.universe_rows}
        as_of_date = self.context.as_of_date

        targets: List[_TargetState] = []
        for echo_index, intent in enumerate(intents):
            isin = intent.isin
            held_row = held_by_isin.get(isin)
            qty_before = _row_decimal(
                held_row.get('quantity') if held_row else None
            ) or Decimal(0)

            raw_price = self.context.effective_prices.get(isin)
            if raw_price is None:
                # Нет ни в портфеле, ни в canonical universe среза — данных
                # для плана нет (валидатор движка увидел бы UNKNOWN_ISIN).
                targets.append(_TargetState(
                    isin=isin, intent=intent, echo_index=echo_index,
                    qty_before=qty_before, floor_qty=qty_before,
                    desired_qty=qty_before, requested_qty=None,
                    price_dec=None, price=None, price_basis=None,
                    valuation_source=_row_valuation_source(held_row),
                    issuer=_row_issuer(held_row, isin),
                    status=TARGET_UNKNOWN_ISIN, sizing_failed=True,
                ))
                continue

            price_float = float(raw_price['price'])
            price_dec = _price_decimal(raw_price['price'])
            basis = str(raw_price['basis'])
            issuer = _row_issuer(universe_by_isin.get(isin) or held_row, isin)

            requested_qty: Optional[int] = None
            reasons: List[str] = []
            if isinstance(intent, SellAllIntent):
                desired: Decimal = Decimal(0)
            else:
                if price_dec <= 0:
                    # matured_zero/нулевая цена: sizing невозможен (покупки
                    # погашенных в high-level path не существуют); sell_all
                    # sizing'а не требует и сюда не попадает по смыслу.
                    targets.append(self._unavailable_state(
                        intent, echo_index, qty_before, price_float, basis,
                        held_row, issuer,
                    ))
                    continue
                requested_qty = _floor_qty(
                    Decimal(str(intent.target_value_rub)), price_dec
                )
                if requested_qty * price_dec < Decimal(str(intent.target_value_rub)):
                    reasons.append(FIT_REASON_INTEGER_QTY)
                desired = Decimal(requested_qty)
                if (
                    basis == PLANNING_PRICE_BASIS_NOMINAL_FALLBACK
                    and desired > qty_before
                ):
                    # Silent nominal fallback для BUY в high-level path
                    # запрещён (файл задачи): BUY sizing только по market.
                    targets.append(self._unavailable_state(
                        intent, echo_index, qty_before, price_float, basis,
                        held_row, issuer,
                    ))
                    continue
                # Pre-size cap (конвейер 030.7): max-position до budget fit.
                if (
                    constraints.max_position_value_rub is not None
                    and desired > qty_before
                ):
                    cap_qty = Decimal(_floor_qty(
                        constraints.max_position_value_rub, price_dec
                    ))
                    if cap_qty < desired:
                        desired = max(cap_qty, qty_before)
                        reasons.append(FIT_REASON_POSITION_CAP)

            # BUY gate (030.6): только для увеличения; sell/reduce — без него.
            if desired > qty_before:
                pool_row = universe_by_isin.get(isin)
                if pool_row is None:
                    # Held-ISIN вне canonical buy-пула среза: buy-SQL не
                    # пропустил бумагу ни по одному structural gate.
                    eligibility = BuyEligibility(
                        isin=isin,
                        structural_eligible=False,
                        policy_eligible=False,
                        structural_reasons=(
                            BuyEligibilityReason(REASON_NOT_IN_BUY_POOL),
                        ),
                    )
                else:
                    eligibility = evaluate_buy_eligibility(
                        isin, pool_row, as_of_date, filters
                    )
                if not eligibility.buy_eligible:
                    # Молчаливого урезания до qty_before нет: отказ явный,
                    # BUY-ноги в плане нет (контракт 030.6/030.7).
                    targets.append(_TargetState(
                        isin=isin, intent=intent, echo_index=echo_index,
                        qty_before=qty_before, floor_qty=qty_before,
                        desired_qty=qty_before, requested_qty=requested_qty,
                        price_dec=price_dec, price=price_float,
                        price_basis=basis,
                        valuation_source=_row_valuation_source(held_row),
                        issuer=issuer, status=TARGET_STATUS_BUY_NOT_ELIGIBLE,
                        fit_reasons=reasons, eligibility=eligibility,
                    ))
                    continue

            targets.append(_TargetState(
                isin=isin, intent=intent, echo_index=echo_index,
                qty_before=qty_before, floor_qty=qty_before,
                desired_qty=desired, requested_qty=requested_qty,
                price_dec=price_dec, price=price_float, price_basis=basis,
                valuation_source=_row_valuation_source(held_row),
                issuer=issuer, fit_reasons=reasons,
            ))
        return targets

    def _unavailable_state(
        self, intent, echo_index, qty_before, price_float, basis, held_row, issuer
    ) -> _TargetState:
        """Target, который нельзя подогнать: PRICE_UNAVAILABLE, без сделки.

        Итог — desired = qty_before (BUY-ноги нет), с явной canonical ценой
        и basis в эхе: молчаливой подмены цены нет, отказ машиночитаем.
        """
        return _TargetState(
            isin=intent.isin, intent=intent, echo_index=echo_index,
            qty_before=qty_before, floor_qty=qty_before,
            desired_qty=qty_before, requested_qty=None,
            price_dec=None, price=price_float, price_basis=basis,
            valuation_source=_row_valuation_source(held_row), issuer=issuer,
            status=TARGET_PRICE_UNAVAILABLE, sizing_failed=True,
        )

    # ------------------------------------------------------------------
    # Canonical валидация и fit-цикл
    # ------------------------------------------------------------------

    def _validate(
        self, trades: List[Dict[str, Any]], constraints: PortfolioConstraints
    ) -> Dict[str, Any]:
        """ЕДИНСТВЕННАЯ валидация — canonical движок (030.2), на frozen as_of.

        qty_delta уходит во float только здесь (float-домен движка); все
        денежные решения приняты в Decimal до этой границы. Движок сам
        читает тот же canonical state и считает те же planning_prices —
        это и есть «одна цена на sizing и валидацию» (плюс явная сверка
        _check_price_consistency по итогам).
        """
        engine_trades = [
            {'isin': t['isin'], 'qty_delta': float(t['qty_delta'])} for t in trades
        ]
        return self._engine.evaluate_scenario(
            engine_trades,
            constraints.to_scenario_constraints(),
            as_of_date=self.context.as_of_date,
        )

    def _net_trades(
        self, targets: List[_TargetState], state: Dict[str, Decimal]
    ) -> List[Dict[str, Any]]:
        """Одна normalized net-сделка на ISIN (порядок эха входа)."""
        trades = []
        for target in targets:
            if target.sizing_failed:
                continue
            delta = state[target.isin] - target.qty_before
            if delta != 0:
                trades.append(
                    {'isin': target.isin, 'qty_delta': _qty_out(delta)}
                )
        return trades

    def _cut_sequence(self, targets: List[_TargetState]) -> List[str]:
        """BUY isins в порядке урезания: lower priority первым; при равных —
        позже в эхе первым (reversed стабильной allocation_order, 030.5)."""
        ordered = allocation_order([target.intent for target in targets])
        return [intent.isin for intent in reversed(ordered)]

    def _next_fix(
        self,
        targets: List[_TargetState],
        state: Dict[str, Decimal],
        validation: Dict[str, Any],
        constraints: PortfolioConstraints,
    ) -> Optional[Tuple[str, Decimal]]:
        """Первый применимый шаг урезания BUY по canonical нарушениям.

        Порядок проверок — конвейер файла задачи: budget -> issuer ->
        position. Каждая величина считается ОТ ТЕКУЩЕГО canonical прогона
        (полный final state): «headroom» ни разу не фиксируется. Возвращает
        (isin, new_qty) либо None — нарушений, исправимых BUY-урезанием,
        больше нет (остальные остаются canonical violations).
        """
        if not validation.get('valid'):
            return None  # structural ошибки количеством не исправляются
        by_isin = {target.isin: target for target in targets}

        def cut_by(isin: str, amount: Decimal) -> Optional[Tuple[str, Decimal]]:
            """Опустить BUY isin минимум на amount (в деньгах), не ниже пола."""
            target = by_isin[isin]
            if target.price_dec is None:
                return None
            if state[isin] - target.floor_qty <= 0:
                return None  # увеличения уже нет — резать нечего
            steps = Decimal(_ceil_steps(amount, target.price_dec))
            new_qty = max(target.floor_qty, state[isin] - steps)
            if new_qty >= state[isin]:
                return None
            return isin, new_qty

        # 1. Budget (только known cash; unknown не превращается в 0).
        budget = validation.get('budget_check') or {}
        if budget.get('known') and budget.get('passed') is False:
            needed = -Decimal(str(validation['summary']['cash_after_estimate']))
            if needed > 0:
                for isin in self._cut_sequence(targets):
                    fix = cut_by(isin, needed)
                    if fix is not None:
                        by_isin[isin].fit_reasons.append(FIT_REASON_BUDGET_CAP)
                        return fix

        # 2. Issuer 15% — на полном final state, после budget-изменений.
        issuer_limits = validation.get('issuer_limits') or {}
        for item in issuer_limits.get('after_all') or []:
            if item['limit_status'] == SCENARIO_LIMIT_STATUS_PASS:
                continue
            issuer = item['issuer']
            issuer_value = Decimal(str(item['value']))
            total_after = Decimal(str(validation['summary']['portfolio_after']))
            limit_frac = Decimal(str(ISSUER_CONCENTRATION_LIMIT_PCT)) / Decimal(100)
            excess = issuer_value - limit_frac * total_after
            if excess <= 0:
                continue
            # Урезание BUY этого эмитента уменьшает и числитель, и
            # знаменатель: нужно d >= (issuer_value - L*total) / (1 - L).
            amount = excess / (Decimal(1) - limit_frac)
            for isin in self._cut_sequence(targets):
                if by_isin[isin].issuer != issuer:
                    continue
                fix = cut_by(isin, amount)
                if fix is not None:
                    by_isin[isin].fit_reasons.append(FIT_REASON_ISSUER_CAP)
                    return fix
            # BUY этого эмитента нет/исчерпаны: violation от untouched
            # holding — автопродажи не будет (canonical violation).

        # 3. Position cap (страховка: pre-size закрывает штатные случаи).
        if constraints.max_position_value_rub is not None:
            for violation in validation.get('constraint_checks') or []:
                if violation['type'] != CONSTRAINT_POSITION_VALUE_LIMIT:
                    continue
                isin = violation['isin']
                target = by_isin.get(isin)
                if target is None or target.price_dec is None:
                    continue  # untouched holding — количеством не исправляем
                capped_qty = max(
                    target.floor_qty,
                    Decimal(_floor_qty(
                        constraints.max_position_value_rub, target.price_dec
                    )),
                )
                if capped_qty < state[isin]:
                    target.fit_reasons.append(FIT_REASON_POSITION_CAP)
                    return isin, capped_qty

        return None

    # ------------------------------------------------------------------
    # Результат
    # ------------------------------------------------------------------

    def _check_price_consistency(
        self,
        targets: List[_TargetState],
        validation: Dict[str, Any],
        trades: List[Dict[str, Any]],
    ) -> Tuple[List[FitDiagnostic], bool]:
        """Canonical цены прогона обязаны совпасть с frozen ценами контекста.

        Sizing считался по context.effective_prices; валидатор движка — по
        своим planning_prices из того же canonical расчёта. Сверяются ISIN,
        вошедшие в план сделками (цена остальных на валидированный final
        state не влияет). Расхождение означает, что срез БД изменился между
        сборкой context и планом: план по уехавшей цене не выдаётся
        (диагностика + feasible=null).
        """
        diagnostics: List[FitDiagnostic] = []
        engine_prices = validation.get('planning_prices') or {}
        consistent = True
        for trade in trades:
            isin = trade['isin']
            context_entry = self.context.effective_prices.get(isin)
            engine_entry = engine_prices.get(isin)
            same = (
                context_entry is not None
                and engine_entry is not None
                and float(engine_entry['price']) == float(context_entry['price'])
                and engine_entry['basis'] == context_entry['basis']
            )
            if not same:
                consistent = False
                diagnostics.append(FitDiagnostic(
                    code=DIAG_CONTEXT_PRICE_MISMATCH,
                    isin=isin,
                    message=(
                        f"{isin}: planning price валидации ({engine_entry}) "
                        f"не совпадает с ценой контекста ({context_entry}) "
                        "— срез данных изменился; план по уехавшей цене "
                        "не выдаётся"
                    ),
                ))
        return diagnostics, consistent

    def _target_result(self, target: _TargetState) -> FitTargetResult:
        """Контракт «Result per target» (030.7) из рабочего состояния."""
        fitted_value: Optional[Decimal] = None
        if target.status == TARGET_FITTED and target.price_dec is not None:
            fitted_value = _money(target.price_dec * target.desired_qty)
        requested = (
            target.intent.target_value_rub
            if isinstance(target.intent, TargetValueIntent) else None
        )
        return FitTargetResult(
            isin=target.isin,
            status=target.status,
            requested_target_value_rub=requested,
            fitted_target_value_rub=fitted_value,
            qty_before=_qty_out(target.qty_before),
            qty_after=_qty_out(target.desired_qty),
            qty_delta=_qty_out(target.desired_qty - target.qty_before),
            price=target.price,
            price_basis=target.price_basis,
            valuation_source=target.valuation_source,
            fit_reasons=tuple(target.fit_reasons),
            buy_reasons=target.eligibility.reasons if target.eligibility else (),
        )


# ---------------------------------------------------------------------------
# Утилиты чтения canonical строк (те же правила fallback'а, что у движка)
# ---------------------------------------------------------------------------


def _row_decimal(value: Any) -> Optional[Decimal]:
    """NUMERIC БД -> Decimal как есть; float — через str (граница relay)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(value)
    except (ValueError, TypeError, ArithmeticError):
        return None


def _row_issuer(row: Optional[Dict[str, Any]], isin: str) -> str:
    """Эмитент строки по правилу движка (issuer -> name -> isin)."""
    if row is None:
        return isin
    return row.get('issuer') or row.get('name') or isin


def _row_valuation_source(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Происхождение оценки удерживаемой позиции (008); у покупок None."""
    if row is None:
        return None
    return row.get('valuation_source')
