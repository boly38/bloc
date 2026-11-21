"""Persistent per-chat preferences for Telegram bridge behavior."""

import json
import os
import threading

from .bridge_config import CHAT_PREFERENCES_PATH


_LOCK = threading.RLock()


def _read():
    try:
        with open(CHAT_PREFERENCES_PATH, encoding="utf-8") as file:
            preferences = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(preferences, dict):
        return {}
    return {
        str(chat_id): value
        for chat_id, value in preferences.items()
        if isinstance(chat_id, (str, int)) and type(value) is bool
    }


def intermediates_enabled(chat_id):
    """Return whether intermediate assistant replies are enabled for one chat."""
    with _LOCK:
        return _read().get(str(chat_id), False)


def set_intermediates_enabled(chat_id, enabled):
    """Persist the intermediate-reply preference for one chat atomically."""
    if type(enabled) is not bool:
        raise ValueError("intermediate-reply preference must be a boolean")
    with _LOCK:
        preferences = _read()
        preferences[str(chat_id)] = enabled
        temporary_path = CHAT_PREFERENCES_PATH + ".tmp"
        directory = os.path.dirname(CHAT_PREFERENCES_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(preferences, file)
        os.replace(temporary_path, CHAT_PREFERENCES_PATH)
