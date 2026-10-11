"""Persistent mapping between Telegram chats and OpenCode sessions."""

import json
import os

from bridge_config import SESSIONS_PATH


def load_sessions():
    try:
        with open(SESSIONS_PATH, encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_sessions(sessions):
    temporary_path = SESSIONS_PATH + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(sessions, file)
    os.replace(temporary_path, SESSIONS_PATH)
