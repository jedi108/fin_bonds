"""
Domain intents high-level планирования и их нормализация (задача 030.5).

Граница ответственности (правило 030 №7): Hermes/CLI приносит предметные
intents, код нормализует и валидирует их. Модуль — ТОЛЬКО типизированные DTO
и нормализация сырого входа: сделок (qty_delta) здесь не строится,
eligibility покупок не проверяется (030.6), fitting target_value в
допустимый final qty не выполняется (030.7). Legacy `rebalance-report
--scenario` модуль не трогает: низкоуровневая агрегация нескольких
qty_delta одного ISIN остаётся как есть; high-level planner строит ОДНУ
normalized target state и ОДНУ net trade на ISIN уже поверх этого модуля.

Intents (минимальный набор 030.5):
- `sell_all(isin)` — итоговая позиция = 0;
- `target_value(isin, target_value_rub, priority)` — ВЕРХНЯЯ ГРАНИЦА
  желаемой итоговой стоимости позиции после плана. Fitter (030.7) выбирает
  максимальный допустимый final qty, для которого
  `final_position_value <= target_value_rub`, с учётом hard constraints
  (canonical validator движка) и бюджета. Следствия: позиции нет → докупка
  до target; ниже target → докупка; выше → сокращение; точного равенства
  нет (целые бумаги + hard limits); отдельного `reduce_to_value` для MVP
  не нужно. Requested и fitted value — контракт ответа planner'а (030.7),
  intent несёт только requested.

Один high-level target на ISIN: повторный intent на тот же ISIN — ошибка
нормализации, «last write wins» запрещён:
- тот же вид (target_value+target_value, sell_all+sell_all) →
  `DUPLICATE_TARGET`;
- разные виды (target_value vs sell_all) → `CONFLICTING_INTENT`.

Приоритет — явное поле `priority` intent'а; целое число, МЕНЬШЕ значение =
ВЫШЕ allocation priority (ранг: 1 — самый приоритетный). Для CLI порядок
повторяемых `--target-value` — явный allocation priority (маппинг
документируется в 030.8). Нормализация priority не выводит молча — intent
без явного priority невалиден. Нормализованные intents эхо-возвращаются
1:1 (в порядке входа) с priority; `allocation_order()` отдаёт
target_value-intents в порядке allocation priority для fitter'а (030.7).

Нормализация не придумывает продажи (правило P0.3): normalized intents —
строго 1:1 эхо запрошенных; auto-fit (030.7) имеет право только (а)
уменьшать requested BUY до допустимого fitted target, (б) применять
explicit `sell_all`, (в) применять сокращение, следующее из explicit
`target_value`. Продажа/сокращение позиции без intent запрещена;
untouched позиция, нарушающая hard constraint, даёт canonical violation +
`feasible=false` (canonical validator движка), стратегическое решение о
продаже — за LLM/пользователем.

Canonical источники (второго копирования значений нет):
- defaults candidate-фильтров — только `default_candidate_filters()`,
  собранный из `RebalanceReportUseCase.default_args()` (тот же parser, что
  и CLI отчёта; единственный источник — `_SCREENER_FILTER_SPECS`,
  029-T06/030.1). В `CandidateFilters` дефолтов нет намеренно.
- лимит концентрации эмитента — canonical константа движка
  `ISSUER_CONCENTRATION_LIMIT_PCT` (реэкспорт ниже, 030.2/030.1); нового
  пользовательского policy flag и второго hardcoded limit здесь нет.
- формальные ограничения целевого портфеля — `PortfolioConstraints`,
  релей в canonical validator движка (`ScenarioConstraints`, 024);
  валидация остаётся в движке, второй валидатор не создаётся.

Деньги нового planning-слоя — `Decimal` (правило 030 №3); float появляется
только как relay на границе float-домена движка
(`PortfolioConstraints.to_scenario_constraints`). Raw-деньги принимаются
как int/str/Decimal/float и канонизируются в Decimal (float → через
str — shortest round-trip).
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

from src.services.rebalance_engine import (
    ISSUER_CONCENTRATION_LIMIT_PCT,
    ScenarioConstraints,
)
from src.use_cases.rebalance_report import RebalanceReportUseCase

# Виды intents (машиночитаемые значения поля 'type' сырой формы).
INTENT_TYPE_SELL_ALL = 'sell_all'
INTENT_TYPE_TARGET_VALUE = 'target_value'

# Коды диагностики нормализации (машиночитаемые, как scenario.errors движка).
INTENT_ERROR_INVALID = 'INVALID_INTENT'
INTENT_ERROR_DUPLICATE_TARGET = 'DUPLICATE_TARGET'
INTENT_ERROR_CONFLICTING_INTENT = 'CONFLICTING_INTENT'

PlanningIntent = Union['SellAllIntent', 'TargetValueIntent']


def _to_decimal(value: Any, field_name: str) -> Decimal:
    """int/str/Decimal/float -> Decimal (деньги planning-слоя, правило 030 №3).

    float конвертируется через str() (shortest round-trip, без артефактов
    двоичного представления); bool числом не считается. NaN/Inf отклоняются.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(
            f"{field_name}: ожидалось число (int/float/str/Decimal), "
            f"получено {type(value).__name__}"
        )
    try:
        result = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field_name}: не число: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name}: должно быть конечным числом, получено {value!r}")
    return result


# ---------------------------------------------------------------------------
# DTO: ограничения и фильтры (frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioConstraints:
    """Формальные ограничения целевого портфеля (024) в planning-слое.

    Проверяются canonical validator'ом движка по ВСЕМУ целевому портфелю
    (constraint_checks: freq_min/freq_max/freq_in, exclude_sovereign,
    min_credit_rating, max_position_value, лимит эмитента) — второй
    валидации здесь нет, только relay `to_scenario_constraints()`.
    None/False = ограничение не проверяется (семантика движка).

    Сюда НЕ входят candidate-фильтры (max_risk/max_list_level/max_ytm/KU/
    maturity): это eligibility новой/увеличиваемой позиции (030.6), а НЕ
    обязанность переписать весь существующий портфель — untouched holdings
    под candidate-фильтры не подпадают.
    """

    freq_min: Optional[int] = None
    freq_max: Optional[int] = None
    freq_in: Optional[Tuple[int, ...]] = None
    min_credit_rating: Optional[str] = None
    exclude_sovereign: bool = False
    max_position_value_rub: Optional[Decimal] = None

    def __post_init__(self):
        # Иммутабельность коллекций + Decimal-деньги: канонизация на границе.
        if self.freq_in is not None:
            object.__setattr__(self, 'freq_in', tuple(self.freq_in))
        if self.max_position_value_rub is not None:
            object.__setattr__(
                self,
                'max_position_value_rub',
                _to_decimal(self.max_position_value_rub, 'max_position_value_rub'),
            )

    def to_scenario_constraints(self) -> ScenarioConstraints:
        """Relay в canonical validator движка (030.2/024).

        Decimal-деньги -> float ТОЛЬКО здесь: float-домен движка — его
        собственное поведение (legacy report); float-сравнений в planning-
        коде этот relay не создаёт.
        """
        return ScenarioConstraints(
            freq_min=self.freq_min,
            freq_max=self.freq_max,
            freq_in=self.freq_in,
            exclude_sovereign=self.exclude_sovereign,
            min_credit_rating=self.min_credit_rating,
            max_position_value=(
                float(self.max_position_value_rub)
                if self.max_position_value_rub is not None
                else None
            ),
        )


@dataclass(frozen=True)
class CandidateFilters:
    """Candidate-фильтры eligibility новой/увеличиваемой позиции (030.6).

    Контракт 030.5 <-> 030.6: policy eligibility покупки (поверх
    structural BUY gate того же 030.6). Это НЕ ограничения целевого
    портфеля (`PortfolioConstraints`): существующий портфель не обязан
    переписываться под фильтры кандидатов.

    Дефолтов у полей нет намеренно: единственный источник defaults —
    `default_candidate_filters()` ниже (canonical args rebalance-report);
    копия значений в DTO разъехалась бы с отчётом.
    """

    max_risk: int
    max_list_level: int
    max_ytm_pct: float
    include_ku: bool
    min_maturity: Optional[str] = None
    max_maturity: Optional[str] = None


def default_candidate_filters() -> CandidateFilters:
    """Canonical defaults candidate-фильтров — из источника отчёта.

    Тот же parser, что и CLI `rebalance-report` (`default_args()` =
    `parse_args([])` поверх `setup_parser`; единственный источник значений
    — `_SCREENER_FILTER_SPECS`, 029-T06/030.1). Planner не дублирует
    defaults: max-risk/max-listlevel/max-ytm/KU/maturity semantics не
    разъедаются с отчётом by construction.
    """
    args = RebalanceReportUseCase.default_args()
    return CandidateFilters(
        max_risk=args.max_risk,
        max_list_level=args.max_listlevel,
        max_ytm_pct=args.max_ytm,
        include_ku=bool(args.include_ku),
        min_maturity=args.min_maturity,
        max_maturity=args.max_maturity,
    )


# ---------------------------------------------------------------------------
# DTO: intents (frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SellAllIntent:
    """sell_all(ISIN): итоговая позиция = 0 после плана."""

    isin: str


@dataclass(frozen=True)
class TargetValueIntent:
    """target_value(ISIN, RUB, priority): верхняя граница итоговой стоимости.

    `target_value_rub` — НЕ точный размер позиции, а верхняя граница:
    fitter (030.7) берёт максимальный допустимый final qty с
    `final_position_value <= target_value_rub`; requested и fitted value
    возвращает planner (030.7). `priority` — явный allocation priority
    (меньше = раньше); нормализацию проходит только с явным priority.
    """

    isin: str
    target_value_rub: Decimal
    priority: int

    def __post_init__(self):
        object.__setattr__(
            self,
            'target_value_rub',
            _to_decimal(self.target_value_rub, 'target_value_rub'),
        )


# ---------------------------------------------------------------------------
# Нормализация
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentDiagnostic:
    """Машиночитаемая диагностика нормализации (аналог scenario.errors)."""

    code: str
    isin: Optional[str]
    message: str


@dataclass(frozen=True)
class IntentNormalization:
    """Результат нормализации: эхо normalized intents + диагностики.

    `intents` — 1:1 эхо входа (порядок входа сохранён), только при полном
    успехе; при любой диагностике — пустой кортеж (частичное применение
    запрещено: план не строится на противоречивом входе).
    """

    intents: Tuple[PlanningIntent, ...]
    diagnostics: Tuple[IntentDiagnostic, ...]

    @property
    def ok(self) -> bool:
        return not self.diagnostics


class _InvalidRawIntent(Exception):
    """Внутренняя ошибка формы сырого intent'а (несёт готовый diagnostic)."""

    def __init__(self, message: str, isin: Optional[str] = None):
        super().__init__(message)
        self.diagnostic = IntentDiagnostic(
            code=INTENT_ERROR_INVALID, isin=isin, message=message
        )


def _intent_kind(intent: PlanningIntent) -> str:
    if isinstance(intent, TargetValueIntent):
        return INTENT_TYPE_TARGET_VALUE
    return INTENT_TYPE_SELL_ALL


def _normalize_one(raw: Any, index: int) -> PlanningIntent:
    """Один сырой intent -> typed DTO; проблема формы -> _InvalidRawIntent."""
    where = f"intent #{index}"
    if not isinstance(raw, Mapping):
        raise _InvalidRawIntent(
            f"{where}: ожидался объект intent (Mapping), "
            f"получен {type(raw).__name__}"
        )
    intent_type = raw.get('type')
    if intent_type not in (INTENT_TYPE_SELL_ALL, INTENT_TYPE_TARGET_VALUE):
        raise _InvalidRawIntent(
            f"{where}: неизвестный type {intent_type!r}; допустимы "
            f"{INTENT_TYPE_SELL_ALL!r} и {INTENT_TYPE_TARGET_VALUE!r}"
        )
    isin = raw.get('isin')
    if not isinstance(isin, str) or not isin.strip():
        raise _InvalidRawIntent(f"{where}: 'isin' должен быть непустой строкой")
    isin = isin.strip()

    if intent_type == INTENT_TYPE_SELL_ALL:
        return SellAllIntent(isin=isin)

    raw_value = raw.get('target_value_rub')
    if raw_value is None:
        raise _InvalidRawIntent(
            f"{where} ({isin}): отсутствует 'target_value_rub'", isin
        )
    try:
        value = _to_decimal(raw_value, 'target_value_rub')
    except ValueError as exc:
        raise _InvalidRawIntent(str(exc), isin) from exc
    if value <= 0:
        raise _InvalidRawIntent(
            f"{where} ({isin}): 'target_value_rub' должен быть > 0 "
            f"(получено {value}); итог «0» выражается intent'ом sell_all",
            isin,
        )
    priority = raw.get('priority')
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise _InvalidRawIntent(
            f"{where} ({isin}): 'priority' обязателен и должен быть целым "
            "числом (меньше = выше allocation priority); молчаливого "
            "default у priority нет",
            isin,
        )
    return TargetValueIntent(isin=isin, target_value_rub=value, priority=priority)


def normalize_intents(raw_intents: Sequence[Any]) -> IntentNormalization:
    """Нормализует сырые intents (CLI/LLM-адаптер) в typed DTO (030.5).

    Сырая форма одного intent (Mapping):
        {'type': 'sell_all', 'isin': 'RU000...'}
        {'type': 'target_value', 'isin': 'RU000...',
         'target_value_rub': 150000, 'priority': 1}

    Правила: деньги — Decimal; isin — непустая строка (trim, как у
    legacy scenario-сделок); priority — явный целый. Один high-level
    target на ISIN: повторный target того же вида -> DUPLICATE_TARGET,
    другого вида -> CONFLICTING_INTENT (last-write-wins запрещён).
    Продажи не придумываются: результат — 1:1 эхо входа.

    Все диагностики собираются сразу (не fail-fast на первой), чтобы
    потребитель получил полный список проблем; при любой диагностике
    `intents` пуст и `ok` = False.
    """
    if isinstance(raw_intents, (str, bytes, Mapping)) or not isinstance(
        raw_intents, (list, tuple)
    ):
        raise TypeError(
            "raw_intents: ожидался список/кортеж сырых intents (Mapping), "
            f"получен {type(raw_intents).__name__}"
        )

    diagnostics = []
    normalized: list = []
    seen: dict = {}  # isin -> (kind, input index, 1-based)
    for index, raw in enumerate(raw_intents, start=1):
        try:
            intent = _normalize_one(raw, index)
        except _InvalidRawIntent as exc:
            diagnostics.append(exc.diagnostic)
            continue
        kind = _intent_kind(intent)
        previous = seen.get(intent.isin)
        if previous is not None:
            prev_kind, prev_index = previous
            if kind == prev_kind:
                code = INTENT_ERROR_DUPLICATE_TARGET
                reason = (
                    f"повторный {kind} для ISIN, у которого уже есть target "
                    f"того же вида (intent #{prev_index})"
                )
            else:
                code = INTENT_ERROR_CONFLICTING_INTENT
                reason = (
                    f"конфликт target'ов: {prev_kind} (intent #{prev_index}) "
                    f"и {kind} на один и тот же ISIN"
                )
            diagnostics.append(
                IntentDiagnostic(
                    code=code,
                    isin=intent.isin,
                    message=(
                        f"{intent.isin}: {reason}. У ISIN должен быть один "
                        "high-level target; last-write-wins запрещён — "
                        "оставьте один intent"
                    ),
                )
            )
            continue
        seen[intent.isin] = (kind, index)
        normalized.append(intent)

    if diagnostics:
        return IntentNormalization(intents=(), diagnostics=tuple(diagnostics))
    return IntentNormalization(intents=tuple(normalized), diagnostics=())


def allocation_order(
    intents: Sequence[PlanningIntent],
) -> Tuple[TargetValueIntent, ...]:
    """target_value-intents в порядке allocation priority (для fitter'а).

    Меньше priority = раньше; равные priority сохраняют порядок эха
    (стабильная сортировка) — детерминированный порядок consumption
    бюджета (030.7). sell_all в allocation не участвует.
    """
    return tuple(
        sorted(
            (i for i in intents if isinstance(i, TargetValueIntent)),
            key=lambda intent: intent.priority,
        )
    )
