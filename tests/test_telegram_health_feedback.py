"""Offline checks for optional provider reachability and long-request feedback."""

import io
import os
import runpy
import socket
import sys
import threading
import time
import urllib.error
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot", "telegram"))
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ALLOWED_CHAT_IDS": "123"}):
    from bloc_bridge import bridge, bridge_runtime, http_api, opper_health
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
        self.clock = 0
        self.progress = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(bridge.time, "monotonic", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(bridge_runtime, "wait", side_effect=self.advance))
        self.stack.enter_context(patch.object(bridge.uuid, "uuid4", return_value=SimpleNamespace(hex="prompt")))
        self.stack.enter_context(patch.object(opencode_api, "session_tree_messages", return_value=[]))
        self.stack.enter_context(patch.object(opencode_api, "submit_async"))
        self.stack.enter_context(patch.object(bridge, "_watch_events"))
        self.probe = self.stack.enter_context(patch.object(opper_health, "health_status"))
        self.status = self.stack.enter_context(patch.object(opencode_api, "session_status", return_value={}))
        self.messages = self.stack.enter_context(patch.object(opencode_api, "session_messages", return_value=[]))

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
        self.messages.side_effect = lambda *_args, **_kwargs: [self.completed()] if self.clock >= 299 else []
        result = self.answer()
        self.assertIn("Done", result)
        self.assertEqual(self.clock, 299)
        self.assertEqual([when for when, _ in self.progress], list(range(30, 300, 30)))
        self.assertIn("270s elapsed", self.progress[-1][1])
        self.probe.assert_not_called()

    def test_heartbeat_and_delivery_time_never_reset_deadline(self):
        def slow_progress(text):
            self.progress.append((self.clock, text))
            self.advance(5)

        with self.assertRaises(bridge.OpenCodeRequestTimeout):
            self.answer(slow_progress)
        self.assertEqual(self.clock, 300)
        self.assertGreater(len(self.progress), 1)
        self.assertTrue(all(when < 300 for when, _ in self.progress))
        self.probe.assert_not_called()
        self.assertTrue(all(0 < item.args[0] <= 10 for item in self.status.call_args_list))

    def test_repeated_progress_events_are_throttled_and_do_not_extend_deadline(self):
        event_queues = []

        def watch(_session, _reference, events, _stop):
            event_queues.append(events)

        def statuses(_timeout):
            if event_queues:
                event_queues[0].put(("assistant", "reasoning" if self.clock % 2 else "tool"))
            return {"private-session": {"type": "busy"}}

        self.messages.return_value = [{
            "info": {"role": "assistant", "id": "assistant", "parentID": "msg_prompt"},
            "parts": [],
        }]
        self.status.side_effect = statuses
        # Run the watcher synchronously so event ordering is deterministic.
        thread = Mock()
        with patch.object(bridge.threading, "Thread", return_value=thread) as create_thread:
            def start():
                kwargs = create_thread.call_args.kwargs
                watch(*kwargs["args"])
            thread.start.side_effect = start
            with self.assertRaises(bridge.OpenCodeRequestTimeout):
                self.answer()

        self.assertEqual(self.clock, 300)
        self.assertEqual([when for when, _ in self.progress], list(range(0, 300, 30)))
        self.assertTrue(all("OpenCode is" in text for _, text in self.progress))
        self.assertEqual(len({text for _, text in self.progress}), len(self.progress))

    def test_correlated_provider_failure_surfaces_before_completion_or_idle(self):
        self.status.return_value = {"private-session": {"type": "busy"}}
        self.messages.return_value = [{
            "info": {
                "role": "assistant", "id": "assistant", "parentID": "msg_prompt",
                "error": {"name": "APIError", "data": {
                    "statusCode": 503, "message": "private credentials and prompt",
                    "responseBody": "private response",
                }},
            },
            "parts": [],
        }]
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
                     patch("sys.stderr", output):
                    bridge._request_worker(123, "private-prompt", {}, 123, "REF12345", 456)
                reply = edit.call_args.args[2]
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
             patch("sys.stderr", output):
            bridge._request_worker(123, "Hi", {}, 123, "REF12345", 456)
        self.assertEqual(edit.call_count, 2)
        self.assertIn("timed out", edit.call_args.args[2])
        self.assertIn("phase=response_wait", output.getvalue())


if __name__ == "__main__":
    unittest.main()
