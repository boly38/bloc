"""Single-consumer Telegram polling, offset management, and retry backoff."""

import json
import sys

from .. import http_api
from ..bridge_config import TELEGRAM_LONG_POLL_TIMEOUT_SECONDS


def poll_updates(stop, handle_update, sessions):
    """Consume updates in order without acknowledging updates not yet handled."""
    offset = 0
    conflict_backoff = 5
    while not stop.is_set():
        try:
            updates = http_api.telegram(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": TELEGRAM_LONG_POLL_TIMEOUT_SECONDS,
                    "allowed_updates": ["message", "callback_query"],
                },
            ) or []
            conflict_backoff = 5
            for update in updates:
                if stop.is_set():
                    break
                handle_update(update, sessions)
                offset = update["update_id"] + 1
        except http_api.TelegramConflict as error:
            print(
                f"Telegram polling conflict: {error}; retrying in {conflict_backoff}s.",
                file=sys.stderr,
                flush=True,
            )
            stop.wait(conflict_backoff)
            conflict_backoff = min(conflict_backoff * 2, 60)
        except (OSError, json.JSONDecodeError) as error:
            print(f"Telegram network error ({type(error).__name__}); retrying in 5s.", file=sys.stderr, flush=True)
            stop.wait(5)
