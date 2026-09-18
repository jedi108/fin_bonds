import argparse
import logging
import sys # Импортируем sys для прямой печати в stderr

from src.use_cases.factory import UseCaseFactory
from src.utils import setup_logging
# from src.use_cases.sync_offers_to_calendar import SyncOffersToCalendarUseCase # Удаляем, если не используется напрямую

# Настройка логирования - вызываем только один раз
setup_logging()
logger = logging.getLogger(__name__)


def main():
    # Удаляем logging.basicConfig здесь, так как setup_logging уже вызван
    # logging.basicConfig(
    #     level=logging.INFO,
    #     format='%(asctime)s - %(levelname)s - %(module)s - %(message)s',
    #     handlers=[
    #         logging.StreamHandler()
    #     ]
    # )

    # Создание фабрики, которая будет поставлять зависимости
    factory = UseCaseFactory()

    # Основной парсер
    parser = argparse.ArgumentParser(description="CLI для управления портфелем облигаций.")
    parser.add_argument('-v', '--verbose', action='store_true', help='Включить подробное логирование (уровень DEBUG).')
    subparsers = parser.add_subparsers(dest='command', help='Доступные команды', required=True)

    # Получаем карту команд и регистрируем их
    use_case_map = factory.get_use_case_map()

    for command, use_case_class in use_case_map.items():
        subparser = subparsers.add_parser(command, help=use_case_class.__doc__)
        use_case_class.setup_parser(subparser)
        subparser.set_defaults(command=command)

    try:
        args = parser.parse_args()
        
        # Если указан флаг -v или команда является тестом, перенастраиваем логирование на уровень DEBUG
        if args.verbose or (args.command and args.command.startswith('test-')):
            logging.getLogger().setLevel(logging.DEBUG)
            # Перенастраиваем с новым уровнем
            # setup_logging(level=logging.DEBUG) # setup_logging уже настраивает StreamHandler
            logger.debug("Включен режим подробного логирования для тестовой команды.")
        
        # Debugging: Print args before execution directly to stderr
        print(f"DEBUG: Parsed arguments: {args}", file=sys.stderr) # <<< ДОБАВЛЕНА/ИЗМЕНЕНА ЭТА СТРОКА

        # Получаем и выполняем выбранный use case
        use_case_instance = factory.get_use_case(args.command)
        use_case_instance.execute(args)
        
        logger.info(f"Команда '{args.command}' успешно выполнена.")

    except Exception as e:
        logger.error(f"Произошла ошибка при выполнении команды: {e}", exc_info=True)
    finally:
        factory.close_db()


if __name__ == '__main__':
    main()