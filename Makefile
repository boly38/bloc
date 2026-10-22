SHELL := /bin/sh

ENV_FILE ?= .env
SERVICE ?=
BACKUP_DIR ?= backups
export ENV_FILE

COMPOSE_SCRIPT := ./scripts/compose.sh

.PHONY: help test diagnostics format-test telegram-chat-id backup restore check-env config build \
	start stop down restart logs status health tui remote-tui repair clean

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "%-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

test: ## Run dependency-free unit tests
	@python3 -m unittest discover -s tests -v

diagnostics: ## Print sanitized Telegram cost diagnostics from the running bridge
	@$(COMPOSE_SCRIPT) diagnostics

format-test: ## Send a static Markdown formatting fixture; usage: make format-test CHAT_ID=<allowed-id>
	@$(COMPOSE_SCRIPT) format-test "$(CHAT_ID)"

telegram-chat-id: ## Safely discover the first Telegram chat ID without starting the bridge
	@ENV_FILE="$(ENV_FILE)" python3 scripts/telegram_chat_id.py

backup: ## Back up persistent volumes under backups/; override with BACKUP_DIR=<directory>
	@$(COMPOSE_SCRIPT) backup "$(BACKUP_DIR)"

restore: ## Restore from the latest backup in backups/ or a specific BACKUP_DIR
	@$(COMPOSE_SCRIPT) restore "$(BACKUP_DIR)"

check-env: ## Check that the environment file exists
	@$(COMPOSE_SCRIPT) check-env

config: ## Validate the Compose configuration for the current mode
	@$(COMPOSE_SCRIPT) config

build: ## Build the OpenCode image and enabled services
	@$(COMPOSE_SCRIPT) build

start: ## Start OpenCode alone or with Telegram according to TELEGRAM_ENABLED
	@$(COMPOSE_SCRIPT) start

stop: ## Stop containers without removing resources
	@$(COMPOSE_SCRIPT) stop

down: ## Stop and remove containers (keep volumes)
	@$(COMPOSE_SCRIPT) down

restart: stop start ## Restart services according to the configuration

logs: ## Follow logs; usage: make logs or make logs SERVICE=telegram
	@$(COMPOSE_SCRIPT) logs "$(SERVICE)"

status: ## Show container status
	@$(COMPOSE_SCRIPT) status

health: ## Check the OpenCode server health
	@$(COMPOSE_SCRIPT) health

tui: ## Open the OpenCode TUI on the running server (requires Docker access)
	@$(COMPOSE_SCRIPT) tui

remote-tui: ## List remote containers or attach to OpenCode; usage: make remote-tui HOST=<ssh-host> [CONTAINER_ID=<id>]
	@$(COMPOSE_SCRIPT) remote-tui "$(HOST)" "$(CONTAINER_ID)"

repair: ## Open the independent repair container
	@$(COMPOSE_SCRIPT) repair

clean: ## Remove the project's containers, networks, and volumes (destructive)
	@$(COMPOSE_SCRIPT) clean
