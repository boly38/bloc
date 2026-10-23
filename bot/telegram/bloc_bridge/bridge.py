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
_PROGRESS_EVENT_MIN_INTERVAL_SECONDS = 5
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


class OpenCodeRequestTimeout(TimeoutError):
    """The overall time allowed for one OpenCode exchange elapsed."""


class OpenCodeRequestStopped(RuntimeError):
    """OpenCode confirmed that an active request stopped."""

    def __init__(self, state, tool_names=()):
        self.state = state
        self.tool_names = tuple(tool_names)
        super().__init__("OpenCode request stopped")


class OpenCodeSubmissionUnconfirmed(RuntimeError):
    """OpenCode is idle but the submitted prompt cannot be found in its history."""


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


def _tool_names(messages):
    """Return unique tool and recognized CLI names, without inputs or outputs."""
    names = []
    seen = set()
    for entry in messages:
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            message = entry if isinstance(entry, dict) else {}
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            tool_name = part.get("tool")
            if isinstance(tool_name, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", tool_name):
                normalized = tool_name.lower()
                if normalized not in seen:
                    names.append(normalized)
                    seen.add(normalized)
            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            tool_input = state.get("input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if tool_name == "bash" and isinstance(command, str) and _GH_CLI_COMMAND.search(command):
                if "gh" not in seen:
                    names.append("gh")
                    seen.add("gh")
    return names


def _format_tool_usage(tool_names):
    safe_names = [
        name.lower()
        for name in tool_names
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", name)
    ]
    return f"Tools used: {', '.join(dict.fromkeys(safe_names)) if safe_names else 'none'}"


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


def _new_telegram_session_title():
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"Telegram {timestamp}__{uuid.uuid4().hex}"


def answer(chat_id, prompt, sessions):
    key = str(chat_id)
    session_id = sessions.get(key)
    if not session_id:
        session = http_api.http(OC + "/session", {"title": _new_telegram_session_title()})
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
    tool_name = part.get("tool") if part_type == "tool" else None
    tool_state = part.get("state") if part_type == "tool" else None
    tool_state = tool_state if isinstance(tool_state, dict) else {}
    tool_status = tool_state.get("status")
    tool_input = tool_state.get("input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    is_test_command = tool_name == "bash" and isinstance(command, str) and bool(_TEST_COMMAND.search(command))
    is_gh_command = tool_name == "bash" and isinstance(command, str) and bool(_GH_CLI_COMMAND.search(command))
    return session_id, message_id, part_type, tool_name, tool_status, is_test_command, is_gh_command


def _event_progress_stage(
    part_type,
    tool_name=None,
    tool_status=None,
    is_test_command=False,
    is_gh_command=False,
):
    if part_type == "reasoning":
        return "Analysis in progress"
    if part_type != "tool":
        return None

    if is_test_command:
        label = "Tests"
    elif is_gh_command:
        label = "GitHub CLI"
    elif isinstance(tool_name, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", tool_name):
        label = _TOOL_PROGRESS_LABELS.get(tool_name.lower(), f"Tool {tool_name}")
    else:
        label = "Tool"
    suffix = {
        "completed": "complete",
        "error": "failed",
    }.get(tool_status, "in progress")
    return f"{label} {suffix}"


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
                (
                    event_session,
                    message_id,
                    part_type,
                    tool_name,
                    tool_status,
                    is_test_command,
                    is_gh_command,
                ) = details
                if event_session == session_id and message_id:
                    try:
                        events.put_nowait(
                            (message_id, part_type, tool_name, tool_status, is_test_command, is_gh_command)
                        )
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


def _retryable_status_error(error):
    return isinstance(error, (TimeoutError, urllib.error.URLError)) or (
        isinstance(error, http_api.OpenCodeHTTPError)
        and (error.status in (408, 429) or error.status >= 500)
    )


@dataclass(frozen=True)
class _MonitorSnapshot:
    """One consistent status/history sample from OpenCode."""

    busy: bool
    prompt_found: bool
    correlated_messages: list[dict]


class _AsyncRequestMonitor:
    """Own the state and lifecycle for monitoring one submitted OpenCode prompt."""

    def __init__(self, session_id, prompt_message_id, reference, started_at):
        self.session_id = session_id
        self.prompt_message_id = prompt_message_id
        self.reference = reference
        self.started_at = started_at
        self.events_to_stop = threading.Event()
        self.events = queue.Queue(maxsize=128)
        self.event_thread = threading.Thread(
            target=_watch_events,
            args=(session_id, reference, self.events, self.events_to_stop),
            name=f"opencode-events-{reference}",
            daemon=True,
        )

        self.monitor_started_at = time.monotonic()
        # Use the persisted wall clock across restarts, then a monotonic deadline locally.
        elapsed_duration = max(0, time.time() - started_at)
        remaining_duration = max(0, OPENCODE_REQUEST_MAX_DURATION_SECONDS - elapsed_duration)
        self.deadline = self.monitor_started_at + remaining_duration
        self.prompt_reconcile_deadline = self.monitor_started_at + 10

        self.abort_reason = None
        self.abort_deadline = None
        self.abort_sent = False
        self.abort_unavailable = False
        self.next_abort_at = 0
        self.reconcile_deadline = None
        self.reconcile_reported = False
        self.idle_samples = 0
        self.reported_read_error = False

        self.current_stage = None
        self.reported_stage = None
        self.reported_cost = None
        self.last_progress_at = time.monotonic()

    def run(self, set_phase, progress):
        """Poll until completion or an OpenCode stop is confirmed."""
        self.event_thread.start()
        try:
            set_phase("response_wait")
            while True:
                bridge_runtime.check_shutdown()
                self._update_stop_reason()
                try:
                    snapshot = self._read_snapshot()
                except Exception as error:
                    self._handle_read_error(error)
                    bridge_runtime.wait(1)
                    continue

                self._raise_correlated_response_error(snapshot)
                self._update_progress_stage(snapshot.correlated_messages)
                if self._is_request_complete(snapshot):
                    return snapshot.correlated_messages

                self._request_abort(snapshot)
                self._resolve_stop_outcome(snapshot)
                self._reconcile_missing_prompt(snapshot)
                self._update_stop_deadlines(snapshot)
                self._report_progress(snapshot, progress)
                bridge_runtime.wait(1)
        finally:
            self.events_to_stop.set()

    def _update_stop_reason(self):
        """Apply user cancellation or the request deadline to monitor state."""
        request = request_store.get(self.reference) if self.reference else None
        cancellation_pending = request and (
            request["state"] == "cancellation_requested"
            or (request["state"] == "recovering" and request.get("detail") == "user_cancelled")
        )
        if cancellation_pending and request.get("detail") == "user_cancelled":
            if self.abort_reason != "user_cancelled":
                self.abort_reason = "user_cancelled"
                self.abort_sent = False
                self.abort_unavailable = False
                self.next_abort_at = time.monotonic()
        elif cancellation_pending and self.abort_reason is None:
            self.abort_reason = request.get("detail") or "cancelled"

        if self.abort_reason is None and time.monotonic() >= self.deadline:
            self.abort_reason = "timed_out"
        if self.abort_reason is not None and self.reconcile_deadline is None:
            self.reconcile_deadline = time.monotonic() + 60
            request_store.update(
                self.reference,
                state="cancellation_requested",
                detail=self.abort_reason,
            )

    def _read_snapshot(self):
        """Read session activity and history, then correlate the active prompt."""
        statuses = opencode_api.session_status(OPENCODE_STATUS_TIMEOUT_SECONDS)
        busy = _session_is_busy(statuses.get(self.session_id))
        messages = opencode_api.session_messages(
            self.session_id,
            timeout=OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        self.idle_samples = 0 if busy else self.idle_samples + 1
        prompt_found = any(
            isinstance(message, dict)
            and isinstance(message.get("info"), dict)
            and message["info"].get("id") == self.prompt_message_id
            and message["info"].get("role") == "user"
            for message in messages
        )
        correlated = opencode_api.assistant_messages_for_prompt(
            messages,
            self.prompt_message_id,
        )
        return _MonitorSnapshot(busy, prompt_found, correlated)

    def _handle_read_error(self, error):
        """Log polling failures safely and mark unconfirmed stop states."""
        if not self.reported_read_error:
            diagnostic = (
                "opencode_status_retry"
                if _retryable_status_error(error)
                else "opencode_status_unavailable"
            )
            print(
                f"{diagnostic} ref={self.reference} error_type={type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            self.reported_read_error = True
        if not _retryable_status_error(error):
            request_store.update(self.reference, state="uncertain", detail="status_unavailable")

        now = time.monotonic()
        if self.abort_deadline is not None and now >= self.abort_deadline:
            request_store.update(self.reference, state="uncertain", detail="stop_unconfirmed")
            self.abort_deadline = None
            self.abort_sent = False
            self.next_abort_at = now + 30
        self._report_unconfirmed_reconciliation(now)

    def _raise_correlated_response_error(self, snapshot):
        """Surface provider errors unless a timeout or cancellation is stopping work."""
        # An abort may itself be recorded as an assistant error. Once stopping
        # was requested, the confirmed stop reason takes precedence.
        if self.abort_reason is not None:
            return
        try:
            for message in snapshot.correlated_messages:
                opencode_api.raise_response_error(message)
        except opencode_api.OpenCodeResponseError as error:
            error.tool_names = _tool_names(snapshot.correlated_messages)
            raise

    def _update_progress_stage(self, correlated_messages):
        """Consume relevant SSE events and retain the latest deterministic stage."""
        assistant_ids = {
            message.get("info", {}).get("id")
            for message in correlated_messages
            if message.get("info", {}).get("id")
        }
        while True:
            try:
                (
                    event_message_id,
                    part_type,
                    tool_name,
                    tool_status,
                    is_test_command,
                    is_gh_command,
                ) = self.events.get_nowait()
            except queue.Empty:
                return
            if event_message_id not in assistant_ids:
                continue
            stage = _event_progress_stage(
                part_type,
                tool_name,
                tool_status,
                is_test_command,
                is_gh_command,
            )
            if stage:
                self.current_stage = stage

    def _is_request_complete(self, snapshot):
        return (
            self.abort_reason is None
            and snapshot.correlated_messages
            and _assistant_response_complete(snapshot.correlated_messages[-1])
            and not snapshot.busy
            and self.idle_samples >= 2
        )

    def _request_abort(self, snapshot):
        """Abort only after the submitted prompt is visible in the busy session."""
        if not (
            self.abort_reason is not None
            and snapshot.prompt_found
            and snapshot.busy
            and not self.abort_sent
            and not self.abort_unavailable
            and time.monotonic() >= self.next_abort_at
        ):
            return

        try:
            # Abort is session-wide, so require this job's prompt in history first.
            self.abort_sent = opencode_api.abort_session(
                self.session_id,
                timeout=OPENCODE_STATUS_TIMEOUT_SECONDS,
            )
            if self.abort_sent:
                self.abort_deadline = time.monotonic() + 60
            else:
                self.next_abort_at = time.monotonic() + 30
        except Exception as error:
            request_store.update(self.reference, state="uncertain", detail="abort_unconfirmed")
            if _retryable_status_error(error):
                self.next_abort_at = time.monotonic() + 30
            else:
                self.abort_unavailable = True

    def _resolve_stop_outcome(self, snapshot):
        """Resolve a requested stop, or fail if the submitted prompt is unconfirmed."""
        if self.abort_reason is None or snapshot.busy or self.idle_samples < 2:
            return
        if snapshot.prompt_found or self.abort_sent:
            raise OpenCodeRequestStopped(self.abort_reason, _tool_names(snapshot.correlated_messages))
        request_store.update(
            self.reference,
            state="failed",
            detail="submission_unconfirmed",
            completed_at=time.time(),
        )
        raise OpenCodeSubmissionUnconfirmed("OpenCode submission could not be confirmed")

    def _reconcile_missing_prompt(self, snapshot):
        """Fail safely when OpenCode is idle but never confirms the submitted prompt."""
        if not (
            self.abort_reason is None
            and not snapshot.prompt_found
            and not snapshot.busy
            and self.idle_samples >= 2
            and time.monotonic() >= self.prompt_reconcile_deadline
        ):
            return
        request_store.update(
            self.reference,
            state="failed",
            detail="submission_unconfirmed",
            completed_at=time.time(),
        )
        raise OpenCodeSubmissionUnconfirmed("OpenCode submission could not be confirmed")

    def _update_stop_deadlines(self, snapshot):
        """Mark abort or submission reconciliation as uncertain when its grace period ends."""
        now = time.monotonic()
        if (
            self.reconcile_deadline is not None
            and not snapshot.prompt_found
            and not self.reconcile_reported
            and now >= self.reconcile_deadline
        ):
            request_store.update(self.reference, state="uncertain", detail="submission_unconfirmed")
            self.reconcile_reported = True
        if self.abort_deadline is not None and now >= self.abort_deadline:
            request_store.update(self.reference, state="uncertain", detail="stop_unconfirmed")
            self.abort_deadline = None
            self.abort_sent = False
            self.next_abort_at = now + 30

    def _report_unconfirmed_reconciliation(self, now):
        """Record a submission that remains unconfirmed during a polling failure."""
        if (
            self.reconcile_deadline is not None
            and not self.reconcile_reported
            and now >= self.reconcile_deadline
        ):
            request_store.update(self.reference, state="uncertain", detail="submission_unconfirmed")
            self.reconcile_reported = True

    def _report_progress(self, snapshot, progress):
        """Send throttled stage, heartbeat, and observed-cost updates."""
        correlated = snapshot.correlated_messages
        observed_cost = cost_reporting.format_observed_cost(correlated)
        stage_pending = self.current_stage is not None and self.current_stage != self.reported_stage
        cost_pending = observed_cost is not None and observed_cost != self.reported_cost
        interval_elapsed = time.monotonic() - self.last_progress_at
        heartbeat_due = interval_elapsed >= OPENCODE_PROGRESS_INTERVAL_SECONDS
        first_update = self.reported_stage is None and self.reported_cost is None
        change_due = (stage_pending or cost_pending) and (
            first_update or interval_elapsed >= _PROGRESS_EVENT_MIN_INTERVAL_SECONDS
        )
        response_finishing = (
            correlated
            and _assistant_response_complete(correlated[-1])
            and not snapshot.busy
        )
        if not (change_due or heartbeat_due) or response_finishing:
            return

        elapsed = int(time.monotonic() - self.monitor_started_at)
        text = f"{self.current_stage or 'OpenCode is working on your request'} ({elapsed}s elapsed)."
        cost_line = observed_cost or self.reported_cost
        if cost_line:
            text = f"{text}\n{cost_line}"
        progress(text)
        self.reported_stage = self.current_stage
        if observed_cost is not None:
            self.reported_cost = observed_cost
        self.last_progress_at = time.monotonic()


def _monitor_async_request(session_id, prompt_message_id, reference, set_phase, progress, started_at):
    """Monitor one submitted prompt until it completes or OpenCode confirms it stopped."""
    monitor = _AsyncRequestMonitor(session_id, prompt_message_id, reference, started_at)
    return monitor.run(set_phase, progress)


def _format_async_result(session_id, correlated, previous_ids, reference):
    response = correlated[-1] if correlated else None
    if response is None:
        raise RuntimeError("OpenCode completed without a correlated assistant response")

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
    if not new_messages:
        new_messages = [{"message": message} for message in correlated]

    _record_exchange_diagnostics(new_messages, response)
    text = opencode_api.text_from_response(response)
    cost_footer = (
        "\n\nCost: unavailable"
        if previous_ids is None
        else cost_reporting.format_exchange_footer([entry["message"] for entry in new_messages])
    )
    return f"{text}{cost_footer}\n\n{_format_tool_usage(_tool_names(new_messages))}"


def answer_async(chat_id, prompt, sessions, reference, set_phase, progress):
    """Submit a prompt asynchronously and collect its correlated result."""
    chat_key = str(chat_id)
    set_phase("session_lookup")
    with _SESSIONS_LOCK:
        session_id = sessions.get(chat_key)
    if not session_id:
        set_phase("session_create")
        session = http_api.http(OC + "/session", {"title": _new_telegram_session_title()})
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
    started_at = time.time()
    # Persist correlation before submission so recovery monitors instead of replaying the prompt.
    submission = request_store.update_if_state(
        reference,
        ("queued", "recovering", "uncertain", "submitting"),
        state="submitting",
        session_id=session_id,
        prompt_message_id=prompt_message_id,
        started_at=started_at,
        detail=None,
    )
    if submission is not None and submission["state"] != "submitting":
        raise OpenCodeRequestStopped("user_cancelled")
    set_phase("prompt_submit")
    try:
        current = request_store.get(reference)
        if current is not None and current["state"] == "cancellation_requested":
            raise OpenCodeRequestStopped("user_cancelled")
        opencode_api.submit_async(
            session_id,
            prompt_message_id,
            request_body,
            OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        request_store.update_if_state(reference, ("submitting",), state="running", detail=None)
    except Exception:
        current = request_store.get(reference)
        if current is None or current["state"] != "cancellation_requested":
            request_store.update(reference, state="uncertain", detail="submission_unconfirmed")
        raise

    correlated = _monitor_async_request(
        session_id,
        prompt_message_id,
        reference,
        set_phase,
        progress,
        started_at,
    )
    set_phase("history_after")
    return _format_async_result(session_id, correlated, previous_ids, reference)


def resume_async_request(request, set_phase, progress):
    """Resume monitoring a submitted OpenCode prompt without resubmitting it."""
    session_id = request.get("session_id")
    prompt_message_id = request.get("prompt_message_id")
    reference = request["reference"]
    started_at = request.get("started_at") or request["created_at"]
    if not session_id or not prompt_message_id:
        request_store.update(
            reference,
            state="failed",
            detail="submission_unconfirmed",
            completed_at=time.time(),
        )
        raise OpenCodeSubmissionUnconfirmed("OpenCode submission could not be confirmed")

    set_phase("recovery_check")
    try:
        statuses = opencode_api.session_status(OPENCODE_STATUS_TIMEOUT_SECONDS)
        messages = opencode_api.session_messages(
            session_id,
            timeout=OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
    except Exception as error:
        if not _retryable_status_error(error):
            request_store.update(reference, state="uncertain", detail="recovery_unavailable")
            raise
        statuses = None
        messages = None

    prompt_found = messages is not None and any(
        isinstance(message, dict)
        and isinstance(message.get("info"), dict)
        and message["info"].get("id") == prompt_message_id
        and message["info"].get("role") == "user"
        for message in messages
    )
    busy = statuses is None or _session_is_busy(statuses.get(session_id))
    if not prompt_found and not busy:
        request_store.update(reference, state="failed", detail="submission_unconfirmed", completed_at=time.time())
        raise OpenCodeSubmissionUnconfirmed("OpenCode submission could not be confirmed")

    if not (request["state"] == "recovering" and request.get("detail") == "user_cancelled"):
        request_store.update(reference, state="running", detail=None)
    correlated = _monitor_async_request(
        session_id,
        prompt_message_id,
        reference,
        set_phase,
        progress,
        started_at,
    )
    set_phase("history_after")
    return _format_async_result(session_id, correlated, None, reference)


def _control_reference(prompt, command):
    words = prompt.split()
    if len(words) == 1:
        return None
    if len(words) != 2 or not re.fullmatch(r"[A-Fa-f0-9]{16}", words[1]):
        raise ValueError(f"Usage: {command} [request-reference]")
    return words[1].upper()


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
            acknowledgement_id = cancelled.get("acknowledgement_id")
            if acknowledgement_id is not None:
                try:
                    edit_reply(
                        chat_id,
                        acknowledgement_id,
                        f"Request {reference} was cancelled before it started.",
                    )
                except Exception as error:
                    _log_request_error(
                        reference,
                        cancelled["update_id"],
                        "telegram_error_edit",
                        time.monotonic(),
                        error,
                    )
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
    if command == "/cancel":
        try:
            reference = _control_reference(prompt, command)
        except ValueError as error:
            return str(error)
        if reference is None:
            return cancel_request_response(chat_id)
        return cancel_request_response(chat_id, reference)
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
            "/cancel [reference] — stop the active request or one identified by reference",
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
            edit_reply(
                chat_id,
                acknowledgement_id,
                f"{text}\n\nUse /status to check progress or /cancel to stop.\nRef: {reference}",
            )
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
            set_phase("telegram_final_edit")
            response = f"{response.rstrip()}\n\nRef: {reference}"
            request_store.update(reference, state="delivering", detail="completed")
            edit_reply(chat_id, acknowledgement_id, response)
            request_store.update(reference, state="completed", detail=None, completed_at=time.time())
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
            set_phase("telegram_error_edit")
            edit_reply(
                chat_id,
                acknowledgement_id,
                error_text,
            )
            if terminal_state != "uncertain":
                request_store.update(
                    reference,
                    state=terminal_state,
                    detail=None,
                    completed_at=time.time(),
                )
            else:
                request_store.update(reference, state="uncertain", detail="stop_unconfirmed")
        except Exception as delivery_error:
            if terminal_state != "uncertain":
                request_store.update(
                    reference,
                    state="delivery_unknown",
                    detail=terminal_state,
                    completed_at=time.time(),
                )
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
    command = _command_name(prompt)
    if command in ("/status", "/cancel"):
        if not request_store.claim_update(update_id):
            return
        try:
            reference = _control_reference(prompt, command)
            if command == "/status":
                response = request_status_response(chat_id, reference)
            elif reference is None:
                response = cancel_request_response(chat_id)
            else:
                response = cancel_request_response(chat_id, reference)
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
                "Use /status to check progress or /cancel to stop it.",
            )
        except Exception as error:
            _log_request_error(active[0]["reference"], update_id, "telegram_acknowledgement", time.monotonic(), error)
        return

    # Record the update before Telegram I/O so redelivery cannot enqueue it twice.
    _, created = request_store.create(reference, chat_id, update_id)
    if not created:
        return
    started = time.monotonic()
    try:
        # Acknowledge before queuing so users get an immediate response.
        acknowledgement = send(
            chat_id,
            f"Request received. Use /status to check progress or /cancel to stop. Ref: {reference}",
        )
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
