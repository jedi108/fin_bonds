"""
Безопасное сравнение внешних подборок облигаций с портфелем (issue #5).

Advisory-only: сервис ничего не покупает, не создаёт заявок и НЕ выдаёт
вердикта покупки. Неизвестное или несопоставимое не превращается в сигнал:
строка получает eligibility `not_comparable`/`blocked` и явные blockers[]/
warnings[] с reason codes. Ключ сопоставления — ТОЛЬКО полный ISIN; имя/серия/
погашение используются лишь для проверки идентичности (IDENTITY_CONFLICT),
fuzzy-match не делает строку matched.

Версионированный контракт строки (CONTRACT_VERSION) — группы identity /
external / market / yield / credit / portfolio / decision. Чистые функции без
БД: данные каталога и PlanningContext приходят параметрами (use case
compare-external собирает их тем же PlanningContextBuilder, что rebalance-plan,
поэтому сравнение и план делят один context_fingerprint).

Принципы (решения ревью issue #5):
- FIX сравнивается по каноническим ytm_pct + ytm_to (ytm_horizon_date), не по
  купону/цене; FLOAT: ytm_pct=null, ytm_reason=floating_coupon, отдельные поля
  ставки, в общий FIX-рейтинг не попадает; устаревшая ставка — blocker.
- rating_status отделяет подтверждённый рейтинг (rating_history) от proxy
  (risk_level/list_level рейтинг НЕ подтверждают) и рейтинга другой серии
  эмитента (issuer_only).
- Экспозиция эмитента считается по агрегату позиций (один ISIN на двух счетах
  суммируется) до любой оценки строки; лимит эмитента — blocker.
"""
import csv
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.services.cashflow import ytm_horizon_date
from src.services.rebalance_engine import (
    CONSTRAINT_CREDIT_RATING_BELOW_MIN,
    CONSTRAINT_ISSUER_LIMIT,
    ISSUER_CONCENTRATION_LIMIT_PCT,
    _to_float,
)
from src.use_cases.bond_filters import MARKET_PRICE_FRESHNESS_HOURS
from src.utils import normalize_rating_code, rating_code_to_score

CONTRACT_VERSION = 1

# Статусы сопоставления внешней строки с каталогом (upper-case).
MATCH_MATCHED = 'MATCHED'
MATCH_UNMATCHED_EXTERNAL = 'UNMATCHED_EXTERNAL'
MATCH_IDENTITY_CONFLICT = 'IDENTITY_CONFLICT'
MATCH_STALE_CROSS_SOURCE = 'STALE_CROSS_SOURCE'

# Eligibility строки: НИКОГДА не содержит вердикта покупки.
ELIGIBILITY_COMPARABLE = 'comparable'
ELIGIBILITY_NOT_COMPARABLE = 'not_comparable'
ELIGIBILITY_BLOCKED = 'blocked'

# rating_status.
RATING_CONFIRMED = 'confirmed'
RATING_MISSING_STATUS = 'missing'
RATING_STALE_STATUS = 'stale'
RATING_INVALID = 'invalid'
RATING_ISSUER_ONLY = 'issuer_only'
RATING_PROXY_ONLY = 'proxy_only'
RATING_CONFLICTING = 'conflicting'

# Reason codes (минимальный набор issue #5 + служебные).
PRICE_STALE = 'PRICE_STALE'
FLOATER_RATE_STALE = 'FLOATER_RATE_STALE'
RATING_MISSING = 'RATING_MISSING'
RATING_STALE = 'RATING_STALE'
RATING_CONFLICT = 'RATING_CONFLICT'
YTM_MISSING = 'YTM_MISSING'
YTM_NOT_COMPARABLE = 'YTM_NOT_COMPARABLE'
EXTERNAL_DATE_UNKNOWN = 'EXTERNAL_DATE_UNKNOWN'
EXTERNAL_SOURCE_MISSING = 'EXTERNAL_SOURCE_MISSING'
EXTERNAL_VALUE_INVALID = 'EXTERNAL_VALUE_INVALID'
EXTERNAL_PRICE_UNKNOWN = 'EXTERNAL_PRICE_UNKNOWN'
ISIN_MISSING = 'ISIN_MISSING'
ISSUER_LINK_MISSING = 'ISSUER_LINK_MISSING'
IDENTITY_CONFLICT = 'IDENTITY_CONFLICT'
HELD_SAME_ISSUER = 'HELD_SAME_ISSUER'
DATA_GATE_BLOCKED = 'DATA_GATE_BLOCKED'
ISSUER_LIMIT = CONSTRAINT_ISSUER_LIMIT
CREDIT_RATING_BELOW_MIN = CONSTRAINT_CREDIT_RATING_BELOW_MIN

# Пороги (shortcut: константы модуля, в config не выносятся до первой нужды).
RATING_STALE_DAYS = 365           # рейтинг старше года не подтверждает
FLOATER_RATE_MAX_AGE_DAYS = 7     # ставка флоатера (RUONIA + спред) старше — stale
DEFAULT_MAX_SOURCE_SKEW_DAYS = 3  # расхождение дат внешнего источника и локальной цены
PRICE_TOLERANCE_PP = 1.0          # допуск цены внешнего источника, п.п. номинала
FLOATER_REFERENCE_RATE = 'RUONIA'
RATE_SCENARIO_SHIFTS_BPS = (-200, -100, 0, 100, 200)

_YTM_TO_VALUES = ('offer', 'maturity')


# ---------------------------------------------------------------------------
# Загрузка и нормализация внешнего списка
# ---------------------------------------------------------------------------


def load_external_rows(path: Path) -> List[Dict[str, Any]]:
    """Читает внешний список: JSON (массив или {"rows"|"bonds": [...]}) или CSV."""
    path = Path(path)
    if path.suffix.lower() == '.csv':
        with path.open(newline='', encoding='utf-8-sig') as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    data = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(data, dict):
        data = data.get('rows', data.get('bonds'))
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError(
            "Внешний список: ожидается JSON-массив объектов (или объект с ключом "
            "rows/bonds) либо CSV с заголовком"
        )
    return data


def _text(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _num(raw: Any, field: str, invalid: List[str]) -> Optional[float]:
    """Число из JSON/CSV; нечисловое/нефинитное → None + пометка (не 0)."""
    text = _text(raw)
    if text is None:
        return None
    try:
        value = float(text.replace('%', '').replace(',', '.'))
    except ValueError:
        value = float('nan')
    if not math.isfinite(value):
        invalid.append(field)
        return None
    return value


def _day(raw: Any, field: str, invalid: List[str]) -> Optional[date]:
    text = _text(raw)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        invalid.append(field)
        return None


def normalize_external_row(raw: Dict[str, Any], row_no: int) -> Dict[str, Any]:
    """Строка внешнего списка → типизированный dict; invalid — поля, не разобранные."""
    invalid: List[str] = []
    ytm_to = (_text(raw.get('ytm_to')) or '').lower() or None
    if ytm_to is not None and ytm_to not in _YTM_TO_VALUES:
        invalid.append('ytm_to')
        ytm_to = None
    isin = _text(raw.get('isin'))
    return {
        'row_no': row_no,
        'isin': isin.upper() if isin else None,
        'source': _text(raw.get('source')),
        'source_snapshot_id': _text(raw.get('source_snapshot_id')),
        'source_as_of': _day(raw.get('source_as_of'), 'source_as_of', invalid),
        'source_as_of_given': _text(raw.get('source_as_of')) is not None,
        'name': _text(raw.get('name')),
        'series': _text(raw.get('series')),
        'price': _num(raw.get('price'), 'price', invalid),
        'yield': _num(raw.get('yield'), 'yield', invalid),
        'ytm_to': ytm_to,
        'rating': normalize_rating_code(raw.get('rating')) or None,
        'maturity': _day(raw.get('maturity'), 'maturity', invalid),
        'qty': _num(raw.get('qty'), 'qty', invalid),
        'invalid': invalid,
    }


# ---------------------------------------------------------------------------
# Рейтинг
# ---------------------------------------------------------------------------


def _as_day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def classify_rating(
    cat: Dict[str, Any],
    external_rating: Optional[str],
    as_of_date: date,
    rating_scale: Optional[Dict[str, int]],
) -> Dict[str, Any]:
    """Группа credit строки: rating_status по правилам issue #5.

    Подтверждает рейтинг ТОЛЬКО валидный credit_rating выпуска из
    rating_history. risk_level/list_level — proxy и рейтинг не подтверждают;
    рейтинг другой серии эмитента — issuer_only; внешний рейтинг сам по себе
    статус не меняет (только conflicting при расхождении с локальным).
    """
    code = normalize_rating_code(cat.get('credit_rating')) or None
    rating_date = _as_day(cat.get('rating_date'))
    score = cat.get('rating_score')
    if score is None and code:
        score = rating_code_to_score(code, rating_scale)
    issuer_rating = normalize_rating_code(cat.get('issuer_rating')) or None
    has_proxy = cat.get('risk_level') is not None or cat.get('list_level') is not None

    if code:
        if score is None:
            status = RATING_INVALID
        elif rating_date is None or (as_of_date - rating_date).days > RATING_STALE_DAYS:
            status = RATING_STALE_STATUS
        elif external_rating and external_rating != code:
            status = RATING_CONFLICTING
        else:
            status = RATING_CONFIRMED
    elif issuer_rating:
        status = RATING_ISSUER_ONLY
    elif has_proxy:
        status = RATING_PROXY_ONLY
    else:
        status = RATING_MISSING_STATUS

    return {
        'credit_rating': code,
        'rating_date': rating_date.isoformat() if rating_date else None,
        'rating_status': status,
        'rating_score': score,
        # proxy-поля: НЕ рейтинг, подтверждать им нельзя
        'risk_level': cat.get('risk_level'),
        'list_level': cat.get('list_level'),
        'issuer_rating': issuer_rating,
        'external_rating': external_rating,
    }


# ---------------------------------------------------------------------------
# Идентичность
# ---------------------------------------------------------------------------


def _norm_text(value: Any) -> str:
    return re.sub(r'[\W_]+', '', str(value or '').casefold())


def identity_conflicts(ext: Dict[str, Any], cat: Dict[str, Any]) -> List[str]:
    """Расхождения имени/серии/погашения с каталогом по тому же ISIN.

    shortcut: сравнение по нормализованной подстроке (без fuzzy-скоринга);
    оно лишь ловит явное несовпадение, ISIN остаётся единственным ключом.
    """
    problems: List[str] = []
    name_n, ticker_n = _norm_text(cat.get('name')), _norm_text(cat.get('ticker'))
    ext_name, ext_series = _norm_text(ext.get('name')), _norm_text(ext.get('series'))
    if ext_name and not (
        ext_name == ticker_n or (name_n and (ext_name in name_n or name_n in ext_name))
    ):
        problems.append(f"название {ext['name']!r} не совпадает с каталогом {cat.get('name')!r}")
    if ext_series and not (ext_series == ticker_n or ext_series in name_n):
        problems.append(f"серия {ext['series']!r} не найдена в каталоге ({cat.get('name')!r})")
    cat_maturity = _as_day(cat.get('maturity_date'))
    if ext.get('maturity') and cat_maturity and ext['maturity'] != cat_maturity:
        problems.append(
            f"погашение {ext['maturity'].isoformat()} ≠ каталог {cat_maturity.isoformat()}"
        )
    return problems


# ---------------------------------------------------------------------------
# Портфель: агрегат позиций и экспозиция эмитента
# ---------------------------------------------------------------------------


def aggregate_held(held_rows) -> Tuple[Dict[str, Dict[str, Any]], Dict[Any, float], float]:
    """Позиции по ISIN (суммируются все счета) → (by_isin, issuer_totals, total).

    issuer_totals ключуется по company_id; позиции без эмитента-ссылки в
    totals не попадают (их экспозицию к эмитенту отнести нельзя).
    """
    by_isin: Dict[str, Dict[str, Any]] = {}
    for row in held_rows:
        item = by_isin.setdefault(
            row['isin'], {'value': 0.0, 'company_id': row.get('company_id')}
        )
        item['value'] += _to_float(row.get('value_rub')) or 0.0
    issuer_totals: Dict[Any, float] = {}
    for item in by_isin.values():
        if item['company_id'] is not None:
            issuer_totals[item['company_id']] = (
                issuer_totals.get(item['company_id'], 0.0) + item['value']
            )
    return by_isin, issuer_totals, sum(item['value'] for item in by_isin.values())


def _pct(part: float, total: float) -> Optional[float]:
    return round(part / total * 100, 2) if total > 0 else None


# ---------------------------------------------------------------------------
# Строка контракта
# ---------------------------------------------------------------------------


class _Issues:
    """Накопитель blockers[]/warnings[] строки (code уникален внутри списка)."""

    def __init__(self) -> None:
        self.blockers: List[Dict[str, str]] = []
        self.warnings: List[Dict[str, str]] = []

    def block(self, code: str, message: str) -> None:
        self.blockers.append({'code': code, 'message': message})

    def warn(self, code: str, message: str) -> None:
        self.warnings.append({'code': code, 'message': message})


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _yield_group(cat: Dict[str, Any], as_of_date: date) -> Dict[str, Any]:
    """Группа yield: FIX — канонический ytm_pct/ytm_to; FLOAT — ytm_pct=null + ставка."""
    floating = bool(cat.get('floating_coupon_flag'))
    _, ytm_to = ytm_horizon_date(cat.get('maturity_date'), cat.get('offer_date'), as_of_date)
    ytm = None if floating else cat.get('ytm_percent')
    ytm_updated = _as_day(cat.get('ytm_updated_at'))
    group: Dict[str, Any] = {
        'coupon_kind': 'float' if floating else 'fix',
        'ytm_pct': round(float(ytm), 2) if ytm is not None else None,
        'ytm_to': ytm_to if ytm is not None else None,
        'ytm_as_of': ytm_updated.isoformat() if ytm_updated else None,
        'ytm_reason': 'floating_coupon' if floating else (
            None if ytm is not None else (cat.get('ytm_reason') or 'not_applicable')
        ),
        'horizon_to': ytm_to,
    }
    if floating:
        rate = _to_float(cat.get('floater_rate'))
        rate_day = _as_day(cat.get('floater_rate_date'))
        spread = _to_float(cat.get('coupon_spread'))
        group.update({
            'current_coupon_rate': rate,
            'coupon_spread_bps': round(spread * 100, 1) if spread is not None else None,
            'reference_rate': FLOATER_REFERENCE_RATE,
            'rate_as_of': rate_day.isoformat() if rate_day else None,
            # сценарии — купонная ставка при сдвиге опорной, НЕ доходность
            'rate_scenarios': [
                {'delta_bps': shift, 'coupon_rate_pct': round(rate + shift / 100, 2)}
                for shift in RATE_SCENARIO_SHIFTS_BPS
            ] if rate is not None else [],
        })
    return group


def build_comparison_row(
    ext: Dict[str, Any],
    cat: Optional[Dict[str, Any]],
    context: Any,
    held: Tuple[Dict[str, Dict[str, Any]], Dict[Any, float], float],
    rating_scale: Optional[Dict[str, int]] = None,
    min_credit_rating: Optional[str] = None,
    max_skew_days: int = DEFAULT_MAX_SOURCE_SKEW_DAYS,
) -> Dict[str, Any]:
    """Одна строка контракта (CONTRACT_VERSION) для внешней строки ext.

    cat — строка каталога (get_comparison_catalog_rows + enrich_with_ytm) или
    None (ISIN не найден); context — PlanningContext (as_of, as_of_date,
    data_gate). Никогда не выдаёт вердикта покупки.
    """
    issues = _Issues()
    as_of_date: date = context.as_of_date
    by_isin, issuer_totals, total_value = held
    min_score = (
        rating_code_to_score(min_credit_rating, rating_scale) if min_credit_rating else None
    )

    # --- внешние данные: обязательные поля и разбор ---------------------
    for field in ext['invalid']:
        issues.warn(EXTERNAL_VALUE_INVALID, f"внешнее поле {field!r} не разобрано — игнорируется")
    if not ext['source']:
        issues.block(EXTERNAL_SOURCE_MISSING, "не указан source внешнего списка")
    src_date = ext['source_as_of']
    if src_date is None or src_date > as_of_date + timedelta(days=1):
        issues.block(
            EXTERNAL_DATE_UNKNOWN,
            "source_as_of не указан/не разобран" if src_date is None
            else f"source_as_of {src_date.isoformat()} в будущем",
        )
    gate = context.data_gate
    if gate.criticals or not gate.is_fresh:
        issues.block(
            DATA_GATE_BLOCKED,
            f"data gate {gate.gate_status} ({gate.status}): данные не свежие/критичные",
        )

    # --- сопоставление по ISIN ------------------------------------------
    match_status = MATCH_MATCHED
    if ext['isin'] is None:
        match_status = MATCH_UNMATCHED_EXTERNAL
        issues.block(ISIN_MISSING, "в строке нет ISIN — ключ сопоставления только полный ISIN")
    elif cat is None:
        match_status = MATCH_UNMATCHED_EXTERNAL
        issues.block(MATCH_UNMATCHED_EXTERNAL, f"ISIN {ext['isin']} отсутствует в каталоге")
    else:
        problems = identity_conflicts(ext, cat)
        if problems:
            match_status = MATCH_IDENTITY_CONFLICT
            issues.block(IDENTITY_CONFLICT, "; ".join(problems))

    external = {
        'row_no': ext['row_no'], 'source': ext['source'],
        'source_snapshot_id': ext['source_snapshot_id'],
        'source_as_of': src_date.isoformat() if src_date else None,
        'price': ext['price'], 'yield': ext['yield'], 'ytm_to': ext['ytm_to'],
        'rating': ext['rating'],
        'maturity': ext['maturity'].isoformat() if ext['maturity'] else None,
    }
    identity = {
        'isin': ext['isin'],
        'series': ext['series'] or (cat or {}).get('ticker'),
        'name': (cat or {}).get('name'),
        'ticker': (cat or {}).get('ticker'),
        'issuer_id': (cat or {}).get('company_id'),
        'issuer': (cat or {}).get('issuer'),
        'external_name': ext['name'],
        'match_status': match_status,
    }

    if cat is None:  # нечего сравнивать: остальные группы — явные null
        no_yield = {
            'coupon_kind': None, 'ytm_pct': None, 'ytm_to': None, 'ytm_as_of': None,
            'ytm_reason': None, 'horizon_to': None, 'external_yield_pct': ext['yield'],
            'external_ytm_to': ext['ytm_to'], 'ytm_diff_pp': None,
            'comparison': ELIGIBILITY_NOT_COMPARABLE,
        }
        no_market = {'price': None, 'price_as_of': None, 'price_source': None}
        return _finish_row(
            identity, external, no_market, no_yield,
            classify_rating({}, ext['rating'], as_of_date, rating_scale),
            _portfolio_group(None, None, None, None, by_isin, issuer_totals, total_value),
            issues,
        )

    # --- market ----------------------------------------------------------
    price = _to_float(cat.get('market_price'))
    price_as_of = _aware(cat.get('market_price_updated_at'))
    market = {
        'price': price,
        'price_as_of': price_as_of.isoformat() if price_as_of else None,
        'price_source': 'bonds_catalog.market_price',
    }
    age_limit = timedelta(hours=MARKET_PRICE_FRESHNESS_HOURS)
    if price is None or price <= 0 or price_as_of is None or context.as_of - price_as_of > age_limit:
        issues.block(PRICE_STALE, "рыночная цена каталога отсутствует или старше "
                                  f"{MARKET_PRICE_FRESHNESS_HOURS} ч")

    # --- cross-source даты -----------------------------------------------
    if (match_status == MATCH_MATCHED and src_date is not None and price_as_of is not None
            and abs((src_date - price_as_of.date()).days) > max_skew_days):
        match_status = MATCH_STALE_CROSS_SOURCE
        identity['match_status'] = match_status

    # --- yield ------------------------------------------------------------
    ygroup = _yield_group(cat, as_of_date)
    ygroup['external_yield_pct'] = ext['yield']
    ygroup['external_ytm_to'] = ext['ytm_to']
    ygroup['ytm_diff_pp'] = None
    comparable = False
    if ygroup['coupon_kind'] == 'float':
        if ext['yield'] is not None:
            issues.warn(YTM_NOT_COMPARABLE,
                        "флоатер: внешний yield не сопоставляется с YTM (ytm_pct=null)")
        rate_day = _as_day(cat.get('floater_rate_date'))
        if rate_day is None or (as_of_date - rate_day).days > FLOATER_RATE_MAX_AGE_DAYS:
            issues.block(FLOATER_RATE_STALE,
                         "ставка флоатера отсутствует или старше "
                         f"{FLOATER_RATE_MAX_AGE_DAYS} дн.")
    elif ygroup['ytm_pct'] is None:
        issues.block(YTM_MISSING, f"локальная YTM не определена ({ygroup['ytm_reason']})")
    elif ext['yield'] is None:
        issues.warn(YTM_MISSING, "во внешней строке нет yield — сравнивать нечего")
    else:
        why: List[str] = []
        if ext['ytm_to'] is None:
            why.append("не указан горизонт внешнего ytm_to")
        elif ext['ytm_to'] != ygroup['ytm_to']:
            why.append(f"горизонт YTM различается: внешний {ext['ytm_to']}, локальный {ygroup['ytm_to']}")
        if match_status == MATCH_STALE_CROSS_SOURCE:
            why.append(f"даты источников расходятся более чем на {max_skew_days} дн.")
        nominal = _to_float(cat.get('nominal'))
        if ext['price'] is None:
            issues.warn(EXTERNAL_PRICE_UNKNOWN, "нет внешней цены — база цены внешней YTM не проверена")
        elif price and nominal:
            local_pct = price / nominal * 100
            if abs(ext['price'] - local_pct) > PRICE_TOLERANCE_PP:
                why.append(f"цена расчёта различается: внешняя {ext['price']}, локальная {local_pct:.2f} (% номинала)")
        if why:
            issues.warn(YTM_NOT_COMPARABLE, "; ".join(why))
        else:
            comparable = True
            ygroup['ytm_diff_pp'] = round(ext['yield'] - ygroup['ytm_pct'], 2)
    ygroup['comparison'] = ELIGIBILITY_COMPARABLE if comparable else ELIGIBILITY_NOT_COMPARABLE

    # --- credit -----------------------------------------------------------
    credit = classify_rating(cat, ext['rating'], as_of_date, rating_scale)
    _rating_issues(credit, min_credit_rating, min_score, issues)

    # --- portfolio ----------------------------------------------------------
    planned = (ext['qty'] * price) if (ext['qty'] is not None and price) else None
    portfolio = _portfolio_group(
        ext['isin'], cat.get('company_id'), ext['qty'], planned, by_isin, issuer_totals, total_value
    )
    if cat.get('company_id') is None:
        issues.block(ISSUER_LINK_MISSING, "выпуск не привязан к эмитенту — экспозиция не считается")
    if portfolio['same_issuer_existing_exposure']:
        issues.warn(HELD_SAME_ISSUER,
                    "same_issuer_existing_exposure: у эмитента уже есть позиции "
                    f"({portfolio['issuer_share_before_pct']}% портфеля) — замена серии не диверсификация")
    after = portfolio['issuer_share_after_pct']
    if after is not None and after > ISSUER_CONCENTRATION_LIMIT_PCT:
        issues.block(ISSUER_LIMIT, f"доля эмитента после {after}% > лимита {ISSUER_CONCENTRATION_LIMIT_PCT}%")

    return _finish_row(identity, external, market, ygroup, credit, portfolio, issues)


def _rating_issues(credit, min_credit_rating, min_score, issues: _Issues) -> None:
    """Статус рейтинга → warning; при заданном пороге → blocker (разные причины)."""
    status = credit['rating_status']
    if status == RATING_CONFIRMED:
        if min_score is not None and (credit['rating_score'] is None or credit['rating_score'] < min_score):
            issues.block(CREDIT_RATING_BELOW_MIN,
                         f"рейтинг {credit['credit_rating']} ниже порога {min_credit_rating}")
        return
    code = {RATING_STALE_STATUS: RATING_STALE, RATING_CONFLICTING: RATING_CONFLICT}.get(
        status, RATING_MISSING)
    message = f"rating_status={status}: рейтинг выпуска не подтверждён"
    if min_credit_rating:  # строгий порог: неподтверждённый рейтинг порог не проходит
        issues.block(code, f"{message}; порог {min_credit_rating} не подтверждён")
    else:
        issues.warn(code, message)


def _portfolio_group(isin, company_id, qty, planned_value, by_isin, issuer_totals, total) -> Dict[str, Any]:
    held_value = by_isin.get(isin, {}).get('value', 0.0) if isin else None
    issuer_value = issuer_totals.get(company_id, 0.0) if company_id is not None else None
    other_held = sorted(
        i for i, item in by_isin.items()
        if company_id is not None and item['company_id'] == company_id and i != isin
    )
    before = _pct(issuer_value, total) if issuer_value is not None else None
    if issuer_value is None:
        after = None
    elif qty is None:
        after = before  # гипотетической покупки нет — экспозиция не меняется
    else:
        after = _pct(issuer_value + planned_value, total + planned_value) if planned_value is not None else None
    return {
        'held_value_rub': round(held_value, 2) if held_value is not None else None,
        'issuer_value_rub': round(issuer_value, 2) if issuer_value is not None else None,
        'issuer_share_before_pct': before,
        'issuer_share_after_pct': after,
        'planned_qty': qty,
        'planned_value_rub': round(planned_value, 2) if planned_value is not None else None,
        'same_issuer_existing_exposure': bool(issuer_value and issuer_value > 0),
        'same_issuer_held_isins': other_held,
    }


def _finish_row(identity, external, market, ygroup, credit, portfolio, issues: _Issues) -> Dict[str, Any]:
    if issues.blockers:
        eligibility = ELIGIBILITY_BLOCKED
    elif ygroup['comparison'] == ELIGIBILITY_COMPARABLE:
        eligibility = ELIGIBILITY_COMPARABLE
    else:
        eligibility = ELIGIBILITY_NOT_COMPARABLE
    return {
        'contract_version': CONTRACT_VERSION,
        'identity': identity,
        'external': external,
        'market': market,
        'yield': ygroup,
        'credit': credit,
        'portfolio': portfolio,
        'decision': {
            'eligibility': eligibility,
            # blocked-строки (нет ISIN, конфликт идентичности, ...) в intent/plan не идут
            'plan_input_allowed': eligibility != ELIGIBILITY_BLOCKED,
            'blockers': issues.blockers,
            'warnings': issues.warnings,
        },
    }


# ---------------------------------------------------------------------------
# Отчёт целиком
# ---------------------------------------------------------------------------


def build_comparison_report(
    raw_rows: List[Dict[str, Any]],
    catalog_by_isin: Dict[str, Dict[str, Any]],
    context: Any,
    rating_scale: Optional[Dict[str, int]] = None,
    min_credit_rating: Optional[str] = None,
    max_skew_days: int = DEFAULT_MAX_SOURCE_SKEW_DAYS,
) -> Dict[str, Any]:
    """Отчёт compare-external: строки контракта + раздельные FIX/FLOAT списки +
    матрица эмитентов + идентификация среза (context_fingerprint плана)."""
    held = aggregate_held(context.held_rows)
    exts = [normalize_external_row(raw, i) for i, raw in enumerate(raw_rows, start=1)]
    rows = [
        build_comparison_row(
            ext, catalog_by_isin.get(ext['isin']) if ext['isin'] else None, context, held,
            rating_scale, min_credit_rating, max_skew_days,
        )
        for ext in exts
    ]

    live = [r for r in rows if r['decision']['eligibility'] != ELIGIBILITY_BLOCKED]
    fix = sorted(
        (r for r in live if r['yield']['coupon_kind'] == 'fix' and r['yield']['ytm_pct'] is not None),
        key=lambda r: (-r['yield']['ytm_pct'], r['identity']['isin']),
    )
    rankings = {
        # FIX и FLOAT никогда не ранжируются общим YTM-списком
        'fix_ytm': [
            {'rank': n, 'isin': r['identity']['isin'], 'ytm_pct': r['yield']['ytm_pct'],
             'ytm_to': r['yield']['ytm_to'], 'eligibility': r['decision']['eligibility']}
            for n, r in enumerate(fix, start=1)
        ],
        'float': [
            {'isin': r['identity']['isin'], 'current_coupon_rate': r['yield']['current_coupon_rate'],
             'rate_as_of': r['yield']['rate_as_of'], 'eligibility': r['decision']['eligibility']}
            for r in live if r['yield']['coupon_kind'] == 'float'
        ],
    }

    gate = context.data_gate
    return {
        'contract_version': CONTRACT_VERSION,
        'advisory_only': True,
        'context_fingerprint': context.context_fingerprint,
        'as_of': context.as_of.isoformat(),
        'data_gate': {
            'status': gate.status, 'gate_status': gate.gate_status, 'is_fresh': gate.is_fresh,
            'criticals': list(gate.criticals), 'warnings': list(gate.warnings),
        },
        'min_credit_rating': min_credit_rating,
        'summary': {
            'rows': len(rows),
            **{
                status: sum(1 for r in rows if r['decision']['eligibility'] == status)
                for status in (ELIGIBILITY_COMPARABLE, ELIGIBILITY_NOT_COMPARABLE, ELIGIBILITY_BLOCKED)
            },
        },
        'rows': rows,
        'rankings': rankings,
        'issuer_matrix': _issuer_matrix(rows, held),
    }


def _issuer_matrix(rows: List[Dict[str, Any]], held) -> List[Dict[str, Any]]:
    """Матрица эмитентов: серии портфеля vs серии внешнего списка + экспозиция
    до/после (если купить все запланированные qty эмитента)."""
    by_isin, issuer_totals, total = held
    groups: Dict[Any, Dict[str, Any]] = {}
    for r in rows:
        company_id = r['identity']['issuer_id']
        if company_id is None or r['identity']['match_status'] == MATCH_UNMATCHED_EXTERNAL:
            continue
        group = groups.setdefault(company_id, {
            'issuer_id': company_id, 'issuer': r['identity']['issuer'],
            'external_isins': [], 'planned_value_rub': 0.0,
        })
        if r['identity']['isin'] not in group['external_isins']:
            group['external_isins'].append(r['identity']['isin'])
            group['planned_value_rub'] += r['portfolio']['planned_value_rub'] or 0.0
    matrix = []
    for company_id, group in sorted(groups.items(), key=lambda kv: str(kv[1]['issuer'])):
        value = issuer_totals.get(company_id, 0.0)
        planned = group.pop('planned_value_rub')
        after = _pct(value + planned, total + planned)
        matrix.append({
            **group,
            'held_isins': sorted(i for i, it in by_isin.items() if it['company_id'] == company_id),
            'exposure_before_pct': _pct(value, total),
            'exposure_after_pct': after,
            'limit_pct': ISSUER_CONCENTRATION_LIMIT_PCT,
            'limit_exceeded': after is not None and after > ISSUER_CONCENTRATION_LIMIT_PCT,
        })
    return matrix
