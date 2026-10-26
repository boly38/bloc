#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMMAND=${1:-}
BACKUP_DIR=${2:-backups}

case "$BACKUP_DIR" in
	/*) ;;
	*) BACKUP_DIR="$ROOT_DIR/$BACKUP_DIR" ;;
esac

VOLUMES="opencode-data workspace telegram-data"
BLOC_PROJECT_NAME=${BLOC_PROJECT_NAME:-bloc-v2}
VOLUME_PREFIX=${BLOC_PROJECT_NAME}_
BACKUP_FORMAT_VERSION=2
ARCHIVE_IMAGE=busybox:1.37.0

case "$BLOC_PROJECT_NAME" in
	''|[!a-z0-9]*|*[!a-z0-9_-]*)
		printf 'Error: BLOC_PROJECT_NAME must start with a lowercase letter or digit and contain only lowercase letters, digits, hyphens, or underscores.\n' >&2
		exit 2
		;;
esac

check_stopped() {
	for suffix in $VOLUMES; do
		volume="$VOLUME_PREFIX$suffix"
		if [ -n "$(docker ps -q --filter "volume=$volume" --filter status=running)" ]; then
			printf 'Error: stop all containers using %s before continuing.\n' "$volume" >&2
			return 1
		fi
	done
}

backup() {
	check_stopped
	umask 077
	stamp=$(date -u +%Y%m%dT%H%M%SZ)
	destination="$BACKUP_DIR/$BLOC_PROJECT_NAME-$stamp"
	mkdir -p "$BACKUP_DIR"
	mkdir "$destination"
	printf '%s\n' "$BACKUP_FORMAT_VERSION" > "$destination/.bloc-backup-version"
	for suffix in $VOLUMES; do
		volume="$VOLUME_PREFIX$suffix"
		if ! docker volume inspect "$volume" >/dev/null 2>&1; then
			if [ "$suffix" = telegram-data ]; then
				docker volume create "$volume" >/dev/null
			else
				printf 'Error: volume %s does not exist; start the deployment before backing it up.\n' "$volume" >&2
				return 1
			fi
		fi
		tmp_archive="$destination/.$suffix.tar.gz.tmp"
		if ! docker run --rm --network none \
			--mount "type=volume,src=$volume,dst=/data,readonly" \
			"$ARCHIVE_IMAGE" tar -czf - -C /data . > "$tmp_archive"; then
			rm -f "$tmp_archive"
			return 1
		fi
		mv "$tmp_archive" "$destination/$suffix.tar.gz"
	done
	printf 'Backup created in %s\n' "$destination"
}

has_archives() {
	[ -f "$1/.bloc-backup-version" ] || return 1
	[ "$(cat "$1/.bloc-backup-version")" = "$BACKUP_FORMAT_VERSION" ] || return 1
	for suffix in $VOLUMES; do
		[ -f "$1/$suffix.tar.gz" ] || return 1
	done
}

resolve_restore_dir() {
	if has_archives "$BACKUP_DIR"; then
		return
	fi

	latest=
	for candidate in "$BACKUP_DIR"/"$BLOC_PROJECT_NAME"-*; do
		[ -d "$candidate" ] || continue
		if has_archives "$candidate"; then
			latest=$candidate
		fi
	done
	if [ -n "$latest" ]; then
		BACKUP_DIR=$latest
		printf 'Using latest complete backup: %s\n' "$BACKUP_DIR"
		return
	fi

	printf 'Error: no compatible BLOC v2 backup found in %s. BLOC v1 backups cannot be restored into this deployment.\n' \
		"$BACKUP_DIR" >&2
	return 1
}

restore() {
	if [ ! -d "$BACKUP_DIR" ]; then
		printf 'Error: backup directory does not exist: %s\n' "$BACKUP_DIR" >&2
		return 1
	fi
	resolve_restore_dir
	for suffix in $VOLUMES; do
		if [ ! -f "$BACKUP_DIR/$suffix.tar.gz" ]; then
			printf 'Error: required archive is missing: %s/%s.tar.gz\n' "$BACKUP_DIR" "$suffix" >&2
			return 1
		fi
		docker run --rm --network none \
			--mount "type=bind,src=$BACKUP_DIR,dst=/backup,readonly" \
			"$ARCHIVE_IMAGE" tar -tzf "/backup/$suffix.tar.gz" >/dev/null
	done
	check_stopped
	printf 'This will replace the contents of the BLOC volumes from %s.\n' "$BACKUP_DIR"
	printf 'Type RESTORE to continue: '
	IFS= read -r confirmation
	if [ "$confirmation" != RESTORE ]; then
		printf 'Restore cancelled.\n'
		return 1
	fi
	for suffix in $VOLUMES; do
		volume="$VOLUME_PREFIX$suffix"
		docker volume create "$volume" >/dev/null
		docker run --rm --network none \
			--mount "type=volume,src=$volume,dst=/data" \
			--mount "type=bind,src=$BACKUP_DIR,dst=/backup,readonly" \
			"$ARCHIVE_IMAGE" sh -c \
			"find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && tar -xzf /backup/$suffix.tar.gz -C /data"
	done
	printf 'Volumes restored from %s. Review the deployment settings before running `make start`.\n' "$BACKUP_DIR"
}

case "$COMMAND" in
	backup) backup ;;
	restore) restore ;;
	*)
		printf 'Usage: %s {backup|restore} [directory]\n' "$0" >&2
		exit 2
		;;
esac
