import io
import json
import os
import sys
import tempfile
import urllib.error
import unittest
from datetime import datetime, timezone
from unittest.mock import call, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot", "telegram"))
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "unit-test-secret", "TELEGRAM_ALLOWED_CHAT_IDS": "123"}):
    import bridge


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def telegram_http_error(code, body, method="getUpdates"):
    return urllib.error.HTTPError(
        bridge.TG + method,
        code,
        "Telegram error",
        {},
        io.BytesIO(json.dumps(body).encode()),
    )


class TelegramBridgeTests(unittest.TestCase):
    def test_telegram_returns_result_from_successful_response(self):
        updates = [{"update_id": 10}]
        with patch.object(bridge, "http", return_value={"ok": True, "result": updates}) as request:
            self.assertEqual(bridge.telegram("getUpdates", {"offset": 0}), updates)
        request.assert_called_once_with(bridge.TG + "getUpdates", {"offset": 0}, 70)

    def test_answer_creates_and_persists_a_session(self):
        response = {"parts": [{"type": "text", "text": "Hello"}]}
        message = {
            "info": {"id": "message-1", "role": "assistant", "agent": "build", "cost": 0.1},
            "parts": [],
        }
        with patch.object(bridge, "http", side_effect=[{"id": "session-1"}, response]) as request:
            with patch.object(bridge, "session_tree_messages", side_effect=[[], [{"session_id": "session-1", "message": message}]]):
                with patch.object(bridge, "save_sessions") as save:
                    result = bridge.answer(123, "Hi", {})

        self.assertEqual(result, "Hello\n\nCost: $0.100000 (build: $0.100000)")
        request.assert_has_calls(
            [
                call(bridge.OC + "/session", {"title": "Telegram 123"}),
                call(
                    bridge.OC + "/session/session-1/message",
                    {"system": bridge.SYSTEM, "parts": [{"type": "text", "text": "Hi"}]},
                ),
            ]
        )
        save.assert_called_once_with({"123": "session-1"})

    def test_answer_reuses_existing_session(self):
        response = {"parts": [{"type": "text", "text": "Existing session"}]}
        message = {"info": {"id": "message-1", "role": "assistant", "cost": 0.25}, "parts": []}
        sessions = {"123": "existing-session"}
        with patch.object(bridge, "http", return_value=response) as request:
            with patch.object(
                bridge,
                "session_tree_messages",
                side_effect=[[], [{"session_id": "existing-session", "message": message}]],
            ):
                result = bridge.answer(123, "Continue", sessions)
        self.assertEqual(result, "Existing session\n\nCost: $0.250000")
        request.assert_called_once()
        self.assertIn("/session/existing-session/message", request.call_args.args[0])

    def test_send_splits_long_replies(self):
        with patch.object(bridge, "telegram") as send_message:
            bridge.send(123, "x" * 8500)

        self.assertEqual(send_message.call_count, 3)
        self.assertEqual([len(item.args[1]["text"]) for item in send_message.call_args_list], [4000, 4000, 500])

    def test_409_conflict_is_recognized_without_exposing_token(self):
        conflict = telegram_http_error(409, {"description": "another getUpdates request is active"})
        with patch("urllib.request.urlopen", side_effect=conflict):
            with self.assertRaises(bridge.TelegramConflict) as caught:
                bridge.telegram("getUpdates", {})

        self.assertNotIn("unit-test-secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_opencode_http_errors_do_not_log_session_ids_or_response_bodies(self):
        error = urllib.error.HTTPError(
            bridge.OC + "/session/private-session-id/message",
            500,
            "Server error",
            {},
            io.BytesIO(b"private response body"),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                bridge.http(bridge.OC + "/session/private-session-id/message")

        self.assertEqual(str(caught.exception), "OpenCode API HTTP 500")
        self.assertTrue(caught.exception.__suppress_context__)

    def test_429_waits_for_retry_after_then_retries(self):
        rate_limit = telegram_http_error(
            429,
            {"parameters": {"retry_after": 4}},
            method="sendMessage",
        )
        success = FakeResponse(b'{"ok": true, "result": {"message_id": 5}}')
        with patch("urllib.request.urlopen", side_effect=[rate_limit, success]) as urlopen:
            with patch.object(bridge.time, "sleep") as sleep:
                result = bridge.telegram("sendMessage", {"chat_id": 123, "text": "Hi"})

        self.assertEqual(result, {"message_id": 5})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(4)

    def test_cost_diagnostics_are_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cost-diagnostics.jsonl")
            with patch.object(bridge, "COST_DIAGNOSTICS_ENABLED", False):
                with patch.object(bridge, "COST_DIAGNOSTICS_PATH", path):
                    bridge.record_cost_diagnostics({"info": {"cost": 0.1}, "parts": []})
            self.assertFalse(os.path.exists(path))

    def test_cost_diagnostics_include_metrics_but_exclude_content_and_ids(self):
        response = {
            "info": {
                "id": "private-message-id",
                "sessionID": "private-session-id",
                "role": "assistant",
                "cost": 0.012,
                "tokens": {"input": 11, "output": 7, "total": 18},
                "modelID": "gpt-5.6-luna",
                "providerID": "opper",
            },
            "parts": [
                {"type": "text", "text": "private response text"},
                {"type": "step-finish", "cost": 0.004, "tokens": {"input": 5, "output": 2}},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cost-diagnostics.jsonl")
            with patch.object(bridge, "COST_DIAGNOSTICS_ENABLED", True):
                with patch.object(bridge, "COST_DIAGNOSTICS_PATH", path):
                    bridge.record_cost_diagnostics(response)
            with open(path, encoding="utf-8") as file:
                serialized = file.read()

        diagnostic = json.loads(serialized)
        self.assertEqual(diagnostic["metrics"]["cost"], 0.012)
        self.assertEqual(diagnostic["metrics"]["tokens.input"], 11)
        self.assertEqual(diagnostic["metrics"]["modelID"], "gpt-5.6-luna")
        self.assertEqual(diagnostic["part_metrics"][0]["type"], "step-finish")
        self.assertEqual(diagnostic["part_metrics"][0]["metrics"]["cost"], 0.004)
        self.assertEqual(diagnostic["part_metrics"][0]["metrics"]["tokens.output"], 2)
        for private_value in ("private-message-id", "private-session-id", "private response text"):
            self.assertNotIn(private_value, serialized)

    def test_answer_diagnostics_records_all_new_assistant_messages(self):
        session_url = bridge.OC + "/session/session-1/message"
        old_message = {"info": {"id": "old-id", "role": "assistant", "cost": 0.1}, "parts": []}
        intermediate = {
            "info": {"id": "intermediate-id", "role": "assistant", "agent": "build", "cost": 0.2},
            "parts": [{"type": "step-finish", "cost": 0.2}],
        }
        final = {
            "info": {"id": "final-id", "role": "assistant", "agent": "helper", "cost": 0.3},
            "parts": [{"type": "text", "text": "Done"}],
        }
        response = {"info": {"id": "final-id", "role": "assistant"}, "parts": [{"type": "text", "text": "Done"}]}
        with patch.object(bridge, "COST_DIAGNOSTICS_ENABLED", True):
            with patch.object(
                bridge,
                "session_tree_messages",
                side_effect=[
                    [{"session_id": "session-1", "message": old_message}],
                    [
                        {"session_id": "session-1", "message": old_message},
                        {"session_id": "session-1", "message": intermediate},
                        {"session_id": "session-1", "message": final},
                    ],
                ],
            ):
                with patch.object(bridge, "http", return_value=response) as request:
                    with patch.object(bridge, "record_cost_diagnostics") as record:
                        result = bridge.answer(123, "Use a tool", {"123": "session-1"})

        self.assertEqual(result, "Done\n\nCost: $0.500000 (build: $0.200000, helper: $0.300000)")
        request.assert_called_once_with(session_url, {"system": bridge.SYSTEM, "parts": [{"type": "text", "text": "Use a tool"}]})
        self.assertEqual(record.call_args_list, [call(intermediate, {"index": 1, "count": 2}), call(final, {"index": 2, "count": 2})])

    def test_cost_report_sums_session_and_current_utc_hour_per_agent(self):
        def message(message_id, cost, agent, timestamp, tokens):
            created = int(datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc).timestamp() * 1000)
            return {
                "info": {
                    "id": message_id,
                    "role": "assistant",
                    "agent": agent,
                    "cost": cost,
                    "time": {"created": created},
                    "tokens": tokens,
                },
                "parts": [],
            }

        messages = [
            message("old", 0.005, "build", "2026-10-11T17:59:00", {"input": 10, "output": 2, "cache": {"read": 3}}),
            message("main", 0.001, "build", "2026-10-11T18:01:00", {"input": 20, "output": 4, "cache": {"read": 5}}),
            message("helper", 0.002, "helper", "2026-10-11T18:02:00", {"input": 30, "output": 6, "cache": {"read": 7}}),
        ]
        child_message = message(
            "child-agent",
            0.004,
            "reviewer",
            "2026-10-11T18:03:00",
            {"input": 40, "output": 8, "cache": {"read": 10}},
        )
        now = datetime(2026, 10, 11, 18, 30, tzinfo=timezone.utc)
        with patch.object(
            bridge,
            "session_tree_messages",
            return_value=[{"session_id": "session-1", "message": item} for item in messages]
            + [{"session_id": "child-session", "message": child_message}],
        ):
            report = bridge.cost_report("session-1", now)

        self.assertIn("Session: $0.012000", report)
        self.assertIn("UTC hour (18:00–19:00): $0.007000", report)
        self.assertIn("Agents (session): build: $0.006000, helper: $0.002000, reviewer: $0.004000", report)
        self.assertIn("Tokens (session): 100 input, 25 cached, 20 output", report)

    def test_cost_report_marks_incomplete_cost_unavailable(self):
        messages = [{"info": {"role": "assistant", "time": {"created": 1791744000000}}, "parts": []}]
        with patch.object(
            bridge,
            "session_tree_messages",
            return_value=[{"session_id": "session-1", "message": message} for message in messages],
        ):
            report = bridge.cost_report("session-1", datetime(2026, 10, 11, 18, 30, tzinfo=timezone.utc))
        self.assertIn("Session: unavailable", report)
        self.assertIn("UTC hour (18:00–19:00): unavailable", report)

    def test_cost_command_does_not_call_the_llm(self):
        messages = [{"info": {"role": "assistant", "cost": 0.125, "time": {"created": 1791744000000}}, "parts": []}]
        with patch.object(
            bridge,
            "session_tree_messages",
            return_value=[{"session_id": "session-1", "message": message} for message in messages],
        ):
            with patch.object(bridge, "http") as request:
                report = bridge.authorized_response(123, "/cost@bloc_bot", {"123": "session-1"})

        self.assertIn("Session: $0.125000", report)
        request.assert_not_called()

    def test_cost_report_uses_the_current_chats_session(self):
        first_session = [{"session_id": "session-a", "message": {"info": {"role": "assistant", "cost": 0.1}}}]
        second_session = [{"session_id": "session-b", "message": {"info": {"role": "assistant", "cost": 0.2}}}]
        with patch.object(bridge, "session_tree_messages", side_effect=[first_session, second_session]) as get_history:
            first = bridge.authorized_response(123, "/cost", {"123": "session-a", "456": "session-b"})
            second = bridge.authorized_response(456, "/cost", {"123": "session-a", "456": "session-b"})

        self.assertIn("Session: $0.100000", first)
        self.assertIn("Session: $0.200000", second)
        self.assertEqual(get_history.call_args_list, [call("session-a"), call("session-b")])

    def test_session_tree_includes_recursive_child_messages(self):
        main_message = {"info": {"id": "main-message", "role": "assistant"}, "parts": []}
        child_message = {"info": {"id": "child-message", "role": "assistant"}, "parts": []}
        with patch.object(bridge, "session_messages", side_effect=[[main_message], [child_message]]) as get_messages:
            with patch.object(bridge, "http", side_effect=[[{"id": "child-session"}], []]) as get_children:
                result = bridge.session_tree_messages("main-session")

        self.assertEqual(
            result,
            [
                {"session_id": "main-session", "message": main_message},
                {"session_id": "child-session", "message": child_message},
            ],
        )
        self.assertEqual(get_messages.call_count, 2)
        get_children.assert_has_calls(
            [
                call(bridge.OC + "/session/main-session/children"),
                call(bridge.OC + "/session/child-session/children"),
            ]
        )


if __name__ == "__main__":
    unittest.main()
