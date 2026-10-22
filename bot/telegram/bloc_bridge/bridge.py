"""Telegram polling loop and orchestration for the OpenCode bridge."""

import json
import queue
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import uuid

from . import bridge_runtime, http_api, opper_health, session_state
from .opencode import cost_reporting, api as opencode_api
from .telegram import markdown_renderer, polling as telegram_polling
from .bridge_config import (
    ALLOWED,
    HEALTH_OPPER_ENABLED,
    MODEL,
    OC,
    OPENCODE_HTTP_TIMEOUT_SECONDS,
    OPENCODE_STATUS_TIMEOUT_SECONDS,
    OPENCODE_PROGRESS_INTERVAL_SECONDS,
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


class OpenCodeRequestTimeout(TimeoutError):
    """The overall time allowed for one OpenCode exchange elapsed."""


def _chat_lock(chat_id):
    key = str(chat_id)
    with _CHAT_LOCKS_LOCK:
        return _CHAT_LOCKS.setdefault(key, threading.Lock())


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


def _assistant_response_complete(message):
    info = message.get("info", {})
    if not isinstance(info, dict):
        return False
    if info.get("finish") == "tool-calls":
        return False
    message_time = info.get("time")
    created = message_time.get("completed") if isinstance(message_time, dict) else None
    if created:
        return True
    return info.get("finish") in ("stop", "content-filter")


def _session_is_busy(status):
    if isinstance(status, str):
        return status in ("busy", "retry")
    return isinstance(status, dict) and status.get("type") in ("busy", "retry")


def _event_details(event):
    if not isinstance(event, dict):
        return None
    properties = event.get("properties")
    if not isinstance(properties, dict):
        return None
    part = properties.get("part")
    if not isinstance(part, dict):
        part = {}
    session_id = properties.get("sessionID") or part.get("sessionID")
    message_id = properties.get("messageID") or part.get("messageID")
    part_type = part.get("type")
    return session_id, message_id, part_type


def _watch_events(session_id, reference, events, stop):
    error_reported = False
    while not stop.is_set():
        try:
            for event in opencode_api.iter_events():
                if stop.is_set():
                    return
                details = _event_details(event)
                if details is None:
                    continue
                event_session, message_id, part_type = details
                if event_session == session_id and message_id:
                    try:
                        events.put_nowait((message_id, part_type))
                    except queue.Full:
                        pass
            if not stop.is_set():
                stop.wait(1)
        except (OSError, RuntimeError, json.JSONDecodeError) as error:
            if stop.is_set():
                return
            if not error_reported:
                _log_event_stream_error(reference, error)
                error_reported = True
            stop.wait(5)


def _log_event_stream_error(reference, error):
    detail = _safe_error_detail(error)
    print(
        f"opencode_event_stream_retry ref={reference} timeout_s={OPENCODE_EVENT_TIMEOUT_SECONDS} "
        f"error_type={type(error).__name__} detail={detail}",
        file=sys.stderr,
        flush=True,
    )


def answer_async(chat_id, prompt, sessions, reference, set_phase, progress):
    """Submit a prompt asynchronously and collect its correlated result."""
    chat_key = str(chat_id)
    set_phase("session_lookup")
    with _SESSIONS_LOCK:
        session_id = sessions.get(chat_key)
    if not session_id:
        set_phase("session_create")
        session = http_api.http(OC + "/session", {"title": f"Telegram {chat_id}"})
        session_id = session["id"]
        with _SESSIONS_LOCK:
            sessions[chat_key] = session_id
            session_state.save_sessions(sessions)

    set_phase("history_before")
    previous_ids = None
    try:
        previous_ids = _message_ids(opencode_api.session_tree_messages(session_id))
    except Exception as error:
        print(
            f"opencode_history_read_failed ref={reference} phase=before error_type={type(error).__name__}",
            file=sys.stderr,
            flush=True,
        )

    request_body = {"system": SYSTEM, "parts": [{"type": "text", "text": prompt}]}
    _model_options(request_body)
    prompt_message_id = f"msg_{uuid.uuid4().hex}"
    stop_events = threading.Event()
    event_queue = queue.Queue(maxsize=128)
    event_thread = threading.Thread(
        target=_watch_events,
        args=(session_id, reference, event_queue, stop_events),
        name=f"opencode-events-{reference}",
        daemon=True,
    )
    event_thread.start()

    deadline = time.monotonic() + OPENCODE_HTTP_TIMEOUT_SECONDS
    try:
        set_phase("prompt_submit")
        opencode_api.submit_async(
            session_id,
            prompt_message_id,
            request_body,
            OPENCODE_HTTP_TIMEOUT_SECONDS,
        )

        set_phase("response_wait")
        idle_samples = 0
        last_progress = None
        last_progress_at = time.monotonic()
        response_messages = []
        while True:
            bridge_runtime.check_shutdown()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OpenCodeRequestTimeout("OpenCode response deadline exceeded")

            timeout = min(OPENCODE_STATUS_TIMEOUT_SECONDS, remaining)
            statuses = opencode_api.session_status(timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OpenCodeRequestTimeout("OpenCode response deadline exceeded")

            busy = _session_is_busy(statuses.get(session_id))
            idle_samples = 0 if busy else idle_samples + 1
            history_timeout = min(OPENCODE_STATUS_TIMEOUT_SECONDS, remaining)
            response_messages = opencode_api.session_messages(session_id, timeout=history_timeout)
            if time.monotonic() >= deadline:
                raise OpenCodeRequestTimeout("OpenCode response deadline exceeded")
            correlated = opencode_api.assistant_messages_for_prompt(response_messages, prompt_message_id)
            for message in correlated:
                opencode_api.raise_response_error(message)
            assistant_ids = {
                message.get("info", {}).get("id")
                for message in correlated
                if message.get("info", {}).get("id")
            }
            stage = None
            while True:
                try:
                    event_message_id, part_type = event_queue.get_nowait()
                except queue.Empty:
                    break
                if event_message_id not in assistant_ids:
                    continue
                event_stage = {
                    "reasoning": "OpenCode is thinking…",
                    "tool": "OpenCode is working with a tool…",
                }.get(part_type)
                if event_stage:
                    stage = event_stage

            if (
                correlated
                and _assistant_response_complete(correlated[-1])
                and not busy
                and idle_samples >= 2
            ):
                break
            now = time.monotonic()
            if (
                (stage and last_progress is None)
                or now - last_progress_at >= OPENCODE_PROGRESS_INTERVAL_SECONDS
            ):
                elapsed = int(now - (deadline - OPENCODE_HTTP_TIMEOUT_SECONDS))
                text = stage or "Still waiting for OpenCode"
                if last_progress is not None or stage is None:
                    text = f"{text} ({elapsed}s elapsed)."
                progress(text)
                last_progress = stage or "waiting"
                last_progress_at = time.monotonic()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OpenCodeRequestTimeout("OpenCode response deadline exceeded")
            bridge_runtime.wait(min(1, max(0, remaining)))

        set_phase("history_after")
        new_messages = []
        if previous_ids is not None:
            try:
                new_messages = _new_assistant_messages(
                    opencode_api.session_tree_messages(session_id),
                    previous_ids,
                )
            except Exception as error:
                print(
                    f"opencode_history_read_failed ref={reference} phase=after error_type={type(error).__name__}",
                    file=sys.stderr,
                    flush=True,
                )

        response = correlated[-1] if correlated else None
        if response is None:
            raise RuntimeError("OpenCode completed without a correlated assistant response")
        _record_exchange_diagnostics(new_messages, response)
        text = opencode_api.text_from_response(response)
        if previous_ids is None:
            return text + "\n\nCost: unavailable"
        return text + cost_reporting.format_exchange_footer([entry["message"] for entry in new_messages])
    finally:
        stop_events.set()


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
        )
    )


def new_session_response(chat_id, sessions):
    """Create and select a new session while preserving the current session."""
    session = http_api.http(OC + "/session", {"title": f"Telegram {chat_id}"})
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


def send(chat_id, text):
    result = None
    for message in markdown_renderer.render_markdown(text):
        payload = {"chat_id": chat_id, "text": message.html, "parse_mode": "HTML"}
        try:
            result = http_api.telegram("sendMessage", payload)
        except http_api.TelegramFormattingError:
            result = http_api.telegram("sendMessage", {"chat_id": chat_id, "text": message.plain})
    return result


def edit_reply(chat_id, message_id, text):
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
    try:
        http_api.telegram("editMessageText", payload)
    except http_api.TelegramFormattingError:
        http_api.telegram(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": first.plain},
        )
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
        return f"deadline_exceeded_s={OPENCODE_HTTP_TIMEOUT_SECONDS}"
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
        else OPENCODE_HTTP_TIMEOUT_SECONDS
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


def _request_worker(chat_id, prompt, sessions, update_id, reference, acknowledgement_id):
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
            edit_reply(chat_id, acknowledgement_id, f"{text}\n\nRef: {reference}")
        except Exception as error:
            _log_request_error(reference, update_id, state["phase"], started, error)
        finally:
            set_phase("response_wait")

    try:
        with lock:
            bridge_runtime.check_shutdown()
            command = _command_name(prompt)
            if command is not None:
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
            set_phase("telegram_final_edit")
            edit_reply(chat_id, acknowledgement_id, response)
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
        if failed_phase.startswith("telegram_"):
            return
        try:
            set_phase("telegram_error_edit")
            if failed_phase.startswith("opencode") or failed_phase in (
                "session_lookup",
                "session_create",
                "history_before",
                "prompt_submit",
                "response_wait",
                "history_after",
            ):
                error_text = _opencode_failure_text(error, reference)
            else:
                error_text = f"Request failed. Please share this reference with the operator: {reference}"
            edit_reply(
                chat_id,
                acknowledgement_id,
                error_text,
            )
        except Exception as delivery_error:
            _log_request_error(reference, update_id, state["phase"], started, delivery_error)


def _request_worker_with_slot(*args):
    try:
        _request_worker(*args)
    finally:
        _REQUEST_SLOTS.release()


def handle_update(update, sessions):
    """Validate an incoming Telegram update before handing it to the request flow."""
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    prompt = message.get("text", "").strip()
    if chat_id is None or not prompt:
        return
    if str(chat_id) not in ALLOWED:
        print(f"Rejected message for chat_id={chat_id}", file=sys.stderr, flush=True)
        return
    update_id = update.get("update_id", "unknown")
    _acknowledge_and_enqueue(chat_id, prompt, sessions, update_id)


def _acknowledge_and_enqueue(chat_id, prompt, sessions, update_id):
    """Acknowledge an authorized message and queue its background worker."""
    reference = uuid.uuid4().hex[:8].upper()
    started = time.monotonic()
    try:
        # Acknowledge before queuing so users get an immediate response.
        acknowledgement = send(chat_id, f"Request received. Ref: {reference}")
    except Exception as error:
        _log_request_error(reference, update_id, "telegram_acknowledgement", started, error)
        # Telegram may have accepted the send despite a transport timeout. Consume the update
        # rather than retrying it and potentially creating a duplicate acknowledgement.
        return

    acknowledgement_id = acknowledgement.get("message_id") if isinstance(acknowledgement, dict) else None
    if acknowledgement_id is None:
        print(
            f"request_failed ref={reference} update_id={update_id} client=telegram "
            "phase=telegram_acknowledgement error_type=MissingMessageId detail=redacted",
            file=sys.stderr,
            flush=True,
        )
        return
    print(
        f"request_accepted ref={reference} update_id={update_id}",
        file=sys.stderr,
        flush=True,
    )
    # Bound queued work before submitting to the executor.
    if not _REQUEST_SLOTS.acquire(blocking=False):
        print(
            f"request_rejected ref={reference} update_id={update_id} reason=bridge_queue_full",
            file=sys.stderr,
            flush=True,
        )
        try:
            edit_reply(
                chat_id,
                acknowledgement_id,
                f"The bridge is busy. Please retry later. Ref: {reference}",
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
        sessions = session_state.load_sessions()
        _log_timeout_configuration()
        bridge_runtime.run_until_stopped(
            stop_event, telegram_polling.poll_updates, stop_event, handle_update, sessions,
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
        f"OpenCode={OPENCODE_HTTP_TIMEOUT_SECONDS}s "
        f"(status={OPENCODE_STATUS_TIMEOUT_SECONDS}s, events={OPENCODE_EVENT_TIMEOUT_SECONDS}s); "
        f"Telegram={TELEGRAM_HTTP_TIMEOUT_SECONDS}s "
        f"(getUpdates long poll={TELEGRAM_LONG_POLL_TIMEOUT_SECONDS}s).",
        file=sys.stderr,
        flush=True,
    )


if __name__ == "__main__":
    main()
