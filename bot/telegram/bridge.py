"""Minimal Telegram long-polling bridge to the official OpenCode HTTP API."""

import base64
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED = {x.strip() for x in os.environ["TELEGRAM_ALLOWED_CHAT_IDS"].split(",") if x.strip()}
TG = f"https://api.telegram.org/bot{TOKEN}/"
OC = os.getenv("OPENCODE_URL", "http://opencode:4096").rstrip("/")
PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD", "")
MODEL = os.getenv("OPENCODE_MODEL", "")
STATE = "/data/sessions.json"
POLLING_LOCK = "/data/telegram-polling.lock"
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
    response = http(f"{OC}/session/{urllib.parse.quote(session_id)}/message", body)
    return text_from_response(response)


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
                if prompt == "/start":
                    send(chat_id, "Hello. Send your request to OpenCode.")
                    continue
                try:
                    send(chat_id, answer(chat_id, prompt, sessions))
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
