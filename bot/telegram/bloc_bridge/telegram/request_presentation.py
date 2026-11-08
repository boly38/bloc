"""State and rendering for the Telegram message that tracks one request."""

import hashlib
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone


_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(token|password|secret|api[_-]?key|authorization)(\s*[:=]\s*)([^\s,;]+)"
)
_MODEL_COMPONENT = re.compile(r"^[A-Za-z0-9@._:+/-]{1,120}$")
_MODEL_VARIANT = re.compile(r"^[A-Za-z0-9@._:+-]{1,40}$")
_MAX_MESSAGE_LENGTH = 3600
_MAX_INLINE_OPTIONS = 6
_STATUS_CHECK_TTL_SECONDS = 180


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


def _model_details(model_reference):
    if not isinstance(model_reference, str):
        return ()
    model_reference, variant_separator, variant = model_reference.strip().partition("#")
    provider, separator, model = model_reference.partition("/")
    if not separator or not _MODEL_COMPONENT.fullmatch(provider) or not _MODEL_COMPONENT.fullmatch(model):
        return ()
    if variant_separator:
        if not _MODEL_VARIANT.fullmatch(variant):
            return ()
    else:
        variant = "default"
    return (
        f"Provider: `{provider}`",
        f"Model: `{model}`",
        f"Variant: `{variant}`",
    )


class RequestPresentation:
    """Keep one editable request message with status history and permission actions."""

    def __init__(self, chat_id, message_id, reference, model_reference=None):
        self.chat_id = chat_id
        self.message_id = message_id
        self.reference = reference
        self.model_details = _model_details(model_reference)
        self.lock = threading.RLock()
        self.progress = "Request received."
        self.status_entries = []
        self.status_expiry_timer = None
        self.permissions = []
        self.permission_letters = {}
        self.next_permission_letter = 0
        self.omitted_permissions = 0
        self.questions = []
        self.question_letters = {}
        self.next_question_letter = 0
        self.question_numbers = {}
        self.next_question_number = 1
        self.question_answers = {}
        self.completed_question_items = set()
        self.announced_question_items = set()
        self.submitting_questions = set()
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
            now = time.monotonic()
            self.expire_statuses(now)
            self.status_entries.append(
                {"expires_at": now + _STATUS_CHECK_TTL_SECONDS, "text": f"[{timestamp}] {text}"}
            )
            self.status_entries = self.status_entries[-8:]

    def expire_statuses(self, now=None):
        """Remove status-check results that have been visible for three minutes."""
        if now is None:
            now = time.monotonic()
        with self.lock:
            remaining = [entry for entry in self.status_entries if entry["expires_at"] > now]
            changed = len(remaining) != len(self.status_entries)
            self.status_entries = remaining
            return changed

    def seconds_until_status_expiry(self):
        with self.lock:
            if self.final_text is not None or not self.status_entries:
                return None
            return max(0, min(entry["expires_at"] for entry in self.status_entries) - time.monotonic())

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

    def sync_questions(self, requests):
        with self.lock:
            if self.final_text is not None:
                return False
            previous = tuple(
                (entry["letter"], entry["request"].get("id")) for entry in self.questions
            )
            valid = {
                request["id"]: request
                for request in requests
                if isinstance(request, dict)
                and isinstance(request.get("id"), str)
                and isinstance(request.get("questions"), list)
                and request["questions"]
            }
            self.question_letters = {
                request_id: letter
                for request_id, letter in self.question_letters.items()
                if request_id in valid
            }
            used_letters = set(self.question_letters.values())
            entries = []
            for request_id in sorted(valid):
                letter = self.question_letters.get(request_id)
                if letter is None:
                    letter = self._next_question_letter(used_letters)
                    if letter is None:
                        continue
                    self.question_letters[request_id] = letter
                    used_letters.add(letter)
                entries.append({"letter": letter, "request": valid[request_id]})
            self.questions = entries
            active_ids = set(valid)
            active_question_keys = {
                (request_id, question_index)
                for request_id, request in valid.items()
                for question_index in range(len(request["questions"]))
            }
            self.question_numbers = {
                key: number for key, number in self.question_numbers.items() if key in active_question_keys
            }
            for request_id in sorted(valid):
                for question_index in range(len(valid[request_id]["questions"])):
                    key = (request_id, question_index)
                    if key not in self.question_numbers:
                        self.question_numbers[key] = self.next_question_number
                        self.next_question_number += 1
            self.question_answers = {
                key: value for key, value in self.question_answers.items() if key[0] in active_ids
            }
            self.completed_question_items = {
                key for key in self.completed_question_items if key[0] in active_ids
            }
            self.announced_question_items = {
                key for key in self.announced_question_items if key[0] in active_ids
            }
            self.submitting_questions.intersection_update(active_ids)
            current = tuple((entry["letter"], entry["request"].get("id")) for entry in entries)
            return previous != current

    def question_for_letter(self, letter):
        with self.lock:
            for entry in self.questions:
                if entry["letter"] == letter:
                    return dict(entry["request"])
        return None

    def question_letter_for_id(self, request_id):
        with self.lock:
            return self.question_letters.get(request_id)

    def question_for_number(self, number):
        current = self.current_question()
        if current is None or current["number"] != number:
            return None
        return dict(current["request"]), current["question_index"]

    def current_question(self):
        with self.lock:
            for entry in self.questions:
                request = entry["request"]
                request_id = request.get("id")
                if request_id in self.submitting_questions:
                    continue
                for question_index in range(len(request.get("questions", []))):
                    if (request_id, question_index) in self.completed_question_items:
                        continue
                    number = self.question_numbers.get((request_id, question_index))
                    if number is not None:
                        return {
                            "request": dict(request),
                            "question_index": question_index,
                            "number": number,
                        }
        return None

    def is_current_question(self, request_id, question_index):
        current = self.current_question()
        return current is not None and (
            current["request"].get("id"),
            current["question_index"],
        ) == (request_id, question_index)

    def reopen_last_question(self, request_id):
        with self.lock:
            request = self._question_request(request_id)
            if request is None:
                return
            questions = request.get("questions", [])
            if questions:
                self.completed_question_items.discard((request_id, len(questions) - 1))
            self.submitting_questions.discard(request_id)

    def question_number(self, request_id, question_index):
        with self.lock:
            return self.question_numbers.get((request_id, question_index))

    @staticmethod
    def question_token(request_id):
        """Return a short opaque callback token that prevents stale-button retargeting."""
        return hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:8]

    def toggle_question_option(self, request_id, question_index, option_index):
        with self.lock:
            prompt = self._question_prompt(request_id, question_index)
            options = prompt.get("options") if isinstance(prompt, dict) else None
            if not isinstance(options, list) or not 0 <= option_index < len(options):
                return False
            option = options[option_index]
            label = option.get("label") if isinstance(option, dict) else None
            value = option.get("value", label) if isinstance(option, dict) else None
            if not isinstance(label, str) or not isinstance(value, str):
                return False
            key = (request_id, question_index)
            selected = list(self.question_answers.get(key, []))
            if prompt.get("multiple") is True:
                if value in selected:
                    selected.remove(value)
                else:
                    selected.append(value)
                self.question_answers[key] = selected
                self.completed_question_items.discard(key)
            else:
                self.question_answers[key] = [value]
                self.completed_question_items.add(key)
            return True

    def complete_question_selection(self, request_id, question_index):
        with self.lock:
            key = (request_id, question_index)
            if not self.question_answers.get(key):
                return False
            self.completed_question_items.add(key)
            return True

    def set_question_answer(self, request_id, question_index, answers):
        with self.lock:
            prompt = self._question_prompt(request_id, question_index)
            if prompt is None or not isinstance(answers, list) or not all(
                isinstance(answer, str) for answer in answers
            ):
                return False
            if len(answers) > 26 or any(len(answer) > 500 for answer in answers):
                return False
            key = (request_id, question_index)
            self.question_answers[key] = list(answers)
            self.completed_question_items.add(key)
            return True

    def answers_for_question(self, request_id):
        with self.lock:
            request = self._question_request(request_id)
            if request is None:
                return None
            prompts = request.get("questions", [])
            if not prompts or any(
                (request_id, index) not in self.completed_question_items
                for index in range(len(prompts))
            ):
                return None
            return [list(self.question_answers.get((request_id, index), [])) for index in range(len(prompts))]

    def answer_for_question_item(self, request_id, question_index):
        with self.lock:
            key = (request_id, question_index)
            if key not in self.completed_question_items:
                return None
            return list(self.question_answers.get(key, []))

    def question_was_announced(self, request_id, question_index):
        with self.lock:
            return (request_id, question_index) in self.announced_question_items

    def mark_question_announced(self, request_id, question_index):
        with self.lock:
            self.announced_question_items.add((request_id, question_index))

    def claim_question_submission(self, request_id):
        with self.lock:
            if self._question_request(request_id) is None or request_id in self.submitting_questions:
                return False
            self.submitting_questions.add(request_id)
            return True

    def release_question_submission(self, request_id):
        with self.lock:
            self.submitting_questions.discard(request_id)

    def resolve_question(self, request_id):
        with self.lock:
            self.questions = [
                entry for entry in self.questions if entry["request"].get("id") != request_id
            ]
            self.question_letters.pop(request_id, None)
            self.question_numbers = {
                key: number for key, number in self.question_numbers.items() if key[0] != request_id
            }
            self.question_answers = {
                key: value for key, value in self.question_answers.items() if key[0] != request_id
            }
            self.completed_question_items = {
                key for key in self.completed_question_items if key[0] != request_id
            }
            self.announced_question_items = {
                key for key in self.announced_question_items if key[0] != request_id
            }
            self.submitting_questions.discard(request_id)

    def _question_request(self, request_id):
        return next(
            (entry["request"] for entry in self.questions if entry["request"].get("id") == request_id),
            None,
        )

    def _question_prompt(self, request_id, question_index):
        request = self._question_request(request_id)
        prompts = request.get("questions") if isinstance(request, dict) else None
        if not isinstance(prompts, list) or not 0 <= question_index < len(prompts):
            return None
        prompt = prompts[question_index]
        return prompt if isinstance(prompt, dict) else None

    def _next_question_letter(self, used_letters):
        for offset in range(26):
            index = (self.next_question_letter + offset) % 26
            letter = chr(ord("A") + index)
            if letter in used_letters:
                continue
            self.next_question_letter = (index + 1) % 26
            return letter
        return None

    def finish(self, text):
        with self.lock:
            self.final_text = text
            if self.status_expiry_timer is not None:
                self.status_expiry_timer.cancel()
                self.status_expiry_timer = None
            self.permissions = []
            self.permission_letters = {}
            self.questions = []
            self.question_letters = {}
            self.question_numbers = {}
            self.question_answers = {}
            self.completed_question_items = set()
            self.announced_question_items = set()
            self.submitting_questions = set()

    def edit_payload(self):
        with self.lock:
            text = self.final_text or self._render_active()
            if self.model_details:
                lines = text.splitlines()
                insert_at = 1 if lines and lines[0].startswith("Ref: ") else 0
                lines[insert_at:insert_at] = self.model_details
                text = "\n".join(lines)
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
        self.expire_statuses()
        progress_lines = self.progress.splitlines()
        cost_lines = [
            line for line in progress_lines if line.startswith(("Cost observed so far:", "Final cost:", "Cost:"))
        ]
        tool_lines = [line for line in progress_lines if line.startswith("Tools used:")]
        action_lines = [
            line
            for line in progress_lines
            if line not in cost_lines and line not in tool_lines
        ]
        lines = [f"Ref: {self.reference}", *cost_lines, *tool_lines, *action_lines]
        if self.status_entries:
            lines.extend(("", "Status checks:", *(entry["text"] for entry in self.status_entries)))
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
        if self.questions:
            lines.extend(("", "OpenCode is waiting for your answers:"))
            current = self.current_question()
            if current is None:
                lines.append("Submitting your answers to OpenCode…")
            else:
                request = current["request"]
                request_id = request.get("id")
                question_index = current["question_index"]
                question_number = current["number"]
                prompt = request["questions"][question_index]
                header = prompt.get("header")
                question = prompt.get("question")
                lines.append(
                    f"Question {question_number} · "
                    f"{header if isinstance(header, str) else 'Question'}"
                )
                if isinstance(question, str):
                    lines.append(question)
                if prompt.get("unsupported") is True:
                    lines.append(
                        "This question type is not supported in Telegram. Use the remote TUI or reject this request."
                    )
                    lines.append(f"Reject this question request with `/reject {question_number}`.")
                    return self._truncate("\n".join(lines))
                options = prompt.get("options")
                options = options if isinstance(options, list) else []
                selected = self.question_answers.get((request_id, question_index), [])
                for option_index, option in enumerate(options[:26]):
                    if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                        continue
                    option_number = option_index + 1
                    value = option.get("value", option["label"])
                    marker = "[x]" if value in selected else "[ ]"
                    description = option.get("description")
                    detail = f" — {description}" if isinstance(description, str) and description else ""
                    lines.append(f"  Option {option_number}: {marker} {option['label']}{detail}")
                if prompt.get("multiple") is True:
                    lines.append("Select one or more options, then tap Done before moving to the next question.")
                if prompt.get("custom", True) is True:
                    lines.append(
                        f"Your own answer: send `/answer {question_number}` followed by your text "
                        "(up to 500 characters)."
                    )
                lines.append(
                    f"Reject the question request containing this question with `/reject {question_number}`."
                )
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
        current = self.current_question()
        if current is not None:
            request = current["request"]
            request_id = request.get("id")
            letter = self.question_letters.get(request_id)
            question_index = current["question_index"]
            question_number = current["number"]
            prompt = request["questions"][question_index]
            options = prompt.get("options") if isinstance(prompt, dict) else []
            options = options if isinstance(options, list) else []
            if not isinstance(prompt, dict) or prompt.get("unsupported") is True:
                options = []
            for option_index, option in enumerate(options[:_MAX_INLINE_OPTIONS]):
                if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                    continue
                label = option["label"]
                value = option.get("value", label)
                selected = value in self.question_answers.get((request_id, question_index), [])
                prefix = "✓ " if selected else ""
                option_number = option_index + 1
                token = self.question_token(request_id)
                callback = f"q:{self.reference}:{letter}:{question_index}:{option_index}:{token}"
                if len(callback.encode("utf-8")) <= 64:
                    rows.append([{
                        "text": f"Question {question_number} · Option {option_number} {prefix}{label}"[:60],
                        "callback_data": callback,
                    }])
            if isinstance(prompt, dict) and prompt.get("multiple") is True:
                token = self.question_token(request_id)
                callback = f"qd:{self.reference}:{letter}:{question_index}:{token}"
                if len(callback.encode("utf-8")) <= 64:
                    rows.append([{
                        "text": f"Done question {question_number}",
                        "callback_data": callback,
                    }])
            token = self.question_token(request_id)
            callback = f"qr:{self.reference}:{letter}:{token}"
            if len(callback.encode("utf-8")) <= 64:
                rows.append([{
                    "text": f"Reject request (question {question_number})",
                    "callback_data": callback,
                }])
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
