"""Telegram polling loop and orchestration for the OpenCode bridge."""

import json
import queue
import re
import secrets
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from . import bridge_runtime, http_api, opper_health, request_store, session_state
from .opencode import cost_reporting, api as opencode_api, exchange
from .telegram import markdown_renderer, polling as telegram_polling
from .telegram.request_presentation import RequestPresentation
from .bridge_config import (
    ALLOWED,
    HEALTH_OPPER_ENABLED,
    MODEL,
    OC,
    OPENCODE_HTTP_TIMEOUT_SECONDS,
    OPENCODE_STATUS_TIMEOUT_SECONDS,
    OPENCODE_PROGRESS_INTERVAL_SECONDS,
    OPENCODE_REQUEST_MAX_DURATION_SECONDS,
    SYSTEM,
    TELEGRAM_HTTP_TIMEOUT_SECONDS,
    TELEGRAM_LONG_POLL_TIMEOUT_SECONDS,
    OPENCODE_EVENT_TIMEOUT_SECONDS,
)


_REQUEST_EXECUTOR = bridge_runtime.RequestWorkers()
_REQUEST_SLOTS = threading.BoundedSemaphore(32)
_SESSIONS_LOCK = threading.RLock()
_CHAT_LOCKS_LOCK = threading.Lock()
_CHAT_LOCKS = {}
_REQUEST_PRESENTATIONS_LOCK = threading.RLock()
_REQUEST_PRESENTATIONS = {}
_MAX_REQUEST_PRESENTATIONS = 256
_PROGRESS_EVENT_MIN_INTERVAL_SECONDS = 5
_PERMISSION_POLL_INTERVAL_SECONDS = 5
_PERMISSION_SESSION_LOOKUP_TIMEOUT_SECONDS = 3
_PERMISSION_ANCESTRY_MAX_DEPTH = 8
_TEST_COMMAND = re.compile(
    r"\b(?:pytest|tox|nox|unittest|cargo\s+test|go\s+test|"
    r"npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+(?:run\s+)?test|"
    r"bun\s+(?:run\s+)?test|mvn\s+test|gradle\s+test|make\s+test)\b",
    re.IGNORECASE,
)
_GH_CLI_COMMAND = re.compile(
    r"\bgh\s+(?:api|auth|issue|pr|repo|run|workflow|release|label|search|gist|secret|variable)\b",
    re.IGNORECASE,
)
_TOOL_PROGRESS_LABELS = {
    "bash": "Command",
    "edit": "File edit",
    "glob": "File search",
    "grep": "File search",
    "read": "File read",
    "task": "Subtask",
    "webfetch": "Documentation fetch",
    "write": "File write",
}


def _chat_lock(chat_id):
    key = str(chat_id)
    with _CHAT_LOCKS_LOCK:
        return _CHAT_LOCKS.setdefault(key, threading.Lock())


def _request_presentation(reference, chat_id=None, acknowledgement_id=None):
    with _REQUEST_PRESENTATIONS_LOCK:
        presentation = _REQUEST_PRESENTATIONS.get(reference)
        if presentation is not None:
            if chat_id is not None and str(presentation.chat_id) != str(chat_id):
                return None
            return presentation
        if chat_id is None or acknowledgement_id is None:
            request = request_store.get(reference)
            if request is not None:
                if request.get("state") in request_store.TERMINAL_STATES:
                    return None
                chat_id = request.get("chat_id") if chat_id is None else chat_id
                acknowledgement_id = (
                    request.get("acknowledgement_id") if acknowledgement_id is None else acknowledgement_id
                )
        if chat_id is None or acknowledgement_id is None:
            return None
        presentation = RequestPresentation(chat_id, acknowledgement_id, reference)
        _REQUEST_PRESENTATIONS[reference] = presentation
        _prune_request_presentations()
        return presentation


def _register_request_presentation(presentation):
    with _REQUEST_PRESENTATIONS_LOCK:
        _REQUEST_PRESENTATIONS[presentation.reference] = presentation
        _prune_request_presentations()
        _prune_request_presentations()


def _prune_request_presentations():
    while len(_REQUEST_PRESENTATIONS) > _MAX_REQUEST_PRESENTATIONS:
        for reference, presentation in list(_REQUEST_PRESENTATIONS.items()):
            with presentation.lock:
                finished = presentation.final_text is not None
            if finished:
                _REQUEST_PRESENTATIONS.pop(reference, None)
                break
        else:
            return


def _request_presentation_for_request(request):
    return _request_presentation(
        request["reference"],
        request.get("chat_id"),
        request.get("acknowledgement_id"),
    )


def _edit_request_presentation(presentation):
    if presentation is None:
        return
    with presentation.lock:
        text, reply_markup = presentation.edit_payload()
        edit_reply(
            presentation.chat_id,
            presentation.message_id,
            text,
            reply_markup=reply_markup,
        )


def _schedule_request_status_expiry(presentation):
    with presentation.lock:
        if presentation.status_expiry_timer is not None:
            presentation.status_expiry_timer.cancel()
            presentation.status_expiry_timer = None
        delay = presentation.seconds_until_status_expiry()
        if delay is None:
            return
        timer = threading.Timer(delay, lambda: _expire_request_statuses(presentation, timer))
        timer.daemon = True
        presentation.status_expiry_timer = timer
    timer.start()


def _expire_request_statuses(presentation, timer):
    with presentation.lock:
        if presentation.status_expiry_timer is not timer:
            return
        presentation.status_expiry_timer = None
        changed = presentation.expire_statuses()
    if changed:
        _schedule_request_status_expiry(presentation)
        _edit_request_presentation(presentation)
    else:
        _schedule_request_status_expiry(presentation)


def _forget_request_presentation(reference):
    with _REQUEST_PRESENTATIONS_LOCK:
        presentation = _REQUEST_PRESENTATIONS.pop(reference, None)
    if presentation is not None:
        with presentation.lock:
            if presentation.status_expiry_timer is not None:
                presentation.status_expiry_timer.cancel()
                presentation.status_expiry_timer = None


def _append_request_status(chat_id, reference, status_text):
    request = request_store.get_for_chat(reference, chat_id)
    if request is None:
        return False
    presentation = _request_presentation_for_request(request)
    if presentation is None:
        return False
    presentation.append_status(status_text)
    _edit_request_presentation(presentation)
    return True


def _finish_request_presentation(request, summary):
    presentation = _request_presentation_for_request(request)
    if presentation is None:
        return False
    presentation.finish(summary)
    _edit_request_presentation(presentation)
    return True


# Compatibility aliases keep the historical bridge module surface while the
# exchange implementation stays independent of Telegram composition.
exchange.set_presentation_lookup(_request_presentation)
OpenCodeRequestTimeout = exchange.OpenCodeRequestTimeout
OpenCodeRequestStopped = exchange.OpenCodeRequestStopped
OpenCodeSubmissionUnconfirmed = exchange.OpenCodeSubmissionUnconfirmed
answer = exchange.answer
def answer_async(*args, **kwargs):
    exchange.OPENCODE_REQUEST_MAX_DURATION_SECONDS = OPENCODE_REQUEST_MAX_DURATION_SECONDS
    exchange._watch_events = _watch_events
    return exchange.answer_async(*args, **kwargs)


def resume_async_request(*args, **kwargs):
    exchange.OPENCODE_REQUEST_MAX_DURATION_SECONDS = OPENCODE_REQUEST_MAX_DURATION_SECONDS
    exchange._watch_events = _watch_events
    return exchange.resume_async_request(*args, **kwargs)
_event_details = exchange._event_details
_event_progress_stage = exchange._event_progress_stage
_session_is_busy = exchange._session_is_busy
_permissions_in_session_tree = exchange._permissions_in_session_tree
_watch_events = exchange._watch_events
_tool_names = exchange._tool_names
_tool_call_count = exchange._tool_call_count
_format_tool_usage = exchange._format_tool_usage


def _new_telegram_session_title():
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"Telegram {timestamp}__{uuid.uuid4().hex}"


exchange.set_session_title(lambda: _new_telegram_session_title())


class _AsyncRequestMonitor(exchange._AsyncRequestMonitor):
    """Compatibility wrapper that preserves bridge-level test seams."""

    def __init__(self, *args, **kwargs):
        exchange.OPENCODE_REQUEST_MAX_DURATION_SECONDS = OPENCODE_REQUEST_MAX_DURATION_SECONDS
        exchange._watch_events = _watch_events
        super().__init__(*args, **kwargs)


def _monitor_async_request(*args, **kwargs):
    """Compatibility seam for callers that historically patched bridge helpers."""
    exchange.OPENCODE_REQUEST_MAX_DURATION_SECONDS = OPENCODE_REQUEST_MAX_DURATION_SECONDS
    exchange._watch_events = _watch_events
    return exchange._monitor_async_request(*args, **kwargs)



def _control_reference(prompt, command):
    words = prompt.split()
    if len(words) == 1:
        return None
    if len(words) != 2 or not re.fullmatch(r"[A-Fa-f0-9]{16}", words[1]):
        raise ValueError(f"Usage: {command} [request-reference]")
    return words[1].upper()


def _permission_selection(prompt, command):
    """Parse a short permission command; request IDs stay inside the bridge."""
    letter = None
    response = "reject" if command == "/deny" else "always" if command == "/allowall" else "once"
    for argument in prompt.split()[1:]:
        normalized = argument.lower()
        if len(normalized) == 1 and "a" <= normalized <= "z" and letter is None:
            letter = normalized.upper()
        elif command == "/allow" and normalized == "always":
            response = normalized
        else:
            usage = {
                "/allow": "Usage: /allow [letter] [always]",
                "/allowall": "Usage: /allowAll <letter>",
                "/deny": "Usage: /deny [letter]",
            }.get(command, "Invalid permission command")
            raise ValueError(f"Usage: {usage}")
    return letter, response


def permission_reply_response(chat_id, prompt, command, reference=None):
    """Apply an explicit Telegram permission command to the active request."""
    letter, decision = _permission_selection(prompt, command)
    if reference is None:
        active = request_store.active_for_chat(chat_id)
        if not active:
            return "No active request has a pending permission."
        if len(active) != 1:
            return "Multiple active requests found; use the remote TUI to manage permissions."
        reference = active[0]["reference"]
    return _respond_to_permission(chat_id, reference, letter, decision)


def _respond_to_permission(chat_id, reference, letter, decision, expected_permission_id=None):
    """Revalidate a displayed permission before applying a human decision."""
    request = request_store.get_for_chat(reference, chat_id)
    if request is None or request["state"] not in request_store.ACTIVE_STATES:
        return "No active request has a pending permission."
    if request["state"] == "cancellation_requested":
        return "The active request is stopping; no permission will be granted."
    session_id = request.get("session_id")
    if not session_id:
        return "The active request has not reached OpenCode yet."

    try:
        pending_requests = opencode_api.pending_permissions(timeout=OPENCODE_STATUS_TIMEOUT_SECONDS)
    except Exception as error:
        print(
            f"permission_lookup_failed ref={reference} error_type={type(error).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return "Could not check pending permissions. Use the remote TUI or /cancel."
    known_sessions = {session_id}
    unrelated_sessions = set()
    try:
        pending = _permissions_in_session_tree(
            session_id,
            pending_requests,
            known_sessions,
            unrelated_sessions,
        )
    except Exception as error:
        print(
            f"permission_session_lookup_failed ref={reference} error_type={type(error).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return "Could not verify which task owns that permission. Use the remote TUI."
    if not pending:
        return "No pending permission for the active request."
    presentation = _request_presentation_for_request(request)
    if presentation is not None:
        presentation.sync_permissions(pending)
    if expected_permission_id is not None:
        selected_permission = next(
            (permission for permission in pending if permission.get("id") == expected_permission_id),
            None,
        )
        if selected_permission is None:
            return f"Permission {letter or ''} is no longer pending. Use /status to refresh the request."
        alias = None
        if presentation is not None:
            alias = next(
                (
                    current_letter
                    for current_letter, permission in presentation.permissions_by_letter().items()
                    if permission.get("id") == expected_permission_id
                ),
                None,
            )
        if letter is not None and alias != letter:
            return f"Permission {letter} changed. Use the current request card to decide."
        letter = alias or letter
    elif letter is None:
        if len(pending) != 1:
            return "Multiple permissions are pending; use the letter shown beside each request."
        selected_permission = pending[0]
        aliases = presentation.permissions_by_letter() if presentation is not None else {}
        letter = next(
            (
                alias
                for alias, permission in aliases.items()
                if permission.get("id") == selected_permission.get("id")
            ),
            "A",
        )
    else:
        selected_permission = presentation.permission_for_letter(letter) if presentation is not None else None
        if selected_permission is None:
            return f"Permission {letter} is no longer pending. Use /status to refresh the request."
        if not any(item.get("id") == selected_permission.get("id") for item in pending):
            return f"Permission {letter} is no longer pending. Use /status to refresh the request."
    if decision == "always":
        always_patterns = selected_permission.get("always")
        if not isinstance(always_patterns, list) or not always_patterns:
            return f"Permission {letter} cannot be remembered; use /allow {letter} for a one-time approval."
    permission_id = selected_permission.get("id")
    if not isinstance(permission_id, str):
        return "The pending permission is invalid; use the remote TUI or /cancel."
    try:
        confirmed = opencode_api.respond_to_permission(
            selected_permission["sessionID"],
            permission_id,
            decision,
            timeout=OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
    except Exception as error:
        print(
            f"permission_reply_failed ref={reference} error_type={type(error).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return "Could not send that permission decision. Use the remote TUI or /cancel."
    if not confirmed:
        return "OpenCode did not confirm the permission decision. Use the remote TUI or /cancel."
    if presentation is not None:
        presentation.resolve_permission(permission_id)
    if decision == "reject":
        response_text = f"Permission {letter} denied. OpenCode can handle the rejection."
    elif decision == "always":
        response_text = f"Permission {letter} allowed and remembered for matching requests."
    else:
        response_text = f"Permission {letter} allowed once. OpenCode can continue."
    return response_text


def _request_status_text(state):
    return {
        "acknowledging": "being acknowledged",
        "queued": "queued and waiting to start",
        "submitting": "submission is being verified",
        "running": "running in OpenCode",
        "cancellation_requested": "stopping; cancellation is being verified",
        "recovering": "being recovered after a bridge restart",
        "uncertain": "may still be running; check before retrying",
        "delivering": "finished; reply delivery is being finalized",
        "completed": "completed",
        "failed": "failed",
        "timed_out": "timed out and stopped",
        "cancelled": "cancelled",
        "rejected": "not started because another request was active",
        "interrupted": "interrupted before it was submitted to OpenCode",
        "acknowledgement_unknown": "not started; acknowledgement delivery could not be confirmed",
        "delivery_unknown": "finished, but reply delivery could not be confirmed",
    }.get(state, "status unavailable")


def _format_elapsed(seconds):
    remaining = max(0, int(seconds))
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _response_tools(response):
    for line in reversed(response.splitlines()):
        if line.startswith("Tools used:"):
            names = line.split(":", 1)[1].strip()
            return [] if names == "none" else [name.strip() for name in names.split(",") if name.strip()]
    return None


def _response_cost(response):
    for line in reversed(response.splitlines()):
        if line.startswith("Cost:"):
            return line.split(":", 1)[1].strip()
    return "unavailable"


def _response_tool_call_count(response):
    for line in reversed(response.splitlines()):
        if line.startswith("Tool calls:"):
            count = line.split(":", 1)[1].strip()
            return count if count.isdecimal() else "unknown"
    return "unknown"


def _request_record(reference, chat_id, acknowledgement_id):
    return request_store.get(reference) or {
        "reference": reference,
        "chat_id": chat_id,
        "acknowledgement_id": acknowledgement_id,
        "created_at": time.time(),
    }


def _terminal_request_summary(request, state, response=None, error=None):
    if response is not None:
        cost_label = f"Final cost: {_response_cost(response)}"
        tool_names = _response_tools(response)
        if tool_names is None:
            tool_names = []
        tool_call_count = _response_tool_call_count(response)
        if tool_call_count == "unknown":
            tool_call_count = 0
    else:
        presentation = _request_presentation(request["reference"])
        observed_cost = presentation.observed_cost if presentation is not None else None
        cost_label = f"Observed cost: {observed_cost}" if observed_cost else "Final cost: unavailable"
        if isinstance(error, OpenCodeSubmissionUnconfirmed):
            tool_names = []
            tool_call_count = 0
        else:
            tool_names = getattr(error, "tool_names", None)
            tool_call_count = getattr(error, "tool_call_count", "unknown")
    tool_count = len(tool_names) if tool_names is not None else "unknown"
    elapsed = time.time() - request.get("created_at", time.time())
    return "\n".join(
        (
            f"Request {request['reference']}",
            f"Status: {_request_status_text(state)}",
            cost_label,
            f"Duration: {_format_elapsed(elapsed)}",
            f"Tools used: {tool_count} distinct; {tool_call_count} calls",
        )
    )


def _finalize_request_presentation(request, state, response=None, error=None):
    summary = _terminal_request_summary(request, state, response, error)
    presentation = _request_presentation_for_request(request)
    if presentation is None:
        edit_reply(
            request["chat_id"],
            request.get("acknowledgement_id"),
            summary,
            reply_markup={"inline_keyboard": []},
        )
        return
    presentation.finish(summary)
    _edit_request_presentation(presentation)


def _append_request_status(chat_id, reference, text):
    request = request_store.get_for_chat(reference, chat_id)
    if request is None:
        return False
    presentation = _request_presentation_for_request(request)
    if presentation is None:
        return False
    with presentation.lock:
        if presentation.final_text is not None:
            return False
    presentation.append_status(text)
    _schedule_request_status_expiry(presentation)
    _edit_request_presentation(presentation)
    return True


def _show_request_status(chat_id, reference=None):
    response = request_status_response(chat_id, reference)
    if reference is None:
        active = request_store.active_for_chat(chat_id)
        reference = active[0]["reference"] if active else None
    if reference is not None and _append_request_status(chat_id, reference, response):
        return True
    send(chat_id, response)
    return False


def _record_control_result(chat_id, reference, response):
    request = request_store.get_for_chat(reference, chat_id) if reference else None
    if request is None:
        send(chat_id, response)
        return
    if request["state"] in request_store.TERMINAL_STATES:
        presentation = _request_presentation_for_request(request)
        if request["state"] == "cancelled" and presentation is not None and presentation.final_text is None:
            error = OpenCodeRequestStopped("user_cancelled")
            _finalize_request_presentation(request, request["state"], error=error)
            _forget_request_presentation(reference)
        else:
            send(chat_id, response)
        return
    if not _append_request_status(chat_id, reference, response):
        send(chat_id, response)


def request_status_response(chat_id, reference=None):
    if reference is None:
        active = request_store.active_for_chat(chat_id)
        if active:
            request = active[0]
        else:
            return "No active request. Use /status <reference> to inspect a previous request."
    else:
        request = request_store.get_for_chat(reference, chat_id)
        if request is None:
            return "No request with that reference was found for this chat."
    return f"Request {request['reference']}: {_request_status_text(request['state'])}."


def cancel_request_response(chat_id, reference=None):
    if reference is None:
        active = request_store.active_for_chat(chat_id)
        if not active:
            return "No active request to cancel."
        if len(active) > 1:
            return "Multiple requests are active. Use /cancel <reference> to choose one."
        request = active[0]
        reference = request["reference"]
    else:
        request = request_store.get_for_chat(reference, chat_id)
        if request is None:
            return "No request with that reference was found for this chat."
    if request["state"] not in request_store.ACTIVE_STATES:
        return f"Request {reference}: {_request_status_text(request['state'])}; it is not active."
    if request["state"] in ("acknowledging", "queued"):
        cancelled = request_store.update_if_state(
            reference,
            ("acknowledging", "queued"),
            state="cancelled",
            detail="cancelled_before_submission",
            completed_at=time.time(),
        )
        if cancelled is not None and cancelled["state"] == "cancelled":
            return f"Request {reference} was cancelled before it started."
        request = cancelled
        if request is None or request["state"] not in request_store.ACTIVE_STATES:
            return f"Request {reference} is no longer active. Use /status {reference}."

    cancellable_states = {
        "submitting",
        "running",
        "cancellation_requested",
        "recovering",
        "uncertain",
    }
    if request["state"] not in cancellable_states:
        return f"Request {reference} is finishing. Use /status {reference} to check its result."
    requested = request_store.update_if_state(
        reference,
        (request["state"],),
        state="cancellation_requested",
        detail="user_cancelled",
    )
    if requested is None:
        return f"No request with that reference was found for this chat."
    if requested["state"] != "cancellation_requested":
        return f"Request {reference}: {_request_status_text(requested['state'])}."
    request = requested
    session_id = request.get("session_id")
    prompt_message_id = request.get("prompt_message_id")
    if not session_id or not prompt_message_id:
        return f"Cancellation recorded for {reference}. OpenCode has not confirmed the stop yet; use /status."
    try:
        messages = opencode_api.session_messages(
            session_id,
            timeout=OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        if not any(
            isinstance(message, dict)
            and isinstance(message.get("info"), dict)
            and message["info"].get("id") == prompt_message_id
            and message["info"].get("role") == "user"
            for message in messages
        ):
            return f"Cancellation recorded for {reference}; OpenCode has not confirmed submission yet. Use /status."
        aborted = opencode_api.abort_session(
            session_id,
            timeout=OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
    except Exception:
        return f"Request {reference}: cancellation could not be confirmed. It may still be running; use /status."
    if aborted:
        return f"Cancellation requested for {reference}. Use /status {reference} to confirm it has stopped."
    return f"OpenCode did not confirm cancellation for {reference}. Use /status before retrying."


def authorized_response(chat_id, prompt, sessions):
    """Route local slash commands and send regular prompts to OpenCode."""
    command = _command_name(prompt)
    if command == "/start":
        return "Welcome! Send a message to OpenCode. Use /help to see the available commands."
    if command == "/help":
        return help_response()
    if command == "/new":
        return new_session_response(chat_id, sessions)
    if command == "/health":
        return health_response(chat_id, sessions)
    if command == "/cost":
        session_id = sessions.get(str(chat_id))
        if not session_id:
            return "No OpenCode session or cost data yet."
        return cost_reporting.cost_report(session_id)
    if command == "/status":
        try:
            reference = _control_reference(prompt, command)
        except ValueError as error:
            return str(error)
        return request_status_response(chat_id, reference)
    if command in ("/cancel", "/abort"):
        try:
            reference = _control_reference(prompt, command)
        except ValueError as error:
            return str(error)
        if reference is None:
            return cancel_request_response(chat_id)
        return cancel_request_response(chat_id, reference)
    if command in ("/allow", "/allowall", "/deny"):
        return permission_reply_response(chat_id, prompt, command)
    if command is not None:
        return "Unknown command. Use /help to list the available commands."
    return answer(chat_id, prompt, sessions)


def _command_name(prompt):
    """Return a normalized Telegram slash command, or None for a regular message."""
    words = prompt.split(maxsplit=1)
    first_word = words[0] if words else ""
    if not first_word.startswith("/"):
        return None
    return first_word.split("@", 1)[0].lower()


def help_response():
    """List Telegram commands without making an OpenCode request."""
    return "\n".join(
        (
            "Available commands:",
            "/start — welcome message",
            "/help — list available commands",
            "/new — start a new session; the previous session is kept",
            "/health — check bridge, OpenCode API, session, and optional provider reachability",
            "/cost — show cost and usage for this chat's current session",
            "/status [reference] — inspect the active or a previous request",
            "/abort — stop the active request; alias for /cancel",
            "/cancel [reference] — stop the active request or one identified by reference",
            "/allow [letter] — approve that permission once (default when one is pending)",
            "/allowAll <letter> — remember an eligible permission rule",
            "/deny [letter] — reject that permission",
        )
    )


def new_session_response(chat_id, sessions):
    """Create and select a new session while preserving the current session."""
    session = http_api.http(OC + "/session", {"title": _new_telegram_session_title()})
    session_id = session.get("id") if isinstance(session, dict) else None
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("OpenCode returned an invalid session")

    key = str(chat_id)
    with _SESSIONS_LOCK:
        updated_sessions = dict(sessions)
        updated_sessions[key] = session_id
        session_state.save_sessions(updated_sessions)
        sessions[key] = session_id

    return (
        "Started a new OpenCode session. The previous session and its history are kept. "
        "Use /cost to see usage for the new session."
    )


def health_response(chat_id, sessions):
    """Report sanitized bridge dependency and persisted-session status without model calls."""
    lines = ["Bridge: healthy"]
    try:
        health = http_api.http(OC + "/global/health", timeout=OPENCODE_STATUS_TIMEOUT_SECONDS)
        opencode_status = (
            "healthy"
            if isinstance(health, dict) and health.get("healthy") is True
            else "unavailable"
        )
        lines.append(f"OpenCode API: {opencode_status}")
    except Exception:
        lines.append("OpenCode API: unavailable")

    session_id = sessions.get(str(chat_id))
    if not session_id:
        lines.append("This chat's session: not initialized")
    else:
        try:
            session = opencode_api.session_details(session_id, timeout=OPENCODE_STATUS_TIMEOUT_SECONDS)
            available = session.get("id") == session_id
            lines.append("This chat's session: available" if available else "This chat's session: unavailable")
        except Exception:
            lines.append("This chat's session: unavailable")
    lines.append(
        opper_health.health_status()
        if HEALTH_OPPER_ENABLED
        else "Opper API: not checked (disabled)"
    )
    return "\n".join(lines)


def send(chat_id, text, reply_markup=None):
    result = None
    for message in markdown_renderer.render_markdown(text):
        payload = {"chat_id": chat_id, "text": message.html, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            result = http_api.telegram("sendMessage", payload)
        except http_api.TelegramFormattingError:
            fallback = {"chat_id": chat_id, "text": message.plain}
            if reply_markup is not None:
                fallback["reply_markup"] = reply_markup
            result = http_api.telegram("sendMessage", fallback)
    return result


def edit_reply(chat_id, message_id, text, reply_markup=None):
    chunks = markdown_renderer.render_markdown(text)
    if not chunks:
        chunks = markdown_renderer.render_markdown(" ")
    first = chunks[0]
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": first.html,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        http_api.telegram("editMessageText", payload)
    except http_api.TelegramFormattingError:
        fallback = {"chat_id": chat_id, "message_id": message_id, "text": first.plain}
        if reply_markup is not None:
            fallback["reply_markup"] = reply_markup
        http_api.telegram("editMessageText", fallback)
    for chunk in chunks[1:]:
        try:
            http_api.telegram(
                "sendMessage",
                {"chat_id": chat_id, "text": chunk.html, "parse_mode": "HTML"},
            )
        except http_api.TelegramFormattingError:
            http_api.telegram("sendMessage", {"chat_id": chat_id, "text": chunk.plain})


def _safe_error_detail(error):
    if isinstance(error, OpenCodeRequestTimeout):
        return f"deadline_exceeded_s={OPENCODE_REQUEST_MAX_DURATION_SECONDS}"
    if isinstance(error, http_api.OpenCodeHTTPError):
        details = [f"http_status={error.status}"]
        for key, value in (("error_name", error.error_name), ("error_code", error.error_code)):
            if value is not None:
                safe_value = re.sub(r"[^A-Za-z0-9_.-]", "", str(value))[:80]
                if safe_value:
                    details.append(f"{key}={safe_value}")
        if error.validation_fields:
            details.append(f"validation_fields={','.join(error.validation_fields)}")
        if error.validation_codes:
            details.append(f"validation_codes={','.join(error.validation_codes)}")
        return " ".join(details)
    if isinstance(error, opencode_api.OpenCodeResponseError):
        error_name = re.sub(r"[^A-Za-z0-9_.-]", "", error.error_name)[:80] or "unknown"
        error_code = (
            re.sub(r"[^A-Za-z0-9_.-]", "", str(error.error_code))[:80]
            if error.error_code is not None
            else None
        )
        suffix = f" error_code={error_code}" if error_code else ""
        if error.status_code is not None:
            suffix += f" http_status={error.status_code}"
        return f"response_error={error_name}{suffix}"
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, TimeoutError):
            return "url_error=TimeoutError socket_timeout=true"
        if isinstance(reason, OSError):
            return f"url_error={type(reason).__name__} errno={reason.errno}"
        return f"url_error={type(reason).__name__}"
    if isinstance(error, OSError):
        return f"os_error={type(error).__name__} errno={error.errno}"
    if isinstance(error, TimeoutError):
        return "socket_timeout=true"
    if isinstance(error, http_api.TelegramRateLimit):
        return f"retry_after={error.retry_after}"
    if isinstance(error, RuntimeError):
        detail = str(error)
        safe_reasons = (
            r"OpenCode returned an invalid [a-z ]+",
            r"OpenCode returned no text",
            r"OpenCode completed without a correlated assistant response",
            r"Telegram API HTTP [0-9]{3} \([A-Za-z]+\)",
        )
        if any(re.fullmatch(reason, detail) for reason in safe_reasons):
            return f"reason={detail}"
    return "detail=redacted"


def _is_timeout_error(error):
    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, urllib.error.URLError):
        return isinstance(error.reason, TimeoutError)
    return isinstance(error, http_api.OpenCodeHTTPError) and error.status in (408, 504)


def _opencode_failure_text(error, reference):
    timed_out = _is_timeout_error(error)
    if timed_out:
        summary = "OpenCode timed out."
    elif isinstance(error, opencode_api.OpenCodeResponseError):
        status = error.status_code
        if error.error_name == "ProviderAuthError" or status in (401, 403):
            summary = "OpenCode reported a provider authentication/access failure. Ask the operator to check credentials."
        elif status == 429:
            summary = "OpenCode reported a provider rate/quota limit. Ask the operator to check provider limits."
        elif status is not None and status >= 500:
            summary = "OpenCode reported a provider service error. Ask the operator to check provider availability."
        else:
            summary = "OpenCode reported an assistant/provider error."
    elif isinstance(error, (urllib.error.URLError, ConnectionError)):
        summary = "OpenCode API is unreachable. Ask the operator to check the service and network."
    elif isinstance(error, http_api.OpenCodeHTTPError):
        summary = f"OpenCode API rejected the request (HTTP {error.status})."
    else:
        summary = "OpenCode request failed."
    recovery_hint = (
        " An operator can inspect the session using the remote TUI (`make remote-tui`)."
        if timed_out
        else ""
    )
    return (
        f"{summary} The request may still be running; please check before retrying."
        f"{recovery_hint} "
        f"Please share this reference with the operator: {reference}"
    )


def _log_request_error(reference, update_id, phase, started, error):
    if phase.startswith("telegram_"):
        client = "telegram"
    elif phase in ("request_enqueue", "command"):
        client = "bridge"
    else:
        client = "opencode"
    elapsed_ms = int((time.monotonic() - started) * 1000)
    timeout = (
        TELEGRAM_HTTP_TIMEOUT_SECONDS
        if client == "telegram"
        else OPENCODE_REQUEST_MAX_DURATION_SECONDS
        if client == "opencode"
        else None
    )
    timeout_field = f" timeout_s={timeout}" if timeout is not None else ""
    if (
        client == "opencode"
        and phase == "response_wait"
        and _is_timeout_error(error)
        and not isinstance(error, OpenCodeRequestTimeout)
    ):
        timeout_field += f" socket_timeout_s={OPENCODE_STATUS_TIMEOUT_SECONDS}"
    print(
        f"request_failed ref={reference} update_id={update_id} client={client} phase={phase} "
        f"elapsed_ms={elapsed_ms}{timeout_field} error_type={type(error).__name__} "
        f"detail={_safe_error_detail(error)}",
        file=sys.stderr,
        flush=True,
    )


def _request_worker(chat_id, prompt, sessions, update_id, reference, acknowledgement_id, resume=False):
    """Run a queued request and update its Telegram acknowledgement with progress or a result."""
    started = time.monotonic()
    state = {"phase": "response"}
    lock = _chat_lock(chat_id)

    def set_phase(phase):
        bridge_runtime.check_shutdown()
        state["phase"] = phase

    def progress(text):
        set_phase("telegram_progress_edit")
        try:
            presentation = _request_presentation(reference, chat_id, acknowledgement_id)
            if presentation is None:
                edit_reply(chat_id, acknowledgement_id, f"{text}\n\nRef: {reference}")
            else:
                presentation.set_progress(text)
                _edit_request_presentation(presentation)
        except Exception as error:
            _log_request_error(reference, update_id, state["phase"], started, error)
        finally:
            set_phase("response_wait")

    try:
        with lock:
            bridge_runtime.check_shutdown()
            stored = request_store.get(reference)
            if stored is not None and stored["state"] == "cancelled":
                return
            command = _command_name(prompt) if prompt is not None else None
            if resume:
                set_phase("recovery_check")
                response = resume_async_request(stored, set_phase, progress)
            elif command is not None:
                if command == "/new":
                    set_phase("session_create")
                elif command in ("/cost", "/health"):
                    set_phase("opencode_cost")
                else:
                    set_phase("command")
                response = authorized_response(chat_id, prompt, sessions)
            else:
                set_phase("opencode_request")
                response = answer_async(chat_id, prompt, sessions, reference, set_phase, progress)
            response = f"{response.rstrip()}\n\nRef: {reference}"
            request_store.update(reference, state="delivering", detail="completed")
            set_phase("telegram_final_send")
            send(chat_id, response)
            request_store.update(reference, state="completed", detail=None, completed_at=time.time())
            completed = _request_record(reference, chat_id, acknowledgement_id)
            set_phase("telegram_terminal_summary_edit")
            _finalize_request_presentation(completed, "completed", response=response)
            _forget_request_presentation(reference)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        print(
            f"request_completed ref={reference} update_id={update_id} elapsed_ms={elapsed_ms}",
            file=sys.stderr,
            flush=True,
        )
    except bridge_runtime.ShutdownRequested:
        print(
            f"request_interrupted ref={reference} update_id={update_id} reason=bridge_shutdown "
            "opencode_may_still_be_running=true",
            file=sys.stderr,
            flush=True,
        )
    except Exception as error:
        failed_phase = state["phase"]
        _log_request_error(reference, update_id, state["phase"], started, error)
        stored = request_store.get(reference)
        if failed_phase.startswith("telegram_"):
            if stored is not None and stored["state"] == "delivering":
                request_store.update(
                    reference,
                    state="delivery_unknown",
                    detail=stored.get("detail"),
                    completed_at=time.time(),
                )
                _finalize_request_presentation(
                    _request_record(reference, chat_id, acknowledgement_id),
                    "delivery_unknown",
                    response=response if "response" in locals() else None,
                )
                _forget_request_presentation(reference)
            elif stored is not None and stored["state"] in request_store.TERMINAL_STATES:
                _forget_request_presentation(reference)
            return
        if isinstance(error, OpenCodeSubmissionUnconfirmed):
            terminal_state = "failed"
            error_text = (
                f"OpenCode did not confirm request {reference}; it is idle and no matching prompt was found. "
                "Please check /status before retrying."
            )
            request_store.update(reference, state="delivering", detail=terminal_state)
        elif isinstance(error, OpenCodeRequestStopped):
            terminal_state = "cancelled" if error.state == "user_cancelled" else "timed_out"
            limit_seconds = OPENCODE_REQUEST_MAX_DURATION_SECONDS
            if limit_seconds % 86400 == 0:
                limit = f"{limit_seconds // 86400}-day limit"
            elif limit_seconds % 3600 == 0:
                limit = f"{limit_seconds // 3600}-hour limit"
            elif limit_seconds % 60 == 0:
                limit = f"{limit_seconds // 60}-minute limit"
            else:
                limit = f"{limit_seconds}-second limit"
            error_text = (
                f"Request {reference} was cancelled and OpenCode confirmed it stopped."
                if terminal_state == "cancelled"
                else f"Request {reference} reached the configured {limit}; OpenCode confirmed it stopped."
            )
            request_store.update(reference, state="delivering", detail=terminal_state)
        elif stored is not None and stored["state"] == "uncertain":
            terminal_state = "uncertain"
            error_text = _opencode_failure_text(error, reference)
        else:
            terminal_state = "failed"
            if failed_phase.startswith("opencode") or failed_phase in (
                "session_lookup",
                "session_create",
                "history_before",
                "prompt_submit",
                "response_wait",
                "history_after",
                "recovery_check",
            ):
                error_text = _opencode_failure_text(error, reference)
            else:
                error_text = f"Request failed. Please share this reference with the operator: {reference}"
            request_store.update(reference, state="delivering", detail=terminal_state)
        tool_names = getattr(error, "tool_names", None)
        if tool_names is not None:
            error_text = f"{error_text}\n\n{_format_tool_usage(tool_names)}"
        try:
            if terminal_state == "uncertain":
                request_store.update(reference, state="uncertain", detail="stop_unconfirmed")
                if not _append_request_status(chat_id, reference, error_text):
                    send(chat_id, error_text)
                return

            set_phase("telegram_error_send")
            request_store.update(reference, state="delivering", detail=terminal_state)
            _finalize_request_presentation(
                _request_record(reference, chat_id, acknowledgement_id),
                terminal_state,
                error=error,
            )
            request_store.update(
                reference,
                state=terminal_state,
                detail=None,
                completed_at=time.time(),
            )
            send(chat_id, error_text)
            _forget_request_presentation(reference)
        except Exception as delivery_error:
            request_store.update(
                reference,
                state="delivery_unknown",
                detail=terminal_state,
                completed_at=time.time(),
            )
            try:
                _finalize_request_presentation(
                    _request_record(reference, chat_id, acknowledgement_id),
                    "delivery_unknown",
                    error=error,
                )
            except Exception as summary_error:
                _log_request_error(reference, update_id, "telegram_terminal_summary_edit", started, summary_error)
            _forget_request_presentation(reference)
            _log_request_error(reference, update_id, state["phase"], started, delivery_error)


def _request_worker_with_slot(*args):
    _run_with_slot(_request_worker, *args)


def _run_with_slot(function, *args):
    try:
        function(*args)
    finally:
        _REQUEST_SLOTS.release()


def _recovery_notice_worker(chat_id, update_id, reference, acknowledgement_id):
    try:
        edit_reply(
            chat_id,
            acknowledgement_id,
            f"Request {reference} was interrupted before OpenCode received it. Please send it again.",
        )
    except Exception as error:
        _log_request_error(
            reference,
            update_id,
            "telegram_error_edit",
            time.monotonic(),
            error,
        )


def _recover_pending_requests(sessions):
    """Recover submitted requests and safely close work that never reached OpenCode."""
    for request in request_store.active_requests():
        reference = request["reference"]
        chat_id = request["chat_id"]
        state = request["state"]
        acknowledgement_id = request.get("acknowledgement_id")
        if state == "acknowledging":
            request_store.update(
                reference,
                state="acknowledgement_unknown",
                detail="bridge_restarted_during_acknowledgement",
                completed_at=time.time(),
            )
            continue
        if state == "queued":
            request_store.update(
                reference,
                state="interrupted",
                detail="bridge_restarted_before_submission",
                completed_at=time.time(),
            )
            if acknowledgement_id is not None and _REQUEST_SLOTS.acquire(blocking=False):
                try:
                    _REQUEST_EXECUTOR.submit(
                        _run_with_slot,
                        _recovery_notice_worker,
                        chat_id,
                        request["update_id"],
                        reference,
                        acknowledgement_id,
                    )
                except Exception:
                    _REQUEST_SLOTS.release()
            continue
        if state == "delivering":
            # Telegram may have applied the edit before a crash prevented recording success.
            request_store.update(
                reference,
                state="delivery_unknown",
                detail=request.get("detail"),
                completed_at=time.time(),
            )
            continue
        if not request.get("session_id") or not request.get("prompt_message_id"):
            request_store.update(
                reference,
                state="interrupted",
                detail="submission_unconfirmed",
                completed_at=time.time(),
            )
            continue
        recovery_detail = request.get("detail") if state == "cancellation_requested" else None
        request_store.update(reference, state="recovering", detail=recovery_detail)
        if not _REQUEST_SLOTS.acquire(blocking=False):
            request_store.update(reference, state="uncertain", detail="recovery_queue_full")
            continue
        try:
            _REQUEST_EXECUTOR.submit(
                _request_worker_with_slot,
                chat_id,
                None,
                sessions,
                request["update_id"],
                reference,
                acknowledgement_id,
                True,
            )
        except Exception:
            _REQUEST_SLOTS.release()
            request_store.update(reference, state="uncertain", detail="recovery_enqueue_failed")


def _poll_with_recovery(stop, sessions):
    _recover_pending_requests(sessions)
    telegram_polling.poll_updates(stop, handle_update, sessions)


def _answer_callback_query(callback_query_id, text=None):
    if not callback_query_id:
        return
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:180]
    try:
        http_api.telegram("answerCallbackQuery", payload)
    except Exception as error:
        print(
            f"telegram_callback_answer_failed error_type={type(error).__name__}",
            file=sys.stderr,
            flush=True,
        )


def _handle_callback_update(update):
    callback = update.get("callback_query")
    message = callback.get("message") if isinstance(callback, dict) else None
    chat_id = message.get("chat", {}).get("id") if isinstance(message, dict) else None
    callback_id = callback.get("id") if isinstance(callback, dict) else None
    data = callback.get("data") if isinstance(callback, dict) else None
    if chat_id is None or str(chat_id) not in ALLOWED:
        _answer_callback_query(callback_id, "This chat is not authorized.")
        return
    if not request_store.claim_update(update.get("update_id", "unknown")):
        _answer_callback_query(callback_id, "This action was already handled.")
        return
    if not isinstance(data, str):
        _answer_callback_query(callback_id, "This action is invalid.")
        return

    parts = data.split(":")
    if len(parts) < 2 or not re.fullmatch(r"[A-Fa-f0-9]{16}", parts[1]):
        _answer_callback_query(callback_id, "This action is invalid.")
        return
    reference = parts[1].upper()
    request = request_store.get_for_chat(reference, chat_id)
    if request is None:
        _answer_callback_query(callback_id, "This request is no longer available.")
        return

    if parts[0] == "status" and len(parts) == 2:
        _show_request_status(chat_id, reference)
        _answer_callback_query(callback_id, "Status added to the request message.")
        return
    if parts[0] == "abort" and len(parts) == 2:
        _answer_callback_query(callback_id, "Requesting abort.")
        response = cancel_request_response(chat_id, reference)
        _record_control_result(chat_id, reference, response)
        return
    if parts[0] == "p" and len(parts) in (4, 5):
        letter, action = parts[2], parts[3]
        decisions = {"o": "once", "a": "always", "d": "reject"}
        if not re.fullmatch(r"[A-Z]", letter) or action not in decisions:
            _answer_callback_query(callback_id, "This permission action is invalid.")
            return
        decision = decisions[action]
        permission_id = parts[4] if len(parts) == 5 else None
        _answer_callback_query(callback_id, "Applying permission decision.")
        response = _respond_to_permission(
            chat_id,
            reference,
            letter,
            decision,
            expected_permission_id=permission_id,
        )
        _record_control_result(chat_id, reference, response)
        return

    _answer_callback_query(callback_id, "This action is invalid.")


def handle_update(update, sessions):
    """Validate an incoming Telegram update before handing it to the request flow."""
    if isinstance(update, dict) and isinstance(update.get("callback_query"), dict):
        _handle_callback_update(update)
        return
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    prompt = message.get("text", "").strip()
    if chat_id is None or not prompt:
        return
    if str(chat_id) not in ALLOWED:
        print(f"Rejected message for chat_id={chat_id}", file=sys.stderr, flush=True)
        return
    update_id = update.get("update_id", "unknown")
    command = _command_name(prompt)
    if command in ("/status", "/cancel", "/abort", "/allow", "/allowall", "/deny"):
        if not request_store.claim_update(update_id):
            return
        try:
            if command == "/status":
                reference = _control_reference(prompt, command)
                _show_request_status(chat_id, reference)
            elif command in ("/cancel", "/abort"):
                reference = _control_reference(prompt, command)
                if reference is None:
                    active = request_store.active_for_chat(chat_id)
                    target = active[0] if active else None
                else:
                    target = request_store.get_for_chat(reference, chat_id)
                response = cancel_request_response(chat_id, reference)
                if target is None:
                    send(chat_id, response)
                else:
                    _record_control_result(chat_id, target["reference"], response)
            else:
                response = permission_reply_response(chat_id, prompt, command)
                active = request_store.active_for_chat(chat_id)
                if len(active) == 1:
                    _record_control_result(chat_id, active[0]["reference"], response)
                else:
                    send(chat_id, response)
        except ValueError as error:
            send(chat_id, str(error))
        except Exception as error:
            print(
                f"request_control_failed command={command[1:]} error_type={type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            send(chat_id, "The request control command failed. Use /status to check the request.")
        return
    _acknowledge_and_enqueue(chat_id, prompt, sessions, update_id)


def _acknowledge_and_enqueue(chat_id, prompt, sessions, update_id):
    """Acknowledge an authorized message and queue its background worker."""
    existing = request_store.get_by_update_id(update_id)
    if existing is not None:
        return

    active = request_store.active_for_chat(chat_id)
    reference = secrets.token_hex(8).upper()
    if active:
        request_store.create(
            reference,
            chat_id,
            update_id,
            state="rejected",
            detail=f"active:{active[0]['reference']}",
        )
        try:
            send(
                chat_id,
                f"This chat already has an active request ({active[0]['reference']}). "
                "Use the request card or /status and /abort to control it.",
            )
        except Exception as error:
            _log_request_error(active[0]["reference"], update_id, "telegram_acknowledgement", time.monotonic(), error)
        return

    # Record the update before Telegram I/O so redelivery cannot enqueue it twice.
    _, created = request_store.create(reference, chat_id, update_id)
    if not created:
        return
    started = time.monotonic()
    presentation = RequestPresentation(chat_id, None, reference)
    acknowledgement_text, acknowledgement_markup = presentation.edit_payload()
    try:
        # Acknowledge before queuing so users get an immediate response.
        acknowledgement = send(chat_id, acknowledgement_text, reply_markup=acknowledgement_markup)
    except Exception as error:
        request_store.update(
            reference,
            state="acknowledgement_unknown",
            detail="delivery_unconfirmed",
            completed_at=time.time(),
        )
        _log_request_error(reference, update_id, "telegram_acknowledgement", started, error)
        # Telegram may have accepted the send despite a transport timeout. Consume the update
        # rather than retrying it and potentially creating a duplicate acknowledgement.
        return

    acknowledgement_id = acknowledgement.get("message_id") if isinstance(acknowledgement, dict) else None
    if acknowledgement_id is None:
        request_store.update(
            reference,
            state="acknowledgement_unknown",
            detail="missing_message_id",
            completed_at=time.time(),
        )
        print(
            f"request_failed ref={reference} update_id={update_id} client=telegram "
            "phase=telegram_acknowledgement error_type=MissingMessageId detail=redacted",
            file=sys.stderr,
            flush=True,
        )
        return
    presentation.message_id = acknowledgement_id
    _register_request_presentation(presentation)
    request_store.update(reference, state="queued", acknowledgement_id=acknowledgement_id)
    print(
        f"request_accepted ref={reference} update_id={update_id}",
        file=sys.stderr,
        flush=True,
    )
    # Bound queued work before submitting to the executor.
    if not _REQUEST_SLOTS.acquire(blocking=False):
        request_store.update(reference, state="rejected", detail="bridge_queue_full", completed_at=time.time())
        print(
            f"request_rejected ref={reference} update_id={update_id} reason=bridge_queue_full",
            file=sys.stderr,
            flush=True,
        )
        try:
            request = request_store.get(reference)
            _finalize_request_presentation(
                request,
                "rejected",
                "The bridge is busy. Please retry later.",
            )
        except Exception as error:
            _log_request_error(reference, update_id, "telegram_error_edit", started, error)
        return
    try:
        _REQUEST_EXECUTOR.submit(
            _request_worker_with_slot,
            chat_id,
            prompt,
            sessions,
            update_id,
            reference,
            acknowledgement_id,
        )
    except bridge_runtime.ShutdownRequested:
        _REQUEST_SLOTS.release()
        raise
    except Exception as error:
        _REQUEST_SLOTS.release()
        request_store.update(reference, state="failed", detail="enqueue_failed", completed_at=time.time())
        _log_request_error(reference, update_id, "request_enqueue", started, error)
        try:
            edit_reply(
                chat_id,
                acknowledgement_id,
                f"Request failed. Please share this reference with the operator: {reference}",
            )
        except Exception as delivery_error:
            _log_request_error(reference, update_id, "telegram_error_edit", started, delivery_error)


def main(stop_event=None):
    """Poll Telegram updates until shutdown, advancing only past consumed updates."""
    stop_event = stop_event or threading.Event()

    def request_shutdown(signum, _frame):
        print(f"Received {signal.Signals(signum).name}; shutting down.", file=sys.stderr, flush=True)
        stop_event.set()

    previous_handlers = {
        signum: signal.signal(signum, request_shutdown)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    polling_lock = None
    try:
        polling_lock = http_api.acquire_polling_lock(stop_event)
        if polling_lock is None:
            return
        request_store.initialize()
        sessions = session_state.load_sessions()
        _log_timeout_configuration()
        bridge_runtime.run_until_stopped(
            stop_event, _poll_with_recovery, stop_event, sessions,
        )
    finally:
        stop_event.set()
        print(
            "Bridge stopping; queued requests are discarded and in-flight OpenCode work may continue.",
            file=sys.stderr,
            flush=True,
        )
        if polling_lock is not None:
            polling_lock.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _log_timeout_configuration():
    print(
        "HTTP timeouts: "
        f"OpenCode request max={OPENCODE_REQUEST_MAX_DURATION_SECONDS}s "
        f"(status={OPENCODE_STATUS_TIMEOUT_SECONDS}s, events={OPENCODE_EVENT_TIMEOUT_SECONDS}s); "
        f"Telegram={TELEGRAM_HTTP_TIMEOUT_SECONDS}s "
        f"(getUpdates long poll={TELEGRAM_LONG_POLL_TIMEOUT_SECONDS}s).",
        file=sys.stderr,
        flush=True,
    )


if __name__ == "__main__":
    main()
