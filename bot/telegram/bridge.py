"""Minimal Telegram long-polling bridge to the official OpenCode HTTP API."""

import base64
import fcntl
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED = {x.strip() for x in os.environ["TELEGRAM_ALLOWED_CHAT_IDS"].split(",") if x.strip()}
TG = f"https://api.telegram.org/bot{TOKEN}/"
OC = os.getenv("OPENCODE_URL", "http://opencode:4096").rstrip("/")
PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD", "")
MODEL = os.getenv("OPENCODE_MODEL", "")
STATE = "/data/sessions.json"
POLLING_LOCK = "/data/telegram-polling.lock"
UTC = timezone.utc
COST_DIAGNOSTICS_ENABLED = os.getenv("TELEGRAM_COST_DIAGNOSTICS", "false").strip().lower() == "true"
COST_DIAGNOSTICS_PATH = "/data/cost-diagnostics.jsonl"
SYSTEM = (
    "Reply in English. Never guess: rely on a test with an official binary, "
    "verified official documentation, or ask the human for clarification. Be concise."
)


class TelegramConflict(RuntimeError):
    """Raised when another client is already polling this bot's updates."""


class TelegramRateLimit(RuntimeError):
    """Raised when Telegram asks the client to wait before retrying."""

    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f"Telegram rate limit; retry after {retry_after}s")


def acquire_polling_lock():
    lock = open(POLLING_LOCK, "w", encoding="utf-8")
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
            method = url[len(TG) :]
            if method == "getUpdates" and error.code == 409:
                raise TelegramConflict(
                    "Telegram returned HTTP 409: another getUpdates request is active for this bot. "
                    "Ensure no other host or deployment is polling with the same token."
                ) from None
            if error.code == 429:
                try:
                    parameters = json.loads(details).get("parameters", {})
                    retry_after = int(parameters.get("retry_after", 1))
                    if retry_after < 1:
                        retry_after = 1
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    retry_after = 1
                raise TelegramRateLimit(retry_after) from None
            # The Telegram API URL contains the bot token; never include it in logs.
            raise RuntimeError(f"Telegram API HTTP {error.code} ({method})") from None
        if url.startswith(OC):
            # Session URLs and response bodies can contain private identifiers or content.
            raise RuntimeError(f"OpenCode API HTTP {error.code}") from None
        raise RuntimeError(f"HTTP {error.code} {url}: {details[:1000]}") from error


def telegram(method, payload):
    while True:
        try:
            return http(TG + method, payload, 70).get("result")
        except TelegramRateLimit as error:
            print(f"Telegram rate limited ({method}); retrying in {error.retry_after}s.", file=sys.stderr, flush=True)
            time.sleep(error.retry_after)


def load_sessions():
    try:
        with open(STATE, encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_sessions(sessions):
    temporary = STATE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(sessions, file)
    os.replace(temporary, STATE)


def _diagnostic_metrics(value, path=(), depth=0):
    if depth > 8:
        return {}

    metrics = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isidentifier() or len(key) > 64:
                continue
            item_path = path + (key,)
            if isinstance(item, (dict, list)):
                metrics.update(_diagnostic_metrics(item, item_path, depth + 1))
                continue

            normalized_key = key.lower()
            metric_context = any(marker in segment.lower() for segment in item_path for marker in ("cost", "token", "usage"))
            if isinstance(item, (int, float)) and not isinstance(item, bool) and metric_context:
                if not isinstance(item, float) or math.isfinite(item):
                    metrics[".".join(item_path)] = item
            elif isinstance(item, str) and normalized_key in {
                "model",
                "modelid",
                "provider",
                "providerid",
                "currency",
                "currencycode",
                "agent",
            } and len(item) <= 100:
                metrics[".".join(item_path)] = item
    elif isinstance(value, list):
        for index, item in enumerate(value[:100]):
            metrics.update(_diagnostic_metrics(item, path + (str(index),), depth + 1))
    return metrics


def record_cost_diagnostics(response, exchange_message=None):
    if not COST_DIAGNOSTICS_ENABLED or not isinstance(response, dict):
        return

    info = response.get("info")
    info = info if isinstance(info, dict) else {}
    parts = response.get("parts")
    part_shapes = []
    part_metrics = []
    if isinstance(parts, list):
        for index, part in enumerate(parts[:20]):
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            safe_type = part_type[:40] if isinstance(part_type, str) else "unknown"
            part_shapes.append(
                {
                    "type": safe_type,
                    "keys": sorted(key for key in part if isinstance(key, str) and key.isidentifier() and len(key) <= 64)[:40],
                }
            )
            metrics = _diagnostic_metrics(part)
            if metrics:
                part_metrics.append({"index": index, "type": safe_type, "metrics": metrics})

    diagnostic = {
        "timestamp": int(time.time()),
        "response_keys": sorted(key for key in response if isinstance(key, str) and key.isidentifier() and len(key) <= 64)[:40],
        "info_keys": sorted(key for key in info if isinstance(key, str) and key.isidentifier() and len(key) <= 64)[:80],
        "part_shapes": part_shapes,
        "part_metrics": part_metrics,
        "metrics": _diagnostic_metrics(info),
    }
    if exchange_message is not None:
        diagnostic["exchange_message"] = exchange_message
    try:
        with open(COST_DIAGNOSTICS_PATH, "a", encoding="utf-8") as file:
            file.write(json.dumps(diagnostic, sort_keys=True) + "\n")
    except OSError as error:
        print(f"Cost diagnostics write failed ({type(error).__name__}).", file=sys.stderr, flush=True)


def text_from_response(response):
    text = "\n".join(
        part.get("text", "")
        for part in response.get("parts", [])
        if part.get("type") == "text" and part.get("text")
    ).strip()
    if text:
        return text
    error = response.get("info", {}).get("error", {})
    details = error.get("data", {}) if isinstance(error, dict) else {}
    message = details.get("message") or error.get("name") if isinstance(error, dict) else None
    if message:
        raise RuntimeError(f"OpenCode/Opper: {message}")
    raise RuntimeError("OpenCode returned no text")


def session_messages(session_id):
    encoded_session_id = urllib.parse.quote(session_id)
    messages = http(f"{OC}/session/{encoded_session_id}/message")
    if not isinstance(messages, list):
        raise RuntimeError("OpenCode returned an invalid message history")
    return messages


def session_tree_messages(session_id):
    messages = []
    visited = set()

    def visit(current_session_id):
        if current_session_id in visited:
            return
        visited.add(current_session_id)
        messages.extend(
            {"session_id": current_session_id, "message": message}
            for message in session_messages(current_session_id)
        )
        encoded_session_id = urllib.parse.quote(current_session_id)
        children = http(f"{OC}/session/{encoded_session_id}/children")
        if not isinstance(children, list):
            raise RuntimeError("OpenCode returned an invalid child session list")
        for child in children:
            child_id = child.get("id") if isinstance(child, dict) else None
            if isinstance(child_id, str) and child_id:
                visit(child_id)

    visit(session_id)
    return messages


def _entry_message(entry):
    if isinstance(entry, dict) and isinstance(entry.get("message"), dict):
        return entry["message"]
    return entry


def _assistant_info(message):
    if not isinstance(message, dict):
        return None
    info = message.get("info")
    if isinstance(info, dict) and info.get("role") == "assistant":
        return info
    return None


def _valid_cost(info):
    cost = info.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        return None
    return float(cost)


def _tokens(info):
    values = info.get("tokens")
    if not isinstance(values, dict):
        return {}
    result = {}
    for name in ("input", "output", "reasoning"):
        value = values.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[name] = value
    cache = values.get("cache")
    if isinstance(cache, dict):
        for name in ("read", "write"):
            value = cache.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[f"cache_{name}"] = value
    return result


def _message_time(info):
    value = info.get("time")
    if not isinstance(value, dict):
        return None
    created = value.get("created")
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created):
        return None
    # OpenCode's DateTimeUtcFromMillis fields are Unix milliseconds.
    return created / 1000 if created > 100_000_000_000 else float(created)


def usage_summary(messages, start_time=None, end_time=None):
    assistant_messages = []
    timestamps_complete = True
    for entry in messages:
        message = _entry_message(entry)
        info = _assistant_info(message)
        if info is None:
            continue
        created = _message_time(info)
        if start_time is not None or end_time is not None:
            if created is None:
                timestamps_complete = False
                continue
            if start_time is not None and created < start_time:
                continue
            if end_time is not None and created >= end_time:
                continue
        assistant_messages.append((info, created))

    costs = [_valid_cost(info) for info, _ in assistant_messages]
    costs_complete = all(cost is not None for cost in costs)
    total = sum(cost for cost in costs if cost is not None) if costs_complete else None
    tokens = {}
    tokens_complete = all(
        isinstance(info.get("tokens"), dict)
        and isinstance(info["tokens"].get("input"), int)
        and not isinstance(info["tokens"].get("input"), bool)
        and info["tokens"]["input"] >= 0
        and isinstance(info["tokens"].get("output"), int)
        and not isinstance(info["tokens"].get("output"), bool)
        and info["tokens"]["output"] >= 0
        for info, _ in assistant_messages
    )
    for info, _ in assistant_messages:
        for name, value in _tokens(info).items():
            tokens[name] = tokens.get(name, 0) + value

    agents = {}
    for (info, _), cost in zip(assistant_messages, costs):
        agent = info.get("agent")
        if cost is None or not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z0-9_. -]{1,40}", agent):
            continue
        agents[agent] = agents.get(agent, 0.0) + cost

    return {
        "cost": total,
        "costs_complete": costs_complete,
        "timestamps_complete": timestamps_complete,
        "tokens": tokens,
        "tokens_complete": tokens_complete,
        "agents": agents,
        "message_count": len(assistant_messages),
    }


def format_cost(cost):
    return f"${cost:.6f}"


def format_exchange_footer(messages):
    usage = usage_summary(messages)
    if usage["message_count"] == 0 or usage["cost"] is None:
        return "\n\nCost: unavailable"
    details = usage["agents"]
    if len(details) == 1:
        agent, cost = next(iter(details.items()))
        return f"\n\nCost: {format_cost(usage['cost'])} ({agent}: {format_cost(cost)})"
    if details:
        breakdown = ", ".join(f"{agent}: {format_cost(cost)}" for agent, cost in sorted(details.items()))
        return f"\n\nCost: {format_cost(usage['cost'])} ({breakdown})"
    return f"\n\nCost: {format_cost(usage['cost'])}"


def cost_report(session_id, now=None):
    messages = session_tree_messages(session_id)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    hour_start = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0).timestamp()
    hour_end = hour_start + 3600
    session_usage = usage_summary(messages)
    hour_usage = usage_summary(messages, hour_start, hour_end)

    session_cost = format_cost(session_usage["cost"]) if session_usage["cost"] is not None else "unavailable"
    if not hour_usage["timestamps_complete"] or hour_usage["cost"] is None:
        hour_cost = "unavailable"
    else:
        hour_cost = format_cost(hour_usage["cost"])
    start_label = datetime.fromtimestamp(hour_start, UTC).strftime("%H:%M")
    end_label = datetime.fromtimestamp(hour_end, UTC).strftime("%H:%M")
    lines = [f"Session: {session_cost}", f"UTC hour ({start_label}–{end_label}): {hour_cost}"]

    if session_usage["cost"] is not None and session_usage["agents"]:
        agents = ", ".join(
            f"{agent}: {format_cost(cost)}" for agent, cost in sorted(session_usage["agents"].items())
        )
        lines.append(f"Agents (session): {agents}")
    tokens = session_usage["tokens"]
    if tokens and session_usage["tokens_complete"]:
        token_parts = []
        for key, label in (
            ("input", "input"),
            ("cache_read", "cached"),
            ("cache_write", "cache write"),
            ("output", "output"),
            ("reasoning", "reasoning"),
        ):
            if key in tokens:
                token_parts.append(f"{tokens[key]} {label}")
        if token_parts:
            lines.append("Tokens (session): " + ", ".join(token_parts))
    return "\n".join(lines)


def answer(chat_id, prompt, sessions):
    key = str(chat_id)
    session_id = sessions.get(key)
    if not session_id:
        session = http(OC + "/session", {"title": f"Telegram {chat_id}"})
        session_id = session["id"]
        sessions[key] = session_id
        save_sessions(sessions)
    body = {"system": SYSTEM, "parts": [{"type": "text", "text": prompt}]}
    if MODEL:
        configured_model, variant_separator, variant = MODEL.partition("#")
        provider, separator, model = configured_model.partition("/")
        if separator:
            body["model"] = {"providerID": provider, "modelID": model}
            if variant_separator and variant:
                body["variant"] = variant
    encoded_session_id = urllib.parse.quote(session_id)
    message_url = f"{OC}/session/{encoded_session_id}/message"
    previous_message_ids = None
    try:
        previous_messages = session_tree_messages(session_id)
        previous_message_ids = {
            (entry.get("session_id"), _assistant_info(_entry_message(entry)).get("id"))
            for entry in previous_messages
            if isinstance(entry, dict)
            and isinstance(entry.get("session_id"), str)
            and _assistant_info(_entry_message(entry)) is not None
            and _assistant_info(_entry_message(entry)).get("id")
        }
    except Exception as error:
        print(f"OpenCode history read failed ({type(error).__name__}).", file=sys.stderr, flush=True)

    response = http(message_url, body)
    new_assistant_messages = []
    if previous_message_ids is not None:
        try:
            current_messages = session_tree_messages(session_id)
            new_assistant_messages = [
                entry
                for entry in current_messages
                if isinstance(entry, dict)
                and isinstance(entry.get("session_id"), str)
                and _assistant_info(_entry_message(entry)) is not None
                and (entry["session_id"], _assistant_info(_entry_message(entry)).get("id")) not in previous_message_ids
            ]
        except Exception as error:
            print(f"OpenCode history read failed ({type(error).__name__}).", file=sys.stderr, flush=True)

    if COST_DIAGNOSTICS_ENABLED:
        if new_assistant_messages:
            count = len(new_assistant_messages)
            for index, entry in enumerate(new_assistant_messages, start=1):
                record_cost_diagnostics(_entry_message(entry), {"index": index, "count": count})
        else:
            record_cost_diagnostics(response)

    text = text_from_response(response)
    if previous_message_ids is None:
        return text + "\n\nCost: unavailable"
    return text + format_exchange_footer(new_assistant_messages)


def authorized_response(chat_id, prompt, sessions):
    command = prompt.split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command == "/start":
        return "Hello. Send your request to OpenCode."
    if command == "/cost":
        session_id = sessions.get(str(chat_id))
        if not session_id:
            return "No OpenCode session or cost data yet."
        try:
            return cost_report(session_id)
        except Exception as error:
            print(f"Cost report unavailable ({type(error).__name__}).", file=sys.stderr, flush=True)
            return "Cost data unavailable. Please try again later."
    return answer(chat_id, prompt, sessions)


def send(chat_id, text):
    # Telegram limits messages to 4096 characters.
    for start in range(0, max(1, len(text)), 4000):
        telegram("sendMessage", {"chat_id": chat_id, "text": text[start : start + 4000]})


def main():
    polling_lock = acquire_polling_lock()
    sessions = load_sessions()
    offset = 0
    conflict_backoff = 5
    while True:
        try:
            updates = telegram("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["message"]}) or []
            conflict_backoff = 5
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                chat = message.get("chat", {})
                chat_id = chat.get("id")
                prompt = message.get("text", "").strip()
                if chat_id is None or not prompt:
                    continue
                if str(chat_id) not in ALLOWED:
                    print(f"Rejected message for chat_id={chat_id}", file=sys.stderr, flush=True)
                    continue
                try:
                    send(chat_id, authorized_response(chat_id, prompt, sessions))
                except Exception as error:
                    print(f"OpenCode error: {error}", file=sys.stderr, flush=True)
                    send(chat_id, "Temporary OpenCode error. Check the service logs.")
        except TelegramConflict as error:
            print(f"Telegram polling conflict: {error}; retrying in {conflict_backoff}s.", file=sys.stderr, flush=True)
            time.sleep(conflict_backoff)
            conflict_backoff = min(conflict_backoff * 2, 60)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            print(f"Telegram network error ({type(error).__name__}); retrying in 5s.", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
