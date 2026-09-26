#!/usr/bin/env python3
"""
Тесты хранения компаний и связи облигаций с ними (storage.py + link_companies).

Работает с тестовой базой PostgreSQL (фикстура db из conftest.py), без
внешних API; таблицы очищаются между тестами.

Запуск: venv/bin/python -m pytest tests/test_companies_storage.py
"""
from datetime import date
from decimal import Decimal

from psycopg2.extras import RealDictCursor

from src.data_models import Bond
from src.use_cases.link_companies import link_bonds_to_companies


def make_bond(isin: str, name: str) -> Bond:
    return Bond(isin=isin, name=name, nominal=Decimal(1000),
                maturity_date=date(2030, 1, 1), currency='RUB')


def test_upsert_company_by_name_and_inn(db):
    # Имена без учёта регистра (в т.ч. кириллица) дают одну компанию
    id1 = db.upsert_company('Газпром Капитал')
    id1_case = db.upsert_company('Газпром капитал')
    assert id1 == id1_case

    id_gazprom = db.upsert_company('Газпром')
    assert id_gazprom != id1

    # Повторный upsert с тем же ИНН возвращает ту же компанию
    id_by_inn = db.upsert_company('ВЭБ.РФ', inn='7711008242')
    id_by_inn2 = db.upsert_company('ВЭБ.РФ', inn='7711008242')
    assert id_by_inn == id_by_inn2


def test_link_bonds_to_companies(db):
    db.add_bonds_to_catalog([
        make_bond('RU000A1', 'Газпром Капитал БО-003Р-14'),
        make_bond('RU000A2', 'Магнит БО-004Р-09'),
        make_bond('RU000A3', 'Свердловская область 35008'),
    ])
    linked, companies = link_bonds_to_companies(db, overrides=None)
    assert linked == 3
    assert companies == 3

    bond1 = db.get_bond_by_isin('RU000A1')
    assert bond1 is not None and bond1.company_id is not None

    total, linked_count = db.get_catalog_company_coverage()
    assert (total, linked_count) == (3, 3)

    overview = db.get_companies_overview()
    with_bonds = {row['name']: row['bonds_count'] for row in overview if row['bonds_count'] > 0}
    assert with_bonds == {'Газпром Капитал': 1, 'Магнит': 1, 'Свердловская область': 1}
    assert len(overview) == 3

    # Повторная привязка только непривязанных — ничего нового
    linked_again, _ = link_bonds_to_companies(db, overrides=None, only_unlinked=True)
    assert linked_again == 0


def test_catalog_upsert_preserves_company_and_enrichments(db):
    db.add_bonds_to_catalog([make_bond('RU000A1', 'Газпром Капитал БО-003Р-14')])
    link_bonds_to_companies(db, overrides=None)
    company_id = db.get_bond_by_isin('RU000A1').company_id
    assert company_id is not None

    # Обогащения вне каталога (MOEX/рыночная цена) — до «принудительного обновления»
    with db.conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("UPDATE bonds_catalog SET list_level = 1, market_price = 98.5 "
                       "WHERE isin = 'RU000A1'")
    db.conn.commit()

    db.add_bonds_to_catalog([make_bond('RU000A1', 'Газпром Капитал БО-003Р-14')])

    with db.conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            "SELECT company_id, list_level, market_price FROM bonds_catalog WHERE isin = 'RU000A1'"
        )
        row = cursor.fetchone()
    assert row['company_id'] == company_id
    assert row['list_level'] == 1
    assert row['market_price'] == Decimal('98.5')


def test_autolink_new_bond(db):
    db.add_bonds_to_catalog([make_bond('RU000A1', 'Газпром Капитал БО-003Р-14')])
    link_bonds_to_companies(db, overrides=None)

    # Новая облигация получает NULL company_id, автопривязка её подхватывает
    db.add_bonds_to_catalog([make_bond('RU000A4', 'Газпром БО-22')])
    total, linked_count = db.get_catalog_company_coverage()
    assert (total, linked_count) == (2, 1)

    linked_new, _ = link_bonds_to_companies(db, overrides=None, only_unlinked=True)
    assert linked_new == 1
    total, linked_count = db.get_catalog_company_coverage()
    assert (total, linked_count) == (2, 2)


def test_overrides_merge_company_groups(db):
    db.add_bonds_to_catalog([
        make_bond('RU000B1', 'МФК Быстроденьги 002P-05'),
        make_bond('RU000B2', 'Быстроденьги 002P-08'),
    ])
    overrides = {'МФК Быстроденьги': 'Быстроденьги'}
    link_bonds_to_companies(db, overrides=overrides)
    overview = db.get_companies_overview()
    assert len(overview) == 1 and overview[0]['bonds_count'] == 2
