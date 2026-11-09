#!/usr/bin/env python3
"""
Тесты нормализации названий облигаций в компании-эмитенты
(src/company_normalization.py).

Примеры — обезличенные, по реальным паттернам названий каталога
(без ISIN личного портфеля).

Запуск: python3 tests/test_company_normalization.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.company_normalization import (  # noqa: E402
    TYPE_COMPANY,
    TYPE_INDIVIDUAL,
    TYPE_REGION,
    TYPE_SOVEREIGN,
    normalize_bond_name,
    resolve_company,
)

# (название выпуска, ожидаемое имя компании, ожидаемый тип сущности)
CASES = [
    # простые корпоративные серии
    ('Газпром БО-22', 'Газпром', TYPE_COMPANY),
    ('Газпром Капитал БО-003Р-14', 'Газпром Капитал', TYPE_COMPANY),
    ('Газпром Нефть 003P-08R', 'Газпром Нефть', TYPE_COMPANY),  # латинская P
    ('Агрофирма Рубеж 001Р-02', 'Агрофирма Рубеж', TYPE_COMPANY),
    ('МегаФон БО-002P-06', 'МегаФон', TYPE_COMPANY),
    ('РУСАЛ БО-001Р-04', 'РУСАЛ', TYPE_COMPANY),
    ('Магнит БО-004Р-09', 'Магнит', TYPE_COMPANY),
    ('Джетленд БО-01', 'Джетленд', TYPE_COMPANY),
    ('Пионер-Лизинг БО7', 'Пионер-Лизинг', TYPE_COMPANY),
    # «выпуск N» — отдельно и поверх серии
    ('ЭР-Телеком Холдинг выпуск 3', 'ЭР-Телеком Холдинг', TYPE_COMPANY),
    ('О\'КЕЙ выпуск 2', 'О\'КЕЙ', TYPE_COMPANY),
    ('РЖД 001Р выпуск 16', 'РЖД', TYPE_COMPANY),
    ('Вита Лайн 001Р выпуск 2', 'Вита Лайн', TYPE_COMPANY),
    ('Софтлайн выпуск 002Р-01', 'Софтлайн', TYPE_COMPANY),
    # ЗО/БЗО-серии
    ('МКБ ЗО26-1', 'МКБ', TYPE_COMPANY),
    ('ФосАгро ЗО28-Д', 'ФосАгро', TYPE_COMPANY),
    ('Газпром Капитал БЗО26-1-Д', 'Газпром Капитал', TYPE_COMPANY),
    # склеенные токены «буквы+цифры»
    ('Селигдар GOLD01', 'Селигдар', TYPE_COMPANY),
    ('Сбербанк России обб2П05', 'Сбербанк России', TYPE_COMPANY),
    ('Денум Солюшнз оббП01', 'Денум Солюшнз', TYPE_COMPANY),
    ('Совкомбанк 1В02', 'Совкомбанк', TYPE_COMPANY),
    ('ОВОЗ РФ 12840119V', 'ОВОЗ РФ', TYPE_COMPANY),
    # имя целиком склеено: срезаем только серийный хвост, бренд с цифрой цел
    ('ГПБ005P12P', 'ГПБ', TYPE_COMPANY),
    ('ИКС5Фин3P1', 'ИКС5Фин', TYPE_COMPANY),
    # бренд с внутренней цифрой не режется по последнему слову
    ('ИКС 5 ФИНАНС 003P-02', 'ИКС 5 ФИНАНС', TYPE_COMPANY),
    # орг-форма в конце отбрасывается
    ('Трансмашхолдинг АО ПБО-07', 'Трансмашхолдинг', TYPE_COMPANY),
    ('Ростелеком ПАО 001P-19R', 'Ростелеком', TYPE_COMPANY),
    # аббревиатура-слово с номером остаётся целиком
    ('ТГК-14 001Р-07', 'ТГК-14', TYPE_COMPANY),
    # серии с буквами после префикса
    ('РЕСО-Лизинг БО-П выпуск 5', 'РЕСО-Лизинг', TYPE_COMPANY),
    ('ПЮДМ БО-П2', 'ПЮДМ', TYPE_COMPANY),
    # серии ВТБ и субординированные
    ('Банк ВТБ Б-1-356', 'Банк ВТБ', TYPE_COMPANY),
    ('Банк ВТБ (ПАО) Б-1-351', 'Банк ВТБ', TYPE_COMPANY),
    ('Банк ВТБ СУБ-Т1-8', 'Банк ВТБ', TYPE_COMPANY),
    # склеенные хвосты с дефисами
    ('Полипласт П02-БО-11', 'Полипласт', TYPE_COMPANY),
    ('Полипласт АО П02-БО-08', 'Полипласт', TYPE_COMPANY),
    ('ТБанк TCS-perp1', 'ТБанк', TYPE_COMPANY),
    # полное юр. наименование в кавычках
    ('Публичное акционерное общество "Ростелеком" биржевая облигация 4B02-06-00124-A-001P 05.09.2025',
     'Ростелеком', TYPE_COMPANY),
    # регионы (субъекты РФ)
    ('Свердловская область 35008', 'Свердловская область', TYPE_REGION),
    ('Оренбургская область выпуск 3', 'Оренбургская область', TYPE_REGION),
    ('Республика Саха (Якутия) 34005', 'Республика Саха (Якутия)', TYPE_REGION),
    ('Москва выпуск 74', 'Москва', TYPE_REGION),
    ('Санкт-Петербург выпуск 2', 'Санкт-Петербург', TYPE_REGION),
    # суверенные
    ('ОФЗ 26249', 'Минфин России', TYPE_SOVEREIGN),
    ('ОФЗ-29024-ПК', 'Минфин России', TYPE_SOVEREIGN),
    ('Казахстан выпуск 11', 'Республика Казахстан', TYPE_SOVEREIGN),
    ('Russia 2032 EUR', 'Россия', TYPE_SOVEREIGN),
    # физлицо-эмитент (обезличенный пример)
    ('Иванов И.И.', 'Иванов И.И.', TYPE_INDIVIDUAL),
    # числа без серии
    ('СФО ВТБ РКС Эталон 03', 'СФО ВТБ РКС Эталон', TYPE_COMPANY),
    # точки и дефисы в бренде
    ('М.Видео выпуск 3', 'М.Видео', TYPE_COMPANY),
    ('ВЭБ.РФ ПБО-002Р-41', 'ВЭБ.РФ', TYPE_COMPANY),
]


def run_normalization_cases() -> int:
    failures = 0
    for bond_name, expected_name, expected_type in CASES:
        ref = normalize_bond_name(bond_name)
        if ref.name != expected_name or ref.entity_type != expected_type:
            failures += 1
            print(
                f"FAIL: {bond_name!r} -> ({ref.name!r}, {ref.entity_type}); "
                f"ожидалось ({expected_name!r}, {expected_type})"
            )
    return failures


def run_override_cases() -> int:
    failures = 0
    overrides = {
        # строка: просто каноническое имя
        'МФК Быстроденьги': 'Быстроденьги',
        # словарь: имя + тип + ИНН
        'Газпром': {'name': 'Газпром Капитал', 'entity_type': TYPE_COMPANY, 'inn': '7736050004'},
    }
    checks = [
        # (вход, ожидаемое имя, ожидаемый тип, ожидаемый ИНН)
        ('МФК Быстроденьги 002P-05', 'Быстроденьги', TYPE_COMPANY, None),
        ('Газпром БО-22', 'Газпром Капитал', TYPE_COMPANY, '7736050004'),
        # без переопределения — обычная эвристика
        ('Магнит БО-004Р-09', 'Магнит', TYPE_COMPANY, None),
    ]
    for bond_name, expected_name, expected_type, expected_inn in checks:
        ref = resolve_company(bond_name, overrides)
        if (ref.name, ref.entity_type, ref.inn) != (expected_name, expected_type, expected_inn):
            failures += 1
            print(f"FAIL override: {bond_name!r} -> {ref}; ожидалось "
                  f"({expected_name!r}, {expected_type}, {expected_inn})")
    return failures


def main() -> int:
    failures = run_normalization_cases() + run_override_cases()
    if failures:
        print(f"\n{failures} тест(ов) провалено.")
        return 1
    print(f"OK: все тесты пройдены ({len(CASES)} кейсов нормализации + overrides).")
    return 0


def test_normalization_cases():
    """Обёртка для pytest: кейсы запускаются и в составе `make test`.

    Чистые юнит-тесты без БД — фикстуры db из conftest.py не требуют.
    """
    failures = run_normalization_cases() + run_override_cases()
    assert failures == 0, f"{failures} кейс(ов) провалено (см. FAIL в выводе)"


if __name__ == '__main__':
    sys.exit(main())
