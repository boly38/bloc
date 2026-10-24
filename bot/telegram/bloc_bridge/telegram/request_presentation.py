"""State and rendering for the Telegram message that tracks one request."""

import re
import threading
import urllib.parse
from datetime import datetime, timezone


_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(token|password|secret|api[_-]?key|authorization)(\s*[:=]\s*)([^\s,;]+)"
)
_MAX_MESSAGE_LENGTH = 3600


def _safe_scope(value):
    if not isinstance(value, str):
        return "scope details unavailable"
    value = " ".join(value.split())
    if value.startswith(("http://", "https://")):
        try:
            parsed = urllib.parse.urlsplit(value)
            host = parsed.hostname or ""
            if parsed.port is not None:
                host = f"{host}:{parsed.port}"
            value = urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except ValueError:
            value = "web resource"
    else:
        value = _SENSITIVE_VALUE.sub(r"\1\2[redacted]", value)
    return value[:117] + "..." if len(value) > 120 else value or "scope details unavailable"


class RequestPresentation:
    """Keep one editable request message with status history and permission actions."""

    def __init__(self, chat_id, message_id, reference):
        self.chat_id = chat_id
        self.message_id = message_id
        self.reference = reference
        self.lock = threading.RLock()
        self.progress = "Request received."
        self.status_entries = []
        self.permissions = []
        self.permission_letters = {}
        self.next_permission_letter = 0
        self.omitted_permissions = 0
        self.final_text = None
        self.observed_cost = None

    def set_progress(self, text):
        with self.lock:
            if self.final_text is not None:
                return
            self.progress = text
            for line in text.splitlines():
                if line.startswith("Cost observed so far:"):
                    self.observed_cost = line.removeprefix("Cost observed so far:").strip()

    def append_status(self, text):
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        with self.lock:
            if self.final_text is not None:
                return
            self.status_entries.append(f"[{timestamp}] {text}")
            self.status_entries = self.status_entries[-8:]

    def sync_permissions(self, requests):
        with self.lock:
            if self.final_text is not None:
                return False
            previous = self._permission_signature()
            valid = {
                request["id"]: request
                for request in requests
                if isinstance(request, dict) and isinstance(request.get("id"), str)
            }
            self.permission_letters = {
                request_id: letter
                for request_id, letter in self.permission_letters.items()
                if request_id in valid
            }
            used_letters = set(self.permission_letters.values())
            entries = []
            for request_id in sorted(valid):
                letter = self.permission_letters.get(request_id)
                if letter is None:
                    letter = self._next_available_letter(used_letters)
                    if letter is None:
                        continue
                    self.permission_letters[request_id] = letter
                    used_letters.add(letter)
                entries.append({"letter": letter, "request": valid[request_id]})
            self.permissions = entries
            self.omitted_permissions = len(valid) - len(entries)
            return previous != self._permission_signature()

    def permission_for_letter(self, letter):
        with self.lock:
            for entry in self.permissions[:5]:
                if entry["letter"] == letter:
                    return dict(entry["request"])
        return None

    def permissions_by_letter(self):
        with self.lock:
            return {entry["letter"]: dict(entry["request"]) for entry in self.permissions[:5]}

    def resolve_permission(self, request_id):
        with self.lock:
            self.permissions = [
                entry for entry in self.permissions if entry["request"].get("id") != request_id
            ]
            self.permission_letters.pop(request_id, None)

    def finish(self, text):
        with self.lock:
            self.final_text = text
            self.permissions = []
            self.permission_letters = {}

    def edit_payload(self):
        with self.lock:
            text = self.final_text or self._render_active()
            return self._truncate(text), self._keyboard()

    def _permission_signature(self):
        signature = []
        for entry in self.permissions:
            request = entry["request"]
            patterns = request.get("patterns")
            always = request.get("always")
            signature.append(
                (
                    entry["letter"],
                    request.get("permission"),
                    tuple(patterns) if isinstance(patterns, list) else (),
                    tuple(always) if isinstance(always, list) else (),
                )
            )
        return tuple(signature)

    def _next_available_letter(self, used_letters):
        for offset in range(26):
            index = (self.next_permission_letter + offset) % 26
            letter = chr(ord("A") + index)
            if letter in used_letters:
                continue
            self.next_permission_letter = (index + 1) % 26
            return letter
        return None

    def _render_active(self):
        lines = [
            self.progress,
            f"Ref: {self.reference}",
            "Use /status or /abort (/cancel); buttons below control this request.",
        ]
        if self.status_entries:
            lines.extend(("", "Status checks:", *self.status_entries))
        if self.permissions:
            lines.extend(("", "Permissions waiting for a decision:"))
            for entry in self.permissions[:5]:
                request = entry["request"]
                permission = request.get("permission")
                if not isinstance(permission, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", permission):
                    permission = "tool action"
                patterns = request.get("patterns")
                patterns = patterns if isinstance(patterns, list) else []
                scopes = [_safe_scope(item) for item in patterns[:3]]
                if len(patterns) > 3:
                    scopes.append(f"and {len(patterns) - 3} more")
                scope_text = ", ".join(scopes) if scopes else "scope details unavailable"
                letter = entry["letter"]
                always = request.get("always")
                if isinstance(always, list) and always:
                    remembered = [_safe_scope(item) for item in always[:3]]
                    if len(always) > 3:
                        remembered.append(f"and {len(always) - 3} more")
                    scope_text = f"{scope_text} (remember scope: {', '.join(remembered)})"
                lines.append(f"{letter}. {permission}: {scope_text}")
                if isinstance(always, list) and always:
                    lines.append(f"Use /allow {letter}, /allowAll {letter}, or /deny {letter}.")
                else:
                    lines.append(f"Use /allow {letter} or /deny {letter}.")
            if len(self.permissions) > 5 or self.omitted_permissions:
                lines.append("Use the remote TUI to inspect additional permission requests.")
        return "\n".join(lines)

    def _keyboard(self):
        if self.final_text is not None:
            return {"inline_keyboard": []}
        rows = [
            [
                {"text": "Status", "callback_data": f"status:{self.reference}"},
                {"text": "Abort", "callback_data": f"abort:{self.reference}"},
            ]
        ]
        for entry in self.permissions[:5]:
            letter = entry["letter"]
            request = entry["request"]
            permission_id = request.get("id", "")
            row = []
            allow_callback = self._permission_callback(letter, "o", permission_id)
            if allow_callback is not None:
                row.append({"text": f"Allow {letter}", "callback_data": allow_callback})
            always = request.get("always")
            if isinstance(always, list) and always:
                always_callback = self._permission_callback(letter, "a", permission_id)
                if always_callback is not None:
                    row.append({"text": f"Always {letter}", "callback_data": always_callback})
            deny_callback = self._permission_callback(letter, "d", permission_id)
            if deny_callback is not None:
                row.append({"text": f"Deny {letter}", "callback_data": deny_callback})
            if row:
                rows.append(row)
        return {"inline_keyboard": rows}

    def _permission_callback(self, letter, action, permission_id):
        callback = f"p:{self.reference}:{letter}:{action}:{permission_id}"
        if len(callback.encode("utf-8")) <= 64:
            return callback
        return None

    @staticmethod
    def _truncate(text):
        if len(text) <= _MAX_MESSAGE_LENGTH:
            return text
        return text[: _MAX_MESSAGE_LENGTH - 24] + "\n[Earlier details omitted.]"
