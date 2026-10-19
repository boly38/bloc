#!/usr/bin/env python3
"""Discover an operator's Telegram chat ID without starting the agent bridge."""

import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.error
import urllib.request


def load_bot_token(env_file):
    """Read only the Telegram token from the local environment file."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return token

    try:
        lines = Path(env_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() == "TELEGRAM_BOT_TOKEN":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value.strip()
    return ""


def matching_chat_id(update, challenge):
    """Return the ID only for a private /start message with the one-time challenge."""
    if not isinstance(update, dict):
        return None
    message = update.get("message", {})
    if not isinstance(message, dict):
        return None
    chat = message.get("chat", {})
    sender = message.get("from", {})
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    text = message.get("text", "")
    if not isinstance(text, str):
        return None
    words = text.split()
    if (
        chat.get("type") != "private"
        or sender.get("is_bot") is True
        or len(words) != 2
        or not words[0].lower().startswith("/start")
        or words[1] != challenge
    ):
        return None
    command = words[0].split("@", 1)[0].lower()
    chat_id = chat.get("id")
    if command != "/start" or not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None
    return chat_id


def generate_challenge():
    """Generate a short-lived, URL-safe challenge for the private /start message."""
    return secrets.token_urlsafe(4)


def telegram_updates(token, offset, timeout):
    """Fetch updates while keeping the bot token out of command output and errors."""
    payload = {
        "timeout": timeout,
        "allowed_updates": ["message"],
    }
    if offset is not None:
        payload["offset"] = offset
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getUpdates",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout + 10) as response:
        result = json.loads(response.read())
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram returned an unsuccessful response")
    updates = result.get("result")
    if not isinstance(updates, list):
        raise RuntimeError("Telegram returned an invalid updates list")
    return updates


def discover_chat_id(token, challenge, timeout_seconds=600, on_connected=None):
    """Confirm Telegram polling works, then wait for the private challenge message."""
    offset = None
    updates = telegram_updates(token, offset, 0)
    if on_connected is not None:
        on_connected()

    deadline = time.monotonic() + timeout_seconds
    while True:
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if isinstance(update_id, int):
                offset = update_id + 1
            chat_id = matching_chat_id(update, challenge) if isinstance(update, dict) else None
            if chat_id is not None:
                return chat_id

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        wait_seconds = min(50, max(1, int(remaining)))
        updates = telegram_updates(token, offset, wait_seconds)
    raise TimeoutError("Timed out waiting for the private challenge message")


def safe_error_message(error):
    """Describe common failures without exposing the token embedded in request URLs."""
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 409:
            return (
                "Error: Telegram rejected long polling (HTTP 409 Conflict). Another process may "
                "already be polling this bot (for example, a Coolify deployment), or a webhook "
                "may be configured. Stop the other poller or remove the webhook, then retry."
            )
        if error.code == 401:
            return "Error: Telegram rejected the bot token (HTTP 401 Unauthorized); check TELEGRAM_BOT_TOKEN."
        return f"Error: Telegram API returned HTTP {error.code}; check bot configuration."
    if isinstance(error, urllib.error.URLError):
        return "Error: could not connect to the Telegram API; check network connectivity."
    if isinstance(error, TimeoutError):
        return "Error: timed out waiting for the private challenge message."
    return "Error: could not discover the chat ID; check connectivity and bot configuration."


def main():
    env_file = os.environ.get("ENV_FILE", ".env")
    token = load_bot_token(env_file)
    if not token:
        print(f"Error: TELEGRAM_BOT_TOKEN is missing from {env_file}.", file=sys.stderr)
        return 1

    challenge = generate_challenge()
    print("Send this exact message in a private chat with your bot:")
    print(f"/start {challenge}")
    print("Connecting to Telegram...", flush=True)
    try:
        chat_id = discover_chat_id(
            token,
            challenge,
            on_connected=lambda: print(
                "Connected. Listening for the private challenge for up to 10 minutes; "
                "the bridge is not started and other chats will not be answered.",
                flush=True,
            ),
        )
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(safe_error_message(error), file=sys.stderr)
        return 1

    print(f"TELEGRAM_ALLOWED_CHAT_IDS={chat_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
