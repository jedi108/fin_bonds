"""
Тесты для сценариев анализа кандидатов на покупку (analyze-buy-candidates).
Проверяют парсер аргументов, фильтрацию в хранилище и вычисление финансовых метрик.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import argparse
import pytest

from src.storage import PortfolioStorage
from src.use_cases.analyze_buy_candidates import AnalyzeBuyCandidatesUseCase


class TestAnalyzeBuyCandidatesParser:
    """Тестирование CLI парсера для analyze-buy-candidates."""

    def _create_parser(self):
        parser = argparse.ArgumentParser()
        AnalyzeBuyCandidatesUseCase.setup_parser(parser)
        return parser

    def test_top_default_arguments(self):
        parser = self._create_parser()
        args = parser.parse_args(['top'])
        assert args.mode == 'top'
        assert AnalyzeBuyCandidatesUseCase._resolve_max_risk(args, default=1) == 1
        assert args.limit == 20
        assert args.min_months == 1
        assert args.coupon_freq is None
        assert args.monthly is False
        assert args.quarterly is False
        assert args.only_ofz is False
        assert args.exclude_ofz is False
        assert AnalyzeBuyCandidatesUseCase._resolve_coupon_freq(args) is None
        assert AnalyzeBuyCandidatesUseCase._resolve_entity_type_filter(args) is None

    def test_top_max_risk_and_min_risk_alias(self):
        parser = self._create_parser()
        args = parser.parse_args(['top', '--max-risk', '2'])
        assert AnalyzeBuyCandidatesUseCase._resolve_max_risk(args, default=1) == 2

        args_alias = parser.parse_args(['top', '--min-risk', '2'])
        assert AnalyzeBuyCandidatesUseCase._resolve_max_risk(args_alias, default=1) == 2

    def test_top_max_risk_and_min_risk_conflict(self):
        parser = self._create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(['top', '--max-risk', '1', '--min-risk', '2'])

    def test_top_coupon_frequency_flags(self):
        parser = self._create_parser()
        args_monthly = parser.parse_args(['top', '--monthly'])
        assert args_monthly.monthly is True
        assert AnalyzeBuyCandidatesUseCase._resolve_coupon_freq(args_monthly) == 12

        args_quarterly = parser.parse_args(['top', '--quarterly'])
        assert args_quarterly.quarterly is True
        assert AnalyzeBuyCandidatesUseCase._resolve_coupon_freq(args_quarterly) == 4

        args_custom = parser.parse_args(['top', '--coupon-freq', '2'])
        assert AnalyzeBuyCandidatesUseCase._resolve_coupon_freq(args_custom) == 2

        args_all = parser.parse_args(['top', '--coupon-freq', 'all'])
        assert AnalyzeBuyCandidatesUseCase._resolve_coupon_freq(args_all) is None

    def test_top_frequency_flags_conflict(self):
        parser = self._create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(['top', '--monthly', '--quarterly'])

        with pytest.raises(SystemExit):
            parser.parse_args(['top', '--monthly', '--coupon-freq', '12'])

    def test_top_ofz_flags_and_aliases(self):
        parser = self._create_parser()
        args_ofz = parser.parse_args(['top', '--only-ofz'])
        assert args_ofz.only_ofz is True
        assert AnalyzeBuyCandidatesUseCase._resolve_entity_type_filter(args_ofz) == 'only_ofz'

        args_ofz_alias = parser.parse_args(['top', '--ofz-only'])
        assert args_ofz_alias.only_ofz is True
        assert AnalyzeBuyCandidatesUseCase._resolve_entity_type_filter(args_ofz_alias) == 'only_ofz'

        args_corp = parser.parse_args(['top', '--exclude-ofz'])
        assert args_corp.exclude_ofz is True
        assert AnalyzeBuyCandidatesUseCase._resolve_entity_type_filter(args_corp) == 'exclude_ofz'

        args_corp_alias = parser.parse_args(['top', '--no-ofz'])
        assert args_corp_alias.exclude_ofz is True
        assert AnalyzeBuyCandidatesUseCase._resolve_entity_type_filter(args_corp_alias) == 'exclude_ofz'

    def test_top_ofz_flags_conflict(self):
        parser = self._create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(['top', '--only-ofz', '--exclude-ofz'])

    def test_floaters_arguments(self):
        parser = self._create_parser()
        args = parser.parse_args(['floaters', '--limit', '10', '--max-risk', '2', '--monthly', '--no-ofz'])
        assert args.mode == 'floaters'
        assert args.limit == 10
        assert args.max_risk == 2
        assert AnalyzeBuyCandidatesUseCase._resolve_coupon_freq(args) == 12
        assert AnalyzeBuyCandidatesUseCase._resolve_entity_type_filter(args) == 'exclude_ofz'

    def test_compare_arguments(self):
        parser = self._create_parser()
        args = parser.parse_args(['compare', '--limit', '30', '--max-risk', '2'])
        assert args.mode == 'compare'
        assert args.limit == 30
        assert args.max_risk == 2

    def test_include_held_flag(self):
        """002.2: --include-held есть во всех трёх режимах, по умолчанию False."""
        parser = self._create_parser()
        for mode in ('top', 'floaters', 'compare'):
            args = parser.parse_args([mode])
            assert args.include_held is False
            args_held = parser.parse_args([mode, '--include-held'])
            assert args_held.include_held is True


class TestAnalyzeBuyCandidatesStorage:
    """Интеграционные тесты выборки кандидатов из базы данных."""

    def _insert_company(self, db: PortfolioStorage, name: str, entity_type: str = 'company') -> int:
        cur = db._cursor()
        cur.execute(
            "INSERT INTO companies (name, entity_type) VALUES (%s, %s) RETURNING id",
            (name, entity_type)
        )
        return cur.fetchone()['id']

    def _insert_bond(self, db: PortfolioStorage, **kwargs):
        cur = db._cursor()
        defaults = {
            'isin': 'RU000A0TEST1',
            'ticker': 'TEST1',
            'name': 'Test Bond 1',
            'currency': 'rub',
            'nominal': Decimal('1000'),
            'maturity_date': date.today() + timedelta(days=365),
            'coupon_quantity_per_year': 12,
            'coupon_rate_percent': Decimal('20.0'),
            'risk_level': 1,
            'is_trade_available': True,
            'is_for_qualified_investors': False,
            'issue_size': 1000000,
            'market_price': Decimal('950.0'),
            'market_price_updated_at': datetime.now(timezone.utc),
            'floating_coupon_flag': False,
            'perpetual_flag': False,
            'amortization_flag': False,
            'company_id': None,
        }
        defaults.update(kwargs)
        fields = list(defaults.keys())
        placeholders = ', '.join(['%s'] * len(fields))
        col_names = ', '.join(fields)
        cur.execute(
            f"INSERT INTO bonds_catalog ({col_names}) VALUES ({placeholders})",
            [defaults[f] for f in fields]
        )

    def test_get_top_buy_candidates_basic_and_filters(self, db: PortfolioStorage):
        comp_id = self._insert_company(db, 'Газпром Нефть', 'company')
        ofz_id = self._insert_company(db, 'Минфин РФ', 'sovereign')

        now = datetime.now(timezone.utc)
        # 1. Валидный фикс, эмитент Газпром Нефть, ежемесячный
        self._insert_bond(
            db,
            isin='RU0000000001',
            ticker='GZPRM1',
            name='Газпром Нефть 01',
            company_id=comp_id,
            coupon_quantity_per_year=12,
            coupon_rate_percent=Decimal('24.0'),
            market_price=Decimal('1000.0'),
            market_price_updated_at=now,
            risk_level=1,
        )

        # 2. ОФЗ, суверенный эмитент, квартальный
        self._insert_bond(
            db,
            isin='RU0000000002',
            ticker='OFZ26238',
            name='ОФЗ 26238',
            company_id=ofz_id,
            coupon_quantity_per_year=4,
            coupon_rate_percent=Decimal('15.0'),
            market_price=Decimal('750.0'),
            market_price_updated_at=now,
            risk_level=1,
        )

        # 3. Бумага уже в портфеле -> должна быть исключена
        self._insert_bond(
            db,
            isin='RU0000000003',
            ticker='PORT1',
            name='Портфельная бумага',
            company_id=comp_id,
            market_price_updated_at=now,
        )
        cur = db._cursor()
        cur.execute(
            "INSERT INTO portfolio_positions (isin, ticker, name, quantity, average_price, current_price, broker_name) "
            "VALUES ('RU0000000003', 'PORT1', 'Портфельная бумага', 10, 1000, 1000, 'tbank')"
        )

        # 4. Риск 2 -> исключается при max_risk=1
        self._insert_bond(
            db,
            isin='RU0000000004',
            ticker='RISK2',
            name='Бумага Риск 2',
            company_id=comp_id,
            risk_level=2,
            market_price_updated_at=now,
        )

        # 5. Устаревшая цена (старше 24ч) -> исключается
        self._insert_bond(
            db,
            isin='RU0000000005',
            ticker='STALE',
            name='Старая цена',
            company_id=comp_id,
            market_price_updated_at=now - timedelta(hours=25),
        )

        # 6. Для квалифицированных инвесторов -> исключается
        self._insert_bond(
            db,
            isin='RU0000000006',
            ticker='QUAL',
            name='Для квалов',
            company_id=comp_id,
            is_for_qualified_investors=True,
            market_price_updated_at=now,
        )

        # 7. Без привязки к компании -> исключается
        self._insert_bond(
            db,
            isin='RU0000000007',
            ticker='NOCOMP',
            name='Без компании',
            company_id=None,
            market_price_updated_at=now,
        )

        # 8. Маленький выпуск (< 500 млн руб) -> исключается
        self._insert_bond(
            db,
            isin='RU0000000008',
            ticker='SMALL',
            name='Малый выпуск',
            company_id=comp_id,
            nominal=Decimal('1000'),
            issue_size=400000,  # 400 млн руб
            market_price_updated_at=now,
        )

        # Базовая выборка (max_risk=1): должны пройти только RU0000000001 и RU0000000002
        candidates = db.get_top_buy_candidates(max_risk=1)
        isins = [c['isin'] for c in candidates]
        assert 'RU0000000001' in isins
        assert 'RU0000000002' in isins
        assert 'RU0000000003' not in isins
        assert 'RU0000000004' not in isins
        assert 'RU0000000005' not in isins
        assert 'RU0000000006' not in isins
        assert 'RU0000000007' not in isins
        assert 'RU0000000008' not in isins

        # Проверка финансовых вычислений для RU0000000001
        # nominal=1000, rate=24%, freq=12, price=1000
        # coupon_payment = (1000 * 24 / 100) / 12 = 20.0
        # average_monthly = (1000 * 24 / 100) / 12 = 20.0
        # current_coupon_yield = (240 / 1000) * 100 = 24.0%
        cand1 = next(c for c in candidates if c['isin'] == 'RU0000000001')
        assert cand1['company'] == 'Газпром Нефть'
        assert Decimal(str(cand1['coupon_payment_per_bond'])) == Decimal('20.00')
        assert Decimal(str(cand1['average_monthly_coupon_per_bond'])) == Decimal('20.00')
        assert Decimal(str(cand1['current_coupon_yield'])) == Decimal('24.00')

        # Фильтр по частоте: только ежемесячные
        monthly_only = db.get_top_buy_candidates(max_risk=1, coupon_freq=12)
        assert [c['isin'] for c in monthly_only] == ['RU0000000001']

        # Фильтр ОФЗ: exclude_ofz
        no_ofz = db.get_top_buy_candidates(max_risk=1, entity_type_filter='exclude_ofz')
        assert [c['isin'] for c in no_ofz] == ['RU0000000001']

        # Фильтр ОФЗ: only_ofz
        only_ofz = db.get_top_buy_candidates(max_risk=1, entity_type_filter='only_ofz')
        assert [c['isin'] for c in only_ofz] == ['RU0000000002']

        # Проверка max_risk=2: должен включить RU0000000004
        risk2_candidates = db.get_top_buy_candidates(max_risk=2)
        assert 'RU0000000004' in [c['isin'] for c in risk2_candidates]

    def test_get_floater_buy_candidates(self, db: PortfolioStorage):
        comp_id = self._insert_company(db, 'ВТБ', 'company')
        now = datetime.now(timezone.utc)

        # Флоатер с рассчитанной ставкой и спредом
        self._insert_bond(
            db,
            isin='RU0000FLOAT1',
            ticker='VTBFL1',
            name='ВТБ Флоатер 1',
            company_id=comp_id,
            floating_coupon_flag=True,
            coupon_spread=Decimal('1.75'),
            coupon_quantity_per_year=12,
            market_price=Decimal('980.0'),
            market_price_updated_at=now,
            risk_level=1,
        )

        cur = db._cursor()
        cur.execute(
            "INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value) "
            "VALUES ('RU0000FLOAT1', CURRENT_DATE, 'floater_coupon_calculator', '21.75')"
        )

        floaters = db.get_floater_buy_candidates(max_risk=1)
        assert len(floaters) == 1
        fl = floaters[0]
        assert fl['isin'] == 'RU0000FLOAT1'
        assert Decimal(str(fl['calculated_coupon_rate'])) == Decimal('21.75')
        assert Decimal(str(fl['coupon_spread'])) == Decimal('1.75')
        assert fl['coupon_type'] == 'FLOAT'
        # coupon payment = (21.75 * 1000 / 100) / 12 = 18.125 -> 18.13
        assert Decimal(str(fl['coupon_payment_per_bond'])) == Decimal('18.13')

    def _insert_held_position(self, db: PortfolioStorage, isin: str, ticker: str, name: str,
                              quantity: Decimal, two_accounts: bool = False):
        """Позиция портфеля; two_accounts=True кладёт ISIN на два счета (кейс задвоения 002.2)."""
        cur = db._cursor()
        rows = [(isin, ticker, name, float(quantity), 1000.0, 1000.0, 'tbank', 'A1')]
        if two_accounts:
            rows.append((isin, ticker, name, 2.0, 1000.0, 1000.0, 'tbank', 'A2'))
        cur.executemany(
            "INSERT INTO portfolio_positions (isin, ticker, name, quantity, average_price, "
            "current_price, broker_name, account_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )

    def test_get_top_buy_candidates_include_held(self, db: PortfolioStorage):
        """002.2: include_held=True возвращает позиции портфеля с бейджем held (N%),
        по умолчанию они исключены; ISIN на двух счетах не задваивает строки."""
        comp_id = self._insert_company(db, 'ГТЛК', 'company')
        now = datetime.now(timezone.utc)
        # Held-бумага с высокой доходностью — «лучшая докупка» (кейс ГТЛК 002P-04)
        self._insert_bond(
            db,
            isin='RU000HELD001',
            ticker='HELD1',
            name='Held Bond 1',
            company_id=comp_id,
            coupon_rate_percent=Decimal('25.0'),
            market_price=Decimal('1000.0'),
            market_price_updated_at=now,
            risk_level=1,
        )
        self._insert_bond(
            db,
            isin='RU000FREE001',
            ticker='FREE1',
            name='Free Bond 1',
            company_id=comp_id,
            coupon_rate_percent=Decimal('10.0'),
            market_price=Decimal('1000.0'),
            market_price_updated_at=now,
            risk_level=1,
        )
        self._insert_held_position(db, 'RU000HELD001', 'HELD1', 'Held Bond 1',
                                   Decimal('5'), two_accounts=True)  # 5 шт + 2 шт

        # По умолчанию позиции портфеля исключены (обратная совместимость)
        default_rows = db.get_top_buy_candidates(max_risk=1)
        assert [r['isin'] for r in default_rows] == ['RU000FREE001']

        # include_held=True: held-бумага видна ровно одной строкой, с бейджем
        rows = db.get_top_buy_candidates(max_risk=1, include_held=True)
        held_rows = [r for r in rows if r['isin'] == 'RU000HELD001']
        assert len(held_rows) == 1
        assert 'RU000FREE001' in [r['isin'] for r in rows]
        # Доля: (5 + 2) * 1000 = 7000 из 7000 всего портфеля = 100.00%
        assert held_rows[0]['held_share_pct'] == Decimal('100.00')
        assert held_rows[0]['held_badge'] == 'held (100.00%)'

    def test_get_floater_buy_candidates_include_held(self, db: PortfolioStorage):
        """002.2: include_held=True для режима floaters."""
        comp_id = self._insert_company(db, 'ВТБ', 'company')
        now = datetime.now(timezone.utc)
        self._insert_bond(
            db,
            isin='RU000HELDFLT',
            ticker='HFLT1',
            name='Held Floater 1',
            company_id=comp_id,
            floating_coupon_flag=True,
            coupon_spread=Decimal('1.5'),
            coupon_quantity_per_year=12,
            market_price=Decimal('980.0'),
            market_price_updated_at=now,
            risk_level=1,
        )
        cur = db._cursor()
        cur.execute(
            "INSERT INTO monitoring_checks (isin, check_date, metric_name, metric_value) "
            "VALUES ('RU000HELDFLT', CURRENT_DATE, 'floater_coupon_calculator', '21.5')"
        )
        self._insert_held_position(db, 'RU000HELDFLT', 'HFLT1', 'Held Floater 1', Decimal('3'))

        assert db.get_floater_buy_candidates(max_risk=1) == []
        rows = db.get_floater_buy_candidates(max_risk=1, include_held=True)
        assert len(rows) == 1
        assert rows[0]['isin'] == 'RU000HELDFLT'
        assert rows[0]['held_badge'] == 'held (100.00%)'

    def test_get_portfolio_comparison_candidates_include_held(self, db: PortfolioStorage):
        """002.2: include_held=True для режима compare."""
        comp_id = self._insert_company(db, 'Газпром', 'company')
        now = datetime.now(timezone.utc)
        for isin, ticker, rate in (
            ('RU000HELD001', 'HELD1', '25.0'),
            ('RU000FREE001', 'FREE1', '10.0'),
        ):
            self._insert_bond(
                db,
                isin=isin,
                ticker=ticker,
                name=ticker,
                company_id=comp_id,
                coupon_rate_percent=Decimal(rate),
                market_price=Decimal('1000.0'),
                market_price_updated_at=now,
                risk_level=1,
            )
        self._insert_held_position(db, 'RU000HELD001', 'HELD1', 'HELD1', Decimal('5'))

        default_rows = db.get_portfolio_comparison_candidates(max_risk=1)
        assert 'RU000HELD001' not in [r['isin'] for r in default_rows]

        rows = db.get_portfolio_comparison_candidates(max_risk=1, include_held=True)
        held = next(r for r in rows if r['isin'] == 'RU000HELD001')
        assert held['held_badge'] == 'held (100.00%)'
        assert held['held_share_pct'] == Decimal('100.00')
