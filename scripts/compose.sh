#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

ENV_FILE=${ENV_FILE:-.env}
COMMAND=${1:-}
if [ "$#" -gt 0 ]; then
	shift
fi

check_env_file() {
	if [ ! -f "$ENV_FILE" ]; then
			printf 'Error: %s is missing. Run: cp .env.template .env\n' "$ENV_FILE" >&2
		return 1
	fi
}

load_mode() {
	check_env_file

	if [ "${TELEGRAM_ENABLED+x}" = x ]; then
		mode=$TELEGRAM_ENABLED
	else
		mode=$(awk -F= '
			/^[[:space:]]*TELEGRAM_ENABLED[[:space:]]*=/ {
				value = substr($0, index($0, "=") + 1)
				sub(/[[:space:]]+#.*$/, "", value)
				gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
				sub(/\r$/, "", value)
				if (value ~ /^".*"$/) value = substr(value, 2, length(value) - 2)
				if (value ~ /^\047.*\047$/) value = substr(value, 2, length(value) - 2)
				print value
				exit
			}' "$ENV_FILE")
	fi

	case "$mode" in
		true|false|'') ;;
		*)
			printf 'Error: TELEGRAM_ENABLED must be true or false (unset defaults to false).\n' >&2
			return 1
			;;
	esac
}

compose_base() {
	COMPOSE_PROFILES= docker compose --project-name bloc --env-file "$ENV_FILE" -f bot/docker-compose.yml "$@"
}

compose_mode() {
	if [ "$mode" = true ]; then
		compose_base --profile telegram "$@"
	else
		compose_base "$@"
	fi
}

compose_all() {
	compose_base --profile telegram "$@"
}

check_telegram_environment() {
	resolved_environment=$(compose_base config --environment)
	printf '%s\n' "$resolved_environment" | awk -F= '
		$1 == "TELEGRAM_BOT_TOKEN" {
			value = substr($0, index($0, "=") + 1)
			if (value ~ /[^[:space:]]/) token = 1
		}
		$1 == "TELEGRAM_ALLOWED_CHAT_IDS" {
			value = substr($0, index($0, "=") + 1)
			if (value ~ /[^[:space:]]/) chats = 1
		}
		END {
			if (!token) {
				print "Error: TELEGRAM_BOT_TOKEN is required when TELEGRAM_ENABLED=true."
				failed = 1
			}
			if (!chats) {
				print "Error: TELEGRAM_ALLOWED_CHAT_IDS is required when TELEGRAM_ENABLED=true."
				failed = 1
			}
			exit failed
		}'
	unset resolved_environment
}

check_mode_and_telegram() {
	load_mode
	if [ "$mode" = true ]; then
		check_telegram_environment
	fi
}

case "$COMMAND" in
	check-env)
		check_env_file
		;;
	config)
		check_mode_and_telegram
		compose_mode config --quiet
		;;
	build)
		check_mode_and_telegram
		if [ "$mode" = true ]; then
			compose_mode build
		fi
		;;
	start)
		check_mode_and_telegram
		if [ "$mode" = true ]; then
			compose_mode up -d --build
		else
			compose_all stop telegram
			compose_mode up -d --build opencode
		fi
		;;
	stop)
		check_env_file
		compose_all stop
		;;
	down)
		check_env_file
		compose_all down
		;;
	logs)
		load_mode
		status=0
		if [ -n "${1:-}" ]; then
			compose_mode logs -f "$1" || status=$?
		else
			compose_mode logs -f || status=$?
		fi
		[ "$status" -eq 0 ] || [ "$status" -eq 130 ]
		;;
	status)
		check_env_file
		compose_all ps
		;;
	health)
		check_env_file
		compose_base exec opencode wget -qO- http://127.0.0.1:4096/global/health
		;;
	repair)
		check_env_file
		docker compose --project-name bloc-repair --env-file "$ENV_FILE" -f bot-repair/docker-compose.yml run --rm repair
		;;
	clean)
		check_env_file
		compose_all down --volumes
		;;
	*)
		printf 'Usage: %s {check-env|config|build|start|stop|down|logs|status|health|repair|clean}\n' "$0" >&2
		exit 2
		;;
esac
