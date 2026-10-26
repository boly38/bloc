"""Deterministic state and timing decisions for an OpenCode request monitor."""

from dataclasses import dataclass
from enum import Enum, auto


class MonitorOutcome(Enum):
    """A monitor result that is not an OpenCode stop reason."""

    SUBMISSION_UNCONFIRMED = auto()


@dataclass
class AsyncRequestMonitorPolicy:
    """Track monitor deadlines and decide outcomes without performing I/O or waiting."""

    monitor_started_at: float
    deadline: float
    prompt_reconcile_deadline: float
    abort_reason: str | None = None
    abort_deadline: float | None = None
    abort_sent: bool = False
    abort_unavailable: bool = False
    next_abort_at: float = 0
    reconcile_deadline: float | None = None
    reconcile_reported: bool = False
    idle_samples: int = 0

    @classmethod
    def start(cls, started_at, max_duration, wall_now, monotonic_now):
        """Build a policy using supplied clock readings, including time spent before recovery."""
        elapsed_duration = max(0, wall_now - started_at)
        remaining_duration = max(0, max_duration - elapsed_duration)
        return cls(
            monitor_started_at=monotonic_now,
            deadline=monotonic_now + remaining_duration,
            prompt_reconcile_deadline=monotonic_now + 10,
        )

    def update_stop_reason(self, request, now):
        """Apply persisted cancellation or timeout state; return a reason to persist, if any."""
        cancellation_pending = request and (
            request["state"] == "cancellation_requested"
            or (
                request["state"] == "recovering"
                and request.get("detail") in ("user_cancelled", "user_cancelled_unconfirmed")
            )
        )
        if cancellation_pending and request.get("detail") in ("user_cancelled", "user_cancelled_unconfirmed"):
            if self.abort_reason != "user_cancelled":
                self.abort_reason = "user_cancelled"
                self.abort_sent = False
                self.abort_unavailable = False
                self.next_abort_at = now
        elif cancellation_pending and self.abort_reason is None:
            self.abort_reason = request.get("detail") or "cancelled"

        if self.abort_reason is None and now >= self.deadline:
            self.abort_reason = "timed_out"
        if self.abort_reason is not None and self.reconcile_deadline is None:
            self.reconcile_deadline = now + 60
            return self.abort_reason
        return None

    def observe_session_status(self, busy):
        """Count consecutive idle samples used to confirm completion or a stop."""
        self.idle_samples = 0 if busy else self.idle_samples + 1

    def request_is_complete(self, response_complete, busy):
        return self.abort_reason is None and response_complete and not busy and self.idle_samples >= 2

    def should_abort(self, prompt_found, busy, now):
        return (
            self.abort_reason is not None
            and prompt_found
            and busy
            and not self.abort_sent
            and not self.abort_unavailable
            and now >= self.next_abort_at
        )

    def record_abort_result(self, sent, now):
        self.abort_sent = sent
        if sent:
            self.abort_deadline = now + 60
        else:
            self.next_abort_at = now + 30

    def record_abort_error(self, retryable, now):
        if retryable:
            self.next_abort_at = now + 30
        else:
            self.abort_unavailable = True

    def stop_outcome(self, prompt_found, busy):
        """Return the confirmed stop reason, submission failure, or None while unresolved."""
        if self.abort_reason is None or busy or self.idle_samples < 2:
            return None
        if prompt_found or self.abort_sent:
            return self.abort_reason
        return MonitorOutcome.SUBMISSION_UNCONFIRMED

    def missing_prompt_expired(self, prompt_found, busy, now):
        return (
            self.abort_reason is None
            and not prompt_found
            and not busy
            and self.idle_samples >= 2
            and now >= self.prompt_reconcile_deadline
        )

    def reconciliation_expired(self, prompt_found, now):
        if (
            self.reconcile_deadline is not None
            and not prompt_found
            and not self.reconcile_reported
            and now >= self.reconcile_deadline
        ):
            self.reconcile_reported = True
            return True
        return False

    def abort_expired(self, now):
        if self.abort_deadline is None or now < self.abort_deadline:
            return False
        self.abort_deadline = None
        self.abort_sent = False
        self.next_abort_at = now + 30
        return True
