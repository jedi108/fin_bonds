"""
Use case: rebalance-plan — high-level CLI планирования (задача 030.8).

Одна команда standard Hermes workflow вместо цикла «создать scenario.json →
прогнать → вручную уменьшить qty → повторить» (baseline проекта: обычная
ребалансировка = report → plan, 2 domain-вызова, 0 ad-hoc кода). Use case —
тонкая композиция canonical домена (правило 030 №1), собственной арифметики
здесь нет:

    CLI-флаги → сырые intents → normalize_intents (030.5)
      → PlanningContext: store.load(--context-id) ИЛИ builder.build() (030.3)
      → AutoFitFitter.fit (030.7: sizing/netting/budget/issuer fit)
      → canonical валидация движком evaluate_scenario (030.2)

Use case присваивает priority intents НА границе CLI: порядок повторения
`--target-value ISIN=RUB` — allocation priority (первый = 1 = самый
приоритетный, МЕНЬШЕ = ВЫШЕ по контракту 030.5); при нехватке бюджета/лимита
эмитента fitter режет lower-priority BUY первым. Повторяемый `--sell-all`
идёт перед target-value intents (fitter применяет sells до увеличений
независимо от порядка).

Candidate-policy покупки (031.1) — отдельная ось CLI, НЕ формальные
ограничения целевого портфеля: `--max-risk/--max-listlevel/--max-ytm/
--include-ku/--min-maturity/--max-maturity` задают exploration-policy
BUY eligibility (030.6) и собираются в `CandidateFilters` для fitter'а;
без флага — canonical default `default_candidate_filters()` (копий
значений в CLI нет, parsing semantics — из `_SCREENER_FILTER_SPECS`
отчёта). sell_all/reduce фильтры не затрагивают: eligibility — только
для BUY/increase (030.6).

План строится на PlanningContext (правило 030 №4): без `--context-id` —
свежая сборка PlanningContextBuilder (frozen as_of, REPEATABLE READ,
canonical data_gate, effective_prices); с `--context-id` — core-owned
context из EphemeralContextStore (030.4; workflow snapshot → sandbox →
plan), БД не перечитывается. Context-level отказы (CONTEXT_NOT_FOUND /
CONTEXT_STALE / CONTEXT_VERSION_MISMATCH / CONTEXT_FINGERPRINT_MISMATCH) —
machine-readable diagnostics, не traceback: expected infeasibility и
ожидаемые operational-состояния возвращаются JSON'ом с exit 0 (как
scenario.errors legacy отчёта). `feasible` (финансовая ось движка) и
data_gate (ось свежести данных) — разные поля, друг в друга не
конвертируются.

Diagnostics (переиспользуют canonical коды, синонимов нет):
- intents: INVALID_TARGET_VALUE (форма `ISIN=RUB`), INVALID_INTENT /
  DUPLICATE_TARGET / CONFLICTING_INTENT (normalize_intents, 030.5);
- context: CONTEXT_* (030.4);
- plan: UNKNOWN_ISIN и CONTEXT_PRICE_MISMATCH (fitter 030.7), структурные
  ошибки движка (UNKNOWN_ISIN / NEGATIVE_QTY_AFTER / NO_COMPANY_LINK),
  BUY_NOT_ELIGIBLE (structured reasons[], 030.6) и PRICE_UNAVAILABLE
  (nominal_fallback/нулевая цена не годится для BUY sizing);
- violations целевого портфеля — canonical constraint_checks движка
  (POSITION_VALUE_LIMIT, COUPON_FREQUENCY_*, SOVEREIGN_NOT_ALLOWED,
  CREDIT_RATING_*, ISSUER_LIMIT, INSUFFICIENT_CASH_ESTIMATE); data
  freshness дублируется новым кодом ошибки НЕТ — canonical data_gate
  контекста (gate_status/is_fresh/criticals/warnings).

Вывод: JSON с envelope'ом (идентификация среза + вердикт valid/feasible/
price_consistent + diagnostics) в каждом view; блоки — по `--view`:
`summary` (portfolio/budget/coupon дельты), `targets` (requested vs fitted,
status, fit_reasons, buy_reasons, planning price/basis, valuation_source),
`trades` (canonical echo движка), `positions-after` (canonical целевой
портфель), `violations` (constraint_checks), `full` (всё + context/
constraints/filters/intents observability). Compact views — срез того же
payload, что и full (второго контура сборки нет). Деньги planning-слоя
(requested/fitted, cash, constraints echo) — decimal-строки (правило 030
№3, как в snapshot 030.10); блоки движка (summary/trades/positions_after/
violations) — его canonical float-релей без переформатирования (копии
расчёта нет). Целевой CSV пишется существующим canonical writer'ом отчёта
(`_write_target_csv`) над positions_after без post-processing.

Observability (full): constraints и candidate-filters echo; normalized
ordered intents с priority; PlanningContext timestamps; per-target
valuation_source / price_basis; requested vs fitted; fit reasons; финальный
вердикт валидации. Secrets/DSN/tokens не выводятся; обычный ответ — не
verbose trace (подробные логи — stderr logging).

Не делается (границы 030.8): сравнение сценариев (030.9), adapter/sandbox
(030.11), изменения legacy `rebalance-report`/`rebalance-analysis-snapshot`
(команда additive, фабрика — только новая строка map).
"""
import argparse
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import (
    TYPE_CHECKING, Any, Dict, List, Optional, Tuple,
)

from src.services.context_store import ContextStoreError, EphemeralContextStore
from src.services.planning_context import (
    PlanningContext,
    PlanningContextBuilder,
    canonical_datetime_str,
    canonical_decimal_str,
)
from src.services.planning_fitter import (
    TARGET_PRICE_UNAVAILABLE,
    TARGET_STATUS_BUY_NOT_ELIGIBLE,
    AutoFitFitter,
)
from src.services.planning_intents import (
    INTENT_TYPE_SELL_ALL,
    INTENT_TYPE_TARGET_VALUE,
    CandidateFilters,
    PortfolioConstraints,
    default_candidate_filters,
    normalize_intents,
)
from src.use_cases.base import UseCase
# Единственный источник parsing semantics candidate-фильтров (029-T06):
# таблица определений отчёта — type/choices/date-валидация переиспользуются,
# canonical DEFAULTS значений отсюда НЕ копируются (default=None, см.
# CANDIDATE_POLICY_FLAG_SPECS ниже).
from src.use_cases.rebalance_report import _SCREENER_FILTER_SPECS
from src.utils import rating_code_to_score

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

# Версия контракта JSON-ответа plan (состав/форма payload); версии
# расчётного контекста — context_schema_version/calculation_policy_version
# (030.3) и derived-версия store'а (030.4).
PLAN_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Candidate-policy флаги plan (031.1): eligibility новой/увеличиваемой
# позиции (030.6) — ДРУГАЯ ось, чем формальные ограничения целевого
# портфеля (--freq-min/--freq-max/--min-credit-rating/--exclude-sovereign/
# --max-position-value). Parsing semantics (type/choices/date-валидация) —
# из единственного источника `_SCREENER_FILTER_SPECS` отчёта (029-T06);
# canonical DEFAULTS значений здесь НЕ materialize (default=None) — их
# единственный источник `default_candidate_filters()` (030.5), явный флаг
# переопределяет. В группу не входят: --freq-in (031.2 — отдельная
# constraint-ось, FREQ_IN_FLAG_SPEC ниже) и --screener-limit (presentation
# control отчёта, 030.6/030.10 — universe контекста не режется).
# rebalance-compare зеркалит те же specs с префиксами --a-/--b-.
# ---------------------------------------------------------------------------
_CANDIDATE_POLICY_FLAGS = (
    '--max-risk', '--max-listlevel', '--max-ytm',
    '--include-ku', '--min-maturity', '--max-maturity',
)
_CANDIDATE_POLICY_HELP = {
    '--max-risk':
        'Candidate-policy покупки (НЕ ограничение целевого портфеля): '
        'максимальный уровень риска Т-Банка кандидата (1-5). Без флага — '
        'canonical default отчёта (default_candidate_filters).',
    '--max-listlevel':
        'Candidate-policy покупки: максимальный уровень листинга MOEX '
        'кандидата (1-3). Без флага — canonical default отчёта.',
    '--max-ytm':
        'Candidate-policy покупки: потолок YTM кандидата, %% годовых '
        '(аномалия = ВДО/дистресс). Без флага — canonical default отчёта.',
    '--include-ku':
        'Candidate-policy покупки: включить бумаги для квалифицированных '
        'инвесторов (canonical default — исключены).',
    '--min-maturity':
        'Candidate-policy покупки: минимальная дата погашения кандидата, '
        'YYYY-MM-DD. Без флага — canonical default отчёта.',
    '--max-maturity':
        'Candidate-policy покупки: максимальная дата погашения кандидата, '
        'YYYY-MM-DD. Без флага — canonical default отчёта.',
}
# (dest, argparse kwargs) с default=None: значения по умолчанию решает
# `_filters_from_args` поверх `default_candidate_filters()`.
CANDIDATE_POLICY_FLAG_SPECS = tuple(
    (
        flag.lstrip('-').replace('-', '_'),
        {
            **dict(_SCREENER_FILTER_SPECS)[flag],
            'default': None,
            'help': _CANDIDATE_POLICY_HELP[flag],
        },
    )
    for flag in _CANDIDATE_POLICY_FLAGS
)

# Constraint-флаг --freq-in (031.2): белый список допустимых частот купонов —
# ФОРМАЛЬНОЕ ОГРАНИЧЕНИЕ целевого портфеля (ось PortfolioConstraints.freq_in →
# to_scenario_constraints → canonical validator движка, constraint_checks
# COUPON_FREQUENCY_*; нарушение подтверждает только движок), НЕ candidate-
# policy (031.1) и не отчётный screener-фильтр кандидатов. Parsing semantics
# (CSV-список целых, 005.3) — из единственного источника
# `_SCREENER_FILTER_SPECS` отчёта (029-T06); canonical default-значения в CLI
# не materialize (default=None = ограничение не задано, семантика движка).
# Взаимоисключимость с --freq-min/--freq-max — конвенция отчёта (ValueError в
# _validate_args): без неё canonical validator (if/elif-цепочка проверок freq)
# молча приоритизировал бы диапазон над списком.
_FREQ_IN_HELP_OVERRIDE = (
    'Формальное ограничение: только указанные частоты купонов в год в '
    'ЦЕЛЕВОМ портфеле, через запятую (напр. 2,4 = «только квартальные и '
    'полугодовые»). Взаимоисключимо с --freq-min/--freq-max.'
)
FREQ_IN_FLAG_SPEC = (
    'freq_in',
    {
        **dict(_SCREENER_FILTER_SPECS)['--freq-in'],
        'help': _FREQ_IN_HELP_OVERRIDE,
    },
)

# Compact projections (файл задачи): каждое view — подмножество одного full
# payload'а. full — по умолчанию (полный план для агента), остальные —
# компактные проекции без лишних блоков.
VIEW_FULL = 'full'
VIEW_CHOICES = (
    'summary', 'targets', 'trades', 'positions-after', 'violations', VIEW_FULL,
)
# view → блоки payload сверх envelope (порядок = VIEW_CHOICES).
_VIEW_BLOCKS: Dict[str, tuple] = {
    'summary': ('summary', 'budget_check'),
    'targets': ('targets',),
    'trades': ('trades',),
    'positions-after': ('positions_after',),
    'violations': ('violations',),
    VIEW_FULL: (
        'context', 'constraints', 'filters', 'intents',
        'summary', 'budget_check', 'targets', 'trades',
        'positions_after', 'violations',
    ),
}
# Envelope — идентификация среза + вердикт + диагностика; есть в каждом view.
_ENVELOPE_KEYS = (
    'artifact', 'plan_schema_version', 'generated_at', 'view',
    'context_fingerprint', 'as_of', 'data_gate',
    'feasible', 'valid', 'price_consistent', 'diagnostics',
)


def _dec_str(value: Any) -> Optional[str]:
    """Деньги planning-слоя → decimal-строка (030 №3); None проходит."""
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return canonical_decimal_str(value)


class RebalancePlanUseCase(UseCase):
    """High-level план ребалансировки: intents → fitted целевой портфель.

    Флаги задают ПОКУПКИ/ПРОДАЖИ намерениями (`--sell-all ISIN`,
    `--target-value ISIN=RUB`), формальные ограничения целевого портфеля
    (--freq-min/--freq-max/--freq-in/--min-credit-rating/--exclude-sovereign/
    --max-position-value) и candidate-policy покупки (031.1: --max-risk/
    --max-listlevel/--max-ytm/--include-ku/--min-maturity/--max-maturity —
    eligibility новой/увеличиваемой позиции, 030.6; без флага — canonical
    default default_candidate_filters()); количества код не спрашивает у
    агента — sizing делает fitter по единой planning-цене.
    """

    def __init__(
        self,
        db,
        rating_scale: Optional[Dict[str, int]] = None,
        store: Optional[EphemeralContextStore] = None,
    ):
        self.db = db
        # Шкала рейтингов из конфига (023): порог --min-credit-rating
        # валидируется существующим rating_code_to_score; та же шкала у
        # движка/fitter'а и у store (derived policy version, 030.4).
        self.rating_scale = rating_scale or {}
        # Инъекция store — для тестов (tmp root/scope); по умолчанию
        # canonical ephemeral store проекта (data/planning_contexts, 030.4).
        self._store = store

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--freq-min', type=int, default=None,
            help='Формальное ограничение: минимальная частота купонов в год '
                 'в ЦЕЛЕВОМ портфеле (024). NULL/0-частота диапазон не проходит.'
        )
        parser.add_argument(
            '--freq-max', type=int, default=None,
            help='Формальное ограничение: максимальная частота купонов в год '
                 'в целевом портфеле (напр. 12).'
        )
        dest, kwargs = FREQ_IN_FLAG_SPEC
        parser.add_argument(
            f"--{dest.replace('_', '-')}", dest=dest, **kwargs
        )
        parser.add_argument(
            '--min-credit-rating', type=str, default=None,
            help='Формальное ограничение: минимальный кредитный рейтинг '
                 'позиций целевого портфеля, напр. AA- (строгий порог по '
                 'latest rating_history; код валидируется шкалой config.yaml).'
        )
        parser.add_argument(
            '--exclude-sovereign', action='store_true',
            help='Формальное ограничение: исключить суверенных эмитентов '
                 '(ОФЗ и евро-РФ) из целевого портфеля.'
        )
        parser.add_argument(
            '--max-position-value', type=float, default=None,
            help='Формальное ограничение: максимальная стоимость одного '
                 'выпуска в целевом портфеле, ₽ (напр. 350000). Fitter '
                 'pre-size\'ит покупки под лимит; нарушение любой позицией '
                 '— violation POSITION_VALUE_LIMIT.'
        )
        # Candidate-policy покупки (031.1): parsing semantics — из
        # _SCREENER_FILTER_SPECS (единственный источник), defaults — НЕ
        # отсюда (default=None → default_candidate_filters).
        for dest, kwargs in CANDIDATE_POLICY_FLAG_SPECS:
            parser.add_argument(
                f"--{dest.replace('_', '-')}", dest=dest, **kwargs
            )
        parser.add_argument(
            '--sell-all', action='append', default=None, metavar='ISIN',
            help='Продать позицию целиком (итог = 0), напр. --sell-all RU000A... '
                 'Повторяемый флаг; продаётся только указанное — продажи не '
                 'придумываются (untouched позиции не трогаются).'
        )
        parser.add_argument(
            '--target-value', action='append', default=None,
            metavar='ISIN=RUB',
            help='Желаемая итоговая стоимость позиции, ВЕРХНЯЯ ГРАНИЦА, ₽: '
                 '--target-value RU000B...=350000 (fitter берёт максимальный '
                 'допустимый final qty, значение округляется вниз до целой '
                 'бумаги). Повторяемый; ПОРЯДОК = allocation priority: '
                 'первый --target-value — самый приоритетный (меньше = '
                 'выше), при нехватке бюджета/лимита эмитента fitter режет '
                 'менее приоритетные (более поздние) покупки первыми.'
        )
        parser.add_argument(
            '--context-id', type=str, default=None,
            help='Opaque id core-owned PlanningContext (030.4, из '
                 'rebalance-analysis-snapshot): план считается по тому же '
                 'frozen срезу (БД не перечитывается). Без флага — свежая '
                 'сборка контекста. Истёкший/неизвестный id — diagnostic '
                 'CONTEXT_* в JSON, не traceback.'
        )
        parser.add_argument(
            '--view', choices=VIEW_CHOICES, default=VIEW_FULL,
            help='Проекция ответа: summary/targets/trades/positions-after/'
                 'violations — компактные блоки поверх envelope (вердикт + '
                 'diagnostics), full — весь план с observability. '
                 'Default: full.'
        )
        parser.add_argument(
            '--format', choices=['json'], default='json',
            help='Формат ответа: json — канонический машинный формат для агента.'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'RebalancePlanUseCase':
        """Создает use case с хранилищем из фабрики (DSN — из .env, не из CLI)."""
        return cls(
            db=factory.get_db_connection(),
            rating_scale=factory.config.get('rating_scale', {}),
        )

    # ------------------------------------------------------------------
    # Точка входа
    # ------------------------------------------------------------------

    def execute(self, args: argparse.Namespace):
        logger.info(
            "Формирование rebalance-plan (plan_schema_version=%s)",
            PLAN_SCHEMA_VERSION,
        )
        payload = self.build_plan(args)
        # JSON — в stdout; логи (logging) идут в stderr и ответ не портят.
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        logger.info("Rebalance-plan сформирован.")

    def build_plan(self, args: argparse.Namespace) -> Dict[str, Any]:
        """Собирает план — программный API (030.8).

        Canonical validation — внутри (единственная точка для CLI и
        программных потребителей). Возвращает тот же dict, что execute()
        печатает как JSON. Ожидаемые отказы (invalid intents, CONTEXT_*)
        — diagnostics в payload без исключения; неверная комбинация
        флагов-ограничений — ValueError по конвенции legacy отчёта.
        """
        self._validate_args(args)

        envelope = self._empty_envelope(args)
        raw_intents, intent_diagnostics = self._parse_intents(args)
        envelope['diagnostics'].extend(intent_diagnostics)

        normalization = normalize_intents(raw_intents)
        envelope['diagnostics'].extend(
            {'code': d.code, 'isin': d.isin, 'message': d.message}
            for d in normalization.diagnostics
        )

        # Любая диагностика границы (форма ISIN=RUB, дубликаты/конфликты)
        # отменяет план: план не строится на противоречивом входе (030.5).
        context: Optional[PlanningContext] = None
        if normalization.ok and not envelope['diagnostics']:
            context, context_diagnostics = self._build_context(args)
            envelope['diagnostics'].extend(context_diagnostics)

        if context is not None:
            constraints = self._constraints_from_args(args)
            filters = self._filters_from_args(args)
            plan_blocks = self._fit_and_project(
                context, list(normalization.intents), constraints, filters
            )
            envelope.update({
                'context_fingerprint': context.context_fingerprint,
                'as_of': canonical_datetime_str(context.as_of),
                'data_gate': self._data_gate_payload(context),
                'feasible': plan_blocks.pop('feasible'),
                'valid': plan_blocks.pop('valid'),
                'price_consistent': plan_blocks.pop('price_consistent'),
                'diagnostics': plan_blocks.pop('diagnostics'),
            })
            envelope.update(plan_blocks)
            envelope['context'] = self._context_payload(
                context, context_id=getattr(args, 'context_id', None)
            )
            envelope['constraints'] = self._constraints_payload(constraints)
            envelope['filters'] = self._filters_payload(filters)
            envelope['intents'] = self._intents_payload(normalization.intents)
        return self._select_view(envelope, args)

    # ------------------------------------------------------------------
    # Валидация флагов и разбор intents (граница CLI)
    # ------------------------------------------------------------------

    def _validate_args(self, args: argparse.Namespace):
        """Формальные ограничения — конвенция legacy отчёта (те же правила)."""
        if args.freq_min is not None and args.freq_min < 1:
            raise ValueError("--freq-min должен быть целым >= 1")
        if args.freq_max is not None and args.freq_max < 1:
            raise ValueError("--freq-max должен быть целым >= 1")
        if (args.freq_min is not None and args.freq_max is not None
                and args.freq_min > args.freq_max):
            raise ValueError("--freq-min не должен превышать --freq-max")
        # --freq-in (031.2) взаимоисключим с диапазоном частот — конвенция
        # отчёта (005.1/P-C, calculate-ytm 005.3): явный отказ комбинации
        # вместо молчаливого приоритета списка или диапазона в if/elif-цепочке
        # canonical validator'а движка. getattr — аддитивность: программный
        # Namespace без нового атрибута (до-031.2 контракт) не ломается.
        freq_in = getattr(args, 'freq_in', None)
        if freq_in is not None and args.freq_min is not None:
            raise ValueError("--freq-min и --freq-in взаимоисключающие: "
                             "выберите один способ задания частот")
        if freq_in is not None and args.freq_max is not None:
            raise ValueError("--freq-max и --freq-in взаимоисключающие: "
                             "выберите один способ задания частот")
        if args.max_position_value is not None and args.max_position_value <= 0:
            raise ValueError("--max-position-value должен быть > 0")
        # Candidate-policy (031.1): sanity-проверка порога — конвенция legacy
        # отчёта; eligibility-валидация — только движок/fitter (030.6).
        max_ytm = getattr(args, 'max_ytm', None)
        if max_ytm is not None and max_ytm <= 0:
            raise ValueError("--max-ytm должен быть > 0")
        if args.min_credit_rating is not None:
            threshold = rating_code_to_score(
                args.min_credit_rating, self.rating_scale
            )
            if threshold is None:
                raise ValueError(
                    f"--min-credit-rating: код {args.min_credit_rating!r} не входит "
                    "в шкалу rating_scale (config.yaml)"
                )

    def _parse_intents(
        self, args: argparse.Namespace
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """CLI-флаги → сырые intents + diagnostics границы (030.8).

        `--sell-all` (повторяемый) и `--target-value` (повторяемый; порядок =
        allocation priority, первый = priority 1). Форма `ISIN=RUB` с
        непустым ISIN и положительным числом; нарушение формы —
        INVALID_TARGET_VALUE (машиночитаемо, не argparse exit). Сделки
        qty_delta от агента нет — sizing делает fitter (030.7).
        """
        diagnostics: List[Dict[str, Any]] = []
        raw_intents: List[Dict[str, Any]] = []
        for isin in args.sell_all or []:
            isin = (isin or '').strip()
            if not isin:
                diagnostics.append({
                    'code': 'INVALID_INTENT', 'isin': None,
                    'message': '--sell-all: ISIN должен быть непустой строкой',
                })
                continue
            raw_intents.append({'type': INTENT_TYPE_SELL_ALL, 'isin': isin})

        for priority, spec in enumerate(args.target_value or [], start=1):
            isin, value = self._parse_target_value(spec)
            if value is None:
                diagnostics.append({
                    'code': 'INVALID_TARGET_VALUE', 'isin': isin,
                    'message': (
                        f"--target-value: ожидалась форма ISIN=RUB с "
                        f"положительным числом, получено {spec!r} "
                        f"(priority {priority})"
                    ),
                })
                continue
            raw_intents.append({
                'type': INTENT_TYPE_TARGET_VALUE,
                'isin': isin,
                'target_value_rub': value,  # строка: Decimal без float-потерь
                'priority': priority,
            })
        return raw_intents, diagnostics

    @staticmethod
    def _parse_target_value(spec: Any) -> Tuple[Optional[str], Optional[str]]:
        """'ISIN=RUB' → (isin, raw_value) либо (isin_or_None, None)."""
        if not isinstance(spec, str) or '=' not in spec:
            return None, None
        isin, _, raw_value = spec.partition('=')
        isin, raw_value = isin.strip(), raw_value.strip()
        if not isin or not raw_value:
            return (isin or None), None
        try:
            value = Decimal(raw_value)
        except InvalidOperation:
            return isin, None
        if not value.is_finite() or value <= 0:
            return isin, None
        return isin, raw_value

    # ------------------------------------------------------------------
    # PlanningContext: store.load(--context-id) | builder.build() (030.3/030.4)
    # ------------------------------------------------------------------

    def _build_context(
        self, args: argparse.Namespace
    ) -> Tuple[Optional[PlanningContext], List[Dict[str, Any]]]:
        """Context по --context-id (030.4) или свежая сборка (030.3).

        Context-level ошибки — machine-readable diagnostics (expected
        operational-состояния: истёкший/неизвестный id, несовместимая
        политика, повреждённый артефакт), не traceback.
        """
        context_id = getattr(args, 'context_id', None)
        if context_id:
            store = self._store or EphemeralContextStore(
                rating_scale=self.rating_scale
            )
            try:
                return store.load(context_id), []
            except ContextStoreError as exc:
                return None, [{
                    'code': exc.code, 'isin': None,
                    'message': f"--context-id {context_id}: {exc}",
                }]
        return PlanningContextBuilder(
            self.db, rating_scale=self.rating_scale
        ).build(), []

    # ------------------------------------------------------------------
    # Fit + проекции (композиция fitter 030.7 + canonical блоков движка)
    # ------------------------------------------------------------------

    def _fit_and_project(
        self,
        context: PlanningContext,
        intents: List[Any],
        constraints: PortfolioConstraints,
        filters: Any,
    ) -> Dict[str, Any]:
        """AutoFitFitter.fit (030.7) + canonical проекции движка (030.2).

        Собственной арифметики нет: targets — контракт «Result per target»
        fitter'а (деньги → decimal-строки), summary/budget_check/trades/
        positions_after/violations — canonical блоки validation движка как
        есть (его float-домен, переформатирование = копия расчёта).
        """
        result = AutoFitFitter(
            self.db, context, rating_scale=self.rating_scale
        ).fit(intents, constraints, filters)
        validation = result['validation']

        diagnostics: List[Dict[str, Any]] = list(result['diagnostics'])
        for error in validation.get('errors') or []:
            # Структурные ошибки движка — тот же контракт code/isin/message.
            diagnostics.append({
                'code': error['type'],
                'isin': error.get('isin'),
                'message': error['message'],
            })
        for target in result['targets']:
            if target.status == TARGET_STATUS_BUY_NOT_ELIGIBLE:
                diagnostics.append({
                    'code': TARGET_STATUS_BUY_NOT_ELIGIBLE,
                    'isin': target.isin,
                    'requested': _dec_str(target.requested_target_value_rub),
                    'reasons': [
                        {'code': r.code, 'actual': r.actual, 'limit': r.limit}
                        for r in target.buy_reasons
                    ],
                    'message': (
                        f"{target.isin}: покупка/увеличение не прошла BUY "
                        "eligibility — BUY-ноги в плане нет"
                    ),
                })
            elif target.status == TARGET_PRICE_UNAVAILABLE:
                diagnostics.append({
                    'code': TARGET_PRICE_UNAVAILABLE,
                    'isin': target.isin,
                    'price_basis': target.price_basis,
                    'message': (
                        f"{target.isin}: planning price ({target.price_basis}) "
                        "не пригодна для BUY sizing — target пропущен"
                    ),
                })

        return {
            'feasible': result['feasible'],
            'valid': validation['valid'],
            'price_consistent': result['price_consistent'],
            'diagnostics': diagnostics,
            'summary': validation.get('summary'),
            'budget_check': validation.get('budget_check'),
            'targets': [
                self._target_payload(t.to_dict()) for t in result['targets']
            ],
            'trades': validation.get('trades') or [],
            'positions_after': validation.get('positions_after') or [],
            'violations': validation.get('constraint_checks'),
        }

    @staticmethod
    def _target_payload(target: Dict[str, Any]) -> Dict[str, Any]:
        """Строка targets: деньги planning-слоя — decimal-строки (030 №3)."""
        payload = dict(target)
        payload['requested_target_value_rub'] = _dec_str(
            target.get('requested_target_value_rub')
        )
        payload['fitted_target_value_rub'] = _dec_str(
            target.get('fitted_target_value_rub')
        )
        return payload

    # ------------------------------------------------------------------
    # Observability-блоки full view
    # ------------------------------------------------------------------

    @staticmethod
    def _data_gate_payload(context: PlanningContext) -> Dict[str, Any]:
        """Canonical data_gate среза (ось свежести данных, не feasible)."""
        return {
            'status': context.data_gate.status,
            'gate_status': context.data_gate.gate_status,
            'is_fresh': context.data_gate.is_fresh,
            'criticals': list(context.data_gate.criticals),
            'warnings': list(context.data_gate.warnings),
        }

    def _context_payload(
        self, context: PlanningContext, context_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Metadata среза из 030.3: версии, fingerprint, frozen as_of,
        timestamps, canonical data_gate, cash (unknown не превращается в 0).
        context_id — эхо --context-id при переиспользовании store'а (030.4);
        у свежей сборки handle нет — идентификатор среза здесь
        context_fingerprint."""
        return {
            'context_id': context_id,
            'context_fingerprint': context.context_fingerprint,
            'context_schema_version': context.context_schema_version,
            'calculation_policy_version': context.calculation_policy_version,
            'as_of': canonical_datetime_str(context.as_of),
            'as_of_date': context.as_of_date.isoformat(),
            'generated_at': canonical_datetime_str(context.generated_at),
            'positions_updated_at': (
                canonical_datetime_str(context.positions_updated_at)
                if context.positions_updated_at is not None else None
            ),
            'cash_updated_at': (
                canonical_datetime_str(context.cash_updated_at)
                if context.cash_updated_at is not None else None
            ),
            'data_gate': self._data_gate_payload(context),
            'cash': {
                'cash_available_rub': _dec_str(context.cash.cash_available_rub),
                'cash_known': context.cash.cash_known,
            },
        }

    def _constraints_payload(
        self, constraints: PortfolioConstraints
    ) -> Dict[str, Any]:
        """Echo формальных ограничений целевого портфеля (024/030.5)."""
        return {
            'freq_min': constraints.freq_min,
            'freq_max': constraints.freq_max,
            'freq_in': list(constraints.freq_in) if constraints.freq_in else None,
            'exclude_sovereign': constraints.exclude_sovereign,
            'min_credit_rating': constraints.min_credit_rating,
            'max_position_value_rub': _dec_str(constraints.max_position_value_rub),
        }

    @staticmethod
    def _filters_payload(filters: Any) -> Dict[str, Any]:
        """Echo candidate-фильтров BUY eligibility (030.6/031.1): resolved
        значения — canonical defaults + явные overrides CLI-флагов."""
        return {
            'max_risk': filters.max_risk,
            'max_list_level': filters.max_list_level,
            'max_ytm_pct': filters.max_ytm_pct,
            'include_ku': filters.include_ku,
            'min_maturity': filters.min_maturity,
            'max_maturity': filters.max_maturity,
        }

    @staticmethod
    def _intents_payload(intents: Tuple[Any, ...]) -> List[Dict[str, Any]]:
        """Normalized ordered intents (эхо 1:1, 030.5): тип, ISIN, requested
        value и явный priority (меньше = выше allocation priority)."""
        payload = []
        for intent in intents:
            if hasattr(intent, 'target_value_rub'):
                payload.append({
                    'type': INTENT_TYPE_TARGET_VALUE,
                    'isin': intent.isin,
                    'target_value_rub': _dec_str(intent.target_value_rub),
                    'priority': intent.priority,
                })
            else:
                payload.append({
                    'type': INTENT_TYPE_SELL_ALL,
                    'isin': intent.isin,
                })
        return payload

    # ------------------------------------------------------------------
    # Envelope и view-проекции
    # ------------------------------------------------------------------

    def _empty_envelope(self, args: argparse.Namespace) -> Dict[str, Any]:
        """Envelope отказа (intents/context): план не построен — null'ы
        честные, verdict и diagnostics машиночитаемы."""
        return {
            'artifact': 'rebalance_plan',
            'plan_schema_version': PLAN_SCHEMA_VERSION,
            'generated_at': canonical_datetime_str(datetime.now(timezone.utc)),
            'view': args.view,
            'context_fingerprint': None,
            'as_of': None,
            'data_gate': None,
            'feasible': None,
            'valid': None,
            'price_consistent': None,
            'diagnostics': [],
        }

    @staticmethod
    def _select_view(
        envelope: Dict[str, Any], args: argparse.Namespace
    ) -> Dict[str, Any]:
        """View = envelope + блоки проекции; один и тот же payload, что и
        full (второго контура сборки нет — views соответствуют full)."""
        payload: Dict[str, Any] = {
            key: envelope[key] for key in _ENVELOPE_KEYS if key in envelope
        }
        for block in _VIEW_BLOCKS.get(args.view, ()):
            if block in envelope:
                payload[block] = envelope[block]
        return payload

    def _constraints_from_args(
        self, args: argparse.Namespace
    ) -> PortfolioConstraints:
        """Формальные ограничения целевого портфеля из флагов (030.5/031.2).
        Деньги — Decimal на границе (float флага → через str); freq_in —
        getattr по той же причине аддитивности, что в _validate_args."""
        return PortfolioConstraints(
            freq_min=args.freq_min,
            freq_max=args.freq_max,
            freq_in=getattr(args, 'freq_in', None),  # None = не задано
            exclude_sovereign=bool(args.exclude_sovereign),
            min_credit_rating=args.min_credit_rating,
            max_position_value_rub=(
                Decimal(str(args.max_position_value))
                if args.max_position_value is not None else None
            ),
        )

    def _filters_from_args(self, args: argparse.Namespace) -> CandidateFilters:
        """Candidate-policy флаги → CandidateFilters для fitter'а (031.1).

        Явный флаг переопределяет; без флага — canonical default
        `default_candidate_filters()` (единственный источник defaults,
        копий значений нет). getattr-доступ — аддитивность: программные
        Namespace без новых атрибутов получают canonical defaults.
        """
        defaults = default_candidate_filters()

        def resolve(dest: str, canonical):
            value = getattr(args, dest, None)
            return canonical if value is None else value

        return CandidateFilters(
            max_risk=resolve('max_risk', defaults.max_risk),
            max_list_level=resolve('max_listlevel', defaults.max_list_level),
            max_ytm_pct=resolve('max_ytm', defaults.max_ytm_pct),
            include_ku=resolve('include_ku', defaults.include_ku),
            min_maturity=resolve('min_maturity', defaults.min_maturity),
            max_maturity=resolve('max_maturity', defaults.max_maturity),
        )
