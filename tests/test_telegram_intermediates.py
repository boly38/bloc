"""Offline tests for optional intermediate assistant replies."""

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot", "telegram"))
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ALLOWED_CHAT_IDS": "123,456"}):
    from bloc_bridge import bridge, chat_preferences, request_store
    from bloc_bridge.opencode import api as opencode_api
    from bloc_bridge.opencode.intermediate_replies import IntermediateReplyTracker


def assistant(message_id, text, parent_id=None, *, completed_at=1, finish=None, extra_parts=()):
    info = {
        "id": message_id,
        "role": "assistant",
        "time": {"completed": completed_at},
    }
    if parent_id is not None:
        info["parentID"] = parent_id
    if finish is not None:
        info["finish"] = finish
    return {
        "info": info,
        "parts": [*extra_parts, {"type": "text", "text": text}],
    }


class IntermediateReplyTrackerTests(unittest.TestCase):
    def test_sends_prior_correlated_text_once_and_holds_back_the_latest_message(self):
        first = assistant("assistant-1", "First update")
        second = assistant("assistant-2", "Second update")
        final = assistant("assistant-final", "Final answer")
        tracker = IntermediateReplyTracker()

        self.assertEqual(tracker.collect([first]), [])
        self.assertEqual(tracker.collect([first, second]), ["First update"])
        self.assertEqual(tracker.collect([first, second, final]), ["Second update"])
        self.assertEqual(tracker.collect([first, second, final]), [])

    def test_ignores_reasoning_and_tool_parts(self):
        first = assistant(
            "assistant-1",
            "Visible update",
            extra_parts=[
                {"type": "reasoning", "text": "private reasoning"},
                {"type": "tool", "tool": "bash", "state": {"output": "private tool output"}},
            ],
        )
        final = assistant("assistant-final", "Final answer")

        self.assertEqual(IntermediateReplyTracker().collect([first, final]), ["Visible update"])

    def test_recovery_skips_existing_messages_then_tracks_new_messages(self):
        previous = assistant("assistant-before-restart", "Do not replay")
        after_restart = assistant("assistant-after-restart", "New update")
        final = assistant("assistant-final", "Final answer")
        tracker = IntermediateReplyTracker(skip_existing=True)

        self.assertEqual(tracker.collect([previous]), [])
        self.assertEqual(tracker.collect([previous, after_restart]), [])
        self.assertEqual(tracker.collect([previous, after_restart, final]), ["New update"])


class TelegramIntermediatePreferenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        database = patch.object(
            request_store,
            "REQUESTS_PATH",
            os.path.join(self.directory.name, "requests.sqlite3"),
        )
        database.start()
        self.addCleanup(database.stop)
        preferences = patch.object(
            chat_preferences,
            "CHAT_PREFERENCES_PATH",
            os.path.join(self.directory.name, "chat-preferences.json"),
        )
        preferences.start()
        self.addCleanup(preferences.stop)
        request_store.initialize()
        self.permissions = patch.object(opencode_api, "pending_permissions", return_value=[])
        self.permissions.start()
        self.addCleanup(self.permissions.stop)
        self.questions = patch.object(opencode_api, "pending_questions", return_value=[])
        self.questions.start()
        self.addCleanup(self.questions.stop)

    def test_preference_defaults_off_and_persists_per_chat(self):
        self.assertFalse(chat_preferences.intermediates_enabled(123))

        chat_preferences.set_intermediates_enabled(123, True)
        chat_preferences.set_intermediates_enabled(456, False)

        self.assertTrue(chat_preferences.intermediates_enabled(123))
        self.assertFalse(chat_preferences.intermediates_enabled(456))

    def test_request_database_migrates_existing_rows_with_intermediates_disabled(self):
        legacy_path = os.path.join(self.directory.name, "legacy.sqlite3")
        with patch.object(request_store, "REQUESTS_PATH", legacy_path):
            connection = sqlite3.connect(legacy_path)
            connection.execute(
                """CREATE TABLE requests (
                    reference TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    update_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    acknowledgement_id INTEGER,
                    session_id TEXT,
                    prompt_message_id TEXT,
                    detail TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL
                )"""
            )
            connection.execute(
                """INSERT INTO requests
                    (reference, chat_id, update_id, state, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                ("E" * 16, "123", "105", "queued", 1, 1),
            )
            connection.commit()
            connection.close()

            request_store.initialize()
            existing = request_store.get("E" * 16)
            created, _ = request_store.create("F" * 16, 123, 106)

        self.assertFalse(existing["intermediates_enabled"])
        self.assertFalse(created["intermediates_enabled"])

    def test_command_is_local_and_changes_only_future_request_snapshots(self):
        active, _ = request_store.create(
            "A" * 16,
            123,
            100,
            state="running",
            intermediates_enabled=False,
        )
        update = {
            "update_id": 101,
            "message": {"chat": {"id": 123}, "text": "/intermediates on"},
        }
        with patch.object(bridge, "send") as send:
            with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
                bridge.handle_update(update, {})

        self.assertTrue(chat_preferences.intermediates_enabled(123))
        self.assertFalse(request_store.get(active["reference"])["intermediates_enabled"])
        self.assertEqual(request_store.get(active["reference"])["state"], "running")
        self.assertEqual(send.call_count, 1)
        self.assertIn("enabled for new requests", send.call_args.args[1])
        submit.assert_not_called()

        request_store.update(active["reference"], state="delivering")
        request_store.update(active["reference"], state="completed")
        following_update = {
            "update_id": 102,
            "message": {"chat": {"id": 123}, "text": "Following request"},
        }
        with patch.object(bridge, "send", return_value={"message_id": 501}):
            with patch.object(bridge._REQUEST_EXECUTOR, "submit"):
                with patch.object(bridge._REQUEST_SLOTS, "acquire", return_value=True):
                    bridge.handle_update(following_update, {})
        following = request_store.get_by_update_id(102)
        self.assertTrue(following["intermediates_enabled"])
        bridge._forget_request_presentation(following["reference"])

    def test_invalid_command_shows_usage_without_changing_preference(self):
        update = {
            "update_id": 103,
            "message": {"chat": {"id": 123}, "text": "/intermediates maybe"},
        }
        with patch.object(bridge, "send") as send:
            with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
                bridge.handle_update(update, {})

        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "Usage: /intermediates on|off")
        self.assertFalse(chat_preferences.intermediates_enabled(123))
        submit.assert_not_called()

    def test_enabled_request_delivers_intermediates_separately_from_final(self):
        request, _ = request_store.create(
            "C" * 16,
            123,
            104,
            state="queued",
            intermediates_enabled=True,
        )
        request_store.update(request["reference"], acknowledgement_id=500)

        def answer(_chat_id, _prompt, _sessions, _reference, _set_phase, _progress, **kwargs):
            kwargs["intermediate_reply"]("Intermediate answer")
            return "Final answer"

        with patch.object(bridge, "answer_async", side_effect=answer):
            with patch.object(bridge, "send") as send:
                with patch.object(bridge, "_finalize_request_presentation"):
                    with patch.object(bridge, "_forget_request_presentation"):
                        bridge._request_worker(123, "prompt", {}, 104, request["reference"], 500)

        self.assertEqual(send.call_count, 2)
        self.assertIn("Intermediate answer", send.call_args_list[0].args[1])
        self.assertIn("Final answer", send.call_args_list[1].args[1])
        self.assertIn(request["reference"], send.call_args_list[0].args[1])
        self.assertEqual(request_store.get(request["reference"])["state"], "completed")

    def test_intermediate_delivery_failure_does_not_fail_final_answer(self):
        request, _ = request_store.create(
            "G" * 16,
            123,
            107,
            state="queued",
            intermediates_enabled=True,
        )
        request_store.update(request["reference"], acknowledgement_id=501)

        def answer(_chat_id, _prompt, _sessions, _reference, _set_phase, _progress, **kwargs):
            kwargs["intermediate_reply"]("Intermediate answer")
            return "Final answer"

        with patch.object(bridge, "answer_async", side_effect=answer):
            with patch.object(bridge, "send", side_effect=[TimeoutError(), {}]) as send:
                with patch.object(bridge, "_finalize_request_presentation"):
                    with patch.object(bridge, "_forget_request_presentation"):
                        bridge._request_worker(123, "prompt", {}, 107, request["reference"], 501)

        self.assertEqual(send.call_count, 2)
        self.assertIn("Final answer", send.call_args_list[-1].args[1])
        self.assertEqual(request_store.get(request["reference"])["state"], "completed")

    def test_async_exchange_forwards_correlated_messages_in_order_and_not_the_final(self):
        reference = "D" * 16
        request_store.create(reference, 123, 105, state="queued", intermediates_enabled=True)
        prompt = {"info": {"id": "msg_prompt", "role": "user"}}
        first = assistant("assistant-1", "First update", "msg_prompt", completed_at=1)
        second = assistant("assistant-2", "Second update", "assistant-1", completed_at=2)
        final = assistant("assistant-final", "Final answer", "assistant-2", completed_at=3, finish="stop")
        history = [prompt, first, second, final]
        session_tree = [
            {"session_id": "session-1", "message": message}
            for message in (first, second, final)
        ]
        intermediates = []
        status_samples = [
            {"session-1": {"type": "busy"}},
            {"session-1": {"type": "busy"}},
            {},
            {},
        ]
        message_samples = [
            [prompt, first],
            [prompt, first, second],
            history,
            history,
        ]

        def session_status(_timeout):
            return status_samples.pop(0) if status_samples else {}

        def session_messages(_session_id, timeout=None):
            return message_samples.pop(0) if message_samples else history

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(bridge.uuid, "uuid4", return_value=SimpleNamespace(hex="prompt"))
            )
            stack.enter_context(
                patch.object(opencode_api, "session_tree_messages", side_effect=[[], session_tree])
            )
            stack.enter_context(patch.object(opencode_api, "submit_async"))
            stack.enter_context(patch.object(opencode_api, "session_status", side_effect=session_status))
            stack.enter_context(patch.object(opencode_api, "session_messages", side_effect=session_messages))
            stack.enter_context(patch.object(bridge, "_watch_events", return_value=None))
            stack.enter_context(patch.object(bridge.bridge_runtime, "wait"))
            result = bridge.answer_async(
                123,
                "prompt text",
                {"123": "session-1"},
                reference,
                lambda _phase: None,
                lambda _text: None,
                intermediate_reply=intermediates.append,
            )

        self.assertIn("Final answer", result)
        self.assertEqual(intermediates, ["First update", "Second update"])


if __name__ == "__main__":
    unittest.main()
