import logging
import pandas as pd
import argparse
from typing import TYPE_CHECKING, List
from decimal import Decimal
from dataclasses import dataclass
from tinkoff.invest import PortfolioPosition as TinkoffPortfolioPosition

from src.tbank.api_client import TbankApiClient
from src.data_models import (
    InstrumentInfo, 
    PortfolioPosition as DataModelPortfolioPosition
)
from tinkoff.invest.utils import quotation_to_decimal
from src.use_cases.base import UseCase

if TYPE_CHECKING:
    from src.use_cases.factory import UseCaseFactory

logger = logging.getLogger(__name__)


class SyncPortfolioUseCase(UseCase):
    """
    Синхронизирует портфель из всех источников (TBank API, Excel) и сохраняет в БД.
    """
    def __init__(self, factory: 'UseCaseFactory'):
        super().__init__(factory)
        self.db = factory.get_db_connection()
        self.tbank_api_client = factory.get_tbank_api_client()
        # self.moex_api_client = factory.get_moex_api_client() # Добавим позже

    @staticmethod
    def setup_parser(parser: argparse.ArgumentParser):
        parser.add_argument(
            '--source',
            type=str,
            default='all',
            choices=['all', 'tbank', 'excel'],
            help="Источник данных для синхронизации: 'tbank', 'excel' или 'all' (по умолчанию)."
        )
        parser.add_argument(
            '--excel_path',
            type=str,
            default=None,
            help="Путь к файлу Excel. Обязателен, если source='excel' или 'all'."
        )
        parser.add_argument(
            '--upsert',
            action='store_true',
            help="Добавлять/обновлять бумаги, не удаляя остальные (upsert-режим)."
        )

    @classmethod
    def create(cls, factory: 'UseCaseFactory') -> 'SyncPortfolioUseCase':
        return cls(factory)

    def execute(self, args: argparse.Namespace):
        logger.info(f"Запуск синхронизации портфеля (источник: {args.source})...")

        positions_to_save = []
        # Задача 022: cash — часть того же canonical snapshot, обновляется тем же
        # запуском. None = cash sync не выполнялся (источник без TBank),
        # True/False = результат; failure не подменяет прошлый cash нулём.
        cash_synced: bool = False
        cash_sync_ran: bool = False

        if args.source in ['all', 'tbank']:
            positions_to_save.extend(self._fetch_from_tbank())
            # Cash синхронизируется даже при пустом списке позиций
            # (портфель «только деньги» — именно тот случай, где cash важен).
            cash_sync_ran = True
            cash_synced = self._sync_tbank_cash()

        # Если источник 'excel' - путь обязателен.
        if args.source == 'excel' and not args.excel_path:
            logger.error("При --source='excel' необходимо указать путь через --excel_path.")
            return

        # Итог по cash — явный, независимо от наличия позиций (задача 022):
        # при failure последний успешный cash остаётся в БД, rebalance-report
        # покажет устаревший cash_updated_at вместо подмены нулём.
        if cash_sync_ran:
            if cash_synced:
                logger.info("Cash sync: RUB cash обновлён тем же запуском, что и позиции.")
            else:
                logger.warning(
                    "Cash sync FAILED: RUB cash НЕ обновлён (ошибка получения). "
                    "В БД остался последний успешный снапшот; cash_available_rub "
                    "может быть устаревшим — проверьте updated_at в rebalance-report."
                )

        # Если путь указан, и источник 'all' или 'excel', добавляем позиции из файла.
        if args.excel_path and args.source in ['all', 'excel']:
            positions_to_save.extend(self._fetch_from_excel(args.excel_path))

        if not positions_to_save:
            logger.warning("Не найдено ни одной позиции для сохранения. Синхронизация завершена.")
            return

        # Гарантируем ненулевой current_value для всех позиций
        for p in positions_to_save:
            if not p.current_value or p.current_value <= Decimal(0):
                qty = p.quantity or Decimal(0)
                px  = p.current_price or Decimal(0)
                p.current_value = qty * px

        # Получаем старый портфель для сравнения (до изменений)
        old_positions = self.db.get_portfolio_positions()
        old_keys = set((p.isin, p.broker_name, p.account_id) for p in old_positions)
        new_keys = set((p.isin, p.broker_name, p.account_id) for p in positions_to_save)

        if args.upsert:
            logger.info(f"Upsert-режим: добавляем/обновляем {len(positions_to_save)} позиций, не удаляя остальные.")
            self.db.add_portfolio_positions(positions_to_save)
        else:
            # Определяем, какие позиции очищать
            if args.source == 'all':
                logger.info("Очищаем все позиции портфеля (режим all, без upsert)...")
                self.db.clear_portfolio_positions()
            else:
                # Очищаем только позиции соответствующего брокера
                if args.source == 'tbank':
                    broker = 'TBank'
                else:
                    broker = None
                if broker:
                    logger.info(f"Очищаем только позиции брокера {broker}...")
                    self.db.clear_portfolio_positions_by_broker(broker)
                else:
                    logger.info("Очищаем все позиции (не удалось определить брокера)...")
                    self.db.clear_portfolio_positions()
            self.db.add_portfolio_positions(positions_to_save)
            # После очистки и добавления — сравниваем, что исчезло
            updated_positions = self.db.get_portfolio_positions()
            updated_keys = set((p.isin, p.broker_name, p.account_id) for p in updated_positions)
            disappeared = old_keys - updated_keys
            if disappeared:
                logger.warning(f"ВНИМАНИЕ: Следующие бумаги исчезли из портфеля после синхронизации (были, но не пришли из источника):")
                for isin, broker, account_id in disappeared:
                    logger.warning(f"  ISIN: {isin}, broker: {broker}, account: {account_id}")

        # Задача 002.1: чистка зомби-позиций при синке — не оставлять активные
        # строки с нулевой оценкой. Погашенные бумаги (maturity_date <
        # CURRENT_DATE) помечаются status='matured' (кейс МОНОП 1P02: 35 шт
        # после погашения 2025-12-04), отсутствующие у брокера (только upsert:
        # в полном режиме их уже удалила очистка) и нулевые количества —
        # status='closed'. Позиции с ненулевым количеством и нулевой оценкой на
        # живых бумагах (кейс ГлобалФ: цена 0 от брокера) НЕ трогаем — это не
        # ошибка синка, их учитывает гейт свежести (WARN_ZOMBIE_ROWS).
        _source_brokers = {'tbank': ['TBank']}
        # Проверка «отсутствует у брокера» осмысленна только в upsert-режиме по
        # источникам, полноценно покрывающим свои счета (API); для 'all' ответ
        # покрывает TBank (Excel, если подключён, полноты файла не гарантирует).
        absence_known = args.upsert and args.source != 'excel'
        closed = self.db.mark_closed_positions(
            keep_keys={(p.isin, p.broker_name, p.account_id or '') for p in positions_to_save} if absence_known else None,
            brokers=_source_brokers.get(args.source) if absence_known else None,
        )
        matured = self.db.mark_matured_positions()
        if closed:
            logger.warning("Зомби-позиции помечены status='closed' при синке:")
            for row in closed:
                logger.warning(
                    f"  ISIN: {row['isin']}, broker: {row['broker_name']}, "
                    f"account: {row['account_id'] or ''}, qty: {row['quantity']} "
                    f"(причина: {row['reason']})"
                )
        if matured:
            logger.warning("Погашенные бумаги помечены status='matured' при синке:")
            for row in matured:
                logger.warning(
                    f"  ISIN: {row['isin']}, broker: {row['broker_name']}, "
                    f"account: {row['account_id'] or ''}, qty: {row['quantity']}"
                )
        if closed or matured:
            logger.info(
                f"Чистка зомби при синке: closed={len(closed)}, matured={len(matured)}. "
                f"Ненулевые позиции с нулевой оценкой на живых бумагах не тронуты "
                f"(гейт: WARN_ZOMBIE_ROWS); разовая чистка — cleanup-portfolio."
            )

        logger.info(f"Проверка: Фактически в БД сохранено {len(self.db.get_portfolio_positions())} позиций.")
        logger.info(f"Синхронизация портфеля успешно завершена. Сохранено позиций: {len(positions_to_save)}")

    def _fetch_from_tbank(self) -> List[DataModelPortfolioPosition]:
        """Получает позиции из TBank API."""
        logger.info("Шаг 1: Получение позиций из TBank API...")
        try:
            positions = self.tbank_api_client.get_portfolio_positions()
            logger.info(f"Получено {len(positions)} позиций из TBank.")
            return positions
        except ValueError:
            # Strict preflight счетов (029-T01, общий helper в TbankApiClient):
            # missing one-of-many configured account -> fail-fast ДО clear/add
            # TBank positions, частичный snapshot не публикуется.
            raise
        except Exception as e:
            logger.error(f"Не удалось получить портфель из TBank API: {e}", exc_info=True)
            return []

    def _sync_tbank_cash(self) -> bool:
        """Синхронизирует денежные позиции (cash) TBank в БД (задача 022).

        Поток: TBank API (OperationsService GetPositions) -> PostgreSQL ->
        rebalance-report. Broker API из отчёта не вызывается.

        Правила:
        - strict preflight счетов (029-T01): missing one-of-many configured
          account -> ValueError пробрасывается наверх, sync прерывается ДО
          записи cash и ДО clear/add positions (частичный snapshot не публикуется);
        - запись только при успешном ответе: API error -> False, строки в БД
          не трогаются (последний успешный cash остаётся, API failure != 0 RUB);
        - пустой ответ тоже не затирает прошлый снапшот (подозрителен);
        - публикация атомарна (029-T03): upsert актуальных строк и prune
          счетов вне configured set — одна DB transaction
          (replace_cash_balances_snapshot), при ошибке ROLLBACK оставляет
          прошлый успешный snapshot, cash_updated_at частично не продвигается;
        - после synthetic zero RUB rows (029-T02) + атомарной публикации
          продвижение агрегированного cash_updated_at — proof нового полного
          TBank RUB snapshot (current-cash guard задачи T14).
        """
        logger.info("Шаг: синхронизация RUB cash из TBank API (GetPositions)...")
        try:
            balances = self.tbank_api_client.get_money_positions()
        except ValueError:
            # 029-T01: strict preflight configured-accounts — не глотать в
            # «cash не обновлён»: прерываем sync целиком, пока ничего не записано.
            raise
        except Exception as e:
            logger.error(f"Не удалось получить cash из TBank API: {e}", exc_info=True)
            return False
        if not balances:
            logger.warning(
                "TBank не вернул денежных позиций (пустой money/blocked) — "
                "cash в БД не изменён, прошлый снапшот не затёрт."
            )
            return False
        try:
            written = self.db.replace_cash_balances_snapshot(
                'TBank', balances, {b['account_id'] for b in balances}
            )
        except Exception as e:
            logger.error(f"Не удалось сохранить cash в PostgreSQL: {e}", exc_info=True)
            return False
        rub_rows = [b for b in balances if str(b['currency']).lower() == 'rub']
        rub_total = sum((b['available'] for b in rub_rows), Decimal(0))
        logger.info(
            f"Cash сохранён в PostgreSQL: строк {written} "
            f"(RUB {len(rub_rows)}, суммарно available RUB = {rub_total})."
        )
        return True

    def _fetch_from_excel(self, excel_path: str) -> List[DataModelPortfolioPosition]:
        """Загружает, обогащает и обрабатывает позиции из Excel файла."""
        logger.info(f"Шаг 2: Загрузка и обработка позиций из Excel файла: {excel_path}")
        try:
            df = pd.read_excel(excel_path, sheet_name='Лист1')

            required_cols = {'ticker', 'broker_name', 'quantity', 'average_price'}
            if not required_cols.issubset(df.columns):
                missing_cols = required_cols - set(df.columns)
                logger.error(f"В Excel-файле отсутствуют обязательные колонки: {missing_cols}")
                return []
            
            # --- Обогащение данных ---
            tickers_to_enrich = df['ticker'].unique().tolist()
            logger.info(f"Начинаем обогащение {len(tickers_to_enrich)} позиций из Excel данными из TBank API...")
            
            enriched_data = self.tbank_api_client.enrich_bonds_by_tickers(tickers_to_enrich)

            if enriched_data:
                enriched_df = pd.DataFrame(enriched_data)
                # Объединяем с исходным DataFrame по тикеру
                df = pd.merge(df, enriched_df, on='ticker', how='left')
            # ------------------------------------
            
            # Отфильтровываем строки, где isin не был найден после обогащения
            if 'isin' in df.columns:
                df.dropna(subset=['isin'], inplace=True)
            else:
                # Если колонка isin так и не появилась, значит ни одна позиция не была обогащена
                logger.warning("Ни одна позиция из Excel не была обогащена данными TBank API. Isin не найдены.")
                return [] # Возвращаем пустой список

            positions = self._dataframe_to_dto_list(df)
            logger.info(f"Получено и обогащено {len(positions)} позиций из Excel.")
            return positions
        except FileNotFoundError:
            logger.error(f"Excel-файл не найден по пути: {excel_path}")
            return []
        except Exception as e:
            logger.error(f"Ошибка при обработке Excel файла: {e}", exc_info=True)
            return []

    def _merge_positions(self, list1: List, list2: List) -> List:
        """Объединяет списки позиций. (Пока что простое сложение)."""
        # В будущем здесь может быть более сложная логика слияния по ISIN
        return list1 + list2

    def _dataframe_to_dto_list(self, df: pd.DataFrame) -> List[DataModelPortfolioPosition]:
        """Конвертирует DataFrame в список DTO PortfolioPosition."""
        positions = []
        for _, row in df.iterrows():
            # Заполняем недостающие данные значениями по умолчанию
            row = row.where(pd.notnull(row), None)
            try:
                _qty = Decimal(str(row.get('quantity', 0)))
                _px  = Decimal(str(row.get('current_price', 0)))
                positions.append(DataModelPortfolioPosition(
                    isin=row.get('isin'),
                    ticker=row.get('ticker'),
                    name=row.get('name'),
                    broker_name=row.get('broker_name'),
                    quantity=_qty,
                    average_price=Decimal(str(row.get('average_price', 0))),
                    current_price=_px,
                    yield_to_maturity=None,  # Пока не рассчитываем YTM
                    portfolio_percent=Decimal(0),  # Будет рассчитано позже
                    current_value=_qty * _px,  # qty * current_price в рублях за 1 шт.
                ))
            except Exception as e:
                logger.error(f"Ошибка конвертации строки для isin {row.get('isin')}: {e}")
        return positions 

@dataclass
class PortfolioPosition(DataModelPortfolioPosition):
    # ... (возможно, какие-то переопределения, если они нужны)
    
    @classmethod
    def from_tinkoff_api(
        cls, 
        position: TinkoffPortfolioPosition, 
        instrument_info: InstrumentInfo
    ) -> "PortfolioPosition":
        """Фабричный метод для создания DTO из комбинации данных TBank."""
        
        return cls(
            isin=instrument_info.isin,
            figi=instrument_info.figi,
            ticker=instrument_info.ticker,
            name=instrument_info.name,
            quantity=quotation_to_decimal(position.quantity),
            average_price=quotation_to_decimal(position.average_position_price),
            current_price=quotation_to_decimal(position.current_price),
            currency=instrument_info.currency,
            yield_to_maturity=None,  # expected_yield - это ₽ брокера, а не YTM%
            broker_name="TBank",
            instrument_type=instrument_info.instrument_type
        ) 