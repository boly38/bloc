"""Offline checks for optional provider reachability and long-request feedback."""

import io
import os
import runpy
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot", "telegram"))
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ALLOWED_CHAT_IDS": "123"}):
    from bloc_bridge import bridge, bridge_runtime, http_api, opper_health, request_store
    from bloc_bridge.opencode import api as opencode_api


class OpperHealthTests(unittest.TestCase):
    def test_environment_flag_defaults_to_disabled_and_explicitly_enables_probe(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "bot", "telegram", "bloc_bridge", "bridge_config.py",
        )
        for value, expected in ((None, False), ("false", False), ("true", True), (" TRUE ", True)):
            with self.subTest(value=value):
                with patch.dict(os.environ, {
                    "TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                }):
                    os.environ.pop("TELEGRAM_HEALTH_OPPER_ENABLED", None)
                    if value is not None:
                        os.environ["TELEGRAM_HEALTH_OPPER_ENABLED"] = value
                    self.assertEqual(runpy.run_path(config_path)["HEALTH_OPPER_ENABLED"], expected)

    def test_disabled_health_never_contacts_opper(self):
        with patch.object(bridge, "HEALTH_OPPER_ENABLED", False), \
             patch.object(http_api, "http", return_value={"healthy": True}), \
             patch.object(opper_health, "health_status") as probe:
            result = bridge.authorized_response(123, "/health", {})
        probe.assert_not_called()
        self.assertIn("Opper API: not checked (disabled)", result)

    def test_enabled_health_includes_probe_even_when_opencode_is_unavailable(self):
        with patch.object(bridge, "HEALTH_OPPER_ENABLED", True), \
             patch.object(http_api, "http", side_effect=ConnectionRefusedError()), \
             patch.object(opper_health, "health_status", return_value="Opper API: reachable") as probe, \
             patch.object(opencode_api, "submit_async") as submit:
            result = bridge.authorized_response(123, "/health", {})
        probe.assert_called_once_with()
        submit.assert_not_called()
        self.assertIn("OpenCode API: unavailable", result)
        self.assertIn("Opper API: reachable", result)

    def test_probe_uses_no_credentials_body_redirects_or_retries(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(opper_health.urllib.request, "build_opener", return_value=opener) as build:
            result = opper_health.health_status()

        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.opper.ai/v3/compat/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.headers, {})
        self.assertIsNone(request.data)
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 5})
        response.read.assert_not_called()
        self.assertIsInstance(build.call_args.args[0], opper_health._NoRedirect)
        self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 302, "", {}, "https://invalid"))
        self.assertIn("reachable (authentication and inference not checked)", result)

    def test_http_statuses_are_explicit_and_error_bodies_are_not_read(self):
        for status, expected in (
            (401, "reachable (authentication and inference not checked)"),
            (403, "check failed (HTTP 403"),
            (302, "check failed (HTTP 302"),
            (429, "rate limited (HTTP 429"),
            (503, "check failed (HTTP 503"),
        ):
            with self.subTest(status=status):
                body = Mock()
                error = urllib.error.HTTPError(opper_health.URL, status, "private detail", {}, body)
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(opper_health.urllib.request, "build_opener", return_value=opener):
                    result = opper_health.health_status()
                self.assertIn(expected, result)
                self.assertNotIn("private detail", result)
                body.read.assert_not_called()
                body.close.assert_called_once()
                opener.open.assert_called_once()

    def test_transport_errors_are_sanitized_and_not_retried(self):
        for error, expected in (
            (TimeoutError("private"), "timed out"),
            (urllib.error.URLError(TimeoutError("private")), "timed out"),
            (urllib.error.URLError("private DNS detail"), "unreachable from the bridge"),
            (ConnectionRefusedError("private"), "unreachable from the bridge"),
        ):
            with self.subTest(error=type(error).__name__):
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(opper_health.urllib.request, "build_opener", return_value=opener):
                    result = opper_health.health_status()
                self.assertIn(expected, result)
                self.assertNotIn("private", result)
                opener.open.assert_called_once()

    def test_shutdown_is_not_swallowed(self):
        opener = Mock()
        opener.open.side_effect = bridge_runtime.ShutdownRequested()
        with patch.object(opper_health.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(bridge_runtime.ShutdownRequested):
                opper_health.health_status()

    def test_unresponsive_api_times_out_without_waiting_for_response_body(self):
        stop = threading.Event()
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            server.settimeout(2)

            def stall():
                connection, _ = server.accept()
                with connection:
                    connection.recv(4096)
                    stop.wait(2)

            thread = threading.Thread(target=stall, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.getsockname()[1]}/models"
                with patch.object(opper_health, "URL", url), \
                     patch.object(opper_health, "TIMEOUT_SECONDS", 0.1), \
                     patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}):
                    started = time.monotonic()
                    result = opper_health.health_status()
                    elapsed = time.monotonic() - started
                self.assertIn("timed out", result)
                self.assertLess(elapsed, 1)
            finally:
                stop.set()
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())


class RequestFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.request_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.request_directory.cleanup)
        self.clock = 0
        self.progress = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(
                request_store,
                "REQUESTS_PATH",
                os.path.join(self.request_directory.name, "requests.sqlite3"),
            )
        )
        request_store.initialize()
        self.stack.enter_context(patch.object(bridge, "OPENCODE_REQUEST_MAX_DURATION_SECONDS", 300))
        self.stack.enter_context(patch.object(bridge.time, "monotonic", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(bridge_runtime, "wait", side_effect=self.advance))
        self.stack.enter_context(patch.object(bridge.uuid, "uuid4", return_value=SimpleNamespace(hex="prompt")))
        self.stack.enter_context(patch.object(opencode_api, "session_tree_messages", return_value=[]))
        self.stack.enter_context(patch.object(opencode_api, "submit_async"))
        self.permissions = self.stack.enter_context(
            patch.object(opencode_api, "pending_permissions", return_value=[])
        )
        self.stack.enter_context(patch.object(bridge, "_watch_events"))
        self.probe = self.stack.enter_context(patch.object(opper_health, "health_status"))
        self.status = self.stack.enter_context(patch.object(opencode_api, "session_status", return_value={}))
        self.messages = self.stack.enter_context(
            patch.object(
                opencode_api,
                "session_messages",
                return_value=[{"info": {"id": "msg_prompt", "role": "user"}}],
            )
        )
        self.abort = self.stack.enter_context(patch.object(opencode_api, "abort_session", return_value=False))

    def advance(self, seconds):
        self.clock += seconds

    def answer(self, progress=None):
        return bridge.answer_async(
            123, "private prompt", {"123": "private-session"}, "REF12345",
            lambda _phase: None, progress or (lambda text: self.progress.append((self.clock, text))),
        )

    def completed(self):
        return {
            "info": {
                "role": "assistant", "id": "assistant", "parentID": "msg_prompt",
                "time": {"completed": 123},
            },
            "parts": [{"type": "text", "text": "Done"}],
        }

    def test_silent_request_emits_periodic_feedback_and_succeeds_near_deadline(self):
        prompt = {"info": {"id": "msg_prompt", "role": "user"}}
        self.messages.side_effect = lambda *_args, **_kwargs: (
            [prompt, self.completed()] if self.clock >= 299 else [prompt]
        )
        result = self.answer()
        self.assertIn("Done", result)
        self.assertEqual(self.clock, 299)
        self.assertEqual([when for when, _ in self.progress], list(range(30, 300, 30)))
        self.assertIn("270s elapsed", self.progress[-1][1])
        self.probe.assert_not_called()

    def test_request_can_complete_after_five_minutes(self):
        with patch.object(bridge, "OPENCODE_REQUEST_MAX_DURATION_SECONDS", 600):
            prompt = {"info": {"id": "msg_prompt", "role": "user"}}
            self.messages.side_effect = lambda *_args, **_kwargs: (
                [prompt, self.completed()] if self.clock >= 400 else [prompt]
            )
            result = self.answer()

        self.assertIn("Done", result)
        self.assertEqual(self.clock, 400)
        self.abort.assert_not_called()

    def test_transient_status_http_timeout_is_retried(self):
        self.status.side_effect = [urllib.error.URLError(TimeoutError()), {}, {}]
        self.messages.side_effect = [
            [{"info": {"id": "msg_prompt", "role": "user"}}, self.completed()],
            [{"info": {"id": "msg_prompt", "role": "user"}}, self.completed()],
        ]

        result = self.answer()

        self.assertIn("Done", result)
        self.assertEqual(self.clock, 2)
        self.abort.assert_not_called()

    def test_heartbeat_and_delivery_time_never_reset_deadline(self):
        def slow_progress(text):
            self.progress.append((self.clock, text))
            self.advance(5)

        with self.assertRaises(bridge.OpenCodeRequestStopped):
            self.answer(slow_progress)
        self.assertEqual(self.clock, 300)
        self.assertGreater(len(self.progress), 1)
        self.assertTrue(all(when < 300 for when, _ in self.progress))
        self.abort.assert_not_called()
        self.probe.assert_not_called()
        self.assertTrue(all(0 < item.args[0] <= 10 for item in self.status.call_args_list))

    def test_repeated_progress_events_are_throttled_and_do_not_extend_deadline(self):
        event_queues = []

        def watch(_session, _reference, events, _stop):
            event_queues.append(events)

        def statuses(_timeout):
            if event_queues:
                event_queues[0].put(("assistant", "tool", "bash", "running", False, False))
            return {"private-session": {"type": "busy"}} if self.clock < 330 else {}

        self.messages.return_value = [
            {"info": {"id": "msg_prompt", "role": "user"}},
            {
                "info": {"role": "assistant", "id": "assistant", "parentID": "msg_prompt"},
                "parts": [],
            },
        ]
        self.status.side_effect = statuses
        # Run the watcher synchronously so event ordering is deterministic.
        thread = Mock()
        with patch.object(bridge.threading, "Thread", return_value=thread) as create_thread:
            def start():
                kwargs = create_thread.call_args.kwargs
                watch(*kwargs["args"])
            thread.start.side_effect = start
            with self.assertRaises(bridge.OpenCodeRequestStopped):
                self.answer()

        self.assertEqual(self.clock, 331)
        self.assertEqual([when for when, _ in self.progress], list(range(0, 331, 30)))
        self.assertTrue(all("Command in progress" in text for _, text in self.progress))
        self.assertEqual(len({text for _, text in self.progress}), len(self.progress))
        self.abort.assert_called_once_with("private-session", timeout=10)

    def test_progress_includes_cost_reported_for_completed_model_calls(self):
        prompt = {"info": {"id": "msg_prompt", "role": "user"}}
        assistant = {
            "info": {
                "role": "assistant",
                "id": "assistant",
                "parentID": "msg_prompt",
                "time": {"completed": 123},
                "cost": 0.000123,
            },
            "parts": [{"type": "text", "text": "Continuing"}],
        }
        self.messages.return_value = [prompt, assistant]
        self.status.side_effect = [{"private-session": {"type": "busy"}}, {}, {}]

        result = self.answer()

        self.assertIn("Continuing", result)
        self.assertTrue(self.progress)
        self.assertIn("Cost observed so far: $0.000123 (partial)", self.progress[0][1])

    def test_progress_distinguishes_stale_agent_activity_from_live_api_heartbeat(self):
        prompt = {"info": {"id": "msg_prompt", "role": "user"}}
        self.messages.side_effect = lambda *_args, **_kwargs: (
            [prompt, self.completed()] if self.clock >= 70 else [prompt]
        )
        self.status.side_effect = lambda _timeout: (
            {"private-session": {"type": "busy"}} if self.clock < 70 else {}
        )

        self.answer()

        self.assertEqual([when for when, _ in self.progress], [30, 60])
        self.assertIn("OpenCode activity: 30s ago; API heartbeat: 0s ago.", self.progress[0][1])
        self.assertIn("OpenCode activity: 60s ago; API heartbeat: 0s ago.", self.progress[1][1])

    def test_progress_hides_recent_api_and_opencode_ages_until_over_five_seconds(self):
        snapshot = bridge.exchange._MonitorSnapshot(True, False, [], [])
        updates = []

        for age in (4, 5, 6):
            monitor = bridge._AsyncRequestMonitor(
                "private-session",
                "private-prompt",
                "REF12345",
                time.time(),
            )
            monitor.current_stage = "Analysis in progress"
            monitor.monitor_started_at = 0
            monitor.last_progress_at = -30
            monitor.last_opencode_activity_at = 0
            monitor.last_api_success_at = 0
            with patch.object(bridge.time, "monotonic", return_value=age):
                monitor._report_progress(snapshot, updates.append)

        self.assertEqual(updates[0], "Analysis in progress (4s elapsed).")
        self.assertEqual(
            updates[1],
            "Analysis in progress (5s elapsed).",
        )
        self.assertEqual(
            updates[2],
            "Analysis in progress (6s elapsed).\n"
            "OpenCode activity: 6s ago; API heartbeat: 6s ago.",
        )

    def test_progress_reports_age_of_last_api_response_during_polling_errors(self):
        prompt = {"info": {"id": "msg_prompt", "role": "user"}}

        def statuses(_timeout):
            if self.clock == 0:
                return {"private-session": {"type": "busy"}}
            if self.clock < 61:
                raise urllib.error.URLError(TimeoutError())
            return {}

        self.status.side_effect = statuses
        self.messages.side_effect = lambda *_args, **_kwargs: (
            [prompt, self.completed()] if self.clock >= 61 else [prompt]
        )

        self.answer()

        self.assertEqual([when for when, _ in self.progress], [30, 60])
        self.assertIn("OpenCode activity: 30s ago; API last responded: 30s ago.", self.progress[0][1])
        self.assertIn("API last responded: 60s ago", self.progress[1][1])

    def test_monitor_surfaces_pending_permission_with_explicit_commands(self):
        reference = "FEDCBA9876543210"
        permission = {
            "id": "private-permission-id",
            "sessionID": "private-session",
            "permission": "webfetch",
            "patterns": ["https://docs.example.test/config"],
            "always": ["*"],
        }
        self.permissions.return_value = [permission]
        presentation = bridge._request_presentation(reference, 123, 509)
        monitor = bridge._AsyncRequestMonitor(
            "private-session",
            "msg_prompt",
            reference,
            bridge.time.time(),
        )
        with patch.object(opencode_api, "session_status", return_value={"private-session": {"type": "busy"}}):
            with patch.object(
                opencode_api,
                "session_messages",
                return_value=[{"info": {"id": "msg_prompt", "role": "user"}}],
            ):
                with patch.object(opencode_api, "respond_to_permission") as respond:
                    snapshot = monitor._read_snapshot()
        monitor._report_progress(snapshot, presentation.set_progress)
        text, keyboard = presentation.edit_payload()

        self.assertIn("OpenCode is waiting for 1 permission", text)
        self.assertIn("/allow A", text)
        self.assertIn("/allowAll A", text)
        self.assertIn("/deny A", text)
        self.assertIn("Always A", [button["text"] for row in keyboard["inline_keyboard"] for button in row])
        respond.assert_not_called()
        bridge._forget_request_presentation(reference)

    def test_correlated_provider_failure_surfaces_before_completion_or_idle(self):
        self.status.return_value = {"private-session": {"type": "busy"}}
        self.messages.return_value = [
            {"info": {"id": "msg_prompt", "role": "user"}},
            {
                "info": {
                    "role": "assistant", "id": "assistant", "parentID": "msg_prompt",
                    "error": {"name": "APIError", "data": {
                        "statusCode": 503, "message": "private credentials and prompt",
                        "responseBody": "private response",
                    }},
                },
                "parts": [],
            },
        ]
        with self.assertRaises(opencode_api.OpenCodeResponseError) as caught:
            self.answer()
        self.assertEqual(self.clock, 0)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("private", str(caught.exception))
        self.assertIn("http_status=503", bridge._safe_error_detail(caught.exception))

    def test_unrelated_assistant_error_is_ignored(self):
        other = {"info": {
            "role": "assistant", "id": "other", "parentID": "other-prompt",
            "error": {"name": "ProviderAuthError", "data": {"message": "private"}},
        }}
        self.messages.return_value = [other, self.completed()]
        self.assertIn("Done", self.answer())

    def test_response_with_partial_text_and_error_is_not_reported_as_success(self):
        message = self.completed()
        message["info"]["error"] = {"name": "ProviderAuthError", "data": {"message": "private"}}
        with self.assertRaises(opencode_api.OpenCodeResponseError):
            opencode_api.text_from_response(message)

    def test_error_categories_reach_user_with_reference_not_provider_text(self):
        for error, expected in (
            (opencode_api.OpenCodeResponseError("ProviderAuthError"), "authentication/access"),
            (opencode_api.OpenCodeResponseError("APIError", status_code=401), "authentication/access"),
            (opencode_api.OpenCodeResponseError("APIError", status_code=429), "rate/quota"),
            (opencode_api.OpenCodeResponseError("APIError", status_code=503), "provider service error"),
            (opencode_api.OpenCodeResponseError("private-provider-text"), "assistant/provider error"),
            (urllib.error.URLError("private-network-detail"), "API is unreachable"),
            (http_api.OpenCodeHTTPError(400), "HTTP 400"),
            (bridge.OpenCodeRequestTimeout(), "timed out"),
        ):
            with self.subTest(expected=expected):
                output = io.StringIO()
                with patch.object(bridge, "answer_async", side_effect=error), \
                     patch.object(bridge, "edit_reply") as edit, \
                     patch.object(bridge, "send") as send, \
                     patch("sys.stderr", output):
                    bridge._request_worker(123, "private-prompt", {}, 123, "REF12345", 456)
                reply = send.call_args.args[1]
                self.assertIn(expected, reply)
                self.assertIn("REF12345", reply)
                self.assertNotIn("private", reply)
                self.assertNotIn("private-prompt", output.getvalue())

    def test_successful_progress_edit_restores_opencode_phase(self):
        def exchange(_chat, _prompt, _sessions, _ref, _phase, progress):
            progress("Still waiting")
            raise bridge.OpenCodeRequestTimeout()

        output = io.StringIO()
        with patch.object(bridge, "answer_async", side_effect=exchange), \
             patch.object(bridge, "edit_reply") as edit, \
             patch.object(bridge, "send") as send, \
             patch("sys.stderr", output):
            bridge._request_worker(123, "Hi", {}, 123, "REF12345", 456)
        self.assertEqual(edit.call_count, 2)
        self.assertNotIn("/status", edit.call_args_list[0].args[2])
        self.assertNotIn("/abort", edit.call_args_list[0].args[2])
        self.assertIn("REF12345", edit.call_args_list[0].args[2])
        self.assertIn("timed out", send.call_args.args[1])
        self.assertIn("phase=response_wait", output.getvalue())


if __name__ == "__main__":
    unittest.main()
