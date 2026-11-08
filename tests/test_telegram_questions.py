"""Offline tests for forwarding OpenCode interactive questions to Telegram."""

import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import call, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot", "telegram"))
with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ALLOWED_CHAT_IDS": "123"}):
    from bloc_bridge import bridge, http_api, request_store
    from bloc_bridge.opencode import api as opencode_api
    from bloc_bridge.telegram.request_presentation import RequestPresentation


class TelegramQuestionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_patch = patch.object(
            request_store,
            "REQUESTS_PATH",
            os.path.join(self.directory.name, "requests.sqlite3"),
        )
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        request_store.initialize()
        self.real_pending_questions = opencode_api.pending_questions
        self.permission_patch = patch.object(opencode_api, "pending_permissions", return_value=[])
        self.permission_patch.start()
        self.addCleanup(self.permission_patch.stop)
        self.pending_question_patch = patch.object(opencode_api, "pending_questions", return_value=[])
        self.pending_question_patch.start()
        self.addCleanup(self.pending_question_patch.stop)
        self.send_patch = patch.object(bridge, "send")
        self.send_mock = self.send_patch.start()
        self.addCleanup(self.send_patch.stop)
        self.references = []

    def tearDown(self):
        for reference in self.references:
            bridge._forget_request_presentation(reference)

    def make_request(self, update_id=300, state="running"):
        reference = f"{update_id:016X}"
        request, _ = request_store.create(reference, 123, update_id, state=state)
        request_store.update(
            reference,
            session_id="private-session-id",
            prompt_message_id="private-prompt-id",
            acknowledgement_id=500 + update_id,
        )
        self.references.append(reference)
        return request_store.get(reference)

    @staticmethod
    def question(request_id="private-question-id", session_id="private-session-id", **overrides):
        question = {
            "id": request_id,
            "sessionID": session_id,
            "questions": [
                {
                    "header": "Color",
                    "question": "Which color should be used?",
                    "options": [
                        {"label": "Blue", "description": "A cool color"},
                        {"label": "Green", "description": "A natural color"},
                    ],
                    "multiple": False,
                    "custom": True,
                }
            ],
        }
        question.update(overrides)
        return question

    def test_question_api_uses_open_code_list_reply_and_reject_endpoints(self):
        question = self.question()
        with patch.object(opencode_api, "http", return_value=[question]) as request:
            normalized = self.real_pending_questions("session-1", timeout=7)
        self.assertEqual(normalized, [{
            **question,
            "questions": [{
                **question["questions"][0],
                "options": [
                    {"label": "Blue", "value": "Blue", "description": "A cool color"},
                    {"label": "Green", "value": "Green", "description": "A natural color"},
                ],
            }],
            "form": False,
        }])
        request.assert_called_once_with(bridge.OC + "/api/question", timeout=7)

        with patch.object(opencode_api, "http", return_value=True) as request:
            self.assertTrue(
                opencode_api.respond_to_question(
                    "question with space",
                    answers=[["Blue"]],
                    timeout=7,
                )
            )
        request.assert_called_once_with(
            bridge.OC + "/api/question/question%20with%20space/reply",
            {"answers": [["Blue"]]},
            timeout=7,
            method="POST",
        )

        with patch.object(opencode_api, "http", return_value=True) as request:
            self.assertTrue(
                opencode_api.respond_to_question("question-id", reject=True, timeout=7)
            )
        request.assert_called_once_with(
            bridge.OC + "/api/question/question-id/reject",
            timeout=7,
            method="POST",
        )

    def test_unchanged_telegram_edit_is_ignored(self):
        with self.assertRaises(http_api.TelegramMessageNotModified):
            http_api._handle_telegram_error(
                http_api.TG + "editMessageText",
                400,
                '{"description":"Bad Request: message is not modified"}',
            )
        with patch.object(
            http_api,
            "telegram",
            side_effect=http_api.TelegramMessageNotModified(),
        ):
            bridge.edit_reply(123, 456, "Same request card")

    def test_callback_failure_is_logged_without_stopping_the_update_handler(self):
        request = self.make_request(update_id=323)
        update = {
            "update_id": 324,
            "callback_query": {
                "id": "status-callback",
                "data": f"status:{request['reference']}",
                "message": {"message_id": request["acknowledgement_id"], "chat": {"id": 123}},
            },
        }
        output = StringIO()
        with patch.object(bridge, "_show_request_status", side_effect=RuntimeError("private detail")):
            with patch.object(bridge, "_answer_callback_query"):
                with redirect_stderr(output):
                    bridge.handle_update(update, {})

        self.assertIn("telegram_callback_failed", output.getvalue())
        self.assertNotIn("private detail", output.getvalue())

    def test_v2_question_forms_are_discovered_and_answered_with_field_values(self):
        raw_form = {
            "id": "frm_question",
            "sessionID": "session-1",
            "title": "Questions",
            "metadata": {"kind": "question"},
            "fields": [
                {
                    "key": "q0",
                    "type": "multiselect",
                    "title": "Platforms",
                    "description": "Which platforms?",
                    "options": [
                        {"value": "web", "label": "Web"},
                        {"value": "mobile", "label": "Mobile"},
                    ],
                    "custom": True,
                },
                {
                    "key": "q1",
                    "type": "number",
                    "title": "Portion",
                    "description": "How many servings?",
                    "minimum": 1,
                },
            ],
        }
        unrelated_form = {
            **raw_form,
            "id": "frm_other_kind",
            "metadata": {"kind": "authentication"},
        }
        not_found = http_api.OpenCodeHTTPError(404)
        with patch.object(opencode_api, "session_tree_session_ids", return_value=["session-1"]):
            with patch.object(
                opencode_api,
                "http",
                side_effect=[not_found, {"data": [raw_form, unrelated_form]}],
            ) as request:
                questions = self.real_pending_questions("session-1", timeout=7)

        self.assertEqual(
            questions,
            [{
                "id": "frm_question",
                "sessionID": "session-1",
                "questions": [{
                    "key": "q0",
                    "header": "Platforms",
                    "question": "Which platforms?",
                    "options": [
                        {"label": "Web", "value": "web"},
                        {"label": "Mobile", "value": "mobile"},
                    ],
                    "multiple": True,
                    "custom": True,
                }, {
                    "key": "q1",
                    "header": "Portion",
                    "question": "How many servings?",
                    "options": [],
                    "multiple": False,
                    "custom": False,
                    "unsupported": True,
                }],
                "form": True,
            }],
        )
        normalized_questions = questions[0]["questions"]
        self.assertEqual(normalized_questions[1]["unsupported"], True)
        presentation = RequestPresentation(123, 456, "A" * 16)
        presentation.sync_questions(questions)
        presentation.set_question_answer("frm_question", 0, ["web"])
        text, keyboard = presentation.edit_payload()
        self.assertIn("Question 2 · Portion", text)
        self.assertIn("This question type is not supported in Telegram", text)
        self.assertFalse(any("Option" in button["text"] for row in keyboard["inline_keyboard"] for button in row))
        self.assertEqual(
            request.call_args_list[1],
            call(bridge.OC + "/api/session/session-1/form", timeout=7),
        )

        with patch.object(opencode_api, "http", return_value=None) as request:
            self.assertTrue(
                opencode_api.respond_to_question_form(
                    "session with space",
                    "form with space",
                    answer={"q0": ["web", "a custom choice"]},
                    timeout=7,
                )
            )
        request.assert_called_once_with(
            bridge.OC + "/api/session/session%20with%20space/form/form%20with%20space/reply",
            {"answer": {"q0": ["web", "a custom choice"]}},
            timeout=7,
            method="POST",
        )

        with patch.object(opencode_api, "http", return_value=None) as request:
            self.assertTrue(
                opencode_api.respond_to_question_form(
                    "session-1",
                    "frm_question",
                    reject=True,
                    timeout=7,
                )
            )
        request.assert_called_once_with(
            bridge.OC + "/api/session/session-1/form/frm_question",
            timeout=7,
            method="DELETE",
        )

    def test_request_card_shows_question_options_without_internal_ids(self):
        request = self.make_request()
        presentation = bridge._request_presentation_for_request(request)
        presentation.sync_questions([self.question()])

        text, keyboard = presentation.edit_payload()

        self.assertIn("Which color should be used?", text)
        self.assertIn("Question 1 · Color", text)
        self.assertIn("Option 1: [ ] Blue — A cool color", text)
        self.assertIn("/answer 1", text)
        self.assertIn("/reject 1", text)
        self.assertNotIn("private-question-id", text)
        self.assertNotIn("private-session-id", text)
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertTrue(any(value.startswith(f"q:{request['reference']}:A:0:0:") for value in callbacks))
        self.assertTrue(any(value.startswith(f"qr:{request['reference']}:A:") for value in callbacks))
        self.assertFalse(any(value.startswith("qc:") for value in callbacks))
        self.assertEqual(
            labels,
            [
                "Status",
                "Abort",
                "Question 1 · Option 1 Blue",
                "Question 1 · Option 2 Green",
                "Reject request (question 1)",
            ],
        )

    def test_single_choice_callback_answers_only_after_rechecking_the_active_question(self):
        request = self.make_request(update_id=301)
        question = self.question()
        presentation = bridge._request_presentation_for_request(request)
        presentation.sync_questions([question])
        _, keyboard = presentation.edit_payload()
        callback_data = next(
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
            if button["callback_data"].startswith(f"q:{request['reference']}:A:0:0:")
        )
        update = {
            "update_id": 302,
            "callback_query": {
                "id": "question-choice",
                "data": callback_data,
                "message": {"message_id": request["acknowledgement_id"], "chat": {"id": 123}},
            },
        }
        with patch.object(opencode_api, "pending_questions", return_value=[question]):
            with patch.object(opencode_api, "respond_to_question", return_value=True) as respond:
                with patch.object(bridge, "edit_reply"):
                    with patch.object(http_api, "telegram"):
                        bridge.handle_update(update, {})

        respond.assert_called_once_with(
            "private-question-id",
            answers=[["Blue"]],
            reject=False,
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        self.assertEqual(request_store.get(request["reference"])["state"], "running")
        self.send_mock.assert_called_once()
        self.assertIn("Question 1 · Color", self.send_mock.call_args.args[1])
        self.assertIn("Answer: Blue", self.send_mock.call_args.args[1])

    def test_multiple_choice_waits_for_done_and_custom_text_is_bounded(self):
        request = self.make_request(update_id=303)
        question = self.question(
            request_id="question-multiple",
            questions=[
                {
                    "header": "Tools",
                    "question": "Which tools?",
                    "options": [
                        {"label": "Search", "description": "Find information"},
                        {"label": "Read", "description": "Read a file"},
                    ],
                    "multiple": True,
                    "custom": False,
                }
            ],
        )
        presentation = bridge._request_presentation_for_request(request)
        presentation.sync_questions([question])
        with patch.object(opencode_api, "pending_questions", return_value=[question]):
            with patch.object(opencode_api, "respond_to_question", return_value=True) as respond:
                with patch.object(bridge, "edit_reply"):
                    result = bridge._update_question_selection(123, request["reference"], "A", 0, 0)
                    self.assertIn("Tap Done", result)
                    respond.assert_not_called()
                    bridge._update_question_selection(123, request["reference"], "A", 0, 1)
                    bridge._update_question_selection(
                        123, request["reference"], "A", 0, complete=True
                    )

        respond.assert_called_once_with(
            "question-multiple",
            answers=[["Search", "Read"]],
            reject=False,
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        self.assertEqual(request_store.get(request["reference"])["state"], "running")

        free_question = self.question(request_id="question-free")
        presentation.sync_questions([free_question])
        with patch.object(opencode_api, "pending_questions", return_value=[free_question]):
            with patch.object(opencode_api, "respond_to_question", return_value=True) as respond:
                with patch.object(bridge, "edit_reply"):
                    bridge.handle_update(
                        {
                            "update_id": 304,
                            "message": {"chat": {"id": 123}, "text": "/answer 2 a custom answer"},
                        },
                        {},
                    )
        respond.assert_called_once_with(
            "question-free",
            answers=[["a custom answer"]],
            reject=False,
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )

    def test_v2_form_answer_uses_the_active_session_and_field_keys(self):
        request = self.make_request(update_id=313)
        form = self.question(
            request_id="frm_multi-platform",
            questions=[{
                "key": "q0",
                "header": "Platforms",
                "question": "Which platforms?",
                "options": [
                    {"label": "Web", "value": "web"},
                    {"label": "Mobile", "value": "mobile"},
                ],
                "multiple": True,
                "custom": True,
            }],
            form=True,
        )
        presentation = bridge._request_presentation_for_request(request)
        presentation.sync_questions([form])
        with patch.object(opencode_api, "pending_questions", return_value=[form]):
            with patch.object(opencode_api, "respond_to_question_form", return_value=True) as respond:
                with patch.object(bridge, "edit_reply"):
                    bridge._update_question_selection(123, request["reference"], "A", 0, 0)
                    bridge._update_question_selection(123, request["reference"], "A", 0, 1)
                    bridge._update_question_selection(
                        123, request["reference"], "A", 0, complete=True
                    )

        respond.assert_called_once_with(
            "private-session-id",
            "frm_multi-platform",
            answer={"q0": ["web", "mobile"]},
            reject=False,
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )

    def test_custom_answer_for_a_later_question_is_not_confused_with_question_one(self):
        request = self.make_request(update_id=314)
        question = self.question(
            request_id="question-custom-later",
            questions=[
                {
                    "header": "First",
                    "question": "First choice?",
                    "options": [{"label": "One", "description": "First option"}],
                    "custom": True,
                },
                {
                    "header": "Second",
                    "question": "Second choice?",
                    "options": [{"label": "Two", "description": "Second option"}],
                    "custom": True,
                },
            ],
        )
        presentation = bridge._request_presentation_for_request(request)
        presentation.sync_questions([question])
        with patch.object(opencode_api, "pending_questions", return_value=[question]):
            with patch.object(opencode_api, "respond_to_question", return_value=True) as respond:
                with patch.object(bridge, "edit_reply") as edit:
                    with patch.object(bridge, "answer_async") as answer:
                        bridge.handle_update(
                            {
                                "update_id": 315,
                                "message": {"chat": {"id": 123}, "text": "/answer 1 first answer"},
                            },
                            {},
                        )
                        text, _ = presentation.edit_payload()
                        bridge.handle_update(
                            {
                                "update_id": 316,
                                "message": {"chat": {"id": 123}, "text": "/answer 2 le jambon"},
                            },
                            {},
                        )

        self.assertIn("Question 2 · Second", text)
        respond.assert_called_once_with(
            "question-custom-later",
            answers=[["first answer"], ["le jambon"]],
            reject=False,
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )
        answer.assert_not_called()
        self.assertEqual(edit.call_count, 3)
        self.assertEqual(self.send_mock.call_count, 2)
        self.assertIn("Question 1 · First", self.send_mock.call_args_list[0].args[1])
        self.assertIn("Question 2 · Second", self.send_mock.call_args.args[1])
        self.assertIn("Answer: le jambon", self.send_mock.call_args.args[1])

    def test_custom_answer_is_documented_without_an_inline_other_button(self):
        request = self.make_request(update_id=316)
        form = self.question(
            request_id="frm-custom-choice",
            questions=[{
                "key": "q0",
                "header": "Filling",
                "question": "What should go in the sandwich?",
                "options": [
                    {"label": "Egg", "value": "egg"},
                    {"label": "Cheese", "value": "cheese"},
                    {"label": "Tomato", "value": "tomato"},
                ],
                "multiple": False,
                "custom": True,
            }],
            form=True,
        )
        presentation = bridge._request_presentation_for_request(request)
        presentation.sync_questions([form])
        text, keyboard = presentation.edit_payload()
        self.assertIn("Your own answer", text)
        self.assertIn("/answer 1", text)
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(
            labels,
            [
                "Status",
                "Abort",
                "Question 1 · Option 1 Egg",
                "Question 1 · Option 2 Cheese",
                "Question 1 · Option 3 Tomato",
                "Reject request (question 1)",
            ],
        )

    def test_answer_command_uses_question_number_without_a_letter(self):
        request = self.make_request(update_id=319)
        form = self.question(
            request_id="frm-custom-number",
            questions=[{
                "key": "q0",
                "header": "Filling",
                "question": "What should go in the sandwich?",
                "options": [
                    {"label": "Egg", "value": "egg"},
                    {"label": "Cheese", "value": "cheese"},
                    {"label": "Tomato", "value": "tomato"},
                ],
                "multiple": False,
                "custom": True,
            }],
            form=True,
        )
        presentation = bridge._request_presentation_for_request(request)
        presentation.sync_questions([form])
        with patch.object(opencode_api, "pending_questions", return_value=[form]):
            with patch.object(opencode_api, "respond_to_question_form", return_value=True) as respond:
                with patch.object(bridge, "edit_reply"):
                    bridge.handle_update(
                        {
                            "update_id": 320,
                            "message": {
                                "chat": {"id": 123},
                                "text": "/answer 1 le jambon",
                            },
                        },
                        {},
                    )

        respond.assert_called_once_with(
            "private-session-id",
            "frm-custom-number",
            answer={"q0": "le jambon"},
            reject=False,
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )

    def test_reject_duplicate_updates_and_unowned_chats_do_not_submit_twice(self):
        request = self.make_request(update_id=305)
        question = self.question()
        presentation = bridge._request_presentation_for_request(request)
        presentation.sync_questions([question])
        update = {
            "update_id": 306,
            "message": {"chat": {"id": 123}, "text": "/reject 1"},
        }
        with patch.object(opencode_api, "pending_questions", return_value=[question]):
            with patch.object(opencode_api, "respond_to_question", return_value=True) as respond:
                with patch.object(bridge, "edit_reply"):
                    bridge.handle_update(update, {})
                    bridge.handle_update(update, {})
        respond.assert_called_once_with(
            "private-question-id",
            answers=None,
            reject=True,
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )

        other_chat_update = {
            "update_id": 307,
            "message": {"chat": {"id": 456}, "text": "/reject 1"},
        }
        with patch.object(bridge, "ALLOWED", frozenset(("123", "456"))):
            with patch.object(opencode_api, "respond_to_question") as other_respond:
                with patch.object(bridge, "send"):
                    bridge.handle_update(other_chat_update, {})
        other_respond.assert_not_called()

    def test_free_form_answer_is_bounded_and_never_becomes_a_new_prompt(self):
        request = self.make_request(update_id=310)
        question = self.question(request_id="question-bounded")
        presentation = bridge._request_presentation_for_request(request)
        presentation.sync_questions([question])
        update = {
            "update_id": 311,
            "message": {"chat": {"id": 123}, "text": f"/answer 1 {'x' * 501}"},
        }
        with patch.object(opencode_api, "pending_questions", return_value=[question]):
            with patch.object(opencode_api, "respond_to_question") as respond:
                with patch.object(bridge, "answer_async") as answer:
                    with patch.object(bridge, "send") as send:
                        bridge.handle_update(update, {})

        self.assertIn("between 1 and 500 characters", send.call_args.args[1])
        respond.assert_not_called()
        answer.assert_not_called()

    def test_question_from_an_unrelated_session_is_not_answered(self):
        request = self.make_request(update_id=308)
        unrelated = self.question(session_id="unrelated-session")
        presentation = bridge._request_presentation_for_request(request)
        presentation.sync_questions([self.question()])
        with patch.object(opencode_api, "pending_questions", return_value=[unrelated]):
            with patch.object(opencode_api, "session_details", return_value={}):
                with patch.object(opencode_api, "respond_to_question") as respond:
                    result = bridge._respond_to_question(123, request["reference"], "A", reject=True)

        self.assertIn("No pending question", result)
        respond.assert_not_called()

    def test_question_owned_by_a_child_session_can_be_rejected(self):
        request = self.make_request(update_id=312)
        child_question = self.question(
            request_id="question-from-child",
            session_id="private-child-session",
        )
        with patch.object(opencode_api, "pending_questions", return_value=[child_question]):
            with patch.object(
                opencode_api,
                "session_details",
                return_value={"parentID": "private-session-id"},
            ):
                with patch.object(opencode_api, "respond_to_question", return_value=True) as respond:
                    with patch.object(bridge, "edit_reply"):
                        result = bridge.question_reply_response(123, "/reject 1", "/reject")

        self.assertIn("rejected", result)
        respond.assert_called_once_with(
            "question-from-child",
            answers=None,
            reject=True,
            timeout=bridge.OPENCODE_STATUS_TIMEOUT_SECONDS,
        )

    def test_rejecting_a_question_turns_opencode_aborted_into_a_normal_stop(self):
        request = self.make_request(update_id=321)
        form = self.question(
            request_id="frm-rejected-question",
            questions=[{
                "key": "q0",
                "header": "Destination",
                "question": "Where should I go?",
                "options": [{"label": "Japan", "value": "japan"}],
                "multiple": False,
                "custom": True,
            }],
            form=True,
        )
        presentation = bridge._request_presentation_for_request(request)
        presentation.sync_questions([form])
        with patch.object(opencode_api, "pending_questions", return_value=[form]):
            with patch.object(opencode_api, "respond_to_question_form", return_value=True):
                with patch.object(bridge, "edit_reply"):
                    response = bridge._respond_to_question(
                        123,
                        request["reference"],
                        "A",
                        reject=True,
                    )

        self.assertIn("rejected", response)
        self.assertEqual(request_store.get(request["reference"])["detail"], "question_rejected")
        monitor = bridge._AsyncRequestMonitor(
            "private-session-id", "private-prompt-id", request["reference"], bridge.time.time()
        )
        snapshot = bridge.exchange._MonitorSnapshot(False, True, [{}], [], [])
        with patch.object(
            opencode_api,
            "raise_response_error",
            side_effect=opencode_api.OpenCodeResponseError("Aborted"),
        ):
            with self.assertRaises(bridge.OpenCodeRequestStopped) as stopped:
                monitor._raise_correlated_response_error(snapshot)

        self.assertEqual(stopped.exception.state, "question_rejected")

    def test_question_rejection_worker_reports_a_normal_cancelled_request(self):
        request = self.make_request(update_id=322)
        with patch.object(
            bridge,
            "answer_async",
            side_effect=bridge.OpenCodeRequestStopped("question_rejected"),
        ):
            with patch.object(bridge, "edit_reply"):
                with patch.object(bridge, "send") as send:
                    bridge._request_worker(
                        123,
                        "private prompt",
                        {},
                        request["update_id"],
                        request["reference"],
                        request["acknowledgement_id"],
                    )

        self.assertIn("question was rejected", send.call_args.args[1].lower())
        self.assertEqual(request_store.get(request["reference"])["state"], "cancelled")

    def test_request_cannot_complete_while_open_code_still_reports_a_question(self):
        monitor = bridge._AsyncRequestMonitor(
            "private-session-id", "private-prompt-id", "A" * 16, bridge.time.time()
        )
        completed_response = {
            "info": {
                "id": "assistant-message",
                "parentID": "private-prompt-id",
                "role": "assistant",
                "finish": "stop",
            },
            "parts": [],
        }
        snapshot = bridge.exchange._MonitorSnapshot(
            False,
            True,
            [completed_response],
            [],
            [self.question()],
        )

        self.assertFalse(monitor._is_request_complete(snapshot))

    def test_recovery_reloads_pending_questions_without_resubmitting_the_prompt(self):
        request = self.make_request(update_id=309, state="running")
        question = self.question()
        bridge._forget_request_presentation(request["reference"])
        with patch.object(bridge._REQUEST_SLOTS, "acquire", return_value=True):
            with patch.object(bridge._REQUEST_EXECUTOR, "submit") as submit:
                bridge._recover_pending_requests({"123": "private-session-id"})
        submit.assert_called_once_with(
            bridge._request_worker_with_slot,
            "123",
            None,
            {"123": "private-session-id"},
            str(request["update_id"]),
            request["reference"],
            request["acknowledgement_id"],
            True,
        )

        monitor = bridge._AsyncRequestMonitor(
            "private-session-id", "private-prompt-id", request["reference"], bridge.time.time()
        )
        with patch.object(opencode_api, "session_status", return_value={"private-session-id": {"type": "busy"}}):
            with patch.object(
                opencode_api,
                "session_messages",
                return_value=[{"info": {"id": "private-prompt-id", "role": "user"}}],
            ):
                with patch.object(opencode_api, "pending_questions", return_value=[question]):
                    monitor._read_snapshot()

        presentation = bridge._request_presentation_for_request(request_store.get(request["reference"]))
        text, _ = presentation.edit_payload()
        self.assertIn("Which color should be used?", text)
        self.assertNotIn("private-session-id", text)
        self.assertEqual(request_store.get(request["reference"])["state"], "recovering")


if __name__ == "__main__":
    unittest.main()
