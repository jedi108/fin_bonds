"""
Тесты задачи 002.6: пайплайн update-ratings наполняет rating_history.

Покрывает:
- utils.normalize_rating_code / utils.rating_code_to_score — маппинг кода
  провайдера ('A-(RU)', 'AAA.ru', 'RUAA+') на шкалу rating_scale из конфига;
- адаптер credit_rating: двойная запись (monitoring_checks + rating_history),
  пропуск 'N/A' и кодов вне шкалы;
- идемпотентность записи за день (миграция 012, уникальный индекс
  isin+rating_date+rating_code): повторный прогон не плодит дубли,
  изменение рейтинга в течение дня сохраняется как отдельная строка.
"""
from datetime import date
from types import SimpleNamespace

from src.data_models import Bond
from src.monitoring.adapters.credit_rating import CreditRatingAdapter
from src.utils import normalize_rating_code, rating_code_to_score

# Реалистичное подмножество rating_scale из config.yaml
RATING_SCALE = {'AAA': 10, 'AA+': 9, 'A': 5, 'A-': 4, 'BBB': 2, 'D': 0}


def add_catalog_row(db, isin: str):
    """Строка каталога: FK rating_history.isin -> bonds_catalog(isin)."""
    db.add_bonds_to_catalog([
        Bond(
            isin=isin,
            name=f'Бонд {isin}',
            nominal=1000,
            maturity_date=date(2030, 1, 1),
            currency='RUB',
        )
    ])


def make_adapter(db, rating: str):
    """Адаптер с одним фейковым провайдером, всегда возвращающим rating."""
    provider = (lambda isin, **kw: {'rating': rating, 'name': isin, 'ticker': isin}) \
        if rating is not None else (lambda isin, **kw: None)
    return CreditRatingAdapter(
        db=db,
        config={'rating_scale': RATING_SCALE},
        moex_client=object(),  # провайдеру не нужен: фейковый источник
        rating_providers={'test': provider},
    )


class TestRatingCodeMapping:
    """Маппинг код -> балл по шкале rating_scale (без БД)."""

    def test_national_scale_suffixes_are_stripped(self):
        assert normalize_rating_code('A-(RU)') == 'A-'
        assert normalize_rating_code('AAA.ru') == 'AAA'
        assert normalize_rating_code(' AA+(RU) ') == 'AA+'
        assert normalize_rating_code('bbb') == 'BBB'

    def test_scale_codes_map_to_score(self):
        assert rating_code_to_score('A-(RU)', RATING_SCALE) == 4
        assert rating_code_to_score('AAA.ru', RATING_SCALE) == 10
        assert rating_code_to_score('RUAA+', RATING_SCALE) == 9
        assert rating_code_to_score('A-', RATING_SCALE) == 4

    def test_na_and_blank_give_none(self):
        assert rating_code_to_score('N/A', RATING_SCALE) is None
        assert rating_code_to_score(None, RATING_SCALE) is None
        assert rating_code_to_score('', RATING_SCALE) is None

    def test_unknown_code_gives_none(self):
        assert rating_code_to_score('ZZ+', RATING_SCALE) is None
        assert rating_code_to_score('НАрушитель', RATING_SCALE) is None

    def test_empty_scale_gives_none(self):
        assert rating_code_to_score('AAA', {}) is None
        assert rating_code_to_score('AAA', None) is None

    def test_agency_and_moex_formats(self):
        # MOEX /cci (sf), НРА (|ru|), Эксперт РА (префикс ru), лишние пробелы
        assert normalize_rating_code('AAA(ru.sf)') == 'AAA'
        assert normalize_rating_code('ruA-(sf)') == 'A-'
        assert normalize_rating_code('A-|ru|') == 'A-'
        assert normalize_rating_code('ruBBB+ ') == 'BBB+'
        assert normalize_rating_code('AA- (RU)') == 'AA-'
        assert normalize_rating_code('ruD') == 'D'
        # 'RD' — не префикс ru + 'D'; нераспознанное не калечится
        assert normalize_rating_code('RD') == 'RD'
        assert normalize_rating_code('Отозван') == 'ОТОЗВАН'


class TestProductionRatingScale:
    """Шкала из config.yaml покрывает спекулятивный уровень и дефолт."""

    @staticmethod
    def scale():
        import pytest
        import yaml
        from pathlib import Path
        path = Path(__file__).parent.parent / 'config.yaml'
        if not path.exists():  # config.yaml не в git (*.yaml) — локальный/деплойный файл
            pytest.skip('config.yaml отсутствует')
        return yaml.safe_load(path.read_text())['rating_scale']

    def test_junk_and_default_are_scored_and_ordered(self):
        scale = self.scale()
        codes = ['BB-(RU)', 'B+', 'B-.ru', 'CCC', 'C', 'RD', 'D']
        scores = [rating_code_to_score(c, scale) for c in codes]
        assert None not in scores, dict(zip(codes, scores))
        assert scores == sorted(scores, reverse=True)
        # прежние баллы не сдвинуты — история в rating_history сравнима
        assert rating_code_to_score('AAA', scale) == 13
        assert rating_code_to_score('BB-', scale) == 1

    def test_structured_finance_maps_to_base_letter(self):
        assert rating_code_to_score('AAA(ru.sf)', self.scale()) == 13


class TestCreditRatingAdapterDualWrite:
    """Адаптер пишет и monitoring_checks, и rating_history (002.6, вариант 1)."""

    ISIN = 'RU000A0RTNG01'

    def test_fetch_writes_both_tables(self, db):
        add_catalog_row(db, self.ISIN)

        result = make_adapter(db, 'A-(RU)').fetch(SimpleNamespace(isin=self.ISIN))

        assert result.value == 'A-(RU)'
        assert db.get_last_monitoring_check(self.ISIN, 'credit_rating') == \
            (date.today(), 'A-(RU)')
        # Код нормализован к виду шкалы конфига, балл — числовой
        assert db.get_rating_history_for_bonds([self.ISIN]) == \
            {self.ISIN: [(date.today(), 4)]}

    def test_fetch_na_writes_monitoring_only(self, db):
        add_catalog_row(db, self.ISIN)

        result = make_adapter(db, None).fetch(SimpleNamespace(isin=self.ISIN))

        assert result.value == 'N/A'
        assert db.get_last_monitoring_check(self.ISIN, 'credit_rating') == \
            (date.today(), 'N/A')
        assert db.get_rating_history_for_bonds([self.ISIN]) == {}

    def test_fetch_unmapped_code_writes_monitoring_only(self, db):
        add_catalog_row(db, self.ISIN)

        make_adapter(db, 'ZZ+').fetch(SimpleNamespace(isin=self.ISIN))

        assert db.get_last_monitoring_check(self.ISIN, 'credit_rating') == \
            (date.today(), 'ZZ+')
        assert db.get_rating_history_for_bonds([self.ISIN]) == {}

    def test_refetch_same_day_is_idempotent(self, db):
        add_catalog_row(db, self.ISIN)
        adapter = make_adapter(db, 'A-(RU)')

        adapter.fetch(SimpleNamespace(isin=self.ISIN))
        adapter.fetch(SimpleNamespace(isin=self.ISIN))  # повторный ночной прогон

        with db.conn.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM rating_history WHERE isin = %s", (self.ISIN,))
            assert cursor.fetchone()[0] == 1

    def test_rating_change_same_day_keeps_history(self, db):
        add_catalog_row(db, self.ISIN)

        make_adapter(db, 'A-(RU)').fetch(SimpleNamespace(isin=self.ISIN))
        make_adapter(db, 'BBB(RU)').fetch(SimpleNamespace(isin=self.ISIN))

        history = db.get_rating_history_for_bonds([self.ISIN])
        assert sorted(score for _, score in history[self.ISIN]) == [2, 4]

    def test_direct_add_rating_dedups_same_code_only(self, db):
        add_catalog_row(db, self.ISIN)

        db.add_rating(self.ISIN, date.today(), 'A-', 4)
        db.add_rating(self.ISIN, date.today(), 'A-', 4)      # дубль за день — no-op
        db.add_rating(self.ISIN, date.today(), 'BBB', 2)     # изменение — отдельная строка
        db.add_rating(self.ISIN, date(2026, 9, 28), 'A-', 4)  # другой день — отдельная строка

        history = db.get_rating_history_for_bonds([self.ISIN])
        assert sorted(history[self.ISIN]) == [
            (date(2026, 9, 28), 4), (date.today(), 2), (date.today(), 4)
        ]


class FakeRatingDb:
    """In-memory подмножество PortfolioStorage, которое трогает адаптер."""

    def __init__(self):
        self.checks = {}   # (isin, metric) -> value (последнее)
        self.history = []  # (isin, score)

    def get_rating_history_for_bonds(self, isins):
        return {i: [(None, sc) for isin, sc in self.history if isin == i] for i in isins}

    def get_last_monitoring_check(self, isin, metric):
        value = self.checks.get((isin, metric))
        return (None, value) if value is not None else None

    def add_monitoring_check(self, isin, metric, value):
        self.checks[(isin, metric)] = value

    def add_rating(self, isin, rating_date, rating_code, rating_score):
        self.history.append((isin, rating_score))


class TestCreditRatingAdapterSignals:
    """Значимость результата: смена источника, нераспознанный код (без БД)."""

    ISIN = 'RU000A10XX01'

    @staticmethod
    def run(db, provider_name, rating):
        adapter = CreditRatingAdapter(
            db=db,
            config={'rating_scale': RATING_SCALE},
            moex_client=object(),
            rating_providers={provider_name: lambda isin, **kw: {'rating': rating, 'agency': 'АКРА'}},
        )
        return adapter.fetch(SimpleNamespace(isin=TestCreditRatingAdapterSignals.ISIN))

    def test_downgrade_same_source_is_significant(self):
        db = FakeRatingDb()
        self.run(db, 'moex', 'AAA')
        result = self.run(db, 'moex', 'A')
        assert result.status == 'changed' and result.is_significant
        assert 'moex, АКРА' in result.message

    def test_change_with_source_switch_is_not_significant(self):
        db = FakeRatingDb()
        self.run(db, 'moex', 'AAA')
        result = self.run(db, 'extra', 'A')
        assert result.status == 'changed' and not result.is_significant
        assert 'смене источника' in result.message
        assert db.checks[(self.ISIN, 'credit_rating_source')] == 'extra'

    def test_unrecognized_code_alerts_once(self):
        db = FakeRatingDb()
        first = self.run(db, 'moex', 'ZZ+')
        again = self.run(db, 'moex', 'ZZ+')
        assert first.is_significant and 'Нераспознанный' in first.message
        assert not again.is_significant
        assert db.history == []  # вне шкалы — в rating_history не пишется

    def test_not_found_is_not_significant(self):
        db = FakeRatingDb()
        adapter = CreditRatingAdapter(
            db=db, config={'rating_scale': RATING_SCALE}, moex_client=object(),
            rating_providers={'moex': lambda isin, **kw: None},
        )
        result = adapter.fetch(SimpleNamespace(isin=self.ISIN))
        assert result.value == 'N/A' and not result.is_significant
        assert (self.ISIN, 'credit_rating_source') not in db.checks

