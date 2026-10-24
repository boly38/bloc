import os
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot", "telegram"))
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ALLOWED_CHAT_IDS": "123"}):
    from bloc_bridge import request_store


class RequestLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = patch.object(request_store, "REQUESTS_PATH", os.path.join(self.directory.name, "requests.sqlite3"))
        self.path.start()
        self.addCleanup(self.path.stop)
        request_store.initialize()

    def test_valid_transitions_are_applied(self):
        request_store.create("A" * 16, 123, 1)
        for state in ("queued", "submitting", "running", "delivering", "completed"):
            request_store.transition("A" * 16, state)
        self.assertEqual(request_store.get("A" * 16)["state"], "completed")

    def test_invalid_transition_is_rejected_without_mutating_state(self):
        request_store.create("B" * 16, 123, 2)
        with self.assertRaises(request_store.InvalidTransition):
            request_store.transition("B" * 16, "completed")
        self.assertEqual(request_store.get("B" * 16)["state"], "acknowledging")

    def test_terminal_state_cannot_be_reopened(self):
        request_store.create("C" * 16, 123, 3)
        request_store.transition("C" * 16, "queued")
        request_store.transition("C" * 16, "submitting")
        request_store.transition("C" * 16, "running")
        request_store.transition("C" * 16, "delivering")
        request_store.transition("C" * 16, "failed")
        with self.assertRaises(request_store.InvalidTransition):
            request_store.transition("C" * 16, "running")

    def test_legacy_update_with_state_uses_the_same_validation(self):
        request_store.create("D" * 16, 123, 4)
        request_store.update("D" * 16, state="queued")
        with self.assertRaises(request_store.InvalidTransition):
            request_store.update("D" * 16, state="completed")

    def test_existing_command_and_recovery_paths_are_valid(self):
        request_store.create("E" * 16, 123, 5)
        request_store.transition("E" * 16, "queued")
        request_store.transition("E" * 16, "delivering")
        request_store.transition("E" * 16, "completed")

        request_store.create("F" * 16, 123, 6)
        request_store.transition("F" * 16, "queued")
        request_store.transition("F" * 16, "submitting")
        request_store.transition("F" * 16, "running")
        request_store.transition("F" * 16, "recovering")
        request_store.transition("F" * 16, "failed")
