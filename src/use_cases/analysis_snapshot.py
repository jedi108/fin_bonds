"""
Use case: rebalance-analysis-snapshot — read-only вход для неизвестной
аналитики (задача 030.10).

Один общий read-only вход для аналитики, которую domain API пока не
покрывает (пример: «сделай свой score из YTM, rating и duration»). Escape
hatch расширяет ВЫЧИСЛЕНИЯ, а не источники данных: снапшот строится на том
же PlanningContext (030.3), что plan/compare — отдельного SQL-контура нет.
Snapshot только агрегирует canonical срез: расчётов в нём нет (правило 030
№1), цены — effective_prices движка, YTM — enrich_with_ytm движка, gate —
classify_freshness, structural eligibility — релей канонического buy-пула.

Поток: PlanningContextBuilder.build() -> EphemeralContextStore.save() ->
handle {context_id, context_fingerprint, expires_at} -> copy-for-reading
payload. Payload — КОПИЯ для чтения, не authoritative PlanningContext:
sandbox возвращает только context_id, plan/compare загружают core-owned
context из store (030.4); граница владения — store, не fingerprint payload'а.

Состав payload:
- context: schema/policy версии (контракт 030.3 + derived-версия store
  030.4), opaque context_id, context_fingerprint, frozen as_of, canonical
  data_gate, freshness metadata (positions/cash updated_at), TTL-окно
  handle (ось store, не data_gate);
- portfolio: canonical cash (unknown не превращается в 0) + текущие
  позиции (агрегат по ISIN, canonical оценка view + planning price/basis);
- universe: canonical raw buy-пул среза — та же широкая выборка
  RAW_UNIVERSE_POLICY, что у plan/compare, БЕЗ presentation top-N
  (screener_limit отчёта — presentation-контроль скринера, здесь не
  применяется); политика пула публикуется рядом (limit — потолок SQL-пула,
  не top-N выдачи);
- structural_buy_eligible + reason codes (030.6): строка canonical
  buy-пула прошла structural gates buy-выборки в SQL на frozen as_of —
  eligibility=true, reasons=[] (релей факта buy-path, НЕ построчный
  пересчёт); held-позиция ВНЕ пула — eligibility=false с канонической
  причиной NOT_IN_BUY_POOL (тот же код, что у fitter 030.7: buy-SQL не
  пропустил бумагу ни по одному structural gate). Построчная Python-оценка
  structural_buy_violations здесь сознательно не применяется: canonical
  срезы (позиции/buy-пул) не несут колонок gates currency/
  is_trade_available/market_price_updated_at — оценка дала бы ложные
  причины; для запрошенного ISIN вне среза построчные reasons даёт
  plan-путь (030.6/030.8). «Есть в каталоге/портфеле» ≠ «можно покупать»;
- capabilities/limitations: machine-readable реестр datasets (что доступно
  post-029, чего нет и почему); --requires проверяет запрошенные коды и
  возвращает ANALYSIS_DATA_UNAVAILABLE + какого source field/dataset не
  хватает (данные не синтезируются; повторяющийся gap — отдельная задача
  на canonical source/ingestion, не молчаливое расширение здесь);
- NULL сохраняется как есть + canonical reason, где он есть (ytm_percent
  = null + ytm_reason; unknown cash = null + cash_known=false); нулём
  неизвестное значение не подменяется;
- secrets: payload не содержит DSN/tokens/credentials/broker-order
  capability (read-only, сценарии/сделки не создаёт); приватные данные
  портфеля — данные пользователя, снапшот помечен приватным артефактом
  (не отправлять наружу; правило дублируется skill'ом 030.12).

Вывод по умолчанию compact (metadata + counts + preview), полный payload —
явно (--full): adapter (030.11) передаёт полный snapshot как data
artifact/file в sandbox, LLM получает только metadata/preview — extensibility
не решается огромным JSON в prompt.

Не делается (границы 030.10): transport/lifecycle для LLM и stable adapter
(030.11), skill SOP (030.12), plan/compare CLI (030.8/030.9), persistent
snapshot DB (запрещено), update/store API для sandbox (030.4).
"""
import argparse
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

from src.services.context_store import EphemeralContextStore
from src.services.planning_context import (
    PlanningContext,
    PlanningContextBuilder,
    canonical_datetime_str,
    canonical_decimal_str,
)
from src.services.planning_fitter import REASON_NOT_IN_BUY_POOL
from src.use_cases.base import UseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)

# Версия контракта самого snapshot-артефакта (состав/форма payload); версии
# расчётного контракта среза — context_schema_version /
# calculation_policy_version (030.3) и derived-версия store'а (030.4).
SNAPSHOT_SCHEMA_VERSION = 1

# Канонический код data gap (файл задачи): анализ, требующий отсутствующего/
# недостаточно свежего dataset, получает его + указание, чего не хватает.
ANALYSIS_DATA_UNAVAILABLE = 'ANALYSIS_DATA_UNAVAILABLE'

# Сколько строк попадает в preview compact-ответа (полный payload — --full).
PREVIEW_ROWS = 5

# Machine-readable реестр datasets post-029 (факт по 030.1): что снапшот
# несёт. Единственный источник кодов для capabilities и --requires.
AVAILABLE_DATASETS: Dict[str, str] = {
    'portfolio_positions': (
        'текущие позиции (агрегат по ISIN): qty, каноническая оценка view '
        '(price/value/valuation_source), planning price/basis'
    ),
    'cash': (
        'canonical cash снапшота (022); unknown cash — null + cash_known=false, '
        'нулём не подменяется'
    ),
    'buy_universe': (
        'canonical raw buy-пул среза (PlanningContext.universe_rows) БЕЗ '
        'presentation top-N; политика пула — рядом (universe.policy)'
    ),
    'planning_prices': (
        'цена расчёта + basis на каждый ISIN среза на frozen as_of (030.2)'
    ),
    'data_gate': (
        'canonical data-freshness gate среза: status/gate_status/is_fresh/'
        'criticals/warnings (тот же, что meta.data_freshness отчёта)'
    ),
    'ytm': (
        'обогащённая каноническая YTM движка: ytm_pct + ytm_reason + '
        'ytm_updated_at; NULL сохраняется с reason'
    ),
    'coupon_rate_frequency': (
        'ставка купона (COALESCE с флоатером), частота, coupon_kind, '
        'amortization/perpetual флаги'
    ),
    'maturity': (
        'дата погашения; у позиций также дата оферты'
    ),
    'credit_rating': (
        'последний рейтинг из rating_history: код + балл (без даты рейтинга — '
        'см. limitations)'
    ),
    'risk_list_level': (
        'риск-уровень и уровень листинга'
    ),
    'issuer_sovereign_ku': (
        'эмитент, entity_type (суверенность), КУ-флаг, доля held-позиции'
    ),
}

# Datasets, которых НЕТ post-029 (честные ограничения, файл задачи): код +
# canonical причина. Анализ, требующий такого dataset, —
# ANALYSIS_DATA_UNAVAILABLE с этим reason; данные здесь не синтезируются.
UNAVAILABLE_DATASETS: Dict[str, str] = {
    'exact_coupon_schedule': (
        'точный график купонных выплат по датам не является canonical dataset '
        '(cashflow-модель расчётная; до backfill — YTM-fallback)'
    ),
    'amortization_schedule': (
        'график амортизации не считается: amortization_schedule_missing '
        '(учитывается только флаг)'
    ),
    'settlement_costs': (
        'оценки денег среза — qty * planning_price без НКД и комиссий '
        'settlement\'а (cash_after_estimate — estimate, не settlement)'
    ),
    'per_instrument_duration': (
        'duration-поля не входят в canonical срез post-029 (нет ни в строках '
        'позиций, ни в buy-пуле)'
    ),
    'per_instrument_market_price_freshness': (
        'свежесть market_price в срезе агрегатна (data_gate criticals/warnings '
        '+ aggregate max); построчные market_price_updated_at не публикуются'
    ),
    'rating_dates': (
        'дата последнего рейтинга не извлекается в срез — только код и балл'
    ),
}


def _parse_requires(raw: str) -> List[str]:
    """'ytm, credit_rating' -> ['ytm', 'credit_rating'] (для argparse type=).

    Пустые элементы и дубликаты отсекаются, порядок сохраняется.
    """
    codes: List[str] = []
    for part in raw.split(','):
        code = part.strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def _opt_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def _opt_float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _opt_decimal_str(value: Any) -> Optional[str]:
    """Деньги/количества в JSON — детерминированные decimal строки (030 №3);
    арифметики в snapshot нет."""
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return canonical_decimal_str(value)


def _opt_iso_day(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _opt_iso_dt(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return canonical_datetime_str(value)
    return str(value)


class AnalysisSnapshotUseCase(UseCase):
    """read-only analysis snapshot поверх PlanningContext (030.10)."""

    def __init__(
        self,
        db,
        rating_scale: Optional[Dict[str, int]] = None,
        store: Optional[EphemeralContextStore] = None,
    ):
        self.db = db
        self.rating_scale = rating_scale or {}
        # Инъекция store — для тестов (tmp root/scope); по умолчанию
        # canonical ephemeral store проекта (data/planning_contexts, 030.4).
        self._store = store

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--format', choices=['json'], default='json',
            help='Формат ответа: json — канонический машинный формат для агента.'
        )
        parser.add_argument(
            '--full', action='store_true',
            help='Полный payload: все строки universe и позиции портфеля. '
                 'По умолчанию compact — metadata, counts и preview '
                 '(полный snapshot передаётся как data artifact, не в prompt).'
        )
        parser.add_argument(
            '--requires', type=_parse_requires, default=None,
            help='Коды datasets через запятую, которые требуются анализу '
                 '(см. capabilities.available_datasets / limitations). '
                 'Отсутствующие -> ANALYSIS_DATA_UNAVAILABLE + список того, '
                 'какого source field/dataset не хватает.'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'AnalysisSnapshotUseCase':
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
            "Формирование analysis-snapshot (snapshot_schema_version=%s)",
            SNAPSHOT_SCHEMA_VERSION,
        )
        payload = self.build_snapshot(args)
        # JSON — в stdout; логи (logging) идут в stderr и ответ не портят.
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        logger.info("Analysis-snapshot сформирован.")

    def build_snapshot(
        self, args: Optional[argparse.Namespace] = None
    ) -> Dict[str, Any]:
        """Собирает snapshot payload — программный API (030.10).

        Тот же snapshot/context builder, что plan/compare
        (PlanningContextBuilder, 030.3), + core-owned context_id от
        EphemeralContextStore (030.4): последующий plan --context-id
        загружает ТОТ ЖЕ authoritative context (по fingerprint).
        """
        context = PlanningContextBuilder(
            self.db, rating_scale=self.rating_scale
        ).build()
        store = self._store or EphemeralContextStore(
            rating_scale=self.rating_scale
        )
        handle = store.save(context)

        full = bool(getattr(args, 'full', False))

        universe_rows = [
            self._universe_row_payload(
                row, context.effective_prices[row['isin']]
            )
            for row in context.universe_rows
        ]
        universe_isins = {row['isin'] for row in context.universe_rows}
        positions = [
            self._held_row_payload(
                row,
                context.effective_prices[row['isin']],
                in_buy_pool=row['isin'] in universe_isins,
            )
            for row in context.held_rows
        ]

        payload: Dict[str, Any] = {
            'artifact': 'analysis_snapshot',
            'snapshot_schema_version': SNAPSHOT_SCHEMA_VERSION,
            'generated_at': canonical_datetime_str(datetime.now(timezone.utc)),
            # Compact по умолчанию (030.10): preview вместо всех строк.
            'preview': not full,
            'context': self._context_payload(context, handle, store),
            'portfolio': self._portfolio_payload(
                context, positions, full
            ),
            'universe': self._universe_payload(context, universe_rows, full),
            'analysis': self._analysis_payload(args),
            'capabilities': self._capabilities_payload(),
            'limitations': [
                {'code': code, 'reason': reason}
                for code, reason in sorted(UNAVAILABLE_DATASETS.items())
            ],
        }
        return payload

    # ------------------------------------------------------------------
    # Блоки payload
    # ------------------------------------------------------------------

    @staticmethod
    def _context_payload(
        context: PlanningContext,
        handle: Mapping[str, Any],
        store: EphemeralContextStore,
    ) -> Dict[str, Any]:
        """Идентификация среза: версии, opaque id, fingerprint, as_of,
        canonical data_gate, freshness metadata и TTL-окно handle (ось
        store, не data_gate)."""
        return {
            'context_id': handle['context_id'],
            'context_fingerprint': handle['context_fingerprint'],
            'context_schema_version': context.context_schema_version,
            'calculation_policy_version': context.calculation_policy_version,
            # Derived-версия политики store'а (030.4): её сверяет load;
            # CONTEXT_VERSION_MISMATCH объясняется этой меткой.
            'calculation_policy_version_derived':
                handle['calculation_policy_version'],
            'as_of': canonical_datetime_str(context.as_of),
            'as_of_date': context.as_of_date.isoformat(),
            'created_at': canonical_datetime_str(handle['created_at']),
            'expires_at': canonical_datetime_str(handle['expires_at']),
            'ttl_seconds': store.ttl_seconds,
            'positions_updated_at': _opt_iso_dt(context.positions_updated_at),
            'cash_updated_at': _opt_iso_dt(context.cash_updated_at),
            'data_gate': {
                'status': context.data_gate.status,
                'gate_status': context.data_gate.gate_status,
                'is_fresh': context.data_gate.is_fresh,
                'criticals': list(context.data_gate.criticals),
                'warnings': list(context.data_gate.warnings),
            },
        }

    @staticmethod
    def _portfolio_payload(
        context: PlanningContext,
        positions: List[Dict[str, Any]],
        full: bool,
    ) -> Dict[str, Any]:
        """Текущий портфель + canonical cash (unknown cash не превращается в 0)."""
        block: Dict[str, Any] = {
            'cash': {
                'cash_available_rub': _opt_decimal_str(
                    context.cash.cash_available_rub
                ),
                'cash_known': context.cash.cash_known,
            },
            'positions_count': len(positions),
        }
        if full:
            block['positions'] = positions
        else:
            block['positions_preview'] = positions[:PREVIEW_ROWS]
        return block

    @staticmethod
    def _universe_payload(
        context: PlanningContext,
        rows: List[Dict[str, Any]],
        full: bool,
    ) -> Dict[str, Any]:
        """Canonical raw universe БЕЗ presentation top-N: политика пула
        публикуется рядом (limit — потолок SQL-пула, не top-N выдачи;
        screener_limit отчёта — presentation-контроль скринера, здесь
        не применяется)."""
        block: Dict[str, Any] = {
            'policy': dict(context.universe_policy),
            'row_count': len(rows),
        }
        if full:
            block['rows'] = rows
        else:
            block['rows_preview'] = rows[:PREVIEW_ROWS]
        return block

    @staticmethod
    def _analysis_payload(args: Optional[argparse.Namespace]) -> Dict[str, Any]:
        """--requires: проверка запрошенных dataset-кодов против реестра.

        Отсутствующий/неизвестный код -> ANALYSIS_DATA_UNAVAILABLE + reason
        (какого source field/dataset не хватает); данные не синтезируются.
        """
        requested = list(getattr(args, 'requires', None) or [])
        missing: List[Dict[str, Any]] = []
        for code in requested:
            if code in UNAVAILABLE_DATASETS:
                missing.append(
                    {'code': code, 'reason': UNAVAILABLE_DATASETS[code]}
                )
            elif code not in AVAILABLE_DATASETS:
                missing.append({
                    'code': code,
                    'reason': (
                        'неизвестный код dataset; см. '
                        'capabilities.available_datasets и limitations'
                    ),
                })
        return {
            'requested_datasets': requested,
            'status': ANALYSIS_DATA_UNAVAILABLE if missing else 'OK',
            'missing': missing,
        }

    @staticmethod
    def _capabilities_payload() -> Dict[str, Any]:
        """Machine-readable capabilities: Hermes по ним знает, какие виды
        анализа данными поддерживаются."""
        return {
            'read_only': True,
            # Broker-order capability и создание scenario/trades запрещены.
            'creates_trades_or_scenarios': False,
            # Код data gap для запрошенной, но не поддерживаемой аналитики.
            'analysis_data_gap_code': ANALYSIS_DATA_UNAVAILABLE,
            'available_datasets': dict(AVAILABLE_DATASETS),
            # Structural eligibility снапшота — принадлежность canonical
            # buy-пулу; policy-фильтры запроса применяются на plan-пути.
            'buy_eligibility_scope': (
                'structural (canonical buy-pool membership, 030.6); '
                'policy-фильтры запроса и построчные reasons для ISIN вне '
                'среза — plan-путь (030.6/030.8)'
            ),
            'context_reuse': (
                'plan/compare принимают context_id и загружают core-owned '
                'PlanningContext (030.4); payload — копия для чтения'
            ),
            'privacy': (
                'приватный portfolio artifact: не отправлять содержимое в '
                'web/search/external LLM/API (правило 030.12)'
            ),
        }

    # ------------------------------------------------------------------
    # Проекции строк (явный whitelist canonical полей post-029)
    # ------------------------------------------------------------------

    @staticmethod
    def _common_analytics(row: Mapping[str, Any], planning_price: Mapping[str, Any]) -> Dict[str, Any]:
        """Аналитические поля, одинаковые для позиций и buy-пула (канонические
        raw-строки + effective_prices движка). Никаких пересчётов."""
        return {
            'isin': row.get('isin'),
            'name': row.get('name') or row.get('isin'),
            'ticker': row.get('ticker'),
            'issuer': row.get('issuer') or row.get('name') or row.get('isin'),
            'entity_type': row.get('entity_type'),
            'ku': bool(row.get('ku')),
            'risk_level': _opt_int(row.get('risk_level')),
            'list_level': _opt_int(row.get('list_level')),
            'credit_rating': row.get('credit_rating') or None,
            'rating_score': _opt_int(row.get('rating_score')),
            'maturity': _opt_iso_day(row.get('maturity_date')),
            'nominal': _opt_decimal_str(row.get('nominal')),
            'coupon_pct': _opt_decimal_str(row.get('coupon_rate_percent')),
            'coupon_freq': _opt_int(row.get('coupon_quantity_per_year')),
            'coupon_kind': 'float' if row.get('floating_coupon_flag') else 'fix',
            'amortization': bool(row.get('amortization_flag')),
            'perpetual': bool(row.get('perpetual_flag')),
            'market_price': _opt_decimal_str(row.get('market_price')),
            # Обогащённая движком YTM: NULL сохраняется + canonical reason.
            'ytm_pct': _opt_float(row.get('ytm_percent')),
            'ytm_reason': row.get('ytm_reason'),
            # Freshness metadata аналитического поля — есть post-029.
            'ytm_updated_at': _opt_iso_dt(row.get('ytm_updated_at')),
            # Цена расчёта + basis движка (030.2) — релей, не пересчёт.
            'planning_price': _opt_float(planning_price.get('price')),
            'planning_price_basis': planning_price.get('basis'),
        }

    def _universe_row_payload(
        self, row: Mapping[str, Any], planning_price: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Строка canonical buy-пула: structural eligibility — релей факта
        buy-path (строка прошла gates buy-выборки в SQL на frozen as_of,
        030.6), построчного пересчёта в snapshot нет."""
        payload = self._common_analytics(row, planning_price)
        payload.update({
            'held_share_pct': _opt_float(row.get('held_share_pct')),
            'structural_buy_eligible': True,
            'structural_buy_reasons': [],
        })
        return payload

    def _held_row_payload(
        self,
        row: Mapping[str, Any],
        planning_price: Mapping[str, Any],
        in_buy_pool: bool,
    ) -> Dict[str, Any]:
        """Позиция портфеля: «есть в портфеле» ≠ «можно покупать» —
        in_buy_universe/structural_buy_eligible разделяют presence и BUY
        eligibility; вне пула — канонический NOT_IN_BUY_POOL (030.7)."""
        payload = self._common_analytics(row, planning_price)
        payload.update({
            'qty': _opt_decimal_str(row.get('quantity')),
            # Каноническая оценка view на штуку (008) — источник строки.
            'price': _opt_decimal_str(row.get('price')),
            'value_rub': _opt_decimal_str(row.get('value_rub')),
            'valuation_source': row.get('valuation_source'),
            'offer': _opt_iso_day(row.get('offer_date')),
            'in_buy_universe': in_buy_pool,
            'structural_buy_eligible': in_buy_pool,
            'structural_buy_reasons': [] if in_buy_pool else [
                {'code': REASON_NOT_IN_BUY_POOL, 'actual': None, 'limit': None}
            ],
        })
        return payload
