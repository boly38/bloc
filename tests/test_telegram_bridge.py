import io
import json
import os
import sys
import tempfile
import time
import threading
import urllib.error
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot", "telegram"))
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "unit-test-secret", "TELEGRAM_ALLOWED_CHAT_IDS": "123"}):
    from bloc_bridge import bridge, bridge_config, http_api, request_store, session_state
    from bloc_bridge.opencode import cost_reporting, api as opencode_api
    from bloc_bridge.telegram.request_presentation import RequestPresentation


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

    def __iter__(self):
        return iter(self.body.splitlines(keepends=True))


def telegram_http_error(code, body, method="getUpdates"):
    return urllib.error.HTTPError(
        bridge_config.TG + method,
        code,
        "Telegram error",
        {},
        io.BytesIO(json.dumps(body).encode()),
    )


class TelegramBridgeTests(unittest.TestCase):
    def setUp(self):
        self.request_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.request_directory.cleanup)
        patcher = patch.object(
            request_store,
            "REQUESTS_PATH",
            os.path.join(self.request_directory.name, "requests.sqlite3"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        request_store.initialize()
        permission_patcher = patch.object(opencode_api, "pending_permissions", return_value=[])
        permission_patcher.start()
        self.addCleanup(permission_patcher.stop)

    def test_http_timeout_defaults_are_separate_for_each_client(self):
        for url, expected in (
            (bridge_config.OC + "/global/health", 300),
            (bridge_config.TG + "getMe", 70),
        ):
            with self.subTest(url=url):
                with patch("urllib.request.urlopen", return_value=FakeResponse(b"{}")) as urlopen:
                    http_api.http(url)
                self.assertEqual(urlopen.call_args.kwargs["timeout"], expected)

    def test_opencode_sse_events_are_decoded_without_losing_payload_fields(self):
        event = {"type": "message.part.updated", "properties": {"sessionID": "session-1"}}
        body = f"data: {json.dumps({'payload': event})}\n\n".encode()
        with patch("urllib.request.urlopen", return_value=FakeResponse(body)) as urlopen:
            result = list(http_api.opencode_events())

        self.assertEqual(result, [event])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 15)
        self.assertEqual(urlopen.call_args.args[0].full_url, bridge_config.OC + "/event")

    def test_tool_progress_uses_safe_tool_metadata_without_echoing_arguments(self):
        event = {
            "properties": {
                "sessionID": "session-1",
                "messageID": "assistant-1",
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "running", "input": {"command": "gh pr view 31 && make test"}},
                },
            }
        }

        details = bridge._event_details(event)

        self.assertEqual(
            details,
            ("session-1", "assistant-1", "tool", "bash", "running", True, True),
        )
        self.assertEqual(bridge._event_progress_stage(*details[2:]), "Tests in progress")
        self.assertEqual(
            bridge._event_progress_stage("tool", "bash", "running", False, True),
            "GitHub CLI in progress",
        )

    def test_tool_usage_log_lists_only_safe_names_and_recognized_gh_cli(self):
        messages = [
            {
                "message": {
                    "parts": [
                        {
                            "type": "tool",
                            "tool": "bash",
                            "state": {
                                "status": "completed",
                                "input": {"command": "gh issue view 31 --json title"},
                                "output": "private command output",
                            },
                        },
                        {"type": "tool", "tool": "read", "state": {"status": "completed"}},
                        {"type": "tool", "tool": "read", "state": {"status": "completed"}},
                        {
                            "type": "tool",
                            "tool": "apply_patch",
                            "state": {"status": "completed", "input": {"patch": "private patch"}},
                        },
                        {
                            "type": "tool",
                            "tool": "todowrite",
                            "state": {"status": "completed", "input": {"todos": ["private task"]}},
                        },
                    ]
                }
            }
        ]

        names = bridge._tool_names(messages)
        report = bridge._format_tool_usage(names)

        self.assertEqual(names, ["bash", "gh", "read", "apply_patch", "todowrite"])
        self.assertEqual(report, "Tools used: bash, gh, read, apply_patch, todowrite")
        self.assertNotIn("private command output", report)
        self.assertNotIn("private patch", report)
        self.assertNotIn("private task", report)

    def test_tool_progress_has_labels_for_patch_and_todo_tools(self):
        self.assertEqual(bridge._event_progress_stage("tool", "apply_patch", "running"), "File patch in progress")
        self.assertEqual(bridge._event_progress_stage("tool", "todowrite", "completed"), "Task tracking complete")

    def test_permission_prompt_shows_sanitized_scope_and_explicit_choices(self):
        presentation = RequestPresentation(123, 456, "REF12345")
        presentation.sync_permissions(
            [
                {
                    "id": "private-permission-id",
                    "permission": "webfetch",
                    "patterns": ["https://docs.example.test/path?token=private-secret"],
                    "always": ["*"],
                }
            ]
        )
        prompt, keyboard = presentation.edit_payload()

        self.assertIn("webfetch: https://docs.example.test/path", prompt)
        self.assertIn("remember scope: *", prompt)
        self.assertIn("/allow A", prompt)
        self.assertIn("/allowAll A", prompt)
        self.assertIn("/deny A", prompt)
        self.assertNotIn("private-permission-id", prompt)
        self.assertNotIn("private-secret", prompt)
        self.assertEqual(
            [button["text"] for row in keyboard["inline_keyboard"] for button in row],
            ["Status", "Abort", "Allow A", "Always A", "Deny A"],
        )

    def test_permission_letters_rotate_after_a_request_is_resolved(self):
        presentation = RequestPresentation(123, 456, "REF12345")
        first = {"id": "per-first", "permission": "read", "patterns": ["a"]}
        second = {"id": "per-second", "permission": "write", "patterns": ["b"]}

        presentation.sync_permissions([first])
        presentation.resolve_permission("per-first")
        presentation.sync_permissions([second])

        self.assertEqual(presentation.permissions_by_letter(), {"B": second})

    def test_http_requests_do_not_add_basic_authentication(self):
        for url in (bridge_config.OC + "/global/health", bridge_config.TG + "getMe"):
            with self.subTest(url=url):
                with patch("urllib.request.urlopen", return_value=FakeResponse(b"{}")) as urlopen:
                    http_api.http(url)

                request = urlopen.call_args.args[0]
                self.assertNotIn("Authorization", request.headers)

    def test_telegram_returns_result_from_successful_response(self):
        updates = [{"update_id": 10}]
        with patch.object(http_api, "http", return_value={"ok": True, "result": updates}) as request:
            self.assertEqual(http_api.telegram("getUpdates", {"offset": 0}), updates)
        request.assert_called_once_with(bridge_config.TG + "getUpdates", {"offset": 0}, 70)

    def test_answer_creates_and_persists_a_session(self):
        session_title = "Telegram 2026-10-22T14:35:12Z__opaque-suffix"
        response = {"parts": [{"type": "text", "text": "Hello"}]}
        message = {
            "info": {"id": "message-1", "role": "assistant", "agent": "build", "cost": 0.1},
            "parts": [],
        }
        with patch.object(bridge, "_new_telegram_session_title", return_value=session_title) as title:
            with patch.object(http_api, "http", side_effect=[{"id": "session-1"}, response]) as request:
                with patch.object(
                    opencode_api,
                    "session_tree_messages",
                    side_effect=[[], [{"session_id": "session-1", "message": message}]],
                ):
                    with patch.object(session_state, "save_sessions") as save:
                        result = bridge.answer(123, "Hi", {})

        self.assertEqual(result, "Hello\n\nCost: $0.100000 (build: $0.100000)")
        title.assert_called_once_with()
        request.assert_has_calls(
            [
                call(bridge_config.OC + "/session", {"title": session_title}),
                call(
                    bridge_config.OC + "/session/session-1/message",
                    {"system": bridge_config.SYSTEM, "parts": [{"type": "text", "text": "Hi"}]},
                ),
            ]
        )
        save.assert_called_once_with({"123": "session-1"})

    def test_answer_reuses_existing_session(self):
        response = {"parts": [{"type": "text", "text": "Existing session"}]}
        message = {"info": {"id": "message-1", "role": "assistant", "cost": 0.25}, "parts": []}
        sessions = {"123": "existing-session"}
        with patch.object(http_api, "http", return_value=response) as request:
            with patch.object(
                opencode_api,
                "session_tree_messages",
                side_effect=[[], [{"session_id": "existing-session", "message": message}]],
            ):
                result = bridge.answer(123, "Continue", sessions)
        self.assertEqual(result, "Existing session\n\nCost: $0.250000")
        request.assert_called_once()
        self.assertIn("/session/existing-session/message", request.call_args.args[0])

    def test_send_splits_long_replies(self):
        with patch.object(http_api, "telegram") as send_message:
            bridge.send(123, "x" * 8500)

        self.assertEqual(send_message.call_count, 3)
        self.assertEqual([len(item.args[1]["text"]) for item in send_message.call_args_list], [4000, 4000, 500])

    def test_edit_reply_replaces_ack_and_sends_remaining_chunks(self):
        with patch.object(http_api, "telegram") as telegram:
            bridge.edit_reply(123, 456, "x" * 8500)

        self.assertEqual(
            [item.args[0] for item in telegram.call_args_list],
            ["editMessageText", "sendMessage", "sendMessage"],
        )
        self.assertEqual(telegram.call_args_list[0].args[1]["message_id"], 456)
        self.assertEqual(
            [len(item.args[1]["text"]) for item in telegram.call_args_list],
            [4000, 4000, 500],
        )

    def test_handle_update_sends_ack_before_enqueuing_worker(self):
        update = {
            "update_id": 987,
            "message": {"chat": {"id": 123}, "text": "Explain this"},
        }
        with patch.object(bridge, "send", return_value={"message_id": 654}) as send:
            with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
                with patch.object(bridge, "_REQUEST_SLOTS") as slots:
                    slots.acquire.return_value = True
                    bridge.handle_update(update, {})

        send.assert_called_once()
        self.assertIn("Ref:", send.call_args.args[1])
        self.assertIn("/status", send.call_args.args[1])
        self.assertIn("/abort", send.call_args.args[1])
        keyboard = send.call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertEqual([button["text"] for button in keyboard[0]], ["Status", "Abort"])
        submit.assert_called_once()
        self.assertEqual(
            submit.call_args.args[:3],
            (bridge._request_worker_with_slot, 123, "Explain this"),
        )
        self.assertEqual(
            submit.call_args.args[3:],
            (
                {},
                987,
                next(
                    line.removeprefix("Ref: ")
                    for line in send.call_args.args[1].splitlines()
                    if line.startswith("Ref: ")
                ),
                654,
            ),
        )
        self.assertTrue(send.call_args.kwargs["reply_markup"]["inline_keyboard"])

    def test_async_answer_uses_correlated_completion_and_reports_cost(self):
        prompt_id = "msg_prompt-id"
        assistant = {
            "info": {
                "id": "assistant-id",
                "parentID": prompt_id,
                "role": "assistant",
                "time": {"completed": 123},
                "cost": 0.25,
            },
            "parts": [{"type": "text", "text": "Done"}],
        }
        sessions = {"123": "session-1"}
        phases = []
        with patch.object(bridge.uuid, "uuid4", return_value=SimpleNamespace(hex=prompt_id.removeprefix("msg_"))):
            with patch.object(
                opencode_api,
                "session_tree_messages",
                side_effect=[[], [{"session_id": "session-1", "message": assistant}]],
            ):
                with patch.object(opencode_api, "submit_async") as submit:
                    with patch.object(opencode_api, "session_status", return_value={}):
                        with patch.object(opencode_api, "session_messages", return_value=[assistant]):
                            with patch.object(opencode_api, "iter_events", return_value=iter(())):
                                result = bridge.answer_async(
                                    123,
                                    "Hi",
                                    sessions,
                                    "REF12345",
                                    phases.append,
                                    lambda _text: None,
                                )

        self.assertEqual(result, "Done\n\nCost: $0.250000\n\nTools used: none\nTool calls: 0")
        submit.assert_called_once()
        self.assertEqual(
            submit.call_args.args[:3],
            (
                "session-1",
                prompt_id,
                {
                    "system": bridge_config.SYSTEM,
                    "parts": [{"type": "text", "text": "Hi"}],
                },
            ),
        )
        self.assertEqual(submit.call_args.args[3], bridge_config.OPENCODE_STATUS_TIMEOUT_SECONDS)
        self.assertIn("response_wait", phases)

    def test_observed_cost_is_partial_and_only_sums_assistant_usage(self):
        messages = [
            {"info": {"role": "assistant", "cost": 0.125}},
            {"info": {"role": "assistant"}},
            {"info": {"role": "user", "cost": 9.0}},
        ]

        self.assertEqual(
            cost_reporting.format_observed_cost(messages),
            "Cost observed so far: $0.125000 (partial)",
        )

    def test_async_answer_creates_session_with_an_opaque_title(self):
        session_title = "Telegram 2026-10-22T14:35:12Z__opaque-suffix"
        sessions = {}
        with ExitStack() as stack:
            title = stack.enter_context(
                patch.object(bridge, "_new_telegram_session_title", return_value=session_title)
            )
            create_session = stack.enter_context(
                patch.object(http_api, "http", return_value={"id": "session-1"})
            )
            save = stack.enter_context(patch.object(session_state, "save_sessions"))
            stack.enter_context(patch.object(bridge, "OPENCODE_REQUEST_MAX_DURATION_SECONDS", 0))
            stack.enter_context(patch.object(bridge.uuid, "uuid4", return_value=SimpleNamespace(hex="prompt")))
            stack.enter_context(patch.object(opencode_api, "session_tree_messages", return_value=[]))
            stack.enter_context(patch.object(opencode_api, "submit_async"))
            stack.enter_context(patch.object(opencode_api, "session_status", return_value={}))
            stack.enter_context(
                patch.object(
                    opencode_api,
                    "session_messages",
                    return_value=[{"info": {"id": "msg_prompt", "role": "user"}}],
                )
            )
            stack.enter_context(patch.object(opencode_api, "abort_session", return_value=False))
            stack.enter_context(patch.object(opencode_api, "iter_events", return_value=iter(())))
            with self.assertRaises(bridge.OpenCodeRequestStopped):
                bridge.answer_async(
                    123,
                    "Hi",
                    sessions,
                    "REF12345",
                    lambda _phase: None,
                    lambda _text: None,
                )

        title.assert_called_once_with()
        create_session.assert_called_once_with(
            bridge_config.OC + "/session",
            {"title": session_title},
        )
        self.assertEqual(sessions, {"123": "session-1"})
        save.assert_called_once_with({"123": "session-1"})

    def test_async_answer_forwards_only_safe_progress_for_its_assistant_message(self):
        prompt_id = "msg_prompt-id"
        incomplete = {
            "info": {"id": "assistant-id", "parentID": prompt_id, "role": "assistant"},
            "parts": [],
        }
        assistant = {
            "info": {
                "id": "assistant-id",
                "parentID": prompt_id,
                "role": "assistant",
                "time": {"completed": 123},
                "cost": 0,
            },
            "parts": [{"type": "text", "text": "Done"}],
        }

        def event_stream():
            yield {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "sessionID": "session-1",
                        "messageID": "assistant-id",
                        "type": "reasoning",
                        "text": "private reasoning content",
                    }
                },
            }
            time.sleep(0.02)

        progress = []
        with patch.object(bridge.uuid, "uuid4", return_value=SimpleNamespace(hex=prompt_id.removeprefix("msg_"))):
            with patch.object(
                opencode_api,
                "session_tree_messages",
                side_effect=[[], [{"session_id": "session-1", "message": assistant}]],
            ):
                with patch.object(opencode_api, "submit_async"):
                    with patch.object(
                        opencode_api,
                        "session_status",
                        side_effect=[{"session-1": {"type": "busy"}}, {}, {}],
                    ):
                        with patch.object(
                            opencode_api,
                            "session_messages",
                            side_effect=[[incomplete], [assistant], [assistant]],
                        ):
                            with patch.object(opencode_api, "iter_events", side_effect=event_stream):
                                result = bridge.answer_async(
                                    123,
                                    "Hi",
                                    {"123": "session-1"},
                                    "REF12345",
                                    lambda _phase: None,
                                    progress.append,
                                )

        self.assertEqual(result, "Done\n\nCost: $0.000000\n\nTools used: none\nTool calls: 0")
        self.assertEqual(
            progress,
            [
                "Analysis in progress (0s elapsed).\n"
                "OpenCode activity: 0s ago; API heartbeat: 0s ago."
            ],
        )
        self.assertNotIn("private reasoning content", " ".join(progress))

    def test_async_answer_raises_explicit_timeout_at_deadline(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(bridge, "OPENCODE_REQUEST_MAX_DURATION_SECONDS", 0))
            stack.enter_context(patch.object(bridge.uuid, "uuid4", return_value=SimpleNamespace(hex="prompt")))
            stack.enter_context(patch.object(opencode_api, "session_tree_messages", return_value=[]))
            stack.enter_context(patch.object(opencode_api, "submit_async"))
            stack.enter_context(patch.object(opencode_api, "session_status", return_value={}))
            stack.enter_context(
                patch.object(
                    opencode_api,
                    "session_messages",
                    return_value=[{"info": {"id": "msg_prompt", "role": "user"}}],
                )
            )
            stack.enter_context(patch.object(opencode_api, "abort_session", return_value=False))
            stack.enter_context(patch.object(opencode_api, "iter_events", return_value=iter(())))
            with self.assertRaises(bridge.OpenCodeRequestStopped):
                bridge.answer_async(
                    123,
                    "Hi",
                    {"123": "session-1"},
                    "REF12345",
                    lambda _phase: None,
                    lambda _text: None,
                )

    def test_assistant_message_chain_is_correlated_to_its_user_prompt(self):
        messages = [
            {"info": {"id": "other", "parentID": "other-prompt", "role": "assistant"}},
            {"info": {"id": "step-1", "parentID": "msg_prompt", "role": "assistant"}},
            {"info": {"id": "step-2", "parentID": "step-1", "role": "assistant"}},
        ]

        result = opencode_api.assistant_messages_for_prompt(messages, "msg_prompt")

        self.assertEqual([message["info"]["id"] for message in result], ["step-1", "step-2"])

    def test_safe_error_details_do_not_include_untrusted_exception_text(self):
        private_detail = "private prompt and token-like-value"
        self.assertEqual(bridge._safe_error_detail(RuntimeError(private_detail)), "detail=redacted")
        wrapped_timeout = urllib.error.URLError(TimeoutError(private_detail))
        self.assertTrue(bridge._is_timeout_error(wrapped_timeout))
        self.assertEqual(
            bridge._safe_error_detail(wrapped_timeout),
            "url_error=TimeoutError socket_timeout=true",
        )
        with self.assertRaises(opencode_api.OpenCodeResponseError) as caught:
            opencode_api.text_from_response(
                {"info": {"error": {"name": "ProviderError", "data": {"message": private_detail}}}}
            )
        self.assertNotIn(private_detail, str(caught.exception))
        self.assertEqual(bridge._safe_error_detail(caught.exception), "response_error=ProviderError")

    def test_open_code_retry_status_is_still_considered_busy(self):
        self.assertTrue(bridge._session_is_busy({"type": "retry"}))

    def test_worker_reports_timeout_without_claiming_request_was_cancelled(self):
        output = io.StringIO()
        with patch.object(bridge, "answer_async", side_effect=bridge.OpenCodeRequestTimeout()):
            with patch.object(bridge, "edit_reply") as edit:
                with patch.object(bridge, "send") as send:
                    with patch("sys.stderr", output):
                        bridge._request_worker(123, "private prompt", {}, 987, "REF12345", 654)

        self.assertIn("may still be running", send.call_args.args[1])
        self.assertIn(
            "An operator can inspect the session using the remote TUI (`make remote-tui`).",
            send.call_args.args[1],
        )
        self.assertIn("REF12345", send.call_args.args[1])
        self.assertIn("Status: failed", edit.call_args.args[2])
        self.assertIn("phase=opencode_request", output.getvalue())
        self.assertIn("timeout_s=432000", output.getvalue())
        self.assertNotIn("private prompt", output.getvalue())

    def test_worker_reports_generic_opencode_error_with_reference_only(self):
        output = io.StringIO()
        with patch.object(bridge, "answer_async", side_effect=RuntimeError("private prompt text")):
            with patch.object(bridge, "edit_reply") as edit:
                with patch.object(bridge, "send") as send:
                    with patch("sys.stderr", output):
                        bridge._request_worker(123, "Hi", {}, 990, "REF98765", 656)

        self.assertIn("OpenCode request failed", send.call_args.args[1])
        self.assertNotIn("remote TUI", send.call_args.args[1])
        self.assertIn("REF98765", send.call_args.args[1])
        self.assertIn("Status: failed", edit.call_args.args[2])
        self.assertNotIn("private prompt text", output.getvalue())
        self.assertIn("detail=redacted", output.getvalue())

    def test_worker_does_not_send_a_second_message_when_final_edit_fails(self):
        output = io.StringIO()
        with patch.object(bridge, "answer_async", return_value="Done"):
            with patch.object(bridge, "edit_reply", side_effect=TimeoutError()) as edit:
                with patch.object(bridge, "send") as send:
                    with patch("sys.stderr", output):
                        bridge._request_worker(123, "Hi", {}, 989, "REF54321", 655)

        edit.assert_called_once()
        send.assert_called_once()
        self.assertIn("phase=telegram_terminal_summary_edit", output.getvalue())
        self.assertNotIn("phase=telegram_error_edit", output.getvalue())

    def test_telegram_ack_timeout_is_not_misreported_as_opencode_failure(self):
        update = {
            "update_id": 988,
            "message": {"chat": {"id": 123}, "text": "Hi"},
        }
        output = io.StringIO()
        with patch.object(bridge, "send", side_effect=TimeoutError()):
            with patch("sys.stderr", output):
                bridge.handle_update(update, {})

        self.assertIn("client=telegram", output.getvalue())
        self.assertIn("phase=telegram_acknowledgement", output.getvalue())
        self.assertIn("timeout_s=70", output.getvalue())

    def test_timeout_configuration_is_logged_without_secrets(self):
        output = io.StringIO()
        with patch("sys.stderr", output):
            bridge._log_timeout_configuration()

        self.assertIn("OpenCode request max=432000s", output.getvalue())
        self.assertIn("events=15s", output.getvalue())
        self.assertIn("Telegram=70s", output.getvalue())
        self.assertIn("long poll=50s", output.getvalue())
        self.assertNotIn("unit-test-secret", output.getvalue())

    def test_409_conflict_is_recognized_without_exposing_token(self):
        conflict = telegram_http_error(409, {"description": "another getUpdates request is active"})
        with patch("urllib.request.urlopen", side_effect=conflict):
            with self.assertRaises(http_api.TelegramConflict) as caught:
                http_api.telegram("getUpdates", {})

        self.assertNotIn("unit-test-secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_opencode_http_errors_do_not_log_session_ids_or_response_bodies(self):
        error = urllib.error.HTTPError(
            bridge_config.OC + "/session/private-session-id/message",
            500,
            "Server error",
            {},
            io.BytesIO(b"private response body"),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                http_api.http(bridge_config.OC + "/session/private-session-id/message")

        self.assertEqual(str(caught.exception), "OpenCode API HTTP 500")
        self.assertTrue(caught.exception.__suppress_context__)

    def test_opencode_http_error_retains_only_safe_classification_metadata(self):
        error = urllib.error.HTTPError(
            bridge_config.OC + "/session/private-session-id/message",
            503,
            "Server error",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "error": {"name": "ProviderUnavailable", "message": "private response text"},
                        "code": "UPSTREAM_DOWN",
                    }
                ).encode()
            ),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(http_api.OpenCodeHTTPError) as caught:
                http_api.http(bridge_config.OC + "/session/private-session-id/message")

        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.error_name, "ProviderUnavailable")
        self.assertEqual(caught.exception.error_code, "UPSTREAM_DOWN")
        self.assertNotIn("private response text", str(caught.exception))
        self.assertNotIn("private-session-id", str(caught.exception))

    def test_opencode_validation_error_logs_field_names_but_not_values(self):
        error = urllib.error.HTTPError(
            bridge_config.OC + "/session/session-1/prompt_async",
            400,
            "Bad request",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "name": "InvalidRequestError",
                        "data": {
                            "issues": [
                                {
                                    "path": ["body", "messageID"],
                                    "code": "invalid_value",
                                    "message": "private validation detail",
                                }
                            ]
                        },
                    }
                ).encode()
            ),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(http_api.OpenCodeHTTPError) as caught:
                http_api.http(bridge_config.OC + "/session/session-1/prompt_async")

        detail = bridge._safe_error_detail(caught.exception)
        self.assertIn("http_status=400", detail)
        self.assertIn("error_name=InvalidRequestError", detail)
        self.assertIn("validation_fields=body.messageID", detail)
        self.assertIn("validation_codes=invalid_value", detail)
        self.assertNotIn("private validation detail", detail)

    def test_429_waits_for_retry_after_then_retries(self):
        rate_limit = telegram_http_error(
            429,
            {"parameters": {"retry_after": 4}},
            method="sendMessage",
        )
        success = FakeResponse(b'{"ok": true, "result": {"message_id": 5}}')
        with patch("urllib.request.urlopen", side_effect=[rate_limit, success]) as urlopen:
            with patch.object(http_api.time, "sleep") as sleep:
                result = http_api.telegram("sendMessage", {"chat_id": 123, "text": "Hi"})

        self.assertEqual(result, {"message_id": 5})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(4)

    def test_cost_diagnostics_are_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cost-diagnostics.jsonl")
            with patch.object(cost_reporting, "COST_DIAGNOSTICS_ENABLED", False):
                with patch.object(cost_reporting, "COST_DIAGNOSTICS_PATH", path):
                    cost_reporting.record_cost_diagnostics({"info": {"cost": 0.1}, "parts": []})
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
            with patch.object(cost_reporting, "COST_DIAGNOSTICS_ENABLED", True):
                with patch.object(cost_reporting, "COST_DIAGNOSTICS_PATH", path):
                    cost_reporting.record_cost_diagnostics(response)
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
        session_url = bridge_config.OC + "/session/session-1/message"
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
        with patch.object(cost_reporting, "COST_DIAGNOSTICS_ENABLED", True):
            with patch.object(
                opencode_api,
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
                with patch.object(http_api, "http", return_value=response) as request:
                    with patch.object(cost_reporting, "record_cost_diagnostics") as record:
                        result = bridge.answer(123, "Use a tool", {"123": "session-1"})

        self.assertEqual(result, "Done\n\nCost: $0.500000 (build: $0.200000, helper: $0.300000)")
        request.assert_called_once_with(session_url, {"system": bridge_config.SYSTEM, "parts": [{"type": "text", "text": "Use a tool"}]})
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
            cost_reporting,
            "session_tree_messages",
            return_value=[{"session_id": "session-1", "message": item} for item in messages]
            + [{"session_id": "child-session", "message": child_message}],
        ):
            report = cost_reporting.cost_report("session-1", now)

        self.assertIn("Session: $0.012000", report)
        self.assertIn("UTC hour (18:00–19:00): $0.007000", report)
        self.assertIn("Agents (session): build: $0.006000, helper: $0.002000, reviewer: $0.004000", report)
        self.assertIn("Tokens (session): 100 input, 25 cached, 20 output", report)

    def test_cost_report_marks_incomplete_cost_unavailable(self):
        messages = [{"info": {"role": "assistant", "time": {"created": 1791744000000}}, "parts": []}]
        with patch.object(
            cost_reporting,
            "session_tree_messages",
            return_value=[{"session_id": "session-1", "message": message} for message in messages],
        ):
            report = cost_reporting.cost_report("session-1", datetime(2026, 10, 11, 18, 30, tzinfo=timezone.utc))
        self.assertIn("Session: unavailable", report)
        self.assertIn("UTC hour (18:00–19:00): unavailable", report)

    def test_cost_command_does_not_call_the_llm(self):
        messages = [{"info": {"role": "assistant", "cost": 0.125, "time": {"created": 1791744000000}}, "parts": []}]
        with patch.object(
            cost_reporting,
            "session_tree_messages",
            return_value=[{"session_id": "session-1", "message": message} for message in messages],
        ):
            with patch.object(http_api, "http") as request:
                report = bridge.authorized_response(123, "/cost@bloc_bot", {"123": "session-1"})

        self.assertIn("Session: $0.125000", report)
        request.assert_not_called()

    def test_help_lists_commands_without_calling_the_llm(self):
        with patch.object(bridge, "answer") as answer:
            response = bridge.authorized_response(123, "/help", {})

        for command in ("/start", "/help", "/new", "/health", "/cost"):
            with self.subTest(command=command):
                self.assertIn(command, response)
        answer.assert_not_called()

    def test_start_welcomes_user_and_points_to_help_without_calling_the_llm(self):
        with patch.object(bridge, "answer") as answer:
            response = bridge.authorized_response(123, "/start", {})

        self.assertIn("Welcome", response)
        self.assertIn("/help", response)
        answer.assert_not_called()

    def test_unknown_slash_commands_return_help_hint_without_calling_the_llm(self):
        with patch.object(bridge, "answer") as answer:
            for prompt in ("/", "/unknown", "/unknown@bloc_bot args"):
                with self.subTest(prompt=prompt):
                    response = bridge.authorized_response(123, prompt, {})
                    self.assertIn("Unknown command", response)
                    self.assertIn("/help", response)

        answer.assert_not_called()

    def test_new_command_persists_a_fresh_session_without_removing_the_old_one(self):
        session_title = "Telegram 2026-10-22T14:35:12Z__opaque-suffix"
        sessions = {"123": "old-session", "456": "other-chat-session"}
        with patch.object(bridge, "_new_telegram_session_title", return_value=session_title) as title:
            with patch.object(http_api, "http", return_value={"id": "new-session"}) as request:
                with patch.object(session_state, "save_sessions") as save:
                    with patch.object(bridge, "answer") as answer:
                        response = bridge.authorized_response(123, "/new", sessions)

        self.assertEqual(sessions, {"123": "new-session", "456": "other-chat-session"})
        title.assert_called_once_with()
        save.assert_called_once_with({"123": "new-session", "456": "other-chat-session"})
        request.assert_called_once_with(
            bridge_config.OC + "/session",
            {"title": session_title},
        )
        self.assertIn("previous session and its history are kept", response)
        answer.assert_not_called()

    def test_new_telegram_session_titles_are_opaque_and_unique_within_same_second(self):
        timestamp = "2026-10-22T14:35:12Z"
        random_suffixes = ["a4f91c2e" * 4, "b5c02d3f" * 4]
        with patch.object(bridge, "datetime") as clock:
            clock.now.return_value.strftime.return_value = timestamp
            with patch.object(
                bridge.uuid,
                "uuid4",
                side_effect=[SimpleNamespace(hex=suffix) for suffix in random_suffixes],
            ):
                titles = [bridge._new_telegram_session_title() for _ in random_suffixes]

        self.assertEqual(
            titles,
            [f"Telegram {timestamp}__{suffix}" for suffix in random_suffixes],
        )
        self.assertEqual(len(set(titles)), 2)
        self.assertNotIn("Telegram 123", " ".join(titles))

    def test_cost_starts_at_zero_after_creating_a_new_session(self):
        sessions = {"123": "old-session"}
        with patch.object(http_api, "http", return_value={"id": "new-session"}):
            with patch.object(session_state, "save_sessions"):
                bridge.authorized_response(123, "/new", sessions)
        with patch.object(cost_reporting, "session_tree_messages", return_value=[]) as history:
            report = bridge.authorized_response(123, "/cost", sessions)

        self.assertIn("Session: $0.000000", report)
        self.assertIn("UTC hour (", report)
        self.assertIn("$0.000000", report)
        history.assert_called_once_with("new-session")

    def test_worker_does_not_forward_unknown_slash_commands_to_the_llm(self):
        with patch.object(bridge, "authorized_response", return_value="Use /help") as respond:
            with patch.object(bridge, "answer_async") as answer:
                with patch.object(bridge, "edit_reply"):
                    with patch.object(bridge, "send"):
                        bridge._request_worker(123, "/unknown", {}, 991, "REF12345", 657)

        respond.assert_called_once_with(123, "/unknown", {})
        answer.assert_not_called()

    def test_health_command_reports_only_sanitized_status_without_a_model_call(self):
        with patch.object(http_api, "http", return_value={"healthy": True}) as request:
            with patch.object(
                opencode_api,
                "session_details",
                return_value={"id": "private-session-id", "title": "private title"},
            ) as get_session:
                with patch.object(bridge, "answer") as answer:
                    report = bridge.authorized_response(123, "/health", {"123": "private-session-id"})

        self.assertEqual(
            report,
            "Bridge: healthy\nOpenCode API: healthy\nThis chat's session: available\nOpper API: not checked (disabled)",
        )
        self.assertNotIn("private-session-id", report)
        self.assertNotIn("private title", report)
        request.assert_called_once_with(
            bridge_config.OC + "/global/health",
            timeout=bridge_config.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        get_session.assert_called_once_with(
            "private-session-id",
            timeout=bridge_config.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        answer.assert_not_called()

    def test_health_command_does_not_require_a_session(self):
        with patch.object(http_api, "http", return_value={"healthy": False}):
            with patch.object(opencode_api, "session_details") as get_session:
                report = bridge.authorized_response(123, "/health", {})

        self.assertEqual(
            report,
            "Bridge: healthy\nOpenCode API: unavailable\nThis chat's session: not initialized\nOpper API: not checked (disabled)",
        )
        get_session.assert_not_called()

    def test_unauthorized_health_command_is_rejected(self):
        update = {"message": {"chat": {"id": 456}, "text": "/health"}}
        with patch.object(bridge, "authorized_response") as respond:
            with patch.object(bridge, "send") as send:
                bridge.handle_update(update, {})

        respond.assert_not_called()
        send.assert_not_called()

    def test_shutdown_signal_stops_before_processing_returned_updates(self):
        stop_event = threading.Event()
        updates = [{"update_id": 10, "message": {"chat": {"id": 123}, "text": "Hello"}}]

        def poll(_method, _payload):
            stop_event.set()
            return updates

        with patch.object(http_api, "acquire_polling_lock") as acquire_lock:
            acquire_lock.return_value.close = Mock()
            with patch.object(http_api, "telegram", side_effect=poll):
                with patch.object(bridge, "handle_update") as handle_update:
                    bridge.main(stop_event)

        handle_update.assert_not_called()
        acquire_lock.return_value.close.assert_called_once_with()

    def test_cost_report_uses_the_current_chats_session(self):
        first_session = [{"session_id": "session-a", "message": {"info": {"role": "assistant", "cost": 0.1}}}]
        second_session = [{"session_id": "session-b", "message": {"info": {"role": "assistant", "cost": 0.2}}}]
        with patch.object(cost_reporting, "session_tree_messages", side_effect=[first_session, second_session]) as get_history:
            first = bridge.authorized_response(123, "/cost", {"123": "session-a", "456": "session-b"})
            second = bridge.authorized_response(456, "/cost", {"123": "session-a", "456": "session-b"})

        self.assertIn("Session: $0.100000", first)
        self.assertIn("Session: $0.200000", second)
        self.assertEqual(get_history.call_args_list, [call("session-a"), call("session-b")])

    def test_session_tree_includes_recursive_child_messages(self):
        main_message = {"info": {"id": "main-message", "role": "assistant"}, "parts": []}
        child_message = {"info": {"id": "child-message", "role": "assistant"}, "parts": []}
        with patch.object(opencode_api, "session_messages", side_effect=[[main_message], [child_message]]) as get_messages:
            with patch.object(opencode_api, "http", side_effect=[[{"id": "child-session"}], []]) as get_children:
                result = opencode_api.session_tree_messages("main-session")

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
                call(bridge_config.OC + "/session/main-session/children"),
                call(bridge_config.OC + "/session/child-session/children"),
            ]
        )

    def test_session_details_uses_the_read_only_session_endpoint(self):
        with patch.object(opencode_api, "http", return_value={"id": "session one"}) as request:
            result = opencode_api.session_details("session one", timeout=10)

        self.assertEqual(result, {"id": "session one"})
        request.assert_called_once_with(bridge_config.OC + "/session/session%20one", timeout=10)


if __name__ == "__main__":
    unittest.main()
