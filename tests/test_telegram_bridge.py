import io
import json
import os
import sys
import urllib.error
import unittest
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
        with patch.object(bridge, "http", side_effect=[{"id": "session-1"}, response]) as request:
            with patch.object(bridge, "save_sessions") as save:
                result = bridge.answer(123, "Hi", {})

        self.assertEqual(result, "Hello")
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
        sessions = {"123": "existing-session"}
        with patch.object(bridge, "http", return_value=response) as request:
            self.assertEqual(bridge.answer(123, "Continue", sessions), "Existing session")
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


if __name__ == "__main__":
    unittest.main()
