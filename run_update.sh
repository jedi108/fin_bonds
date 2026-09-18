#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$SCRIPT_DIR}"
LOG_FILE="$PROJECT_DIR/cron.log"

# Переходим в директорию с проектом. Если не удается - выходим из скрипта.
cd "$PROJECT_DIR" || { echo "Could not cd to $PROJECT_DIR" >> "$LOG_FILE"; exit 1; }

# Активируем виртуальное окружение
# Убедитесь, что путь к venv/bin/activate правильный
source "$PROJECT_DIR/venv/bin/activate"

# Поддержка доверенных сертификатов (в т.ч. Минцифры для Tinkoff/T-Bank API)
if [ -f /etc/ssl/certs/ca-certificates.crt ]; then
    export GRPC_DEFAULT_SSL_ROOTS_FILE_PATH="/etc/ssl/certs/ca-certificates.crt"
fi

# --- Логика запуска ---
# Добавляем временную метку в лог, чтобы разделять запуски
echo "==============================================" >> "$LOG_FILE"
echo "Starting update job at $(date)" >> "$LOG_FILE"
echo "==============================================" >> "$LOG_FILE"

# 1. Обновляем полный каталог облигаций и рыночные цены. Логируем результат.
echo "--- 1. Updating bonds catalog and market prices ---" >> "$LOG_FILE"
python main.py update-bonds --update-prices >> "$LOG_FILE" 2>&1

# 2. Рассчитываем спред для новых флоатеров в портфеле
echo "--- 2. Calculating spread for new floaters ---" >> "$LOG_FILE"
python main.py calculate-spread >> "$LOG_FILE" 2>&1

# 3. Обновляем рейтинги и другие отслеживаемые параметры. Логируем результат.
echo "--- 3. Updating ratings ---" >> "$LOG_FILE"
python main.py update-ratings >> "$LOG_FILE" 2>&1

# 4. Проверяем приближающиеся оферты для флоатеров. Логируем результат.
echo "--- 4. Checking for upcoming floater offers ---" >> "$LOG_FILE"
python main.py check-offers >> "$LOG_FILE" 2>&1

# 5. Проверяем изменения и отправляем уведомления, если они есть. Логируем результат.
# echo "--- 5. Checking for changes ---" >> "$LOG_FILE"
# python main.py check_changes >> "$LOG_FILE" 2>&1

echo "Update job finished at $(date)" >> "$LOG_FILE"
echo "" >> "$LOG_FILE" 