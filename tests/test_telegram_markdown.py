import html.parser
import json
import os
import sys
import unittest
from unittest.mock import call, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot", "telegram"))
with patch.dict(
    os.environ,
    {"TELEGRAM_BOT_TOKEN": "unit-test-secret", "TELEGRAM_ALLOWED_CHAT_IDS": "123"},
):
    from bloc_bridge import bridge, bridge_config, http_api
    from bloc_bridge.telegram import format_test, markdown_renderer


class _TagBalanceParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, _attrs):
        if tag not in ("br",):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise AssertionError(f"Unbalanced HTML tag: {tag}")


class TelegramMarkdownTests(unittest.TestCase):
    def test_render_formats_supported_inline_and_block_markdown(self):
        rendered = markdown_renderer.render_markdown(
            "# Heading\n\n**bold**, *italic*, ~~removed~~, `code`\n\n"
            "- one\n- two\n\n> quoted"
        )

        self.assertEqual(len(rendered), 1)
        message = rendered[0]
        self.assertIn("<b>Heading</b>", message.html)
        self.assertIn("<b>bold</b>", message.html)
        self.assertIn("<i>italic</i>", message.html)
        self.assertIn("<s>removed</s>", message.html)
        self.assertIn("<code>code</code>", message.html)
        self.assertIn("• one\n• two", message.html)
        self.assertIn("│ quoted", message.html)
        self.assertEqual(
            message.plain,
            "Heading\n\nbold, italic, removed, code\n\n• one\n• two\n\n│ quoted",
        )

    def test_html_like_text_and_markdown_escapes_are_safe(self):
        message = markdown_renderer.render_markdown(
            r"<script>alert('x')</script> & \*literal\* "
            "[docs](https://example.com/a?x=1&y=2)"
        )[0]

        self.assertIn("&lt;script&gt;", message.html)
        self.assertIn("&amp;", message.html)
        self.assertIn("*literal*", message.html)
        self.assertIn("docs (https://example.com/a?x=1&amp;y=2)", message.html)
        self.assertIn("docs (https://example.com/a?x=1&y=2)", message.plain)
        parser = _TagBalanceParser()
        parser.feed(message.html)
        self.assertEqual(parser.stack, [])

    def test_code_fences_preserve_content_and_language(self):
        message = markdown_renderer.render_markdown(
            "```python\nprint('<tag> & text')\n```"
        )[0]

        self.assertIn('<pre><code class="language-python">', message.html)
        self.assertIn("print('&lt;tag&gt; &amp; text')", message.html)
        self.assertIn("print('<tag> & text')", message.plain)

    def test_unclosed_code_fence_degrades_to_a_closed_code_block(self):
        message = markdown_renderer.render_markdown("```python\nprint('still readable')")[0]

        self.assertTrue(message.html.endswith("</code></pre>"))
        parser = _TagBalanceParser()
        parser.feed(message.html)
        self.assertEqual(parser.stack, [])
        self.assertIn("print('still readable')", message.plain)

    def test_malformed_and_unsupported_markdown_remains_safe_text(self):
        message = markdown_renderer.render_markdown("**unfinished <tag>\n| cell | cell |\n")[0]

        self.assertIn("**unfinished &lt;tag&gt;", message.html)
        self.assertIn("| cell | cell |", message.plain)
        parser = _TagBalanceParser()
        parser.feed(message.html)
        self.assertEqual(parser.stack, [])

    def test_long_code_block_is_split_into_balanced_bounded_messages(self):
        source = "```python\n" + "print('🧪 & <ok>')\n" * 30 + "```"
        messages = markdown_renderer.render_markdown(source, max_units=90)

        self.assertGreater(len(messages), 1)
        self.assertEqual("".join(message.plain for message in messages), source.split("\n", 1)[1].rsplit("\n```", 1)[0])
        for message in messages:
            self.assertLessEqual(len(message.plain.encode("utf-16-le")) // 2, 90)
            self.assertTrue(message.html.startswith('<pre><code class="language-python">'))
            self.assertTrue(message.html.endswith("</code></pre>"))
            parser = _TagBalanceParser()
            parser.feed(message.html)
            self.assertEqual(parser.stack, [])

    def test_plain_fallback_is_used_only_after_telegram_formatting_rejection(self):
        with patch.object(
            http_api,
            "telegram",
            side_effect=[http_api.TelegramFormattingError("bad formatting"), None],
        ) as send_message:
            bridge.send(123, "**answer** & <safe>")

        self.assertEqual(
            send_message.call_args_list,
            [
                call(
                    "sendMessage",
                    {"chat_id": 123, "text": "<b>answer</b> &amp; &lt;safe&gt;", "parse_mode": "HTML"},
                ),
                call("sendMessage", {"chat_id": 123, "text": "answer & <safe>"}),
            ],
        )

    def test_non_formatting_telegram_error_is_not_retried_as_plain_text(self):
        with patch.object(http_api, "telegram", side_effect=RuntimeError("network error")) as send_message:
            with self.assertRaisesRegex(RuntimeError, "network error"):
                bridge.send(123, "**answer**")

        send_message.assert_called_once()

    def test_telegram_parse_errors_are_classified_without_exposing_response_details(self):
        details = json.dumps({"description": "Bad Request: can't parse entities: unsupported tag"})
        with self.assertRaises(http_api.TelegramFormattingError):
            http_api._handle_telegram_error(bridge_config.TG + "sendMessage", 400, details)

    def test_format_fixture_checks_allowlist_and_sends_without_calling_opencode(self):
        with patch.object(format_test.bridge, "send") as send_message:
            format_test.send_fixture(123)

        send_message.assert_called_once_with(123, format_test.FIXTURE)
        with self.assertRaisesRegex(ValueError, "not in TELEGRAM_ALLOWED_CHAT_IDS"):
            format_test.send_fixture(456)

    def test_manual_fixture_exercises_split_chunks_with_balanced_html(self):
        messages = markdown_renderer.render_markdown(format_test.FIXTURE)

        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message.plain.encode("utf-16-le")) // 2, 4000)
            parser = _TagBalanceParser()
            parser.feed(message.html)
            self.assertEqual(parser.stack, [])


if __name__ == "__main__":
    unittest.main()
