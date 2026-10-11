"""Telegram polling loop and orchestration for the OpenCode bridge."""

import json
import sys
import time
import urllib.error
import urllib.parse

import cost_reporting
import http_api
import opencode_api
import session_state
from bridge_config import ALLOWED, MODEL, OC, SYSTEM


def _assistant_message_key(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("session_id"), str):
        return None
    message = entry.get("message")
    info = message.get("info") if isinstance(message, dict) else None
    if not isinstance(info, dict) or info.get("role") != "assistant" or not info.get("id"):
        return None
    return entry["session_id"], info["id"]


def _message_ids(messages):
    """Build internal keys so only messages created by this exchange are counted."""
    return {key for entry in messages if (key := _assistant_message_key(entry)) is not None}


def _new_assistant_messages(messages, previous_ids):
    return [
        entry
        for entry in messages
        if (key := _assistant_message_key(entry)) is not None and key not in previous_ids
    ]


def _record_exchange_diagnostics(messages, response):
    if not cost_reporting.COST_DIAGNOSTICS_ENABLED:
        return
    if not messages:
        cost_reporting.record_cost_diagnostics(response)
        return
    count = len(messages)
    for index, entry in enumerate(messages, start=1):
        cost_reporting.record_cost_diagnostics(entry["message"], {"index": index, "count": count})


def _model_options(body):
    configured_model, variant_separator, variant = MODEL.partition("#")
    provider, separator, model = configured_model.partition("/")
    if not separator:
        return
    body["model"] = {"providerID": provider, "modelID": model}
    if variant_separator and variant:
        body["variant"] = variant


def answer(chat_id, prompt, sessions):
    key = str(chat_id)
    session_id = sessions.get(key)
    if not session_id:
        session = http_api.http(OC + "/session", {"title": f"Telegram {chat_id}"})
        session_id = session["id"]
        sessions[key] = session_id
        session_state.save_sessions(sessions)

    request_body = {"system": SYSTEM, "parts": [{"type": "text", "text": prompt}]}
    _model_options(request_body)
    message_url = f"{OC}/session/{urllib.parse.quote(session_id)}/message"

    previous_ids = None
    try:
        previous_ids = _message_ids(opencode_api.session_tree_messages(session_id))
    except Exception as error:
        print(f"OpenCode history read failed ({type(error).__name__}).", file=sys.stderr, flush=True)

    response = http_api.http(message_url, request_body)
    new_messages = []
    if previous_ids is not None:
        try:
            new_messages = _new_assistant_messages(opencode_api.session_tree_messages(session_id), previous_ids)
        except Exception as error:
            print(f"OpenCode history read failed ({type(error).__name__}).", file=sys.stderr, flush=True)

    _record_exchange_diagnostics(new_messages, response)
    text = opencode_api.text_from_response(response)
    if previous_ids is None:
        return text + "\n\nCost: unavailable"
    return text + cost_reporting.format_exchange_footer([entry["message"] for entry in new_messages])


def authorized_response(chat_id, prompt, sessions):
    command = prompt.split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command == "/start":
        return "Hello. Send your request to OpenCode."
    if command == "/cost":
        session_id = sessions.get(str(chat_id))
        if not session_id:
            return "No OpenCode session or cost data yet."
        try:
            return cost_reporting.cost_report(session_id)
        except Exception as error:
            print(f"Cost report unavailable ({type(error).__name__}).", file=sys.stderr, flush=True)
            return "Cost data unavailable. Please try again later."
    return answer(chat_id, prompt, sessions)


def send(chat_id, text):
    # Telegram limits messages to 4096 characters.
    for start in range(0, max(1, len(text)), 4000):
        http_api.telegram("sendMessage", {"chat_id": chat_id, "text": text[start : start + 4000]})


def handle_update(update, sessions):
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    prompt = message.get("text", "").strip()
    if chat_id is None or not prompt:
        return
    if str(chat_id) not in ALLOWED:
        print(f"Rejected message for chat_id={chat_id}", file=sys.stderr, flush=True)
        return
    try:
        send(chat_id, authorized_response(chat_id, prompt, sessions))
    except Exception as error:
        print(f"OpenCode error: {error}", file=sys.stderr, flush=True)
        send(chat_id, "Temporary OpenCode error. Check the service logs.")


def main():
    polling_lock = http_api.acquire_polling_lock()
    sessions = session_state.load_sessions()
    offset = 0
    conflict_backoff = 5
    while True:
        try:
            updates = http_api.telegram("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["message"]}) or []
            conflict_backoff = 5
            for update in updates:
                offset = update["update_id"] + 1
                handle_update(update, sessions)
        except http_api.TelegramConflict as error:
            print(f"Telegram polling conflict: {error}; retrying in {conflict_backoff}s.", file=sys.stderr, flush=True)
            time.sleep(conflict_backoff)
            conflict_backoff = min(conflict_backoff * 2, 60)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            print(f"Telegram network error ({type(error).__name__}); retrying in 5s.", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
