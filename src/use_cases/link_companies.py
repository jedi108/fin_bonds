#!/usr/bin/env python3
"""
Use case привязки облигаций каталога к компаниям-эмитентам (юрлицам).

Схема связи «одна компания — много облигаций»: bonds_catalog.company_id ->
companies.id. Имя компании определяется нормализацией названия выпуска
(src/company_normalization.py) с ручными переопределениями из config.yaml.
Индивидуальные ИНН физлиц-эмитентов не хранятся (персональные данные).
"""
import argparse
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from src.company_normalization import TYPE_INDIVIDUAL, resolve_company
from src.use_cases.base import UseCase
from src.storage import PortfolioStorage

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


def link_bonds_to_companies(
    db: PortfolioStorage,
    overrides: Optional[Dict[str, Any]],
    inn_by_isin: Optional[Dict[str, str]] = None,
    only_unlinked: bool = True
) -> Tuple[int, int]:
    """
    Привязывает облигации к компаниям: нормализует названия, создаёт
    недостающие компании, проставляет bonds_catalog.company_id.
    Возвращает (привязано облигаций, затронуто компаний).

    Используется use case'ом link-companies и автопривязкой в update-bonds.
    """
    bonds = db.get_bonds_for_company_linking(only_unlinked=only_unlinked)
    if not bonds:
        return 0, 0

    company_ids: Dict[Tuple[str, str], int] = {}
    links: List[Tuple[str, int]] = []
    for isin, name in bonds:
        ref = resolve_company(name, overrides)
        inn = inn_by_isin.get(isin) if inn_by_isin else None
        if ref.entity_type == TYPE_INDIVIDUAL:
            inn = None  # ИНН физлиц не храним
        key = (ref.name.lower(), ref.entity_type)
        if key not in company_ids:
            company_ids[key] = db.upsert_company(
                name=ref.name, entity_type=ref.entity_type, inn=inn
            )
        links.append((isin, company_ids[key]))

    linked = db.link_bonds_to_companies(links)
    logger.info(
        f"Привязано облигаций к компаниям: {linked} (компаний затронуто: {len(company_ids)})."
    )
    return linked, len(company_ids)


class LinkCompaniesUseCase(UseCase):
    """
    Привязка облигаций к компаниям-эмитентам: backfill и отчёт по группировке.
    """

    def __init__(self, factory: 'UseCaseFactory'):
        super().__init__(factory)
        self.db: PortfolioStorage = self.factory.get_db_connection()
        self.config = self.factory.config

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--report', action='store_true',
            help='Только показать группировку по компаниям, ничего не записывая'
        )
        parser.add_argument(
            '--all', action='store_true',
            help='Перепривязать все облигации (по умолчанию — только без компании)'
        )
        parser.add_argument(
            '--top', type=int, default=30,
            help='Сколько крупнейших компаний показать в отчёте (по умолчанию 30)'
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'LinkCompaniesUseCase':
        """Создает экземпляр use case с зависимостями из фабрики."""
        return cls(factory)

    def execute(self, args: argparse.Namespace):
        overrides = self.config.get('company_overrides') or {}
        if args.report:
            self._print_grouping_report(overrides, top=args.top)
            return

        only_unlinked = not args.all
        bonds_before = self.db.get_bonds_for_company_linking(only_unlinked=True)
        logger.info(
            f"Облигаций без компании: {len(bonds_before)} "
            f"(режим: {'только непривязанные' if only_unlinked else 'перепривязка всех'})."
        )

        linked, companies = link_bonds_to_companies(
            self.db, overrides, only_unlinked=only_unlinked
        )
        logger.info(f"Готово: привязано {linked} облигаций, компаний затронуто {companies}.")

        self._print_coverage()
        self._print_overview(top=args.top)

    def _print_grouping_report(self, overrides: Dict[str, Any], top: int):
        """Показывает, как сгруппируются облигации, без записи в БД."""
        bonds = self.db.get_bonds_for_company_linking(only_unlinked=False)
        groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        for isin, name in bonds:
            ref = resolve_company(name, overrides)
            groups[(ref.name, ref.entity_type)].append(name)

        logger.info(
            f"Отчёт по группировке: {len(bonds)} облигаций -> {len(groups)} компаний."
        )
        for (name, entity_type), members in sorted(
                groups.items(), key=lambda kv: -len(kv[1]))[:top]:
            examples = ', '.join(sorted(set(members))[:3])
            logger.info(f"  [{entity_type:<10}] {name:<40} облигаций: {len(members)}")
            logger.info(f"      примеры: {examples}")
        if len(groups) > top:
            logger.info(f"  ... и ещё {len(groups) - top} компаний")

    def _print_coverage(self):
        total, linked = self.db.get_catalog_company_coverage()
        percent = (linked / total * 100) if total else 0.0
        logger.info(f"Покрытие каталога: {linked}/{total} облигаций ({percent:.1f}%).")

    def _print_overview(self, top: int):
        overview = self.db.get_companies_overview()
        logger.info(f"Всего компаний в справочнике: {len(overview)}. Крупнейшие:")
        for row in overview[:top]:
            logger.info(
                f"  [{row['entity_type']:<10}] {row['name']:<40} облигаций: {row['bonds_count']}"
            )
