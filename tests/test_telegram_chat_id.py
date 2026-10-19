import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import telegram_chat_id


class TelegramChatIdTests(unittest.TestCase):
    def test_loads_only_the_bot_token_from_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "OPPER_API_KEY=do-not-read\nTELEGRAM_BOT_TOKEN='bot-token'\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
                self.assertEqual(telegram_chat_id.load_bot_token(env_file), "bot-token")

    def test_environment_token_takes_precedence(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "environment-token"}):
            self.assertEqual(telegram_chat_id.load_bot_token("missing.env"), "environment-token")

    def test_matching_chat_id_requires_private_challenge_command(self):
        matching_update = {
            "message": {
                "chat": {"id": 123, "type": "private"},
                "from": {"is_bot": False},
                "text": "/start@bloc_bot private-challenge",
            }
        }
        self.assertEqual(telegram_chat_id.matching_chat_id(matching_update, "private-challenge"), 123)

        group_update = {
            "message": {
                "chat": {"id": -123, "type": "group"},
                "from": {"is_bot": False},
                "text": "/start private-challenge",
            }
        }
        wrong_challenge = {
            **matching_update,
            "message": {**matching_update["message"], "text": "/start other-value"},
        }
        for update in (group_update, wrong_challenge):
            with self.subTest(update=update):
                self.assertIsNone(telegram_chat_id.matching_chat_id(update, "private-challenge"))

    def test_challenge_is_six_url_safe_characters(self):
        with patch.object(telegram_chat_id.secrets, "token_urlsafe", return_value="Ab12_-") as generate:
            challenge = telegram_chat_id.generate_challenge()

        generate.assert_called_once_with(4)
        self.assertEqual(len(challenge), 6)

    def test_get_updates_omits_an_unset_offset(self):
        response_body = json.dumps({"ok": True, "result": []}).encode("utf-8")
        with patch.object(telegram_chat_id.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = response_body
            updates = telegram_chat_id.telegram_updates("test-token", None, 50)

        request = urlopen.call_args.args[0]
        self.assertEqual(updates, [])
        self.assertNotIn("offset", json.loads(request.data))
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)

    def test_discovery_announces_listening_after_successful_connection(self):
        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": 123, "type": "private"},
                "from": {"is_bot": False},
                "text": "/start private-challenge",
            },
        }
        announcements = []
        with patch.object(telegram_chat_id, "telegram_updates", return_value=[update]) as poll:
            chat_id = telegram_chat_id.discover_chat_id(
                "bot-token",
                "private-challenge",
                on_connected=lambda: announcements.append("connected"),
            )

        self.assertEqual(chat_id, 123)
        self.assertEqual(announcements, ["connected"])
        poll.assert_called_once_with("bot-token", None, 0)

    def test_conflict_error_mentions_another_poller_without_exposing_token(self):
        error = urllib.error.HTTPError(
            "https://api.telegram.org/botsecret-token/getUpdates",
            409,
            "Conflict",
            None,
            None,
        )

        message = telegram_chat_id.safe_error_message(error)

        self.assertIn("409 Conflict", message)
        self.assertIn("Coolify", message)
        self.assertIn("webhook", message)
        self.assertNotIn("secret-token", message)
        error.close()


if __name__ == "__main__":
    unittest.main()
