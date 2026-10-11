"""HTTP transport and Telegram polling helpers."""

import base64
import fcntl
import json
import sys
import time
import urllib.error
import urllib.request
from typing import NoReturn

from bridge_config import OC, PASSWORD, POLLING_LOCK_PATH, TG


class TelegramConflict(RuntimeError):
    """Raised when another client is already polling this bot's updates."""


class TelegramRateLimit(RuntimeError):
    """Raised when Telegram asks the client to wait before retrying."""

    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f"Telegram rate limit; retry after {retry_after}s")


def acquire_polling_lock():
    lock = open(POLLING_LOCK_PATH, "w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another Telegram bridge shares this data volume; waiting for its polling lock.", file=sys.stderr, flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def http(url, payload=None, timeout=70):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    if PASSWORD:
        value = base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        request.add_header("Authorization", f"Basic {value}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read()) if response.status != 204 else None
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        error.close()
        if url.startswith(TG):
            _handle_telegram_error(url, error.code, details)
        if url.startswith(OC):
            # Session URLs and response bodies can contain private identifiers or content.
            raise RuntimeError(f"OpenCode API HTTP {error.code}") from None
        raise RuntimeError(f"HTTP {error.code} {url}: {details[:1000]}") from error


def _handle_telegram_error(url, status, details) -> NoReturn:
    method = url[len(TG) :]
    if method == "getUpdates" and status == 409:
        raise TelegramConflict(
            "Telegram returned HTTP 409: another getUpdates request is active for this bot. "
            "Ensure no other host or deployment is polling with the same token."
        ) from None
    if status == 429:
        try:
            parameters = json.loads(details).get("parameters", {})
            retry_after = max(1, int(parameters.get("retry_after", 1)))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            retry_after = 1
        raise TelegramRateLimit(retry_after) from None
    # Telegram URLs contain the bot token; never include them in logs.
    raise RuntimeError(f"Telegram API HTTP {status} ({method})") from None


def telegram(method, payload):
    while True:
        try:
            return http(TG + method, payload, 70).get("result")
        except TelegramRateLimit as error:
            print(f"Telegram rate limited ({method}); retrying in {error.retry_after}s.", file=sys.stderr, flush=True)
            time.sleep(error.retry_after)
