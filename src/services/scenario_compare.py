"""
Scenario compare — A/B сравнение альтернатив на одном PlanningContext (задача 030.9).

Закрывает реальный класс ошибки: нельзя молча сравнивать plan A и plan B,
рассчитанные по разным `PlanningContext` — delta смешала бы эффект стратегии
с изменением рыночных/портфельных данных между вызовами. LLM не должна
писать `execute_code` ради арифметики сравнения — сравнение делает домен.

Два контракта (файл задачи), одна арифметика сравнения:

- **Preferred**: `build_compare` создаёт ОДИН PlanningContext (030.3) и
  рассчитывает внутри него две или более альтернативы (каждый вариант —
  `AutoFitFitter.fit` 030.7 на том же frozen context), возвращает каждый
  plan result + delta. Raw universe до variant screening гарантируется
  самим контекстом: relaxed/strict варианты применяют свои CandidateFilters
  к одному набору исходных строк — strict не удаляет заранее строки,
  нужные relaxed.
- **Fallback**: `compare_plan_results` принимает ГОТОВЫЕ plan results
  (контракт fitter'а 030.7 либо плоские canonical проекции 030.8) и
  проверяет одинаковый `context_fingerprint`; при несовпадении —
  диагностика `CONTEXT_MISMATCH` (код файла задачи), delta не публикуется
  (None), метрики каждого варианта остаются его собственными canonical
  выходами.

Правило 030 №1 («один calculator»): метрики извлекаются ТОЛЬКО из canonical
outputs fitter/engine/validатора (summary/trades/positions_after/
constraint_checks), собственного пересчёта портфеля «третьей формулой» нет.
Единственная новая арифметика — сама дельта сравнения (variant − baseline):
новый код — Decimal (`Decimal(str(float))`, shortest round-trip), выдача —
decimal-строки (правило 030 №3); canonical значения движка публикуются как
есть (переформатирование = копия расчёта, прецедент 030.8).

Минимальные метрики сравнения (файл задачи) и их canonical источники:
- securities_value        ← validation.summary.securities_after
- cash_after_estimate     ← validation.summary.cash_after_estimate
- coupon_month            ← validation.summary.coupon_month_after
- coupon_year             ← validation.summary.coupon_year_after
- delta_month/delta_year  ← validation.summary.delta_month/delta_year
  (canonical дельты к portfolio_before; before у вариантов общий, поэтому
  разница метрик = разница эффектов стратегий)
- positions_count         ← len(validation.positions_after)
- max_issuer_share_pct    ← max(positions_after[].issuer_pct) (025)
- violations_count        ← len(constraint_checks); None — проверки не
  выполнялись (структурно невалидный сценарий), не «нет нарушений»
- changed_isins           ← sorted({trades[].isin}) — net-сделки плана

Persistent snapshot DB для compare не нужна (файл задачи): preferred-путь
живёт в одном процессе поверх immutable PlanningContext; cross-process
переиспользование среза — задача 030.4 (--context-id), не compare.

Не делается (границы 030.9): CLI-проводка (use case rebalance-compare),
adapter/sandbox (030.10/030.11), изменения legacy rebalance-report /
rebalance-plan / rebalance-analysis-snapshot.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.services.planning_context import PlanningContext, canonical_decimal_str
from src.services.planning_fitter import AutoFitFitter
from src.services.planning_intents import (
    CandidateFilters,
    PlanningIntent,
    PortfolioConstraints,
)

# Версия контракта ответа compare (состав metrics/delta); версии расчётного
# контекста — context_schema_version/calculation_policy_version (030.3).
COMPARE_SCHEMA_VERSION = 1

# Диагностика fallback-пути (код файла задачи): готовые plan results
# принадлежат разным PlanningContext. Это НЕ CONTEXT_FINGERPRINT_MISMATCH
# store'а (030.4 — повреждение core-owned артефакта): здесь оба result
# честно рассчитаны, просто по разным срезам — сравнивать их запрещено.
COMPARE_ERROR_CONTEXT_MISMATCH = 'CONTEXT_MISMATCH'

# Дельты (variant − baseline): деньги/доли — Decimal-строки; счётчики — int.
_DELTA_DECIMAL_KEYS = (
    'securities_value',
    'cash_after_estimate',
    'coupon_month',
    'coupon_year',
    'delta_month',
    'delta_year',
    'max_issuer_share_pct',
)
_DELTA_COUNT_KEYS = ('positions_count', 'violations_count')


@dataclass(frozen=True)
class CompareVariant:
    """Одна альтернатива preferred-пути: normalized intents (030.5) +
    ограничения целевого портфеля + опциональные candidate-фильтры (030.6).

    Constraints/filters — per-variant (strict/relaxed); raw universe у всех
    вариантов общий — он зафиксирован в PlanningContext ДО screening.
    intents — typed DTO `SellAllIntent`/`TargetValueIntent` (выход
    `normalize_intents`, как у `AutoFitFitter.fit`, 030.7).
    """

    name: str
    intents: Sequence[PlanningIntent]
    constraints: PortfolioConstraints = PortfolioConstraints()
    filters: Optional[CandidateFilters] = None


def _canonical_view(result: Mapping[str, Any]) -> Mapping[str, Any]:
    """Единый доступ к canonical блокам двух форм plan result (030.7/030.8):
    результат fitter'а несёт их во вложенном `validation`, плоская проекция
    plan — на верхнем уровне. Сами блоки в обоих формах — canonical выходы
    движка без переформатирования."""
    validation = result.get('validation')
    return validation if isinstance(validation, Mapping) else result


def extract_plan_metrics(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Минимальные метрики сравнения (файл задачи) из canonical outputs.

    Принимает plan result в форме fitter'а (030.7) или плоской проекции
    (030.8). Собственной арифметики нет — только чтение canonical блоков;
    значения публикуются как есть (float-релей движка, прецедент 030.8).
    """
    view = _canonical_view(result)
    summary = view.get('summary') or {}
    positions = view.get('positions_after') or []
    # constraint_checks движка / violations проекции plan — один canonical
    # блок валидатора; None (не считались) отличим от пустого списка.
    checks = view.get('constraint_checks')
    if checks is None:
        checks = result.get('violations')
    trades = result.get('trades') or view.get('trades') or []
    issuer_pcts = [
        position['issuer_pct']
        for position in positions
        if position.get('issuer_pct') is not None
    ]
    return {
        'securities_value': summary.get('securities_after'),
        'cash_after_estimate': summary.get('cash_after_estimate'),
        'coupon_month': summary.get('coupon_month_after'),
        'coupon_year': summary.get('coupon_year_after'),
        'delta_month': summary.get('delta_month'),
        'delta_year': summary.get('delta_year'),
        'positions_count': len(positions),
        'max_issuer_share_pct': max(issuer_pcts) if issuer_pcts else None,
        'violations_count': len(checks) if checks is not None else None,
        'changed_isins': sorted({trade['isin'] for trade in trades}),
    }


def _metric_delta(baseline: Mapping[str, Any], variant: Mapping[str, Any]) -> Dict[str, Any]:
    """Delta сравнения variant − baseline (единственная арифметика 030.9).

    Деньги/доли: Decimal(`Decimal(str(float))` — без артефактов binary float)
    → decimal-строка (правило 030 №3). Счётчики — int. None (unknown cash,
    не выполнялись проверки) в любой из позиций → None: неизвестность не
    маскируется под нулевую дельту.
    """
    delta: Dict[str, Any] = {}
    for key in _DELTA_DECIMAL_KEYS:
        before, after = baseline.get(key), variant.get(key)
        delta[key] = (
            canonical_decimal_str(Decimal(str(after)) - Decimal(str(before)))
            if before is not None and after is not None
            else None
        )
    for key in _DELTA_COUNT_KEYS:
        before, after = baseline.get(key), variant.get(key)
        delta[key] = after - before if before is not None and after is not None else None
    return delta


def compare_plan_results(
    results: Sequence[Mapping[str, Any]],
    names: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Сравнить ГОТОВЫЕ plan results с проверкой одного context (fallback).

    Каждый result — Mapping с `context_fingerprint` и canonical блоками
    (форма fitter'а 030.7 или плоской проекции 030.8). Baseline — первый
    вариант; `delta[имя]` = variant − baseline для остальных. При
    несовпадении fingerprint: `comparable=false`, `context_fingerprint=null`,
    `delta=null`, диагностика `CONTEXT_MISMATCH` (со всеми различающимися
    fingerprint'ами); метрики каждого варианта остаются публикацией его
    собственных canonical выходов — финансовая delta не публикуется как
    сопоставимая (файл задачи).
    """
    if isinstance(results, (str, bytes, Mapping)) or not isinstance(results, (list, tuple)):
        raise TypeError(
            "results: ожидался список/кортеж plan results (Mapping), "
            f"получен {type(results).__name__}"
        )
    if len(results) < 2:
        raise ValueError(
            f"compare требует две или более альтернативы, получено {len(results)}"
        )
    if names is None:
        names = tuple(f'variant_{index}' for index in range(1, len(results) + 1))
    if len(names) != len(results):
        raise ValueError('names: по одному имени на каждый plan result')
    if len(set(names)) != len(names):
        raise ValueError('names: имена вариантов должны быть уникальны')

    fingerprints: List[str] = []
    for result in results:
        if not isinstance(result, Mapping):
            raise TypeError(
                "results: каждый plan result — Mapping (контракт fitter 030.7 "
                f"или проекция 030.8), получен {type(result).__name__}"
            )
        fingerprint = result.get('context_fingerprint')
        if not isinstance(fingerprint, str) or not fingerprint:
            raise TypeError(
                'results: plan result без context_fingerprint — сравнение '
                'невозможно (fingerprint-проверка обязательна, файл задачи)'
            )
        fingerprints.append(fingerprint)

    distinct = sorted(set(fingerprints))
    comparable = len(distinct) == 1
    diagnostics: List[Dict[str, Any]] = []
    if not comparable:
        diagnostics.append({
            'code': COMPARE_ERROR_CONTEXT_MISMATCH,
            'isin': None,
            'context_fingerprints': distinct,
            'message': (
                'plan results рассчитаны по разным PlanningContext '
                f"({len(distinct)} различных context_fingerprint) — финансовая "
                "delta не публикуется как сопоставимая: получите планы по "
                "одному срезу (rebalance-compare или --context-id)"
            ),
        })

    metrics: Dict[str, Dict[str, Any]] = {}
    for name, result in zip(names, results):
        metrics[name] = {
            'feasible': result.get('feasible'),
            'valid': _canonical_view(result).get('valid'),
            **extract_plan_metrics(result),
        }

    delta: Optional[Dict[str, Dict[str, Any]]] = None
    if comparable:
        baseline_name = names[0]
        delta = {
            name: {'baseline': baseline_name,
                   **_metric_delta(metrics[baseline_name], metrics[name])}
            for name in names[1:]
        }

    return {
        'comparable': comparable,
        'context_fingerprint': distinct[0] if comparable else None,
        'diagnostics': diagnostics,
        'metrics': metrics,
        'delta': delta,
    }


def build_compare(
    db: Any,
    context: PlanningContext,
    variants: Sequence[CompareVariant],
    rating_scale: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Preferred-путь (файл задачи): ОДИН PlanningContext — N альтернатив.

    Каждый вариант подгоняется `AutoFitFitter.fit` (030.7) на том же frozen
    context (sizing/budget/issuer fit + canonical валидация движка 030.2),
    затем результаты сравниваются `compare_plan_results` — fingerprint'и
    совпадают by construction (один context), но проверка выполняется
    честно по значениям. Возвращает compare outcome + каждый plan result
    (контракт fitter'а как есть) в `variants`.
    """
    if len(variants) < 2:
        raise ValueError(
            f"compare требует две или более альтернативы, получено {len(variants)}"
        )
    names = [variant.name for variant in variants]
    if len(set(names)) != len(names):
        raise ValueError('имена вариантов должны быть уникальны')

    results = [
        AutoFitFitter(db, context, rating_scale=rating_scale).fit(
            variant.intents, variant.constraints, variant.filters
        )
        for variant in variants
    ]
    outcome = compare_plan_results(results, names=names)
    return {
        'compare_schema_version': COMPARE_SCHEMA_VERSION,
        **outcome,
        'variants': {name: result for name, result in zip(names, results)},
    }
