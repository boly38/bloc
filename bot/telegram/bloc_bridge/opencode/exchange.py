"""OpenCode exchange orchestration independent of Telegram command routing."""

import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import bridge_runtime, http_api, request_store, session_state
from ..bridge_config import (
    MODEL,
    OC,
    OPENCODE_EVENT_TIMEOUT_SECONDS,
    OPENCODE_PROGRESS_INTERVAL_SECONDS,
    OPENCODE_REQUEST_MAX_DURATION_SECONDS,
    OPENCODE_STATUS_TIMEOUT_SECONDS,
    SYSTEM,
)
from . import api as opencode_api
from . import cost_reporting
from . import monitor_policy
from . import tooling


_SESSIONS_LOCK = threading.RLock()
_PROGRESS_EVENT_MIN_INTERVAL_SECONDS = 5
_PROGRESS_AGE_DISPLAY_THRESHOLD_SECONDS = 5
_PERMISSION_POLL_INTERVAL_SECONDS = 5
_PERMISSION_SESSION_LOOKUP_TIMEOUT_SECONDS = 3
_PERMISSION_ANCESTRY_MAX_DEPTH = 8
class OpenCodeRequestTimeout(TimeoutError):
    """The overall time allowed for one OpenCode exchange elapsed."""


class OpenCodeRequestStopped(RuntimeError):
    """OpenCode confirmed that an active request stopped."""

    def __init__(self, state, tool_names=(), tool_call_count=0):
        self.state = state
        self.tool_names = tuple(tool_names)
        self.tool_call_count = tool_call_count
        super().__init__("OpenCode request stopped")


class OpenCodeSubmissionUnconfirmed(RuntimeError):
    """OpenCode is idle but the submitted prompt cannot be found in its history."""


_presentation_lookup = lambda _reference: None
_session_title = lambda: _new_telegram_session_title()


def set_presentation_lookup(callback):
    """Install the bridge-owned request-card lookup used for permission updates."""
    global _presentation_lookup
    _presentation_lookup = callback


def set_session_title(callback):
    global _session_title
    _session_title = callback


def _request_presentation(reference):
    return _presentation_lookup(reference)


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


_tool_names = tooling.tool_names
_tool_call_count = tooling.tool_call_count
_format_tool_usage = tooling.format_tool_usage


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
        session = http_api.http(OC + "/session", {"title": _session_title()})
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
    tool_name, tool_status, is_test_command, is_gh_command = tooling.event_tool_metadata(part)
    return session_id, message_id, part_type, tool_name, tool_status, is_test_command, is_gh_command


def _event_progress_stage(
    part_type,
    tool_name=None,
    tool_status=None,
    is_test_command=False,
    is_gh_command=False,
):
    return tooling.progress_stage(part_type, tool_name, tool_status, is_test_command, is_gh_command)


def _permission_signature(permissions):
    entries = [
        (permission["id"], permission.get("permission"))
        for permission in permissions
        if isinstance(permission, dict) and isinstance(permission.get("id"), str)
    ]
    return tuple(sorted(entries, key=lambda entry: entry[0]))


def _permission_belongs_to_session_tree(session_id, root_session_id, known_session_ids):
    """Check bounded session ancestry so child-agent prompts stay with their request."""
    current_session_id = session_id
    traversed = []
    for _ in range(_PERMISSION_ANCESTRY_MAX_DEPTH):
        if current_session_id == root_session_id or current_session_id in known_session_ids:
            known_session_ids.update(traversed)
            known_session_ids.add(current_session_id)
            return True
        details = opencode_api.session_details(
            current_session_id,
            timeout=_PERMISSION_SESSION_LOOKUP_TIMEOUT_SECONDS,
        )
        parent_session_id = details.get("parentID")
        if not isinstance(parent_session_id, str) or not parent_session_id:
            return False
        traversed.append(current_session_id)
        current_session_id = parent_session_id
    return False


def _permissions_in_session_tree(root_session_id, requests, related_sessions, unrelated_sessions):
    matches = []
    for request in requests:
        if not isinstance(request, dict):
            continue
        session_id = request.get("sessionID")
        if not isinstance(session_id, str) or session_id in unrelated_sessions:
            continue
        if _permission_belongs_to_session_tree(session_id, root_session_id, related_sessions):
            matches.append(request)
        else:
            unrelated_sessions.add(session_id)
    return sorted(matches, key=lambda request: request.get("id", ""))


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


def _safe_error_detail(error):
    if isinstance(error, http_api.OpenCodeHTTPError):
        return f"http_status={error.status}"
    if isinstance(error, urllib.error.URLError):
        return f"url_error={type(error.reason).__name__}"
    return f"error_type={type(error).__name__}"


def _retryable_status_error(error):
    return isinstance(error, (TimeoutError, urllib.error.URLError)) or (
        isinstance(error, http_api.OpenCodeHTTPError)
        and (error.status in (408, 429) or error.status >= 500)
    )


def _assistant_activity_signature(messages):
    """Describe observable message changes without retaining message text."""
    signature = []
    for message in messages:
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict):
            continue
        message_time = info.get("time")
        message_time = message_time if isinstance(message_time, dict) else {}
        tokens = info.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        parts_signature = []
        parts = message.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                state = part.get("state")
                state = state if isinstance(state, dict) else {}
                text = part.get("text")
                output = state.get("output")
                parts_signature.append(
                    (
                        part.get("type"),
                        part.get("tool"),
                        state.get("status"),
                        len(text) if isinstance(text, str) else None,
                        len(output) if isinstance(output, str) else None,
                    )
                )
        error = info.get("error")
        signature.append(
            (
                info.get("id"),
                info.get("finish"),
                message_time.get("completed"),
                error.get("name") if isinstance(error, dict) else None,
                info.get("cost"),
                tokens.get("input"),
                tokens.get("output"),
                tuple(parts_signature),
            )
        )
    return tuple(signature)


@dataclass(frozen=True)
class _MonitorSnapshot:
    """One consistent status/history sample from OpenCode."""

    busy: bool
    prompt_found: bool
    correlated_messages: list[dict]
    pending_permissions: list[dict]


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

        self.policy = monitor_policy.AsyncRequestMonitorPolicy.start(
            started_at,
            OPENCODE_REQUEST_MAX_DURATION_SECONDS,
            time.time(),
            time.monotonic(),
        )
        self.monitor_started_at = self.policy.monitor_started_at
        self.reported_read_error = False

        self.current_stage = None
        self.reported_stage = None
        self.reported_cost = None
        self.last_progress_at = time.monotonic()
        self.last_opencode_activity_at = self.monitor_started_at
        self.last_api_success_at = self.monitor_started_at
        self.last_activity_signature = None
        self.last_snapshot = _MonitorSnapshot(True, False, [], [])
        self.pending_permissions = []
        self.related_permission_sessions = {session_id}
        self.unrelated_permission_sessions = set()
        self.last_permission_poll_at = self.monitor_started_at - _PERMISSION_POLL_INTERVAL_SECONDS
        self.reported_permission_signature = ()
        self.reported_permission_error = False
        self.reported_permission_session_error = False

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
                    self._report_progress(self.last_snapshot, progress, api_responding=False)
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
        reason_to_persist = self.policy.update_stop_reason(request, time.monotonic())
        if reason_to_persist is not None:
            request_store.update(
                self.reference,
                state="cancellation_requested",
                detail=reason_to_persist,
            )

    def _read_snapshot(self):
        """Read session activity and history, then correlate the active prompt."""
        statuses = opencode_api.session_status(OPENCODE_STATUS_TIMEOUT_SECONDS)
        busy = _session_is_busy(statuses.get(self.session_id))
        messages = opencode_api.session_messages(
            self.session_id,
            timeout=OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        self.policy.observe_session_status(busy)
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
        self._poll_pending_permissions()
        now = time.monotonic()
        self.last_api_success_at = now
        activity_signature = _assistant_activity_signature(correlated)
        if activity_signature != self.last_activity_signature:
            self.last_activity_signature = activity_signature
            self.last_opencode_activity_at = now
        snapshot = _MonitorSnapshot(busy, prompt_found, correlated, list(self.pending_permissions))
        self.last_snapshot = snapshot
        return snapshot

    def _poll_pending_permissions(self):
        """Refresh permission prompts for this request's session at a bounded cadence."""
        now = time.monotonic()
        if now - self.last_permission_poll_at < _PERMISSION_POLL_INTERVAL_SECONDS:
            return
        self.last_permission_poll_at = now
        try:
            requests = opencode_api.pending_permissions(timeout=OPENCODE_STATUS_TIMEOUT_SECONDS)
        except Exception as error:
            if not self.reported_permission_error:
                print(
                    f"opencode_permission_poll_failed ref={self.reference} "
                    f"error_type={type(error).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                self.reported_permission_error = True
            return

        self.reported_permission_error = False
        previous = self.pending_permissions
        try:
            self.pending_permissions = _permissions_in_session_tree(
                self.session_id,
                requests,
                self.related_permission_sessions,
                self.unrelated_permission_sessions,
            )
        except Exception as error:
            if not self.reported_permission_session_error:
                print(
                    f"permission_session_lookup_failed ref={self.reference} "
                    f"error_type={type(error).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                self.reported_permission_session_error = True
            return
        else:
            self.reported_permission_session_error = False
        if _permission_signature(previous) != _permission_signature(self.pending_permissions):
            self.last_opencode_activity_at = time.monotonic()
        if previous and not self.pending_permissions:
            self.current_stage = "Permission decision submitted; OpenCode is resuming"
        presentation = _request_presentation(self.reference)
        if presentation is not None:
            presentation.sync_permissions(self.pending_permissions)

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
        if self.policy.abort_expired(now):
            request_store.update(self.reference, state="uncertain", detail="stop_unconfirmed")
        self._report_unconfirmed_reconciliation(now)

    def _raise_correlated_response_error(self, snapshot):
        """Surface provider errors unless a timeout or cancellation is stopping work."""
        # An abort may itself be recorded as an assistant error. Once stopping
        # was requested, the confirmed stop reason takes precedence.
        if self.policy.abort_reason is not None:
            return
        try:
            for message in snapshot.correlated_messages:
                opencode_api.raise_response_error(message)
        except opencode_api.OpenCodeResponseError as error:
            error.tool_names = _tool_names(snapshot.correlated_messages)
            error.tool_call_count = _tool_call_count(snapshot.correlated_messages)
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
            self.last_opencode_activity_at = time.monotonic()
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
        response_complete = bool(
            snapshot.correlated_messages
            and _assistant_response_complete(snapshot.correlated_messages[-1])
        )
        return self.policy.request_is_complete(response_complete, snapshot.busy)

    def _request_abort(self, snapshot):
        """Abort only after the submitted prompt is visible in the busy session."""
        if not (
            self.policy.should_abort(
                snapshot.prompt_found,
                snapshot.busy,
                time.monotonic(),
            )
        ):
            return

        try:
            # Abort is session-wide, so require this job's prompt in history first.
            abort_sent = opencode_api.abort_session(
                self.session_id,
                timeout=OPENCODE_STATUS_TIMEOUT_SECONDS,
            )
            self.policy.record_abort_result(abort_sent, time.monotonic())
        except Exception as error:
            request_store.update(self.reference, state="uncertain", detail="abort_unconfirmed")
            self.policy.record_abort_error(_retryable_status_error(error), time.monotonic())

    def _resolve_stop_outcome(self, snapshot):
        """Resolve a requested stop, or fail if the submitted prompt is unconfirmed."""
        outcome = self.policy.stop_outcome(snapshot.prompt_found, snapshot.busy)
        if outcome is None:
            return
        if outcome is not monitor_policy.MonitorOutcome.SUBMISSION_UNCONFIRMED:
            raise OpenCodeRequestStopped(
                outcome,
                _tool_names(snapshot.correlated_messages),
                _tool_call_count(snapshot.correlated_messages),
            )
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
            self.policy.missing_prompt_expired(
                snapshot.prompt_found,
                snapshot.busy,
                time.monotonic(),
            )
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
        if self.policy.reconciliation_expired(snapshot.prompt_found, now):
            request_store.update(self.reference, state="uncertain", detail="submission_unconfirmed")
        if self.policy.abort_expired(now):
            request_store.update(self.reference, state="uncertain", detail="stop_unconfirmed")

    def _report_unconfirmed_reconciliation(self, now):
        """Record a submission that remains unconfirmed during a polling failure."""
        if self.policy.reconciliation_expired(prompt_found=False, now=now):
            request_store.update(self.reference, state="uncertain", detail="submission_unconfirmed")

    def _report_progress(self, snapshot, progress, api_responding=True):
        """Send throttled stage, heartbeat, and observed-cost updates."""
        correlated = snapshot.correlated_messages
        now = time.monotonic()
        permission_signature = _permission_signature(snapshot.pending_permissions)
        observed_cost = cost_reporting.format_observed_cost(correlated)
        stage_pending = self.current_stage is not None and self.current_stage != self.reported_stage
        cost_pending = observed_cost is not None and observed_cost != self.reported_cost
        permission_pending = permission_signature != self.reported_permission_signature
        interval_elapsed = now - self.last_progress_at
        heartbeat_due = interval_elapsed >= OPENCODE_PROGRESS_INTERVAL_SECONDS
        first_update = (
            self.reported_stage is None
            and self.reported_cost is None
            and not self.reported_permission_signature
        )
        change_due = (stage_pending or cost_pending or permission_pending) and (
            first_update or interval_elapsed >= _PROGRESS_EVENT_MIN_INTERVAL_SECONDS
        )
        response_finishing = api_responding and (
            correlated
            and _assistant_response_complete(correlated[-1])
            and not snapshot.busy
        )
        if not (change_due or heartbeat_due) or response_finishing:
            return

        elapsed = int(now - self.monitor_started_at)
        activity_age = int(now - self.last_opencode_activity_at)
        api_age = int(now - self.last_api_success_at)
        if snapshot.pending_permissions:
            count = len(snapshot.pending_permissions)
            permission_label = "permission" if count == 1 else "permissions"
            text = f"OpenCode is waiting for {count} {permission_label} ({elapsed}s elapsed)."
        else:
            text = f"{self.current_stage or 'OpenCode is working on your request'} ({elapsed}s elapsed)."
        if (
            activity_age > _PROGRESS_AGE_DISPLAY_THRESHOLD_SECONDS
            or api_age > _PROGRESS_AGE_DISPLAY_THRESHOLD_SECONDS
        ):
            api_heartbeat = (
                f"API heartbeat: {api_age}s ago"
                if api_responding
                else f"API last responded: {api_age}s ago"
            )
            text = f"{text}\nOpenCode activity: {activity_age}s ago; {api_heartbeat}."
        cost_line = observed_cost or self.reported_cost
        if cost_line:
            text = f"{text}\n{cost_line}"
        progress(text)
        self.reported_stage = self.current_stage
        if observed_cost is not None:
            self.reported_cost = observed_cost
        self.reported_permission_signature = permission_signature
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
    return (
        f"{text}{cost_footer}\n\n{_format_tool_usage(_tool_names(new_messages))}"
        f"\nTool calls: {_tool_call_count(new_messages)}"
    )


def answer_async(chat_id, prompt, sessions, reference, set_phase, progress):
    """Submit a prompt asynchronously and collect its correlated result."""
    chat_key = str(chat_id)
    set_phase("session_lookup")
    with _SESSIONS_LOCK:
        session_id = sessions.get(chat_key)
    if not session_id:
        set_phase("session_create")
        session = http_api.http(OC + "/session", {"title": _session_title()})
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
