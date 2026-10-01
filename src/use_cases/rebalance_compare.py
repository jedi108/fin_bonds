"""
Use case: rebalance-compare — A/B сравнение альтернатив на одном
PlanningContext (задача 030.9).

Одна команда вместо «прогнать rebalance-plan дважды и сравнивать JSON
руками/execute_code»: compare создаёт ОДИН PlanningContext и рассчитывает
внутри него оба варианта, поэтому delta не может смешать эффект стратегии
с изменением рыночных/портфельных данных между вызовами (мотив 030.9).
Use case — тонкая композиция canonical домена (правило 030 №1), собственной
арифметики расчёта здесь нет:

    CLI-флаги (варианты A/B) → normalize_intents (030.5)
      → PlanningContext: store.load(--context-id) ИЛИ builder.build() (030.3/030.4)
      → AutoFitFitter.fit на одном context для каждого варианта (030.7)
      → canonical проекции движка (030.2) + scenario_compare (030.9)

Варианты задаются prefixed-флагами (зеркала флагов rebalance-plan):
`--a-sell-all/--a-target-value/--a-freq-min/...` и `--b-...`; различаться
могут и intents, и формальные ограничения (strict vs relaxed). Raw universe
до variant screening гарантирован самим PlanningContext (030.3): оба
варианта применяют свои фильтры к одному набору исходных строк.

Сравнение — только по canonical outputs (правило 030 №1): метрики каждого
варианта извлекаются из summary/trades/positions_after/constraint_checks
движка (`scenario_compare.extract_plan_metrics`), единственная новая
арифметика — delta вариант − baseline (первый вариант), деньгами в Decimal,
выдача decimal-строками (правило 030 №3).

Diagnostics (переиспользуют canonical коды, синонимов нет): intents —
INVALID_TARGET_VALUE / INVALID_INTENT / DUPLICATE_TARGET /
CONFLICTING_INTENT (атрибут `variant` указывает вариант), context — CONTEXT_*
(030.4); на fallback-пути (программный API `compare_plan_results`) —
CONTEXT_MISMATCH (0309: готовые plan results с разными fingerprint —
delta не публикуется). Ожидаемые отказы — machine-readable JSON с exit 0;
неверная комбинация флагов-ограничений — ValueError по конвенции отчёта.

Вывод: JSON envelope (артефакт, версия контракта, идентификация среза:
fingerprint/as_of/data_gate; вердикт comparable + diagnostics) + `metrics`
(минимальные метрики сравнения по вариантам), `delta` (variant − baseline,
decimal-строки; null при not comparable) и `variants` (каждый plan result:
canonical блоки 030.8 — summary/budget_check/targets/trades/positions_after/
violations). Legacy `rebalance-report`, `rebalance-plan` и
`rebalance-analysis-snapshot` не меняются (additive-правило 030 №2).

Не делается (границы 030.9): persistent snapshot DB для compare, adapter/
sandbox (030.10/030.11), per-variant CLI candidate-фильтры (policy-фильтры
покупки — canonical defaults отчёта, как в 030.8; per-variant filters
доступны программному API CompareVariant).
"""
import argparse
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List

from src.services.planning_context import canonical_datetime_str
from src.services.planning_intents import (
    default_candidate_filters,
    normalize_intents,
)
from src.services.scenario_compare import (
    COMPARE_SCHEMA_VERSION,
    CompareVariant,
    compare_plan_results,
)
from src.use_cases.base import UseCase
from src.use_cases.rebalance_plan import RebalancePlanUseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

# Варианты CLI: ровно пара A/B (файл задачи — A/B сравнение; произвольное
# число альтернатив поддерживает программный API build_compare).
_VARIANT_PREFIXES = ('a', 'b')

# Флаги одного варианта — зеркала rebalance-plan (суффикс dest, argparse
# kwargs). Граница CLI (парсинг/валидация) переиспользуется у 030.8 —
# собственных копий семантики флагов compare не создаёт.
_VARIANT_FLAG_SPECS = (
    ('sell_all', dict(
        action='append', default=None, metavar='ISIN',
        help='Вариант: продать позицию целиком (итог = 0), как --sell-all '
             'rebalance-plan. Повторяемый.',
    )),
    ('target_value', dict(
        action='append', default=None, metavar='ISIN=RUB',
        help='Вариант: желаемая итоговая стоимость позиции, ВЕРХНЯЯ '
             'ГРАНИЦА, ₽ (fitter округляет вниз до целой бумаги). '
             'Повторяемый; порядок = allocation priority внутри варианта.',
    )),
    ('freq_min', dict(
        type=int, default=None,
        help='Вариант: формальное ограничение — минимальная частота купонов '
             'в год в целевом портфеле (024).',
    )),
    ('freq_max', dict(
        type=int, default=None,
        help='Вариант: формальное ограничение — максимальная частота купонов '
             'в год в целевом портфеле.',
    )),
    ('min_credit_rating', dict(
        type=str, default=None,
        help='Вариант: формальное ограничение — минимальный кредитный '
             'рейтинг позиций целевого портфеля (шкала config.yaml).',
    )),
    ('exclude_sovereign', dict(
        action='store_true',
        help='Вариант: формальное ограничение — исключить суверенных '
             'эмитентов из целевого портфеля.',
    )),
    ('max_position_value', dict(
        type=float, default=None,
        help='Вариант: формальное ограничение — максимальная стоимость '
             'одного выпуска в целевом портфеле, ₽ (fitter pre-size\'ит '
             'покупки под лимит).',
    )),
)


class RebalanceCompareUseCase(UseCase):
    """A/B сравнение альтернатив на одном PlanningContext (030.9).

    Граница CLI (парсинг intents/ограничений, сборка context, canonical
    проекции плана) переиспользуется у `RebalancePlanUseCase` (030.8):
    второго парсера флагов и второй проекции плана compare не создаёт.
    """

    def __init__(self, db, rating_scale=None, store=None):
        self.db = db
        self.rating_scale = rating_scale or {}
        # Инъекция store — для тестов (tmp root/scope), как в 030.8.
        self._store = store
        self._plan = RebalancePlanUseCase(
            db=db, rating_scale=self.rating_scale, store=store
        )

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        for prefix in _VARIANT_PREFIXES:
            for suffix, kwargs in _VARIANT_FLAG_SPECS:
                parser.add_argument(
                    f'--{prefix}-{suffix.replace("_", "-")}',
                    dest=f'{prefix}_{suffix}',
                    **kwargs,
                )
        parser.add_argument(
            '--context-id', type=str, default=None,
            help='Opaque id core-owned PlanningContext (030.4, из '
                 'rebalance-analysis-snapshot): ОБА варианта считаются по '
                 'тому же frozen срезу. Без флага — свежая сборка контекста. '
                 'Ошибки CONTEXT_* — machine-readable diagnostics.'
        )
        parser.add_argument(
            '--format', choices=['json'], default='json',
            help='Формат ответа: json — канонический машинный формат для агента.'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'RebalanceCompareUseCase':
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
            "Сравнение альтернатив rebalance-compare "
            "(compare_schema_version=%s)", COMPARE_SCHEMA_VERSION,
        )
        payload = self.build_compare(args)
        # JSON — в stdout; логи (logging) идут в stderr и ответ не портят.
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        logger.info("Rebalance-compare сформирован.")

    def build_compare(self, args: argparse.Namespace) -> Dict[str, Any]:
        """Собирает сравнение — программный API (030.9).

        Возвращает тот же dict, что execute() печатает как JSON. Ожидаемые
        отказы (invalid intents, CONTEXT_*) — diagnostics в payload без
        исключения; неверная комбинация флагов-ограничений — ValueError по
        конвенции legacy отчёта.
        """
        envelope: Dict[str, Any] = {
            'artifact': 'rebalance_compare',
            'compare_schema_version': COMPARE_SCHEMA_VERSION,
            'generated_at': canonical_datetime_str(datetime.now(timezone.utc)),
            'context_fingerprint': None,
            'as_of': None,
            'data_gate': None,
            'comparable': None,
            'diagnostics': [],
            'metrics': None,
            'delta': None,
            'variants': None,
        }

        # 1. Intents/ограничения вариантов — граница CLI plan (030.8):
        #    парсинг, валидация значений и нормализация переиспользованы.
        variants: List[CompareVariant] = []
        for prefix in _VARIANT_PREFIXES:
            sub_args = self._variant_args(args, prefix)
            # ValueError (конвенция отчёта) — неверная комбинация флагов.
            self._plan._validate_args(sub_args)
            raw_intents, intent_diagnostics = self._plan._parse_intents(sub_args)
            envelope['diagnostics'].extend(
                {**diagnostic, 'variant': prefix}
                for diagnostic in intent_diagnostics
            )
            normalization = normalize_intents(raw_intents)
            envelope['diagnostics'].extend(
                {
                    'code': diagnostic.code,
                    'isin': diagnostic.isin,
                    'message': diagnostic.message,
                    'variant': prefix,
                }
                for diagnostic in normalization.diagnostics
            )
            variants.append(CompareVariant(
                name=prefix,
                intents=list(normalization.intents),
                constraints=self._plan._constraints_from_args(sub_args),
            ))
        if envelope['diagnostics']:
            # Сравнение не строится на противоречивом входе (030.5).
            return envelope

        # 2. ОДИН PlanningContext (030.3/030.4) — фундамент сопоставимости.
        context, context_diagnostics = self._plan._build_context(
            argparse.Namespace(context_id=getattr(args, 'context_id', None))
        )
        if context_diagnostics:
            envelope['diagnostics'].extend(context_diagnostics)
            return envelope

        # 3. Каждый вариант — fitter на том же context; проекции — canonical
        #    блоки 030.8 (вторая проекция плана не пишется).
        filters = default_candidate_filters()
        variant_blocks: Dict[str, Any] = {}
        for variant in variants:
            projection = self._plan._fit_and_project(
                context, list(variant.intents), variant.constraints, filters
            )
            variant_blocks[variant.name] = {
                'name': variant.name,
                'context_fingerprint': context.context_fingerprint,
                **projection,
            }

        # 4. Сравнение — canonical outputs; delta вариант − baseline (030.9).
        comparison = compare_plan_results(
            [variant_blocks[variant.name] for variant in variants],
            names=[variant.name for variant in variants],
        )
        envelope.update({
            'context_fingerprint': context.context_fingerprint,
            'as_of': canonical_datetime_str(context.as_of),
            'data_gate': self._plan._data_gate_payload(context),
            'comparable': comparison['comparable'],
            'diagnostics': envelope['diagnostics'] + comparison['diagnostics'],
            'metrics': comparison['metrics'],
            'delta': comparison['delta'],
            'variants': variant_blocks,
        })
        return envelope

    # ------------------------------------------------------------------
    # Граница CLI: prefixed-флаги → namespace plan (030.8)
    # ------------------------------------------------------------------

    @staticmethod
    def _variant_args(args: argparse.Namespace, prefix: str) -> argparse.Namespace:
        """Флаги одного варианта под именами атрибутов plan — переиспользование
        boundary-логики 030.8 (`_parse_intents`/`_validate_args`/
        `_constraints_from_args`) без копий семантики."""
        return argparse.Namespace(**{
            suffix: getattr(args, f'{prefix}_{suffix}')
            for suffix, _ in _VARIANT_FLAG_SPECS
        })
