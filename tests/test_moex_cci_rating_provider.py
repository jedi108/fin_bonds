"""
Тесты задачи 002.9 (Р10): MOEX-провайдер рейтингов на эндпоинте /iss/cci/rating.

Старый источник /iss/securities/<secid>/credit_ratings.json не существует
(провайдер возвращал None для любой облигации). Новый — get_security_rating_cci:
emitent_id по ISIN → карточка рейтингов выпуска; исторические записи 'Отозван'
исключаются, берётся свежая по rating_date. Фейк-клиент повторяет контракт
MoexApiClient.get_security_rating_cci (форматированные строки блока
cci_rating_securities).
"""
import sys
import types

from src.monitoring.rating_parser import get_rating_from_moex, load_rating_providers


class FakeMoexClient:
    """Возвращает заранее заданные записи cci_rating_securities."""

    def __init__(self, records=None):
        self.records = records

    def get_security_rating_cci(self, isin: str):
        return self.records


def record(rating: str, rating_date: str, agency: str = 'АКРА'):
    return {
        'agency_id': 1,
        'agency_name_short_ru': agency,
        'rating_level_name_short_ru': rating,
        'rating_date': rating_date,
        'rating_publicate_date': rating_date,
    }


class TestMoexCciProvider:
    ISIN = 'RU000A1075J3'  # живой ISIN вне портфеля; в тестах — фейк-клиент

    def test_success_returns_latest_rating_and_agency(self):
        client = FakeMoexClient([
            record('A-(RU)', '2026-05-08'),
            record('BBB+(RU)', '2025-03-01'),
        ])

        result = get_rating_from_moex(self.ISIN, client)

        assert result == {
            'rating': 'A-(RU)',
            'agency': 'АКРА',
            'name': self.ISIN,
            'ticker': self.ISIN,
        }

    def test_revoked_records_excluded_even_if_newest(self):
        client = FakeMoexClient([
            record('Отозван', '2026-12-01', agency='Эксперт РА'),
            record('A-(RU)', '2026-05-08'),
        ])

        result = get_rating_from_moex(self.ISIN, client)

        assert result is not None
        assert result['rating'] == 'A-(RU)'

    def test_all_revoked_returns_none(self):
        client = FakeMoexClient([record('Отозван', '2012-01-01', agency='Эксперт РА')])

        assert get_rating_from_moex(self.ISIN, client) is None

    def test_no_records_returns_none(self):
        """emitent_id не найден / эндпоинт пуст → None → адаптер переходит к следующему провайдеру."""
        assert get_rating_from_moex(self.ISIN, FakeMoexClient(None)) is None
        assert get_rating_from_moex(self.ISIN, FakeMoexClient([])) is None

    def test_blank_rating_in_latest_record(self):
        client = FakeMoexClient([record('', '2026-05-08')])

        assert get_rating_from_moex(self.ISIN, client) is None


class TestMoexWorstOf:
    """Worst-of по действующим рейтингам агентств (последняя запись каждого)."""

    ISIN = 'RU000A10XX02'
    SCALE = {'AAA': 13, 'AA': 11, 'A': 8, 'A-': 7, 'BBB+': 6}

    def test_worst_active_agency_rating_wins(self):
        client = FakeMoexClient([
            record('AA(RU)', '2026-06-01', agency='АКРА'),
            record('ruA-', '2026-03-01', agency='Эксперт РА'),
            record('A.ru', '2026-07-01', agency='НКР'),
        ])
        result = get_rating_from_moex(self.ISIN, client, rating_scale=self.SCALE)
        assert (result['rating'], result['agency']) == ('ruA-', 'Эксперт РА')

    def test_agency_withdrawal_drops_its_older_rating(self):
        client = FakeMoexClient([
            record('BBB+(RU)', '2025-01-01', agency='НКР'),
            record('Отозван', '2026-02-01', agency='НКР'),
            record('AA(RU)', '2026-01-01', agency='АКРА'),
        ])
        result = get_rating_from_moex(self.ISIN, client, rating_scale=self.SCALE)
        assert result['rating'] == 'AA(RU)'

    def test_agency_latest_record_replaces_its_history(self):
        client = FakeMoexClient([
            record('BBB+(RU)', '2024-01-01', agency='АКРА'),
            record('AA(RU)', '2026-01-01', agency='АКРА'),
        ])
        assert get_rating_from_moex(self.ISIN, client, rating_scale=self.SCALE)['rating'] == 'AA(RU)'

    def test_unknown_codes_only_fall_back_to_latest(self):
        client = FakeMoexClient([
            record('ZZ1', '2026-01-01', agency='АКРА'),
            record('ZZ2', '2026-05-01', agency='НКР'),
        ])
        assert get_rating_from_moex(self.ISIN, client, rating_scale=self.SCALE)['rating'] == 'ZZ2'


class TestLoadRatingProviders:
    """Внешние провайдеры подключаются опциональным модулем private_plugins."""

    def _block_plugins(self, monkeypatch):
        # None в sys.modules → ModuleNotFoundError при импорте
        monkeypatch.setitem(sys.modules, 'private_plugins', None)
        monkeypatch.setitem(sys.modules, 'private_plugins.rating_providers', None)

    def test_without_plugins_only_moex(self, monkeypatch):
        self._block_plugins(monkeypatch)
        assert list(load_rating_providers()) == ['moex']

    def test_plugin_providers_appended_after_moex(self, monkeypatch):
        extra = lambda isin, **kw: {'rating': 'A'}
        pkg = types.ModuleType('private_plugins')
        mod = types.ModuleType('private_plugins.rating_providers')
        mod.RATING_PROVIDERS = {'extra': extra, 'moex': extra}
        monkeypatch.setitem(sys.modules, 'private_plugins', pkg)
        monkeypatch.setitem(sys.modules, 'private_plugins.rating_providers', mod)

        providers = load_rating_providers()

        assert list(providers) == ['moex', 'extra']
        assert providers['moex'] is get_rating_from_moex  # встроенный не перетирается
        assert providers['extra'] is extra
