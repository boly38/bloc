"""Telegram polling loop and orchestration for the OpenCode bridge."""

from concurrent.futures import ThreadPoolExecutor
import json
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import uuid

import cost_reporting
import http_api
import markdown_renderer
import opencode_api
import session_state
from bridge_config import (
    ALLOWED,
    MODEL,
    OC,
    OPENCODE_HTTP_TIMEOUT_SECONDS,
    OPENCODE_STATUS_TIMEOUT_SECONDS,
    SYSTEM,
    TELEGRAM_HTTP_TIMEOUT_SECONDS,
    TELEGRAM_LONG_POLL_TIMEOUT_SECONDS,
    OPENCODE_EVENT_TIMEOUT_SECONDS,
)


_REQUEST_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="telegram-request")
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
        except (TimeoutError, OSError, urllib.error.URLError, RuntimeError, json.JSONDecodeError) as error:
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
    prompt_message_id = uuid.uuid4().hex
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
        response_messages = []
        while True:
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
            assistant_ids = {
                message.get("info", {}).get("id")
                for message in correlated
                if message.get("info", {}).get("id")
            }
            while True:
                try:
                    event_message_id, part_type = event_queue.get_nowait()
                except queue.Empty:
                    break
                if event_message_id not in assistant_ids:
                    continue
                stage = {
                    "reasoning": "OpenCode is thinking…",
                    "tool": "OpenCode is working with a tool…",
                }.get(part_type)
                if stage and stage != last_progress:
                    progress(stage)
                    last_progress = stage

            if (
                correlated
                and _assistant_response_complete(correlated[-1])
                and not busy
                and idle_samples >= 2
            ):
                break
            time.sleep(min(1, max(0, remaining)))

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
    command = prompt.split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command == "/start":
        return "Hello. Send your request to OpenCode."
    if command == "/cost":
        session_id = sessions.get(str(chat_id))
        if not session_id:
            return "No OpenCode session or cost data yet."
        return cost_reporting.cost_report(session_id)
    return answer(chat_id, prompt, sessions)


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
        return " ".join(details)
    if isinstance(error, opencode_api.OpenCodeResponseError):
        error_name = re.sub(r"[^A-Za-z0-9_.-]", "", error.error_name)[:80] or "unknown"
        error_code = (
            re.sub(r"[^A-Za-z0-9_.-]", "", str(error.error_code))[:80]
            if error.error_code is not None
            else None
        )
        suffix = f" error_code={error_code}" if error_code else ""
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
    started = time.monotonic()
    state = {"phase": "response"}
    lock = _chat_lock(chat_id)

    def set_phase(phase):
        state["phase"] = phase

    def progress(text):
        set_phase("telegram_progress_edit")
        try:
            edit_reply(chat_id, acknowledgement_id, f"{text}\n\nRef: {reference}")
        except Exception as error:
            _log_request_error(reference, update_id, state["phase"], started, error)
            set_phase("response_wait")

    try:
        with lock:
            command = prompt.split(maxsplit=1)[0].split("@", 1)[0].lower()
            if command == "/start":
                set_phase("command")
                response = authorized_response(chat_id, prompt, sessions)
            elif command == "/cost":
                set_phase("opencode_cost")
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
                if _is_timeout_error(error):
                    error_text = (
                        "OpenCode timed out. The request may still be running; "
                        f"please check before retrying. Ref: {reference}"
                    )
                else:
                    error_text = f"OpenCode request failed. Please share this reference with the operator: {reference}"
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
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    prompt = message.get("text", "").strip()
    if chat_id is None or not prompt:
        return True
    if str(chat_id) not in ALLOWED:
        print(f"Rejected message for chat_id={chat_id}", file=sys.stderr, flush=True)
        return True
    update_id = update.get("update_id", "unknown")
    reference = uuid.uuid4().hex[:8].upper()
    started = time.monotonic()
    try:
        acknowledgement = send(chat_id, f"Request received. Ref: {reference}")
    except Exception as error:
        _log_request_error(reference, update_id, "telegram_acknowledgement", started, error)
        # Telegram may have accepted the send despite a transport timeout. Consume the update
        # rather than retrying it and potentially creating a duplicate acknowledgement.
        return True

    acknowledgement_id = acknowledgement.get("message_id") if isinstance(acknowledgement, dict) else None
    if acknowledgement_id is None:
        print(
            f"request_failed ref={reference} update_id={update_id} client=telegram "
            "phase=telegram_acknowledgement error_type=MissingMessageId detail=redacted",
            file=sys.stderr,
            flush=True,
        )
        return True
    print(
        f"request_accepted ref={reference} update_id={update_id}",
        file=sys.stderr,
        flush=True,
    )
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
        return True
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
    return True


def main():
    polling_lock = http_api.acquire_polling_lock()
    sessions = session_state.load_sessions()
    _log_timeout_configuration()

    offset = 0
    conflict_backoff = 5
    while True:
        try:
            updates = http_api.telegram(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": TELEGRAM_LONG_POLL_TIMEOUT_SECONDS,
                    "allowed_updates": ["message"],
                },
            ) or []
            conflict_backoff = 5
            for update in updates:
                if not handle_update(update, sessions):
                    break
                offset = update["update_id"] + 1
        except http_api.TelegramConflict as error:
            print(f"Telegram polling conflict: {error}; retrying in {conflict_backoff}s.", file=sys.stderr, flush=True)
            time.sleep(conflict_backoff)
            conflict_backoff = min(conflict_backoff * 2, 60)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            print(f"Telegram network error ({type(error).__name__}); retrying in 5s.", file=sys.stderr, flush=True)
            time.sleep(5)


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
