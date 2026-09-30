"""
Тесты задачи 002.9 (Р10): MOEX-провайдер рейтингов на эндпоинте /iss/cci/rating.

Старый источник /iss/securities/<secid>/credit_ratings.json не существует
(провайдер возвращал None для любой облигации). Новый — get_security_rating_cci:
emitent_id по ISIN → карточка рейтингов выпуска; исторические записи 'Отозван'
исключаются, берётся свежая по rating_date. Фейк-клиент повторяет контракт
MoexApiClient.get_security_rating_cci (форматированные строки блока
cci_rating_securities).
"""
from src.monitoring.rating_parser import get_rating_from_moex


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
        """emitent_id не найден / эндпоинт пуст → None → адаптер падает на smartlab."""
        assert get_rating_from_moex(self.ISIN, FakeMoexClient(None)) is None
        assert get_rating_from_moex(self.ISIN, FakeMoexClient([])) is None

    def test_blank_rating_in_latest_record(self):
        client = FakeMoexClient([record('', '2026-05-08')])

        assert get_rating_from_moex(self.ISIN, client) is None
