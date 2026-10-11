SHELL := /bin/sh

ENV_FILE ?= .env
SERVICE ?=
export ENV_FILE

COMPOSE_SCRIPT := ./scripts/compose.sh

.PHONY: help test diagnostics check-env config build start stop down restart logs status health repair clean

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "%-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

test: ## Run dependency-free unit tests
	@python3 -m unittest discover -s tests -v

diagnostics: ## Print sanitized Telegram cost diagnostics from the running bridge
	@$(COMPOSE_SCRIPT) diagnostics

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

repair: ## Open the independent repair container
	@$(COMPOSE_SCRIPT) repair

clean: ## Remove the project's containers, networks, and volumes (destructive)
	@$(COMPOSE_SCRIPT) clean
