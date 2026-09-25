ENV_DIR ?= ./workspace/fin_bonds/env
export SOPS_CONFIG ?= $(ENV_DIR)/.sops.yaml

# Загружаем переопределения из .env.deploy, если файл существует
-include $(ENV_DIR)/.env.deploy

DEPLOY_HOST     ?= your-server
DEPLOY_USER     ?= app
DEPLOY_DIR      ?= /home/$(DEPLOY_USER)/prod/fin_bonds
DEPLOY_SERVICE  ?= fin_bonds
DEPLOY_ENC_FILE ?= $(ENV_DIR)/.env.prod.enc
DEPLOY_CONF_DIR ?= ./workspace/deploy
SKILLS_DIR ?= ./workspace/fin_bonds/skills_hermes
VENV_PY ?= $(shell test -x ./venv/bin/python && echo ./venv/bin/python || echo python3)

.PHONY: help deploy deploy-init env-push sops-edit sops-decrypt sops-encrypt run-remote logs status ssh timer-install timer-status check-deploy-env validate-skills hooks-install migrate-remote

check-deploy-env:
	@if [ "$(DEPLOY_HOST)" = "your-server" ] || [ -z "$(DEPLOY_HOST)" ]; then \
		echo "❌ Ошибка: укажите параметры сервера в $(ENV_DIR)/.env.deploy (DEPLOY_HOST, DEPLOY_USER, DEPLOY_DIR)"; \
		exit 1; \
	fi

help:
	@echo "Доступные команды:"
	@echo "  make deploy         - Синхронизировать код на $(DEPLOY_HOST) и доставить .env"
	@echo "  make deploy-init    - Первичная инициализация каталога и venv на сервере"
	@echo "  make env-push       - Доставить расшифрованный .env на сервер через SSH pipe"
	@echo "  make sops-edit      - Редактировать $(DEPLOY_ENC_FILE) через SOPS"
	@echo "  make sops-decrypt   - Расшифровать $(DEPLOY_ENC_FILE) в локальный .env"
	@echo "  make sops-encrypt   - Зашифровать локальный .env в $(DEPLOY_ENC_FILE)"
	@echo "  make run-remote     - Запустить run_update.sh на сервере"
	@echo "  make logs           - Смотреть логи (cron.log) на сервере"
	@echo "  make status         - Проверить статус таймера/сервиса на сервере"
	@echo "  make ssh            - Войти по SSH в директорию проекта на сервере"
	@echo "  make timer-install  - Установить systemd service и timer на сервере"
	@echo "  make timer-status   - Проверить статус systemd таймера"
	@echo "  make migrate-remote - Применить миграции БД на сервере (PostgreSQL; идемпотентно)"
	@echo "  make validate-skills - Валидация frontmatter скиллов HERMES (skills_hermes)"
	@echo "  make hooks-install  - Установить pre-commit hook валидации скиллов в сабмодуль workspace"

# Первичная инициализация на сервере
deploy-init:
	@echo "==> Инициализация каталога $(DEPLOY_DIR) на $(DEPLOY_HOST)..."
	ssh $(DEPLOY_USER)@$(DEPLOY_HOST) "mkdir -p $(DEPLOY_DIR)/data $(DEPLOY_DIR)/plots"
	@echo "==> Создание виртуального окружения python venv..."
	ssh $(DEPLOY_USER)@$(DEPLOY_HOST) "test -f $(DEPLOY_DIR)/venv/bin/pip || (rm -rf $(DEPLOY_DIR)/venv && python3 -m venv $(DEPLOY_DIR)/venv)"
	@echo "==> Успешно инициализировано!"

# Доставка секретов на сервер через SSH pipe (приватный ключ age не передается на сервер)
env-push:
	@echo "==> Доставка .env на $(DEPLOY_HOST):$(DEPLOY_DIR)/.env..."
	@sops --config $(SOPS_CONFIG) --input-type dotenv --output-type dotenv -d $(DEPLOY_ENC_FILE) | \
		ssh $(DEPLOY_USER)@$(DEPLOY_HOST) "cat > $(DEPLOY_DIR)/.env && chmod 600 $(DEPLOY_DIR)/.env"
	@echo "==> Секреты успешно доставлены (права 600)!"

# Редактирование секретов на лету
sops-edit:
	sops --config $(SOPS_CONFIG) --input-type dotenv --output-type dotenv $(DEPLOY_ENC_FILE)

# Расшифровка в локальный .env
sops-decrypt:
	@sops --config $(SOPS_CONFIG) --input-type dotenv --output-type dotenv -d $(DEPLOY_ENC_FILE) > .env
	@chmod 600 .env
	@echo "==> Локальный .env обновлен из $(DEPLOY_ENC_FILE)"

# Шифрование локального .env в prod
sops-encrypt:
	@sops --config $(SOPS_CONFIG) --input-type dotenv --output-type dotenv --encrypt .env > $(DEPLOY_ENC_FILE)
	@echo "==> $(DEPLOY_ENC_FILE) успешно зашифрован из .env"

# Полный деплой
deploy: deploy-init env-push
	@echo "==> Синхронизация файлов проекта на $(DEPLOY_HOST)..."
	rsync -avz --delete \
		--exclude '.git/' \
		--exclude '.gitmodules' \
		--exclude 'workspace/' \
		--exclude 'venv/' \
		--exclude '.venv/' \
		--exclude '__pycache__/' \
		--exclude '*.pyc' \
		--exclude 'node_modules/' \
		--exclude 'package-lock.json' \
		--exclude '.env*' \
		--exclude 'data/*.db' \
		--exclude 'data/*.log' \
		--exclude 'cron.log' \
		--exclude '*.log' \
		--exclude '.DS_Store' \
		--exclude '.idea/' \
		--exclude '.vscode/' \
		--exclude '.cursor/' \
		--exclude '.crush/' \
		--exclude '_output_/' \
		./ $(DEPLOY_USER)@$(DEPLOY_HOST):$(DEPLOY_DIR)/
	@echo "==> Обновление зависимостей pip..."
	ssh $(DEPLOY_USER)@$(DEPLOY_HOST) "$(DEPLOY_DIR)/venv/bin/pip install --upgrade pip && $(DEPLOY_DIR)/venv/bin/pip install -r $(DEPLOY_DIR)/requirements.txt && $(DEPLOY_DIR)/venv/bin/pip install --no-deps 'git+https://github.com/RussianInvestments/invest-python.git'"
	ssh $(DEPLOY_USER)@$(DEPLOY_HOST) "chmod +x $(DEPLOY_DIR)/run_update.sh"
	@$(MAKE) --no-print-directory migrate-remote
	@echo "==> Деплой успешно завершен!"

# Применение миграций БД на сервере. Идемпотентно; на сервере без
# PostgreSQL (POSTGRES_DSN пуст в .env) шаг пропускается с сообщением.
migrate-remote:
	@echo "==> Применение миграций БД на $(DEPLOY_HOST)..."
	@ssh $(DEPLOY_USER)@$(DEPLOY_HOST) "cd $(DEPLOY_DIR) && set -a && . ./.env && set +a && \
		if [ -n \"\$${POSTGRES_DSN:-}\" ]; then \
			venv/bin/python main.py migrate-db; \
		else \
			echo 'POSTGRES_DSN не задан - миграции пропущены (SQLite).'; \
		fi"

# Валидация frontmatter скиллов HERMES перед коммитом/деплоем
validate-skills:
	@test -d $(SKILLS_DIR) || { echo "Ошибка: нет $(SKILLS_DIR) (инициализируйте сабмодуль: git submodule update --init workspace)"; exit 1; }
	$(VENV_PY) $(SKILLS_DIR)/scripts/validate_skills.py

# Однократная установка pre-commit hook валидации скиллов в сабмодуль fin_tasks
hooks-install:
	@test -d workspace/.git -o -f workspace/.git || { echo "Ошибка: сабмодуль workspace не инициализирован"; exit 1; }
	git -C workspace config core.hooksPath fin_bonds/skills_hermes/hooks
	@echo "==> pre-commit hook установлен (git -C workspace config core.hooksPath)"

# Ручной запуск обновления на сервере
run-remote:
	@echo "==> Запуск run_update.sh на $(DEPLOY_HOST)..."
	ssh $(DEPLOY_USER)@$(DEPLOY_HOST) "$(DEPLOY_DIR)/run_update.sh"

# Просмотр логов
logs:
	ssh $(DEPLOY_USER)@$(DEPLOY_HOST) "tail -f -n 100 $(DEPLOY_DIR)/cron.log 2>/dev/null || journalctl -u $(DEPLOY_SERVICE) -f -n 100"

# Статус
status:
	ssh $(DEPLOY_USER)@$(DEPLOY_HOST) "echo '=== Directory status ===' && ls -la $(DEPLOY_DIR) && echo '=== Systemd Timer ===' && (systemctl list-timers | grep $(DEPLOY_SERVICE) || true)"

# Интерактивный вход по SSH в каталог проекта
ssh:
	ssh -t $(DEPLOY_USER)@$(DEPLOY_HOST) "cd $(DEPLOY_DIR) && exec bash -l"

# Установка systemd service и timer
timer-install:
	@echo "==> Установка systemd юнитов на $(DEPLOY_HOST)..."
	scp $(DEPLOY_CONF_DIR)/$(DEPLOY_SERVICE).service $(DEPLOY_USER)@$(DEPLOY_HOST):/tmp/$(DEPLOY_SERVICE).service
	scp $(DEPLOY_CONF_DIR)/$(DEPLOY_SERVICE).timer $(DEPLOY_USER)@$(DEPLOY_HOST):/tmp/$(DEPLOY_SERVICE).timer
	ssh $(DEPLOY_USER)@$(DEPLOY_HOST) "sudo mv /tmp/$(DEPLOY_SERVICE).service /etc/systemd/system/ && sudo mv /tmp/$(DEPLOY_SERVICE).timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now $(DEPLOY_SERVICE).timer"
	@echo "==> Таймер успешно установлен и активирован!"

timer-status:
	ssh $(DEPLOY_USER)@$(DEPLOY_HOST) "systemctl status $(DEPLOY_SERVICE).timer"
