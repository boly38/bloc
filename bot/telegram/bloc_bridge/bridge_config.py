"""Environment-backed configuration shared by the Telegram bridge modules."""

import os


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED = {item.strip() for item in os.environ["TELEGRAM_ALLOWED_CHAT_IDS"].split(",") if item.strip()}
TG = f"https://api.telegram.org/bot{TOKEN}/"
OC = os.getenv("OPENCODE_URL", "http://opencode:4096").rstrip("/")
MODEL = os.getenv("OPENCODE_MODEL", "")

OPENCODE_HTTP_TIMEOUT_SECONDS = 300
OPENCODE_STATUS_TIMEOUT_SECONDS = 10
OPENCODE_EVENT_TIMEOUT_SECONDS = 15
OPENCODE_PROGRESS_INTERVAL_SECONDS = 30
OPENCODE_REQUEST_MAX_DURATION_SECONDS = int(os.getenv("OPENCODE_REQUEST_MAX_DURATION_SECONDS", "1800"))
if OPENCODE_REQUEST_MAX_DURATION_SECONDS <= 0:
    raise ValueError("OPENCODE_REQUEST_MAX_DURATION_SECONDS must be positive")
TELEGRAM_HTTP_TIMEOUT_SECONDS = 70
TELEGRAM_LONG_POLL_TIMEOUT_SECONDS = 50

SESSIONS_PATH = "/data/sessions.json"
REQUESTS_PATH = "/data/requests.sqlite3"
POLLING_LOCK_PATH = "/data/telegram-polling.lock"
COST_DIAGNOSTICS_ENABLED = os.getenv("TELEGRAM_COST_DIAGNOSTICS", "false").strip().lower() == "true"
COST_DIAGNOSTICS_PATH = "/data/cost-diagnostics.jsonl"
HEALTH_OPPER_ENABLED = os.getenv("TELEGRAM_HEALTH_OPPER_ENABLED", "false").strip().lower() == "true"

SYSTEM = (
    "Never guess: rely on a test with an official binary, "
    "verified official documentation, or ask the human for clarification. Be concise. "
    "Format replies using only this Markdown subset: paragraphs, # headings, **bold**, *italic*, "
    "~~strikethrough~~, simple bullet or numbered lists, > block quotes, `inline code`, "
    "fenced code blocks with an optional language, and [links](https://example.com). "
    "Do not emit raw HTML, tables, nested lists, images, or reference-style links. "
    "Do not nest inline emphasis or put formatting inside link labels. "
    "Do not escape Markdown for Telegram; the bridge converts this subset."
)
