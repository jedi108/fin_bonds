"""
Тесты 005.3 — calculate-ytm: машинный формат и фильтры.

Покрывают:
- `--format json`: канонический JSON (schema_version=1, согласован с
  rebalance-report), stdout — чистый JSON, логи — только в stderr (P-F);
- `--coupon-freq-min` / `--coupon-freq-max` / `--coupon-freq-in` (P-C, 023):
  диапазон «от квартальных до ежемесячных», квартальные максимум,
  список частот, NULL-частота не проходит, --limit считается по итоговой
  выдаче (расширенный пул);
- `--exclude-sovereign` (P-G): ОФЗ по связке companies.entity_type='sovereign',
  фолбэк на имя «ОФЗ…», евро-РФ;
- воронку отсечений R9 (кейс ППК РЭО 01: причина отсечки видна в excluded);
- R6: в дефолтном режиме корневой уровень логирования WARNING+ (INFO — под -v);
- сохранение текстового режима (ASCII-таблица по умолчанию, колонка «Частота»).

Все данные — синтетические фикстуры на тестовой БД (POSTGRES_DSN_TEST),
реальные составы портфеля не используются.
"""
import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.use_cases.calculate_ytm import (
    _BOND_JSON_KEYS,
    _BOND_JSON_KEYS_PORTFOLIO,
    _EXCLUDED_KEYS,
    CalculateYtmUseCase,
    SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# Фикстуры (синтетические данные)
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _restore_root_log_level():
    """execute() меняет корневой уровень логирования (R6) — восстанавливаем."""
    root = logging.getLogger()
    saved_level, saved_handlers_level = root.level, [
        h.level for h in root.handlers
    ]
    yield
    root.setLevel(saved_level)
    for handler, level in zip(root.handlers, saved_handlers_level):
        handler.setLevel(level)


def _insert_company(db, name: str, entity_type: str = 'company') -> int:
    cur = db._cursor()
    cur.execute(
        "INSERT INTO companies (name, entity_type) VALUES (%s, %s) RETURNING id",
        (name, entity_type),
    )
    return cur.fetchone()['id']


def _insert_bond(db, **kwargs):
    """Облигация каталога с дефолтами, видимыми buy-выборкой движка."""
    cur = db._cursor()
    defaults = {
        'isin': 'RU000A0TEST1',
        'ticker': 'TEST1',
        'name': 'Тест Бонд 1',
        'currency': 'rub',
        'nominal': Decimal('1000'),
        'maturity_date': date.today() + timedelta(days=730),
        'coupon_quantity_per_year': 2,
        'coupon_rate_percent': Decimal('15.0'),
        'risk_level': 1,
        'list_level': 1,
        'is_trade_available': True,
        'is_for_qualified_investors': False,
        'issue_size': 1000000,
        'market_price': Decimal('950.0'),
        'market_price_updated_at': _NOW,
        'floating_coupon_flag': False,
        'perpetual_flag': False,
        'amortization_flag': False,
        'company_id': None,
        'ytm': None,
        'ytm_updated_at': None,
    }
    defaults.update(kwargs)
    fields = list(defaults.keys())
    placeholders = ', '.join(['%s'] * len(fields))
    col_names = ', '.join(fields)
    cur.execute(
        f"INSERT INTO bonds_catalog ({col_names}) VALUES ({placeholders})",
        [defaults[f] for f in fields],
    )
    db.conn.commit()  # видимость в subprocess-CLI (отдельное соединение)


def _insert_position(db, isin: str, quantity: float, price: float,
                     broker: str = 'TBank', account: str = 't1'):
    cur = db._cursor()
    cur.execute(
        "INSERT INTO portfolio_positions (isin, ticker, name, quantity, average_price, "
        "current_price, current_value, broker_name, account_id, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (isin, 'TICK', 'Позиция', quantity, price, price, quantity * price,
         broker, account, _NOW),
    )
    db.conn.commit()  # видимость в subprocess-CLI (отдельное соединение)


def _set_stored_ytm(db, isin: str, ytm_percent: str):
    """Каноническая YTM, записанная в каталог напрямую (детерминированный порядок)."""
    with db.conn:
        cursor = db._cursor()
        cursor.execute(
            "UPDATE bonds_catalog SET ytm = %s, ytm_null_reason = NULL, "
            "ytm_updated_at = CURRENT_TIMESTAMP WHERE isin = %s",
            (Decimal(ytm_percent), isin),
        )


def _parse(cli: list) -> argparse.Namespace:
    """Namespace команды как из CLI (все флаги 005.3 присутствуют)."""
    parser = argparse.ArgumentParser()
    CalculateYtmUseCase.setup_parser(parser)
    return parser.parse_args(cli)


def _run_json(use_case, cli: list, capsys) -> dict:
    use_case.execute(_parse(cli))
    captured = capsys.readouterr()
    # stdout — чистый JSON: парсится целиком, логов внутри нет (P-F)
    return json.loads(captured.out)


# ---------------------------------------------------------------------------
# Парсер и валидация
# ---------------------------------------------------------------------------

class TestParser:
    def test_defaults_keep_text_mode_and_no_filters(self):
        args = _parse(['--mode', 'buy'])
        assert args.format == 'text'
        assert args.coupon_freq_min is None
        assert args.coupon_freq_max is None
        assert args.coupon_freq_in is None
        assert args.exclude_sovereign is False

    def test_new_flags_parse(self):
        args = _parse(['--mode', 'buy', '--format', 'json',
                       '--coupon-freq-max', '4', '--exclude-sovereign'])
        assert args.format == 'json'
        assert args.coupon_freq_max == 4
        assert args.exclude_sovereign is True

    def test_coupon_freq_in_parses_list(self):
        assert _parse(['--mode', 'buy', '--coupon-freq-in', '2,4']).coupon_freq_in == [2, 4]
        assert _parse(['--mode', 'buy', '--coupon-freq-in', '12']).coupon_freq_in == [12]

    def test_coupon_freq_in_bad_value_rejected(self):
        with pytest.raises(SystemExit):
            _parse(['--mode', 'buy', '--coupon-freq-in', '2,квартал'])

    def test_validate_args_conflict_and_zero(self):
        use_case = CalculateYtmUseCase.__new__(CalculateYtmUseCase)
        args = _parse(['--mode', 'buy', '--coupon-freq-max', '4',
                       '--coupon-freq-in', '2,4'])
        with pytest.raises(ValueError):
            use_case._validate_args(args)
        args_zero = _parse(['--mode', 'buy', '--coupon-freq-max', '0'])
        with pytest.raises(ValueError):
            use_case._validate_args(args_zero)


# ---------------------------------------------------------------------------
# JSON-контракт (--format json, P-F)
# ---------------------------------------------------------------------------

def test_json_contract_buy_mode(db, capsys):
    """JSON-режим buy: канонический контракт, согласованный с rebalance-report."""
    _insert_bond(db, isin='RU000A0JFIX1', ticker='FIX1', name='Тест Фикс 001',
                 coupon_quantity_per_year=2, coupon_rate_percent=Decimal('15.0'))
    _insert_bond(db, isin='RU000A0JFLT2', ticker='FLT2', name='Тест Флоат 002',
                 coupon_quantity_per_year=4, coupon_rate_percent=Decimal('12.0'),
                 floating_coupon_flag=True)

    use_case = CalculateYtmUseCase(db=db)
    data = _run_json(use_case, ['--mode', 'buy', '--format', 'json'], capsys)

    assert data['schema_version'] == SCHEMA_VERSION == 1
    assert data['meta']['mode'] == 'buy'
    assert set(data['filters']) == {
        'mode', 'min_ytm_pct', 'max_risk', 'max_listlevel', 'max_ytm_pct',
        'min_maturity', 'max_maturity', 'no_amortization', 'monthly_coupons',
        'fixed_coupon', 'floating_coupon', 'coupon_freq_max', 'coupon_freq_min',
        'coupon_freq_in', 'exclude_sovereign', 'include_held', 'limit',
    }
    assert data['filters']['coupon_freq_max'] is None
    assert data['excluded'] == {key: 0 for key in _EXCLUDED_KEYS}
    assert data['count'] == 2
    assert data['candidates_total'] == 2

    rows = {row['isin']: row for row in data['bonds']}
    fix = rows['RU000A0JFIX1']
    assert set(fix) == set(_BOND_JSON_KEYS)
    assert fix['ytm_reason'] is None
    assert fix['ytm_pct'] is not None and 8.0 < fix['ytm_pct'] < 20.0
    assert fix['coupon_pct'] == 15.0
    assert fix['freq'] == 2
    assert fix['coupon_kind'] == 'fix'
    assert fix['amort'] is False and fix['ku'] is False
    assert fix['issuer'] == 'Тест Фикс 001'  # фолбэк: нет связки с эмитентом
    assert fix['maturity'] == (date.today() + timedelta(days=730)).isoformat()

    flt = rows['RU000A0JFLT2']
    assert flt['ytm_pct'] is None
    assert flt['ytm_reason'] == 'floating_coupon'
    assert flt['coupon_kind'] == 'float'
    assert flt['freq'] == 4


def test_json_mode_error_keeps_stdout_clean(db, capsys):
    """Ошибка в json-режиме уходит в исключение: stdout без мусора."""
    use_case = CalculateYtmUseCase(db=db)
    with pytest.raises(ValueError):
        use_case.execute(_parse(['--mode', 'buy', '--format', 'json',
                                 '--coupon-freq-max', '4',
                                 '--coupon-freq-in', '2,4']))
    captured = capsys.readouterr()
    assert captured.out == ''


# ---------------------------------------------------------------------------
# Фильтр частоты купонов (--coupon-freq-max / --coupon-freq-in, P-C)
# ---------------------------------------------------------------------------

def _seed_three_freqs(db):
    """Ежемесячная (12), квартальная (4) и полугодовая (2) бумаги."""
    _insert_bond(db, isin='RU000A0FRQ12', ticker='FRQ12', name='Ежемесячный Тест',
                 coupon_quantity_per_year=12, coupon_rate_percent=Decimal('20.0'))
    _insert_bond(db, isin='RU000A0FRQ04', ticker='FRQ04', name='Квартальный Тест',
                 coupon_quantity_per_year=4, coupon_rate_percent=Decimal('15.0'))
    _insert_bond(db, isin='RU000A0FRQ02', ticker='FRQ02', name='Полугодовой Тест',
                 coupon_quantity_per_year=2, coupon_rate_percent=Decimal('14.0'))


def test_coupon_freq_max_selects_quarterly_and_rarer(db):
    """Кейс сессии «квартальные максимум»: freq <= 4, ежемесячные отсечены."""
    _seed_three_freqs(db)
    use_case = CalculateYtmUseCase(db=db)
    args = _parse(['--mode', 'buy', '--coupon-freq-max', '4'])
    isins = [b['isin'] for b in use_case._get_bonds_for_buying(args)]
    assert isins == ['RU000A0FRQ04', 'RU000A0FRQ02']  # порядок SQL-сортировки


def test_coupon_freq_in_selects_exact_frequencies(db):
    _seed_three_freqs(db)
    use_case = CalculateYtmUseCase(db=db)
    args = _parse(['--mode', 'buy', '--coupon-freq-in', '2,4'])
    isins = {b['isin'] for b in use_case._get_bonds_for_buying(args)}
    assert isins == {'RU000A0FRQ04', 'RU000A0FRQ02'}


def test_coupon_freq_null_frequency_does_not_pass(db):
    """NULL-частота фильтру не проходит (конвенция rebalance-report)."""
    _insert_bond(db, isin='RU000A0FRQNN', ticker='FRQNN', name='Без Частоты Тест',
                 coupon_quantity_per_year=None)
    use_case = CalculateYtmUseCase(db=db)

    args_no_filter = _parse(['--mode', 'buy'])
    assert 'RU000A0FRQNN' in [
        b['isin'] for b in use_case._get_bonds_for_buying(args_no_filter)
    ]

    args_freq = _parse(['--mode', 'buy', '--coupon-freq-max', '12'])
    assert 'RU000A0FRQNN' not in [
        b['isin'] for b in use_case._get_bonds_for_buying(args_freq)
    ]


# ---------------------------------------------------------------------------
# Диапазон частоты купонов (--coupon-freq-min/--coupon-freq-max, 023/P-C)
# ---------------------------------------------------------------------------

def test_coupon_freq_range_4_to_12(db):
    """023: «от квартальных до ежемесячных» = 4 <= freq <= 12, не freq <= 4."""
    _seed_three_freqs(db)
    use_case = CalculateYtmUseCase(db=db)
    args = _parse(['--mode', 'buy', '--coupon-freq-min', '4',
                   '--coupon-freq-max', '12'])
    isins = {b['isin'] for b in use_case._get_bonds_for_buying(args)}
    assert isins == {'RU000A0FRQ04', 'RU000A0FRQ12'}


def test_coupon_freq_min_without_max(db):
    _seed_three_freqs(db)
    use_case = CalculateYtmUseCase(db=db)
    args = _parse(['--mode', 'buy', '--coupon-freq-min', '4'])
    isins = {b['isin'] for b in use_case._get_bonds_for_buying(args)}
    assert isins == {'RU000A0FRQ04', 'RU000A0FRQ12'}


def test_coupon_freq_null_frequency_does_not_pass_strict_range(db):
    """NULL/0-частота строгий диапазон не проходит."""
    _insert_bond(db, isin='RU000A0FRQNN', ticker='FRQNN', name='Без Частоты Тест',
                 coupon_quantity_per_year=None)
    use_case = CalculateYtmUseCase(db=db)
    args = _parse(['--mode', 'buy', '--coupon-freq-min', '4',
                   '--coupon-freq-max', '12'])
    assert 'RU000A0FRQNN' not in [
        b['isin'] for b in use_case._get_bonds_for_buying(args)
    ]


def test_coupon_freq_min_conflicts_and_bounds(db):
    use_case = CalculateYtmUseCase.__new__(CalculateYtmUseCase)
    with pytest.raises(ValueError):
        use_case._validate_args(_parse([
            '--mode', 'buy', '--coupon-freq-min', '4', '--coupon-freq-in', '2,4']))
    with pytest.raises(ValueError):
        use_case._validate_args(_parse(['--mode', 'buy', '--coupon-freq-min', '0']))
    with pytest.raises(ValueError):
        use_case._validate_args(_parse([
            '--mode', 'buy', '--coupon-freq-min', '12', '--coupon-freq-max', '4']))


def test_old_style_namespace_without_new_flags_still_works(db):
    """Совместимость: Namespace без новых атрибутов — прежнее поведение."""
    _seed_three_freqs(db)
    use_case = CalculateYtmUseCase(db=db)
    args = argparse.Namespace(
        mode='buy', min_ytm=None, max_risk=5, min_maturity=None, max_maturity=None,
        no_amortization=False, monthly_coupons=False, fixed_coupon=False,
        floating_coupon=False, limit=10, max_listlevel=2, max_ytm=35.0,
    )
    assert len(use_case._get_bonds_for_buying(args)) == 3


def test_limit_applies_to_final_result_with_widened_pool(db):
    """--limit считается по итоговой выдаче: без фильтра топ-2 — ежемесячные,
    с --coupon-freq-max 4 топ-2 заполняют квартальные (пул расширен)."""
    _insert_bond(db, isin='RU000A0WDM01', ticker='WDM01', name='Месячный Топ 1',
                 coupon_quantity_per_year=12, coupon_rate_percent=Decimal('20.0'))
    _insert_bond(db, isin='RU000A0WDM02', ticker='WDM02', name='Месячный Топ 2',
                 coupon_quantity_per_year=12, coupon_rate_percent=Decimal('19.0'))
    _insert_bond(db, isin='RU000A0WDQ01', ticker='WDQ01', name='Квартальный 1',
                 coupon_quantity_per_year=4, coupon_rate_percent=Decimal('15.0'))
    _insert_bond(db, isin='RU000A0WDQ02', ticker='WDQ02', name='Квартальный 2',
                 coupon_quantity_per_year=4, coupon_rate_percent=Decimal('14.0'))
    _set_stored_ytm(db, 'RU000A0WDM01', '30')
    _set_stored_ytm(db, 'RU000A0WDM02', '25')
    _set_stored_ytm(db, 'RU000A0WDQ01', '20')
    _set_stored_ytm(db, 'RU000A0WDQ02', '15')

    use_case = CalculateYtmUseCase(db=db)
    args = _parse(['--mode', 'buy', '--coupon-freq-max', '4', '--limit', '2'])
    bonds = use_case._get_bonds_for_buying(args)
    assert [b['isin'] for b in bonds] == ['RU000A0WDQ01', 'RU000A0WDQ02']

    # без фильтра частоты лимит работает как раньше — топ по YTM
    args_plain = _parse(['--mode', 'buy', '--limit', '2'])
    assert [b['isin'] for b in use_case._get_bonds_for_buying(args_plain)] == [
        'RU000A0WDM01', 'RU000A0WDM02'
    ]


# ---------------------------------------------------------------------------
# --exclude-sovereign (P-G)
# ---------------------------------------------------------------------------

def test_exclude_sovereign_removes_ofz_and_russia_eurobonds(db):
    """ОФЗ (по связке entity_type=sovereign), ОФЗ по фолбэку имени и
    Russia-евробонды исключены; корпоративная бумага остаётся."""
    minfin_id = _insert_company(db, 'Минфин Тест', entity_type='sovereign')
    _insert_bond(db, isin='RU000A0SOV01', ticker='SOV01', name='ОФЗ 26238 Тест',
                 coupon_quantity_per_year=2, company_id=minfin_id)
    _insert_bond(db, isin='RU000A0SOV02', ticker='SOV02', name='ОФЗ-ИН 52004 Тест',
                 coupon_quantity_per_year=2, company_id=None)  # фолбэк на имя
    _insert_bond(db, isin='RU000A0SOV03', ticker='SOV03', name='Russia 2042 Тест',
                 coupon_quantity_per_year=2, company_id=minfin_id)
    _insert_bond(db, isin='RU000A0COR01', ticker='COR01', name='Корпоративный Тест',
                 coupon_quantity_per_year=2)

    use_case = CalculateYtmUseCase(db=db)

    args_all = _parse(['--mode', 'buy'])
    assert {b['isin'] for b in use_case._get_bonds_for_buying(args_all)} == {
        'RU000A0SOV01', 'RU000A0SOV02', 'RU000A0SOV03', 'RU000A0COR01'
    }

    args_no_sov = _parse(['--mode', 'buy', '--exclude-sovereign'])
    bonds = use_case._get_bonds_for_buying(args_no_sov)
    assert [b['isin'] for b in bonds] == ['RU000A0COR01']


def test_exclude_sovereign_funnel_counts(db, capsys):
    """Воронка R9: --exclude-sovereign виден в excluded.sovereign."""
    minfin_id = _insert_company(db, 'Минфин Тест', entity_type='sovereign')
    _insert_bond(db, isin='RU000A0SOV11', ticker='SOV11', name='ОФЗ 26212 Тест',
                 company_id=minfin_id)
    _insert_bond(db, isin='RU000A0COR11', ticker='COR11', name='Корпоративный Тест 11')
    use_case = CalculateYtmUseCase(db=db)
    data = _run_json(use_case, ['--mode', 'buy', '--format', 'json',
                                '--exclude-sovereign'], capsys)
    assert data['excluded']['sovereign'] == 1
    assert data['count'] == 1
    assert data['bonds'][0]['isin'] == 'RU000A0COR11'
    assert data['bonds'][0]['issuer'] == 'Корпоративный Тест 11'
    assert data['filters']['exclude_sovereign'] is True


def test_exclude_sovereign_portfolio_mode(db, capsys):
    """Фильтр работает и в portfolio-режиме (колонки issuer/entity_type
    добавлены в portfolio-выборку аддитивно)."""
    minfin_id = _insert_company(db, 'Минфин Тест', entity_type='sovereign')
    _insert_bond(db, isin='RU000A0PSV01', ticker='PSV01', name='ОФЗ 29007 Тест',
                 coupon_quantity_per_year=2, floating_coupon_flag=True,
                 company_id=minfin_id)
    _insert_bond(db, isin='RU000A0PCR01', ticker='PCR01', name='Корпоративный Тест 12')
    _insert_position(db, 'RU000A0PSV01', quantity=10, price=980.0)
    _insert_position(db, 'RU000A0PCR01', quantity=5, price=950.0)

    use_case = CalculateYtmUseCase(db=db)
    data = _run_json(use_case, ['--mode', 'portfolio', '--format', 'json',
                                '--exclude-sovereign'], capsys)
    assert data['meta']['mode'] == 'portfolio'
    assert data['excluded']['sovereign'] == 1
    assert [row['isin'] for row in data['bonds']] == ['RU000A0PCR01']
    row = data['bonds'][0]
    assert set(row) == set(_BOND_JSON_KEYS_PORTFOLIO)
    assert row['qty'] == 5
    assert row['avg_price'] == 950.0


# ---------------------------------------------------------------------------
# Воронка отсечений (R9) — кейс ППК РЭО 01
# ---------------------------------------------------------------------------

def test_funnel_shows_truncation_reason_ppk_case(db, capsys):
    """П-I/ППК РЭО 01: флоатер риск-0 freq-4 не виден в топ-1 — причина
    (обрезано лимитом выдачи) видна в excluded.limit_truncated."""
    _insert_bond(db, isin='RU000A0PPK01', ticker='PPK01', name='ППК Тест 01 РЭО',
                 risk_level=0, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('11.0'), floating_coupon_flag=True)
    _insert_bond(db, isin='RU000A0PPK02', ticker='PPK02', name='ППК Тест 02 РЭО',
                 risk_level=0, coupon_quantity_per_year=4,
                 coupon_rate_percent=Decimal('13.0'), floating_coupon_flag=True)

    use_case = CalculateYtmUseCase(db=db)
    data = _run_json(use_case, ['--mode', 'buy', '--format', 'json',
                                '--floating-coupon', '--max-risk', '1',
                                '--limit', '1'], capsys)
    assert data['count'] == 1
    assert data['candidates_total'] == 2
    assert data['bonds'][0]['isin'] == 'RU000A0PPK02'  # топ по купону
    assert data['excluded']['limit_truncated'] == 1
    assert data['excluded']['freq'] == 0
    assert data['excluded']['sovereign'] == 0


def test_funnel_counts_freq_exclusions(db, capsys):
    _seed_three_freqs(db)
    use_case = CalculateYtmUseCase(db=db)
    data = _run_json(use_case, ['--mode', 'buy', '--format', 'json',
                                '--coupon-freq-max', '4'], capsys)
    assert data['excluded']['freq'] == 1
    assert data['candidates_total'] == 3
    assert data['count'] == 2


def test_funnel_counts_min_ytm(db, capsys):
    """--min-ytm отсекает по рассчитанной движком YTM (NULL не проходит) —
    счётчик excluded.min_ytm отражает отсечённых."""
    _seed_three_freqs(db)
    use_case = CalculateYtmUseCase(db=db)
    data = _run_json(use_case, ['--mode', 'buy', '--format', 'json',
                                '--min-ytm', '19'], capsys)
    # на цене 95: YTM ≈ 23% (купон 20% ежем.), ≈ 18% (15% кварт.), ≈ 16.5%
    # (14% полугод.) — порог 19 оставляет только ежемесячную
    assert data['excluded']['min_ytm'] == 2
    assert data['count'] == data['candidates_total'] - data['excluded']['min_ytm']


# ---------------------------------------------------------------------------
# R6: логи WARNING+ по умолчанию, INFO — под --verbose
# ---------------------------------------------------------------------------

def test_default_mode_sets_root_level_warning():
    """Без --verbose корневой уровень поднимается до WARNING (INFO скрыт)."""
    logging.getLogger().setLevel(logging.INFO)
    use_case = CalculateYtmUseCase(db=None)
    args = argparse.Namespace(mode='buy', format='text', limit=10)  # нет verbose
    use_case.execute(args)  # упадёт внутри тихо (text-режим глотает ошибки)
    assert logging.getLogger().level == logging.WARNING


def test_verbose_mode_keeps_info_level():
    """С --verbose уровень не поднимается: INFO/DEBUG остаются видимыми."""
    logging.getLogger().setLevel(logging.INFO)
    use_case = CalculateYtmUseCase(db=None)
    args = argparse.Namespace(mode='buy', format='text', verbose=True, limit=10)
    use_case.execute(args)
    assert logging.getLogger().level == logging.INFO


# ---------------------------------------------------------------------------
# Текстовый режим сохранён (по умолчанию)
# ---------------------------------------------------------------------------

def test_text_mode_default_prints_table_with_freq_column(db, capsys):
    """По умолчанию — прежняя ASCII-таблица; колонка «Частота» всегда."""
    _seed_three_freqs(db)
    use_case = CalculateYtmUseCase(db=db)
    use_case.execute(_parse(['--mode', 'buy', '--coupon-freq-max', '4']))
    out = capsys.readouterr().out
    assert 'RU000A0FRQ04' in out
    assert 'RU000A0FRQ12' not in out  # фильтр работает и в текстовом режиме
    assert 'Частота' in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)  # это не JSON — таблица


# ---------------------------------------------------------------------------
# Анти-P-F: одна команда, stdout — чистый JSON (CLI subprocess)
# ---------------------------------------------------------------------------

def _cli_env() -> dict:
    env = os.environ.copy()
    env['PYTHONPATH'] = '.'
    test_dsn = env.get('POSTGRES_DSN_TEST')
    assert test_dsn, "POSTGRES_DSN_TEST не задан — CLI-тест не может идти на тестовую БД"
    # DSN только через окружение процесса (load_dotenv не перекрывает его);
    # в командной строке никакого DSN нет — анти-P-A.
    env['POSTGRES_DSN'] = test_dsn
    return env


def test_cli_json_stdout_pure_and_info_logs_silenced(db):
    """`python main.py calculate-ytm --mode buy --format json`:
    rc == 0, stdout — чистый JSON; INFO-логи (в т.ч. «успешно выполнена»)
    в stderr по умолчанию скрыты (R6/P-F)."""
    _insert_bond(db, isin='RU000A0CLI01', ticker='CLI01', name='Кли Тест Бонд')
    res = subprocess.run(
        [sys.executable, 'main.py', 'calculate-ytm', '--mode', 'buy',
         '--format', 'json', '--exclude-sovereign'],
        capture_output=True, text=True, env=_cli_env(),
    )
    assert res.returncode == 0, f"Stderr: {res.stderr}"
    data = json.loads(res.stdout)  # stdout — чистый JSON (логи в stderr)
    assert data['schema_version'] == 1
    assert [row['isin'] for row in data['bonds']] == ['RU000A0CLI01']
    assert 'успешно выполнена' not in res.stderr  # INFO скрыт без -v


def test_cli_verbose_restores_info_logs(db):
    """С -v INFO-логи возвращаются в stderr (включая воронку R9)."""
    _insert_bond(db, isin='RU000A0CLI02', ticker='CLI02', name='Кли Вербоз Бонд')
    res = subprocess.run(
        [sys.executable, 'main.py', '-v', 'calculate-ytm', '--mode', 'buy',
         '--format', 'json'],
        capture_output=True, text=True, env=_cli_env(),
    )
    assert res.returncode == 0, f"Stderr: {res.stderr}"
    json.loads(res.stdout)  # stdout по-прежнему чистый JSON
    assert 'успешно выполнена' in res.stderr
    assert 'Применённые фильтры' in res.stderr  # R9: фильтры логируются


def test_cli_conflict_error_goes_to_stderr_only(db):
    """WARNING+ проходят и без -v: конфликт флагов — ERROR в stderr,
    rc == 1, stdout пуст (машинный контракт не рвётся)."""
    res = subprocess.run(
        [sys.executable, 'main.py', 'calculate-ytm', '--mode', 'buy',
         '--format', 'json', '--coupon-freq-max', '4',
         '--coupon-freq-in', '2,4'],
        capture_output=True, text=True, env=_cli_env(),
    )
    assert res.returncode == 1
    assert res.stdout == ''
    assert 'взаимоисключающие' in res.stderr
