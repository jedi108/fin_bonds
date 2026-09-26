#!/usr/bin/env python3
"""
Тесты хранения компаний и связи облигаций с ними (storage.py + link_companies).

Работает с in-memory SQLite, без внешних API.

Запуск: python3 tests/test_companies_storage.py
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_models import Bond  # noqa: E402
from src.monitoring.storage import Database  # noqa: E402
from src.use_cases.link_companies import link_bonds_to_companies  # noqa: E402

FAILURES = []


def check(condition, message):
    if condition:
        return
    FAILURES.append(message)
    print(f"FAIL: {message}")


def make_bond(isin: str, name: str) -> Bond:
    return Bond(isin=isin, name=name, nominal=Decimal(1000),
                maturity_date=date(2030, 1, 1), currency='RUB')


def main() -> int:
    db = Database(':memory:')

    # --- Схема: таблица companies и колонка company_id ---
    tables = {
        row['name'] for row in
        db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    check('companies' in tables, 'таблица companies не создана')
    columns = [row['name'] for row in db.conn.execute("PRAGMA table_info(bonds_catalog)").fetchall()]
    check('company_id' in columns, 'колонка bonds_catalog.company_id не создана')

    # --- upsert_company: по имени (без учёта регистра), по ИНН ---
    id1 = db.upsert_company('Газпром Капитал')
    id1_case = db.upsert_company('Газпром капитал')
    check(id1 == id1_case, f'имена без учёта регистра должны давать одну компанию: {id1} != {id1_case}')

    id_gazprom = db.upsert_company('Газпром')
    check(id_gazprom != id1, 'разные имена — разные компании')

    id_by_inn = db.upsert_company('ВЭБ.РФ', inn='7711008242')
    id_by_inn2 = db.upsert_company('ВЭБ.РФ', inn='7711008242')
    check(id_by_inn == id_by_inn2, 'повторный upsert с тем же ИНН возвращает ту же компанию')

    # --- Привязка облигаций ---
    db.add_bonds_to_catalog([
        make_bond('RU000A1', 'Газпром Капитал БО-003Р-14'),
        make_bond('RU000A2', 'Магнит БО-004Р-09'),
        make_bond('RU000A3', 'Свердловская область 35008'),
    ])
    linked, companies = link_bonds_to_companies(db, overrides=None)
    check(linked == 3, f'привязано {linked}, ожидалось 3')
    check(companies == 3, f'компаний {companies}, ожидалось 3')

    bond1 = db.get_bond_by_isin('RU000A1')
    check(bond1 is not None and bond1.company_id == id1,
          'RU000A1 должна быть привязана к «Газпром Капитал»')

    total, linked_count = db.get_catalog_company_coverage()
    check((total, linked_count) == (3, 3), f'покрытие {linked_count}/{total}, ожидалось 3/3')

    overview = db.get_companies_overview()
    # 3 компании с облигациями + 2 «Газпром»/«ВЭБ.РФ» без облигаций из upsert-тестов
    with_bonds = {row['name']: row['bonds_count'] for row in overview if row['bonds_count'] > 0}
    check(with_bonds == {'Газпром Капитал': 1, 'Магнит': 1, 'Свердловская область': 1},
          f'группировка неверна: {with_bonds}')
    check(len(overview) == 5, f'в справочнике {len(overview)} компаний, ожидалось 5')

    # Повторная привязка только непривязанных — ничего нового
    linked_again, _ = link_bonds_to_companies(db, overrides=None, only_unlinked=True)
    check(linked_again == 0, f'повторная привязка дала {linked_again}, ожидался 0')

    # --- Обновление каталога не стирает привязку и обогащения ---
    db.conn.execute("UPDATE bonds_catalog SET list_level = 1, market_price = 98.5 "
                    "WHERE isin = 'RU000A1'")
    db.conn.commit()
    db.add_bonds_to_catalog([  # «принудительное обновление» как в update-bonds
        make_bond('RU000A1', 'Газпром Капитал БО-003Р-14'),
        make_bond('RU000A2', 'Магнит БО-004Р-09'),
    ])
    row = db.conn.execute(
        "SELECT company_id, list_level, market_price FROM bonds_catalog WHERE isin = 'RU000A1'"
    ).fetchone()
    check(row['company_id'] == id1, 'upsert каталога стёр company_id')
    check(row['list_level'] == 1, 'upsert каталога стёр list_level (обогащение MOEX)')
    check(row['market_price'] == 98.5, 'upsert каталога стёр market_price')

    # Новая облигация получает NULL company_id, автопривязка её подхватывает
    db.add_bonds_to_catalog([make_bond('RU000A4', 'Газпром БО-22')])
    total, linked_count = db.get_catalog_company_coverage()
    check((total, linked_count) == (4, 3), f'после вставки покрытие {linked_count}/{total}, ожидалось 3/4')
    linked_new, _ = link_bonds_to_companies(db, overrides=None, only_unlinked=True)
    check(linked_new == 1, f'автопривязка новой облигации: {linked_new}, ожидалась 1')
    total, linked_count = db.get_catalog_company_coverage()
    check((total, linked_count) == (4, 4), f'итоговое покрытие {linked_count}/{total}, ожидалось 4/4')

    # --- Overrides: слияние групп через config ---
    db2 = Database(':memory:')
    db2.add_bonds_to_catalog([
        make_bond('RU000B1', 'МФК Быстроденьги 002P-05'),
        make_bond('RU000B2', 'Быстроденьги 002P-08'),
    ])
    overrides = {'МФК Быстроденьги': 'Быстроденьги'}
    link_bonds_to_companies(db2, overrides=overrides)
    overview2 = db2.get_companies_overview()
    check(len(overview2) == 1 and overview2[0]['bonds_count'] == 2,
          f'overrides должны слить две группы в одну, получено: {overview2}')

    db.close()
    db2.close()

    if FAILURES:
        print(f"\n{len(FAILURES)} проверок провалено.")
        return 1
    print("OK: все тесты storage/привязки пройдены.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
