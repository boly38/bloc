"""Offline tests for durable Telegram request admission and controls."""

import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot", "telegram"))
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ALLOWED_CHAT_IDS": "123"}):
    from bloc_bridge import bridge, http_api, request_store
    from bloc_bridge.opencode import api as opencode_api


class TelegramRequestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path_patch = patch.object(
            request_store,
            "REQUESTS_PATH",
            os.path.join(self.directory.name, "requests.sqlite3"),
        )
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        request_store.initialize()
        self.real_pending_permissions = opencode_api.pending_permissions
        self.permission_patch = patch.object(opencode_api, "pending_permissions", return_value=[])
        self.permission_patch.start()
        self.addCleanup(self.permission_patch.stop)

    def test_create_is_idempotent_for_a_telegram_update(self):
        request, created = request_store.create("A" * 16, 123, 101)
        duplicate, duplicate_created = request_store.create("B" * 16, 123, 101)

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(request["reference"], duplicate["reference"])

    def test_request_database_stores_only_private_metadata(self):
        request, _ = request_store.create("9" * 16, 123, 100)
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="msg_private",
        )
        connection = sqlite3.connect(request_store.REQUESTS_PATH)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(requests)")}
        finally:
            connection.close()
        mode = stat.S_IMODE(os.stat(request_store.REQUESTS_PATH).st_mode)

        self.assertNotIn("prompt", columns)
        self.assertNotIn("response", columns)
        self.assertEqual(mode, 0o600)

    def test_active_request_rejects_another_prompt_in_the_same_chat(self):
        first = {"update_id": 101, "message": {"chat": {"id": 123}, "text": "First"}}
        second = {"update_id": 102, "message": {"chat": {"id": 123}, "text": "Second"}}
        with patch.object(bridge, "send", side_effect=[{"message_id": 501}, {"message_id": 502}]) as send:
            with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
                with patch.object(bridge._REQUEST_SLOTS, "acquire", return_value=True):
                    bridge.handle_update(first, {})
                    active = request_store.active_for_chat(123)[0]
                    bridge.handle_update(second, {})

        self.assertEqual(send.call_count, 2)
        self.assertIn(active["reference"], send.call_args.args[1])
        self.assertIn("already has an active request", send.call_args.args[1])
        submit.assert_called_once()
        self.assertEqual(request_store.get_by_update_id(102)["state"], "rejected")
        self.assertIsNone(request_store.get_by_update_id(102)["acknowledgement_id"])

    def test_duplicate_telegram_update_does_not_acknowledge_or_enqueue_twice(self):
        update = {"update_id": 103, "message": {"chat": {"id": 123}, "text": "Once"}}
        with patch.object(bridge, "send", return_value={"message_id": 503}) as send:
            with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
                with patch.object(bridge._REQUEST_SLOTS, "acquire", return_value=True):
                    bridge.handle_update(update, {})
                    bridge.handle_update(update, {})

        send.assert_called_once()
        submit.assert_called_once()

    def test_request_acknowledgement_shows_the_configured_model(self):
        update = {"update_id": 1031, "message": {"chat": {"id": 123}, "text": "Use Copilot"}}
        model_reference = "github-copilot/gpt-6-luna#medium"
        with patch.object(bridge, "MODEL", model_reference):
            with patch.object(bridge, "send", return_value={"message_id": 504}) as send:
                with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
                    with patch.object(bridge._REQUEST_SLOTS, "acquire", return_value=True):
                        bridge.handle_update(update, {})

        acknowledgement = send.call_args.args[1]
        self.assertIn("Provider: `github-copilot`", acknowledgement)
        self.assertIn("Model: `gpt-6-luna`", acknowledgement)
        self.assertIn("Variant: `medium`", acknowledgement)
        request = request_store.get_by_update_id(1031)
        bridge._forget_request_presentation(request["reference"])
        submit.assert_called_once()

    def test_non_model_command_sends_only_its_result_without_a_request_card(self):
        update = {"update_id": 1031, "message": {"chat": {"id": 123}, "text": "/start"}}
        with patch.object(bridge, "send") as send:
            with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
                with patch.object(bridge, "_REQUEST_SLOTS") as slots:
                    slots.acquire.return_value = True
                    bridge.handle_update(update, {})
                    worker, *args = submit.call_args.args
                    worker(*args)

        self.assertEqual(submit.call_args.args[0], bridge._run_with_slot)
        self.assertEqual(submit.call_args.args[1], bridge._command_worker)
        self.assertEqual(send.call_count, 1)
        self.assertIn("Welcome!", send.call_args.args[1])
        self.assertNotIn("Ref:", send.call_args.args[1])
        self.assertNotIn("reply_markup", send.call_args.kwargs)
        self.assertIsNone(request_store.get_by_update_id(1031))

    def test_status_is_private_to_the_chat_and_does_not_expose_session_ids(self):
        request, _ = request_store.create("C" * 16, 123, 104, state="running")
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="private-prompt-id",
        )

        self.assertIn("running in OpenCode", bridge.request_status_response(123, request["reference"]))
        self.assertIn(request["reference"], bridge.request_status_response(123))
        self.assertIn("found for this chat", bridge.request_status_response(456, request["reference"]))
        self.assertNotIn("private-session-id", bridge.request_status_response(123, request["reference"]))
        self.assertNotIn("private-prompt-id", bridge.request_status_response(123, request["reference"]))

    def test_status_command_appends_timestamped_update_to_request_card(self):
        request, _ = request_store.create("C" * 16, 123, 1041, state="running")
        request_store.update(request["reference"], acknowledgement_id=541)
        presentation = bridge._request_presentation(request["reference"], 123, 541)
        presentation.set_progress("Analysis in progress")
        update = {"update_id": 1042, "message": {"chat": {"id": 123}, "text": "/status"}}
        with patch.object(bridge, "edit_reply") as edit:
            with patch.object(bridge, "send") as send:
                bridge.handle_update(update, {})

        send.assert_not_called()
        self.assertEqual(edit.call_count, 1)
        self.assertIn("Status checks:", edit.call_args.args[2])
        self.assertRegex(edit.call_args.args[2], r"\[\d\d:\d\d:\d\d UTC\] Request .+: running in OpenCode\.")
        self.assertNotIn("/cancel", edit.call_args.args[2])
        self.assertNotIn("/abort", edit.call_args.args[2])
        self.assertEqual(edit.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["text"], "Status")
        bridge._forget_request_presentation(request["reference"])

    def test_status_inline_button_updates_same_request_card(self):
        request, _ = request_store.create("C" * 16, 123, 1043, state="running")
        request_store.update(request["reference"], acknowledgement_id=542)
        bridge._request_presentation(request["reference"], 123, 542)
        update = {
            "update_id": 1044,
            "callback_query": {
                "id": "callback-status",
                "data": f"status:{request['reference']}",
                "message": {"message_id": 542, "chat": {"id": 123}},
            },
        }
        with patch.object(bridge, "edit_reply") as edit:
            with patch.object(http_api, "telegram") as telegram:
                bridge.handle_update(update, {})

        self.assertIn("Status checks:", edit.call_args.args[2])
        telegram.assert_called_once_with(
            "answerCallbackQuery",
            {"callback_query_id": "callback-status", "text": "Status added to the request message."},
        )
        bridge._forget_request_presentation(request["reference"])

    def test_status_check_disappears_after_three_minutes_without_changing_progress(self):
        class FakeTimer:
            def __init__(self, interval, function):
                self.interval = interval
                self.function = function
                self.cancelled = False

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

            def fire(self):
                self.function()

        request, _ = request_store.create("C" * 16, 123, 1045, state="running")
        request_store.update(request["reference"], acknowledgement_id=543)
        presentation = bridge._request_presentation(request["reference"], 123, 543)
        presentation.set_progress("Request is still running")

        with patch("bloc_bridge.telegram.request_presentation.time.monotonic", return_value=100):
            with patch.object(bridge.threading, "Timer", FakeTimer):
                with patch.object(bridge, "edit_reply") as edit:
                    self.assertTrue(bridge._append_request_status(123, request["reference"], "Still running"))
                    timer = presentation.status_expiry_timer
                    self.assertEqual(timer.interval, 180)

                    with patch(
                        "bloc_bridge.telegram.request_presentation.time.monotonic",
                        return_value=280,
                    ):
                        timer.fire()

        self.assertEqual(edit.call_count, 2)
        self.assertNotIn("Status checks:", edit.call_args.args[2])
        self.assertIn("Request is still running", edit.call_args.args[2])
        bridge._forget_request_presentation(request["reference"])

    def test_cancel_requests_real_opencode_abort_and_reports_confirmation(self):
        request, _ = request_store.create("D" * 16, 123, 105, state="running")
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="msg_prompt",
        )
        with patch.object(opencode_api, "abort_session", return_value=True) as abort:
            with patch.object(
                opencode_api,
                "session_messages",
                return_value=[{"info": {"id": "msg_prompt", "role": "user"}}],
            ):
                response = bridge.cancel_request_response(123, request["reference"])

        abort.assert_called_once_with(
            "private-session-id",
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        self.assertIn("Cancellation requested", response)
        self.assertEqual(request_store.get(request["reference"])["state"], "cancellation_requested")

    def test_cancel_without_reference_targets_the_active_request(self):
        request, _ = request_store.create("A" * 16, 123, 1050, state="running")
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="msg_prompt",
        )
        update = {"update_id": 1051, "message": {"chat": {"id": 123}, "text": "/cancel"}}
        with patch.object(opencode_api, "abort_session", return_value=True) as abort:
            with patch.object(
                opencode_api,
                "session_messages",
                return_value=[{"info": {"id": "msg_prompt", "role": "user"}}],
            ):
                with patch.object(bridge, "send") as send:
                    bridge.handle_update(update, {})

        abort.assert_called_once_with(
            "private-session-id",
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        self.assertIn(request["reference"], send.call_args.args[1])
        self.assertEqual(request_store.get(request["reference"])["state"], "cancellation_requested")

    def test_permission_inline_button_applies_the_lettered_decision(self):
        request, _ = request_store.create("B" * 16, 123, 1057, state="running")
        request_store.update(request["reference"], session_id="active-session", acknowledgement_id=511)
        permission = {
            "id": "per1",
            "sessionID": "active-session",
            "permission": "webfetch",
            "patterns": ["https://docs.example.test"],
        }
        presentation = bridge._request_presentation(request["reference"], 123, 511)
        presentation.sync_permissions([permission])
        update = {
            "update_id": 1058,
            "callback_query": {
                "id": "permission-callback",
                "data": f"p:{request['reference']}:A:o:per1",
                "message": {"message_id": 511, "chat": {"id": 123}},
            },
        }

        with patch.object(opencode_api, "pending_permissions", return_value=[permission]):
            with patch.object(opencode_api, "respond_to_permission", return_value=True) as respond:
                with patch.object(bridge, "edit_reply") as edit:
                    with patch.object(bridge.http_api, "telegram") as telegram:
                        bridge.handle_update(update, {})

        respond.assert_called_once_with(
            "active-session",
            "per1",
            "once",
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        self.assertIn("Permission A allowed once", edit.call_args.args[2])
        self.assertNotIn("Permissions waiting for a decision", edit.call_args.args[2])
        self.assertEqual(telegram.call_args.args[0], "answerCallbackQuery")

    def test_opencode_abort_uses_session_abort_endpoint(self):
        with patch.object(opencode_api, "http", return_value=True) as request:
            self.assertTrue(opencode_api.abort_session("session with space", timeout=7))

        request.assert_called_once_with(
            f"{bridge.OC}/api/session/session%20with%20space/interrupt",
            timeout=7,
            method="POST",
        )

    def test_pending_permissions_uses_each_session_endpoint_and_normalizes_scope(self):
        raw_permission = {
            "id": "per1",
            "sessionID": "root-session",
            "action": "webfetch",
            "resources": ["https://docs.example.test"],
            "save": ["https://docs.example.test"],
        }
        with patch.object(opencode_api, "session_tree_session_ids", return_value=["root-session"]):
            with patch.object(opencode_api, "http", return_value={"data": [raw_permission]}) as request:
                self.assertEqual(
                    self.real_pending_permissions("root-session", timeout=7),
                    [{
                        "id": "per1",
                        "sessionID": "root-session",
                        "permission": "webfetch",
                        "patterns": ["https://docs.example.test"],
                        "always": ["https://docs.example.test"],
                    }],
                )

        request.assert_called_once_with(f"{bridge.OC}/api/session/root-session/permission", timeout=7)

    def test_monitor_only_surfaces_permissions_for_its_active_session(self):
        permission = {
            "id": "per-active",
            "sessionID": "active-session",
            "permission": "webfetch",
            "patterns": ["https://docs.example.test"],
        }
        monitor = bridge._AsyncRequestMonitor("active-session", "msg_prompt", "REF12345", bridge.time.time())
        with patch.object(opencode_api, "session_status", return_value={"active-session": {"type": "busy"}}):
            with patch.object(
                opencode_api,
                "session_messages",
                return_value=[{"info": {"id": "msg_prompt", "role": "user"}}],
            ):
                with patch.object(
                    opencode_api,
                    "pending_permissions",
                    return_value=[permission, {**permission, "id": "per-other", "sessionID": "other-session"}],
                ):
                    with patch.object(opencode_api, "session_details", return_value={}):
                        snapshot = monitor._read_snapshot()

        self.assertEqual(snapshot.pending_permissions, [permission])

    def test_permission_reply_uses_the_session_scoped_endpoint(self):
        with patch.object(opencode_api, "http", return_value=True) as request:
            self.assertTrue(
                opencode_api.respond_to_permission(
                    "session with space",
                    "permission with space",
                    "once",
                    timeout=7,
                )
            )

        request.assert_called_once_with(
            f"{bridge.OC}/api/session/session%20with%20space/permission/permission%20with%20space/reply",
            {"decision": "once"},
            timeout=7,
        )

    def test_permission_rejection_includes_feedback_for_the_agent(self):
        with patch.object(opencode_api, "http", return_value=True) as request:
            self.assertTrue(
                opencode_api.respond_to_permission(
                    "session with space",
                    "permission with space",
                    "reject",
                    timeout=7,
                    message="The user denied this permission.",
                )
            )

        request.assert_called_once_with(
            f"{bridge.OC}/api/session/session%20with%20space/permission/permission%20with%20space/reply",
            {"decision": "reject", "message": "The user denied this permission."},
            timeout=7,
        )

    def test_allow_command_approves_only_the_active_sessions_permission_once(self):
        request, _ = request_store.create("B" * 16, 123, 1052, state="running")
        request_store.update(request["reference"], session_id="active-session")
        pending = [
            {"id": "per-other", "sessionID": "other-session", "permission": "read", "patterns": ["/**"]},
            {"id": "per-active", "sessionID": "active-session", "permission": "webfetch", "patterns": ["https://docs.example.test"]},
        ]
        update = {"update_id": 1053, "message": {"chat": {"id": 123}, "text": "/allow"}}
        with patch.object(opencode_api, "pending_permissions", return_value=pending) as list_pending:
            with patch.object(opencode_api, "session_details", return_value={}):
                with patch.object(opencode_api, "respond_to_permission", return_value=True) as respond:
                    with patch.object(bridge, "send") as send:
                        bridge.handle_update(update, {})

        list_pending.assert_called_once_with(
            "active-session",
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        respond.assert_called_once_with(
            "active-session",
            "per-active",
            "once",
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        self.assertIn("allowed once", send.call_args.args[1])

    def test_permission_commands_require_explicit_always_for_persistent_approval(self):
        request, _ = request_store.create("B" * 16, 123, 1054, state="running")
        request_store.update(request["reference"], session_id="active-session")
        with patch.object(
            opencode_api,
            "pending_permissions",
            return_value=[{
                "id": "per1",
                "sessionID": "active-session",
                "permission": "read",
                "patterns": ["/**"],
                "always": ["/**"],
            }],
        ):
            with patch.object(opencode_api, "respond_to_permission", return_value=True) as respond:
                response = bridge.permission_reply_response(123, "/allow always", "/allow")

        respond.assert_called_once_with(
            "active-session",
            "per1",
            "always",
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        self.assertIn("remembered", response)

    def test_deny_command_rejects_without_persisting_a_rule(self):
        request, _ = request_store.create("D" * 16, 123, 1056, state="running")
        request_store.update(request["reference"], session_id="active-session")
        with patch.object(
            opencode_api,
            "pending_permissions",
            return_value=[{"id": "per1", "sessionID": "active-session", "permission": "read", "patterns": ["/**"]}],
        ):
            with patch.object(opencode_api, "respond_to_permission", return_value=True) as respond:
                response = bridge.permission_reply_response(123, "/deny", "/deny")

        respond.assert_called_once_with(
            "active-session",
            "per1",
            "reject",
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
            message=(
                "The user denied this permission. Do not retry this action or use another tool to perform the same "
                "action. Continue without it if possible; otherwise explain that it was denied."
            ),
        )
        self.assertIn("Permission A denied", response)
        self.assertIn("not to retry", response)

    def test_permission_reply_includes_descendant_agent_sessions(self):
        request, _ = request_store.create("C" * 16, 123, 1055, state="running")
        request_store.update(request["reference"], session_id="active-session")
        child_permission = {
            "id": "per-child",
            "sessionID": "child-session",
            "permission": "read",
            "patterns": ["/workspace/README.md"],
        }
        with patch.object(opencode_api, "pending_permissions", return_value=[child_permission]):
            with patch.object(
                opencode_api,
                "session_details",
                return_value={"parentID": "active-session"},
            ):
                with patch.object(opencode_api, "respond_to_permission", return_value=True) as respond:
                    response = bridge.permission_reply_response(123, "/allow", "/allow")

        respond.assert_called_once_with(
            "child-session",
            "per-child",
            "once",
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        self.assertIn("allowed once", response)

    def test_cancel_does_not_claim_success_when_opencode_returns_false(self):
        request, _ = request_store.create("E" * 16, 123, 106, state="running")
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="msg_prompt",
        )
        with patch.object(opencode_api, "abort_session", return_value=False):
            with patch.object(
                opencode_api,
                "session_messages",
                return_value=[{"info": {"id": "msg_prompt", "role": "user"}}],
            ):
                response = bridge.cancel_request_response(123, request["reference"])

        self.assertIn("did not confirm cancellation", response)
        self.assertEqual(request_store.get(request["reference"])["state"], "cancellation_requested")

    def test_cancel_on_uncertain_submission_queues_reconciliation_without_replay(self):
        request, _ = request_store.create("A" * 16, 123, 1061, state="uncertain")
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="msg_prompt",
            acknowledgement_id=518,
        )
        with patch.object(bridge._REQUEST_SLOTS, "acquire", return_value=True):
            with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
                response = bridge.cancel_request_response(123, request["reference"])

        self.assertIn("reconciling", response)
        self.assertEqual(request_store.get(request["reference"])["state"], "recovering")
        self.assertEqual(request_store.get(request["reference"])["detail"], "user_cancelled_unconfirmed")
        submit.assert_called_once_with(
            bridge._request_worker_with_slot,
            "123",
            None,
            {},
            "1061",
            request["reference"],
            518,
            True,
        )

    def test_abort_callback_error_is_reported_without_crashing_the_bridge(self):
        request, _ = request_store.create("B" * 16, 123, 1062, state="uncertain")
        update = {
            "update_id": 1063,
            "callback_query": {
                "id": "callback-id",
                "data": f"abort:{request['reference']}",
                "message": {"chat": {"id": 123}, "message_id": 519},
            },
        }
        with patch.object(request_store, "get_for_chat", return_value=request):
            with patch.object(bridge, "_answer_callback_query"):
                with patch.object(bridge, "cancel_request_response", side_effect=request_store.InvalidTransition("bad state")):
                    with patch.object(bridge, "_append_request_status") as append_status:
                        bridge._handle_callback_update(update)

        append_status.assert_called_once()

    def test_explicit_prompt_rejection_is_not_marked_uncertain(self):
        request, _ = request_store.create("D" * 16, 123, 1064, state="queued")
        with patch.object(opencode_api, "session_tree_messages", return_value=[]):
            with patch.object(
                opencode_api,
                "submit_async",
                side_effect=http_api.OpenCodeHTTPError(400),
            ):
                with self.assertRaises(http_api.OpenCodeHTTPError):
                    bridge.answer_async(
                        123,
                        "private prompt",
                        {"123": "private-session"},
                        request["reference"],
                        lambda _phase: None,
                        lambda _text: None,
                    )

        self.assertEqual(request_store.get(request["reference"])["state"], "submitting")

    def test_ambiguous_prompt_submission_remains_uncertain_for_reconciliation(self):
        request, _ = request_store.create("E" * 16, 123, 1065, state="queued")
        with patch.object(opencode_api, "session_tree_messages", return_value=[]):
            with patch.object(
                opencode_api,
                "submit_async",
                side_effect=http_api.OpenCodeHTTPError(500),
            ) as submit:
                with patch.object(bridge.exchange, "resume_async_request", return_value="reconciled") as reconcile:
                    result = bridge.answer_async(
                        123,
                        "private prompt",
                        {"123": "private-session"},
                        request["reference"],
                        lambda _phase: None,
                        lambda _text: None,
                    )

        self.assertEqual(result, "reconciled")
        self.assertEqual(request_store.get(request["reference"])["state"], "uncertain")
        self.assertEqual(request_store.get(request["reference"])["detail"], "submission_ack_unconfirmed")
        self.assertEqual(
            reconcile.call_args.args[0]["prompt_message_id"],
            request_store.get(request["reference"])["prompt_message_id"],
        )
        submit.assert_called_once()

    def test_submission_ack_error_queues_reconciliation_monitor(self):
        request, _ = request_store.create("C" * 16, 123, 1066, state="queued")
        request_store.update(request["reference"], acknowledgement_id=519)

        def ambiguous_submission(*_args):
            request_store.update(
                request["reference"],
                state="submitting",
                session_id="private-session-id",
                prompt_message_id="msg_prompt",
                started_at=bridge.time.time(),
            )
            request_store.update(
                request["reference"],
                state="uncertain",
                detail="submission_ack_unconfirmed",
            )
            raise RuntimeError("ambiguous acknowledgement")

        with patch.object(bridge, "answer_async", side_effect=ambiguous_submission):
            with patch.object(bridge._REQUEST_SLOTS, "acquire", return_value=True):
                with patch.object(bridge._REQUEST_SLOTS, "release") as release_slot:
                    with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
                        with patch.object(bridge, "_append_request_status", return_value=True):
                            with patch.object(bridge, "send"):
                                bridge._request_worker(
                                    123,
                                    "private prompt",
                                    {},
                                    "1066",
                                    request["reference"],
                                    519,
                                )

        self.assertEqual(request_store.get(request["reference"])["state"], "recovering")
        self.assertEqual(request_store.get(request["reference"])["detail"], "submission_ack_unconfirmed")
        submit.assert_called_once_with(
            bridge._request_worker_with_slot,
            "123",
            None,
            {},
            "1066",
            request["reference"],
            519,
            True,
        )
        release_slot.assert_not_called()

    def test_recovery_resumes_submitted_requests_without_resubmitting_prompts(self):
        request, _ = request_store.create("F" * 16, 123, 107, state="running")
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="msg_private",
            acknowledgement_id=507,
        )
        with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
            with patch.object(bridge._REQUEST_SLOTS, "acquire", return_value=True):
                bridge._recover_pending_requests({"123": "private-session-id"})

        self.assertEqual(request_store.get(request["reference"])["state"], "recovering")
        self.assertEqual(submit.call_args.args[0], bridge._request_worker_with_slot)
        self.assertEqual(
            submit.call_args.args[1:7],
            ("123", None, {"123": "private-session-id"}, "107", request["reference"], 507),
        )
        self.assertTrue(submit.call_args.args[7])

    def test_recovery_marks_queued_requests_interrupted_without_replaying_them(self):
        request, _ = request_store.create("1" * 16, 123, 108, state="queued")
        request_store.update(request["reference"], acknowledgement_id=508)
        with patch.object(bridge, "edit_reply") as edit:
            with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
                with patch.object(bridge._REQUEST_SLOTS, "acquire", return_value=True):
                    bridge._recover_pending_requests({})

        self.assertEqual(request_store.get(request["reference"])["state"], "interrupted")
        self.assertEqual(submit.call_args.args[1], bridge._recovery_notice_worker)
        edit.assert_not_called()

    def test_recovered_prompt_returns_completed_response_without_new_submission(self):
        request, _ = request_store.create("2" * 16, 123, 109, state="running")
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="msg_prompt",
            acknowledgement_id=509,
            started_at=bridge.time.time(),
        )
        assistant = {
            "info": {
                "role": "assistant",
                "id": "assistant-id",
                "parentID": "msg_prompt",
                "time": {"completed": 1},
                "cost": 0.1,
            },
            "parts": [{"type": "text", "text": "Recovered result"}],
        }
        prompt = {"info": {"id": "msg_prompt", "role": "user"}, "parts": []}
        with patch.object(opencode_api, "session_status", return_value={}):
            with patch.object(opencode_api, "session_messages", return_value=[prompt, assistant]):
                with patch.object(
                    opencode_api,
                    "session_tree_messages",
                    return_value=[
                        {"session_id": "active-session", "message": prompt},
                        {"session_id": "active-session", "message": assistant},
                    ],
                ):
                    with patch.object(opencode_api, "submit_async") as submit:
                        with patch.object(bridge, "_watch_events"):
                            with patch.object(bridge.bridge_runtime, "wait"):
                                with patch.object(bridge, "edit_reply") as edit:
                                    with patch.object(bridge, "send") as send:
                                        bridge._request_worker(
                                            123,
                                            None,
                                            {},
                                            "109",
                                            request["reference"],
                                            509,
                                            True,
                                        )

        self.assertIn("Status: completed", edit.call_args.args[2])
        self.assertIn("Final cost: unavailable", edit.call_args.args[2])
        self.assertEqual(
            send.call_args.args[1],
            "Recovered result\n\nCost: unavailable\n\nTools used: none\nTool calls: 0",
        )
        self.assertEqual(request_store.get(request["reference"])["state"], "completed")
        submit.assert_not_called()

    def test_final_cost_reply_keeps_reference_after_acknowledgement_edit(self):
        request, _ = request_store.create("5" * 16, 123, 112, state="queued")
        request_store.update(request["reference"], acknowledgement_id=510)
        with patch.object(bridge, "answer_async", return_value="Done\n\nCost: $0.250000"):
            with patch.object(bridge, "edit_reply") as edit:
                with patch.object(bridge, "send") as send:
                    bridge._request_worker(
                        123,
                        "Calculate this",
                        {},
                        "112",
                        request["reference"],
                        510,
                    )

        self.assertIn("Final cost: $0.250000", edit.call_args.args[2])
        self.assertIn(request["reference"], edit.call_args.args[2])
        self.assertIn("Tools used: none; 0 calls", edit.call_args.args[2])
        self.assertEqual(edit.call_args.kwargs["reply_markup"], {"inline_keyboard": []})
        self.assertEqual(send.call_args.args[1], "Done\n\nCost: $0.250000")

    def test_recovery_does_not_replay_an_unconfirmed_submission(self):
        request, _ = request_store.create("4" * 16, 123, 111, state="running")
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="msg_prompt",
            started_at=bridge.time.time(),
        )
        with patch.object(opencode_api, "session_status", return_value={}):
            with patch.object(opencode_api, "session_messages", return_value=[]):
                with patch.object(opencode_api, "submit_async") as submit:
                    with self.assertRaises(bridge.OpenCodeSubmissionUnconfirmed):
                        bridge.resume_async_request(
                            request_store.get(request["reference"]),
                            lambda _phase: None,
                            lambda _text: None,
                        )

        self.assertEqual(request_store.get(request["reference"])["state"], "running")
        submit.assert_not_called()

    def test_recovered_unconfirmed_submission_finishes_cleanly(self):
        request, _ = request_store.create("F" * 16, 123, 1111, state="recovering")
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="msg_prompt",
            acknowledgement_id=520,
        )
        with patch.object(opencode_api, "session_status", return_value={}):
            with patch.object(opencode_api, "session_messages", return_value=[]):
                with patch.object(opencode_api, "submit_async") as submit:
                    with patch.object(bridge, "edit_reply") as edit:
                        with patch.object(bridge, "send") as send:
                            bridge._request_worker(
                                123,
                                None,
                                {},
                                "1111",
                                request["reference"],
                                520,
                                True,
                            )

        self.assertEqual(request_store.get(request["reference"])["state"], "failed")
        self.assertIn("did not confirm request", send.call_args.args[1])
        self.assertIn("Status: failed", edit.call_args.args[2])
        submit.assert_not_called()

    def test_monitor_confirms_user_cancellation_after_open_code_becomes_idle(self):
        request, _ = request_store.create("3" * 16, 123, 110, state="cancellation_requested")
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="msg_prompt",
            detail="user_cancelled",
        )
        with patch.object(bridge, "_watch_events"):
            with patch.object(opencode_api, "abort_session", return_value=True) as abort:
                with patch.object(
                    opencode_api,
                    "session_status",
                    side_effect=[{"private-session-id": {"type": "busy"}}, {}, {}],
                ):
                    with patch.object(
                        opencode_api,
                        "session_messages",
                        return_value=[{"info": {"id": "msg_prompt", "role": "user"}}],
                    ):
                        with patch.object(bridge.bridge_runtime, "wait"):
                            with self.assertRaises(bridge.OpenCodeRequestStopped) as stopped:
                                bridge._monitor_async_request(
                                    "private-session-id",
                                    "msg_prompt",
                                    request["reference"],
                                    lambda _phase: None,
                                    lambda _text: None,
                                    bridge.time.time(),
                                )

        self.assertEqual(stopped.exception.state, "user_cancelled")
        abort.assert_called_once()

    def test_monitor_reports_timeout_when_open_code_records_abort_as_assistant_error(self):
        request, _ = request_store.create("2" * 16, 123, 109, state="running")
        started_at = bridge.time.time()
        request_store.update(
            request["reference"],
            session_id="private-session-id",
            prompt_message_id="msg_prompt",
            started_at=started_at,
        )
        response = [
            {"info": {"id": "msg_prompt", "role": "user"}},
            {
                "info": {
                    "id": "assistant-aborted",
                    "parentID": "msg_prompt",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "error": {"name": "Aborted"},
                },
                "parts": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "gh pr view 31"},
                        },
                    }
                ],
            },
        ]

        with patch.object(bridge, "OPENCODE_REQUEST_MAX_DURATION_SECONDS", 0):
            with patch.object(bridge, "_watch_events"):
                with patch.object(opencode_api, "abort_session", return_value=True) as abort:
                    with patch.object(
                        opencode_api,
                        "session_status",
                        side_effect=[{"private-session-id": {"type": "busy"}}, {}, {}],
                    ):
                        with patch.object(opencode_api, "session_messages", return_value=response):
                            with patch.object(bridge.bridge_runtime, "wait"):
                                with self.assertRaises(bridge.OpenCodeRequestStopped) as stopped:
                                    bridge._monitor_async_request(
                                        "private-session-id",
                                        "msg_prompt",
                                        request["reference"],
                                        lambda _phase: None,
                                        lambda _text: None,
                                        started_at,
                                    )

        self.assertEqual(stopped.exception.state, "timed_out")
        self.assertEqual(
            request_store.get(request["reference"])["detail"],
            "timed_out",
        )
        abort.assert_called_once()

        with patch.object(bridge, "answer_async", side_effect=stopped.exception):
            with patch.object(bridge, "edit_reply") as edit:
                with patch.object(bridge, "send") as send:
                    bridge._request_worker(
                        123,
                        "Prepare a pull request",
                        {},
                        109,
                        request["reference"],
                        500,
                    )

        self.assertEqual(request_store.get(request["reference"])["state"], "timed_out")
        self.assertIn("timed out and stopped", bridge.request_status_response(123, request["reference"]))
        self.assertIn("Status: timed out and stopped", edit.call_args.args[2])
        self.assertIn("Tools used: bash, gh; 1 calls", edit.call_args.args[2])
        self.assertIn("reached the configured 5-day limit", send.call_args.args[1])
        self.assertIn("Tools used: bash, gh", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
