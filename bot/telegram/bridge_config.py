"""Environment-backed configuration shared by the Telegram bridge modules."""

import os


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED = {item.strip() for item in os.environ["TELEGRAM_ALLOWED_CHAT_IDS"].split(",") if item.strip()}
TG = f"https://api.telegram.org/bot{TOKEN}/"
OC = os.getenv("OPENCODE_URL", "http://opencode:4096").rstrip("/")
PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD", "")
MODEL = os.getenv("OPENCODE_MODEL", "")

SESSIONS_PATH = "/data/sessions.json"
POLLING_LOCK_PATH = "/data/telegram-polling.lock"
COST_DIAGNOSTICS_ENABLED = os.getenv("TELEGRAM_COST_DIAGNOSTICS", "false").strip().lower() == "true"
COST_DIAGNOSTICS_PATH = "/data/cost-diagnostics.jsonl"

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
