"""Offline tests for deterministic asynchronous request-monitor decisions."""

import os
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot", "telegram"))
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ALLOWED_CHAT_IDS": "123"}):
    from bloc_bridge.opencode.monitor_policy import AsyncRequestMonitorPolicy, MonitorOutcome


class AsyncRequestMonitorPolicyTests(unittest.TestCase):
    def make_policy(self, max_duration=300, started_at=100, wall_now=100, monotonic_now=10):
        return AsyncRequestMonitorPolicy.start(started_at, max_duration, wall_now, monotonic_now)

    def test_completion_requires_two_idle_samples_and_no_stop_reason(self):
        policy = self.make_policy()
        policy.observe_session_status(busy=False)
        self.assertFalse(policy.request_is_complete(response_complete=True, busy=False))

        policy.observe_session_status(busy=False)
        self.assertTrue(policy.request_is_complete(response_complete=True, busy=False))
        self.assertFalse(policy.request_is_complete(response_complete=True, busy=True))

        policy.update_stop_reason({"state": "cancellation_requested", "detail": "user_cancelled"}, now=11)
        self.assertFalse(policy.request_is_complete(response_complete=True, busy=False))

    def test_recovered_cancellation_requests_abort_for_visible_busy_prompt(self):
        policy = self.make_policy()

        reason_to_persist = policy.update_stop_reason(
            {"state": "recovering", "detail": "user_cancelled"},
            now=20,
        )

        self.assertEqual(reason_to_persist, "user_cancelled")
        self.assertTrue(policy.should_abort(prompt_found=True, busy=True, now=20))
        self.assertFalse(policy.should_abort(prompt_found=False, busy=True, now=20))

    def test_timeout_uses_elapsed_wall_time_and_monotonic_deadline(self):
        policy = self.make_policy(max_duration=100, started_at=100, wall_now=160, monotonic_now=50)

        self.assertIsNone(policy.update_stop_reason({"state": "running"}, now=89))
        self.assertEqual(policy.update_stop_reason({"state": "running"}, now=90), "timed_out")

    def test_stop_waits_for_two_idle_samples_before_confirming_cancellation(self):
        policy = self.make_policy()
        policy.update_stop_reason(
            {"state": "cancellation_requested", "detail": "user_cancelled"},
            now=11,
        )
        policy.record_abort_result(sent=True, now=12)

        policy.observe_session_status(busy=False)
        self.assertIsNone(policy.stop_outcome(prompt_found=True, busy=False))
        policy.observe_session_status(busy=False)
        self.assertEqual(policy.stop_outcome(prompt_found=True, busy=False), "user_cancelled")

    def test_cancelled_request_without_a_visible_prompt_is_not_confirmed_as_stopped(self):
        policy = self.make_policy()
        policy.update_stop_reason(
            {"state": "cancellation_requested", "detail": "user_cancelled"},
            now=11,
        )
        policy.observe_session_status(busy=False)
        policy.observe_session_status(busy=False)

        outcome = policy.stop_outcome(prompt_found=False, busy=False)

        self.assertIs(outcome, MonitorOutcome.SUBMISSION_UNCONFIRMED)

    def test_recovery_fails_missing_prompt_only_after_reconciliation_grace(self):
        policy = self.make_policy(monotonic_now=10)
        policy.observe_session_status(busy=False)
        policy.observe_session_status(busy=False)

        self.assertFalse(policy.missing_prompt_expired(prompt_found=False, busy=False, now=19))
        self.assertTrue(policy.missing_prompt_expired(prompt_found=False, busy=False, now=20))


if __name__ == "__main__":
    unittest.main()
