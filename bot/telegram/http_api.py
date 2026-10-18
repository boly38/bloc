"""HTTP transport and Telegram polling helpers."""

import fcntl
import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import NoReturn

from bridge_config import (
    OC,
    OPENCODE_EVENT_TIMEOUT_SECONDS,
    OPENCODE_HTTP_TIMEOUT_SECONDS,
    POLLING_LOCK_PATH,
    TG,
    TELEGRAM_HTTP_TIMEOUT_SECONDS,
)


class TelegramConflict(RuntimeError):
    """Raised when another client is already polling this bot's updates."""


class TelegramRateLimit(RuntimeError):
    """Raised when Telegram asks the client to wait before retrying."""

    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f"Telegram rate limit; retry after {retry_after}s")


class TelegramFormattingError(RuntimeError):
    """Raised when Telegram rejects message entity or HTML formatting."""


class OpenCodeHTTPError(RuntimeError):
    """An OpenCode HTTP error with a safe status code for diagnostics."""

    def __init__(self, status, error_name=None, error_code=None, validation_fields=(), validation_codes=()):
        self.status = status
        self.error_name = error_name
        self.error_code = error_code
        self.validation_fields = validation_fields
        self.validation_codes = validation_codes
        super().__init__(f"OpenCode API HTTP {status}")


def http_timeout(url):
    if url.startswith(TG):
        return TELEGRAM_HTTP_TIMEOUT_SECONDS
    if url.startswith(OC):
        return OPENCODE_HTTP_TIMEOUT_SECONDS
    return TELEGRAM_HTTP_TIMEOUT_SECONDS


def acquire_polling_lock():
    lock = open(POLLING_LOCK_PATH, "w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another Telegram bridge shares this data volume; waiting for its polling lock.", file=sys.stderr, flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def http(url, payload=None, timeout=None):
    if timeout is None:
        timeout = http_timeout(url)
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

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
            error_name = None
            error_code = None
            validation_fields = set()
            validation_codes = set()
            try:
                response_error = json.loads(details)
                if isinstance(response_error, dict):
                    error_name = response_error.get("name")
                    error_value = response_error.get("error")
                    if error_name is None:
                        if isinstance(error_value, dict):
                            error_name = error_value.get("name") or error_value.get("code")
                        elif isinstance(error_value, str):
                            error_name = error_value
                    error_data = response_error.get("data")
                    candidate_code = response_error.get("code")
                    if candidate_code is None and isinstance(error_data, dict):
                        candidate_code = error_data.get("code")
                    if isinstance(candidate_code, (str, int)):
                        error_code = candidate_code
                    issue_list = response_error.get("issues") or response_error.get("errors")
                    if isinstance(error_data, dict):
                        issue_list = issue_list or error_data.get("issues") or error_data.get("errors")
                    if isinstance(issue_list, list):
                        for issue in issue_list:
                            if not isinstance(issue, dict):
                                continue
                            path = issue.get("path")
                            if isinstance(path, list):
                                safe_parts = [
                                    re.sub(r"[^A-Za-z0-9_.-]", "", str(part))[:40]
                                    for part in path
                                    if isinstance(part, (str, int))
                                ]
                                safe_path = ".".join(part for part in safe_parts if part)[:100]
                                if safe_path:
                                    validation_fields.add(safe_path)
                            issue_code = issue.get("code")
                            if isinstance(issue_code, str):
                                safe_code = re.sub(r"[^A-Za-z0-9_.-]", "", issue_code)[:80]
                                if safe_code:
                                    validation_codes.add(safe_code)
            except json.JSONDecodeError:
                pass
            raise OpenCodeHTTPError(
                error.code,
                error_name,
                error_code,
                tuple(sorted(validation_fields)),
                tuple(sorted(validation_codes)),
            ) from None
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
    if status == 400 and method == "sendMessage":
        try:
            description = json.loads(details).get("description", "").lower()
        except (AttributeError, TypeError, json.JSONDecodeError):
            description = ""
        if any(
            marker in description
            for marker in (
                "parse entities",
                "can't parse",
                "entity",
                "unsupported start tag",
                "unsupported end tag",
            )
        ):
            raise TelegramFormattingError("Telegram rejected message formatting") from None
    # Telegram URLs contain the bot token; never include them in logs.
    raise RuntimeError(f"Telegram API HTTP {status} ({method})") from None


def telegram(method, payload):
    while True:
        try:
            return http(TG + method, payload, TELEGRAM_HTTP_TIMEOUT_SECONDS).get("result")
        except TelegramRateLimit as error:
            print(f"Telegram rate limited ({method}); retrying in {error.retry_after}s.", file=sys.stderr, flush=True)
            time.sleep(error.retry_after)


def opencode_events():
    """Yield decoded OpenCode SSE events without logging response content."""
    request = urllib.request.Request(
        OC + "/event",
        headers={"Accept": "text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=OPENCODE_EVENT_TIMEOUT_SECONDS) as response:
            for line in response:
                if isinstance(line, str):
                    line = line.encode()
                if not line.startswith(b"data:"):
                    if line.startswith(b":"):
                        yield None
                    continue
                data = line[5:].strip()
                if not data:
                    yield None
                    continue
                event = json.loads(data)
                if isinstance(event, dict) and isinstance(event.get("payload"), dict):
                    event = event["payload"]
                if isinstance(event, dict):
                    yield event
    except urllib.error.HTTPError as error:
        error.close()
        raise OpenCodeHTTPError(error.code) from None
