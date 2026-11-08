"""Telegram commands and OpenCode responses for interactive questions."""

import sys


class QuestionControl:
    """Validate, sequence, and submit answers for one active Telegram request."""

    def __init__(
        self,
        request_store,
        opencode_api,
        questions_in_session_tree,
        presentation_for_request,
        edit_active_presentation,
        send,
        status_timeout,
    ):
        self.request_store = request_store
        self.opencode_api = opencode_api
        self.questions_in_session_tree = questions_in_session_tree
        self.presentation_for_request = presentation_for_request
        self.edit_active_presentation = edit_active_presentation
        self.send = send
        self.status_timeout = status_timeout

    def _pending_for_session(self, session_id):
        requests = self.opencode_api.pending_questions(
            session_id,
            timeout=self.status_timeout(),
        )
        return self.questions_in_session_tree(session_id, requests, {session_id}, set())

    @staticmethod
    def _parse_command(prompt, command):
        words = prompt.split(maxsplit=2)
        if command == "/reject":
            if len(words) != 2 or not words[1].isdecimal():
                raise ValueError("Usage: /reject <question-number>")
            return int(words[1]), None
        if len(words) != 3 or not words[1].isdecimal():
            raise ValueError("Usage: /answer <question-number> <choice-number(s) or text>")
        question_number = int(words[1])
        if not 1 <= question_number <= 1000:
            raise ValueError("Question number must be between 1 and 1000.")
        answer = words[2].strip()
        if not answer or len(answer) > 500:
            raise ValueError("Answers must contain between 1 and 500 characters.")
        return question_number, answer

    def _send_answer_entry(
        self,
        chat_id,
        reference,
        presentation,
        question_request,
        answers=None,
        reject=False,
        waiting_for_more=False,
        question_indices=None,
    ):
        """Append question context and its answer to Telegram without logging content."""
        request_id = question_request.get("id")
        prompts = question_request.get("questions", [])
        if not isinstance(request_id, str) or not isinstance(prompts, list):
            return False
        if question_indices is None:
            question_indices = (
                list(range(len(prompts)))
                if reject
                else [
                    index
                    for index in range(len(prompts))
                    if not presentation.question_was_announced(request_id, index)
                ]
            )
        lines = [
            "OpenCode question rejected:" if reject else (
                "Answer recorded; OpenCode is waiting for the remaining answers:"
                if waiting_for_more
                else "Answer submitted to OpenCode:"
            )
        ]
        for index in question_indices:
            if not 0 <= index < len(prompts) or not isinstance(prompts[index], dict):
                continue
            prompt = prompts[index]
            question_number = presentation.question_number(request_id, index)
            header = prompt.get("header")
            question_text = prompt.get("question")
            lines.append(f"Question {question_number} · {header or 'Question'}")
            if isinstance(question_text, str) and question_text:
                lines.append(question_text)
            if reject:
                lines.append("Answer: rejected")
                continue
            values = answers[index] if isinstance(answers, list) and index < len(answers) else []
            options = prompt.get("options")
            options = options if isinstance(options, list) else []
            labels = []
            for value in values:
                label = next(
                    (
                        option.get("label")
                        for option in options
                        if isinstance(option, dict) and option.get("value", option.get("label")) == value
                    ),
                    value,
                )
                labels.append(str(label))
            lines.append(f"Answer: {', '.join(labels) if labels else 'No answer'}")
        if len(lines) == 1:
            return False
        if not reject:
            for index in question_indices:
                presentation.mark_question_announced(request_id, index)
        try:
            self.send(chat_id, "\n".join(lines))
        except Exception as error:
            print(
                f"question_answer_entry_failed ref={reference} error_type={type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return False
        return True

    def reply_from_command(self, chat_id, prompt, command):
        """Apply an explicit numeric answer or rejection to the current question."""
        question_number, answer = self._parse_command(prompt, command)
        active = self.request_store.active_for_chat(chat_id)
        if not active:
            return "No active request has a pending question."
        if len(active) != 1:
            return "Multiple active requests found; use the remote TUI to answer questions."
        reference = active[0]["reference"]
        request = self.request_store.get_for_chat(reference, chat_id)
        presentation = self.presentation_for_request(request) if request is not None else None
        session_id = request.get("session_id") if request is not None else None
        if not session_id:
            return "The active request has not reached OpenCode yet."
        try:
            pending = self._pending_for_session(session_id)
        except Exception as error:
            print(
                f"question_lookup_failed ref={reference} error_type={type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return "Could not verify the pending question. Use the remote TUI or /cancel."
        if presentation is not None:
            presentation.sync_questions(pending)
        selected = presentation.question_for_number(question_number) if presentation is not None else None
        if selected is None:
            current = presentation.current_question() if presentation is not None else None
            if current is not None:
                return f"Please answer question {current['number']} first."
            return f"Question {question_number} is no longer pending. Use /status to refresh the request."
        question_request, question_index = selected
        question_id = question_request.get("id")
        if not isinstance(question_id, str):
            return f"Question {question_number} is no longer pending. Use /status to refresh the request."
        request_letter = presentation.question_letter_for_id(question_id)
        if command == "/reject":
            return self.respond(
                chat_id,
                reference,
                request_letter,
                reject=True,
                expected_question_id=question_id,
            )

        prompts = question_request.get("questions", [])
        prompt_info = prompts[question_index]
        if not isinstance(prompt_info, dict):
            return "That question is invalid. Use the remote TUI to answer it."
        if prompt_info.get("unsupported") is True:
            return "This question type is not supported in Telegram. Use the remote TUI or reject it."
        options = prompt_info.get("options")
        options = options if isinstance(options, list) else []
        selections = [item.strip() for item in answer.split(",")]
        selected_values = []
        for selection in selections:
            if not selection.isdecimal():
                selected_values = []
                break
            option_index = int(selection) - 1
            if not 0 <= option_index < len(options):
                selected_values = []
                break
            option = options[option_index]
            label = option.get("label") if isinstance(option, dict) else None
            value = option.get("value", label) if isinstance(option, dict) else None
            if not isinstance(label, str) or not isinstance(value, str):
                selected_values = []
                break
            selected_values.append(value)
        if selected_values:
            if len(selected_values) > 1 and prompt_info.get("multiple") is not True:
                return "This question accepts one choice only."
            if not presentation.set_question_answer(question_id, question_index, selected_values):
                return "That answer could not be applied. Use the current request card."
        elif prompt_info.get("custom", True) is True:
            if not presentation.set_question_answer(question_id, question_index, [answer]):
                return "That answer could not be applied. Use the current request card."
        else:
            return "Choose a numbered option."

        answers = presentation.answers_for_question(question_id)
        if answers is None:
            partial_answers = [
                presentation.answer_for_question_item(question_id, index) or []
                for index in range(len(prompts))
            ]
            self._send_answer_entry(
                chat_id,
                reference,
                presentation,
                question_request,
                answers=partial_answers,
                waiting_for_more=True,
                question_indices=[question_index],
            )
            return f"Answer recorded for question {question_number}. Continue with the next question."
        return self.respond(
            chat_id,
            reference,
            request_letter,
            answers=answers,
            expected_question_id=question_id,
        )

    def _reopen_for_retry(self, presentation, question_id):
        if presentation is None or not isinstance(question_id, str):
            return
        presentation.reopen_last_question(question_id)
        self.edit_active_presentation(presentation)

    def respond(
        self,
        chat_id,
        reference,
        letter,
        answers=None,
        reject=False,
        expected_question_id=None,
        expected_question_token=None,
    ):
        """Revalidate question ownership before sending an answer to OpenCode."""
        request = self.request_store.get_for_chat(reference, chat_id)
        if request is None or request["state"] not in self.request_store.ACTIVE_STATES:
            return "No active request has a pending question."
        if request["state"] == "cancellation_requested":
            return "The active request is stopping; no question answer will be sent."
        session_id = request.get("session_id")
        if not session_id:
            return "The active request has not reached OpenCode yet."

        presentation = self.presentation_for_request(request)
        try:
            pending = self._pending_for_session(session_id)
        except Exception as error:
            if not reject:
                question = presentation.question_for_letter(letter) if presentation is not None else None
                self._reopen_for_retry(presentation, question.get("id") if question else expected_question_id)
            print(
                f"question_lookup_failed ref={reference} error_type={type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return "Could not verify the pending question. Use the remote TUI or /cancel."
        if presentation is not None:
            presentation.sync_questions(pending)
        if not pending:
            return "No pending question for the active request."
        question = presentation.question_for_letter(letter) if presentation is not None else None
        if question is None:
            return "The question is no longer pending. Use /status to refresh the request."
        question_id = question.get("id")
        if expected_question_token is not None and (
            not isinstance(question_id, str)
            or presentation is None
            or presentation.question_token(question_id) != expected_question_token
        ):
            return "The question changed. Use the current request card to answer."
        if expected_question_id is not None and question_id != expected_question_id:
            return "The question changed. Use the current request card to answer."
        selected = next((item for item in pending if item.get("id") == question_id), None)
        if selected is None or not isinstance(question_id, str):
            return "The question is no longer pending. Use /status to refresh the request."
        if not reject and (not isinstance(answers, list) or len(answers) != len(question.get("questions", []))):
            return "Answer every question in this request before submitting it."
        if presentation is not None and not presentation.claim_question_submission(question_id):
            return "That question answer is already being submitted."
        try:
            if question.get("form") is True:
                answer = {}
                if not reject:
                    for index, prompt in enumerate(question.get("questions", [])):
                        key = prompt.get("key") if isinstance(prompt, dict) else None
                        if not isinstance(key, str):
                            raise ValueError("Question form field is invalid")
                        values = answers[index]
                        answer[key] = values if prompt.get("multiple") is True else (values[0] if values else "")
                confirmed = self.opencode_api.respond_to_question_form(
                    selected["sessionID"],
                    question_id,
                    answer=answer,
                    reject=reject,
                    timeout=self.status_timeout(),
                )
            else:
                confirmed = self.opencode_api.respond_to_question(
                    question_id,
                    answers=answers,
                    reject=reject,
                    timeout=self.status_timeout(),
                )
        except Exception as error:
            if presentation is not None:
                presentation.release_question_submission(question_id)
                if not reject:
                    self._reopen_for_retry(presentation, question_id)
            print(
                f"question_reply_failed ref={reference} error_type={type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return "Could not send that question response. Use the remote TUI or /cancel."
        if not confirmed:
            if presentation is not None:
                presentation.release_question_submission(question_id)
                if not reject:
                    self._reopen_for_retry(presentation, question_id)
            return "OpenCode did not confirm the question response. Use the remote TUI or /cancel."
        if reject:
            self.request_store.update(reference, detail="question_rejected")
        if presentation is not None:
            self._send_answer_entry(
                chat_id,
                reference,
                presentation,
                question,
                answers=answers,
                reject=reject,
            )
            presentation.resolve_question(question_id)
            self.edit_active_presentation(presentation)
        return (
            "Question request rejected. OpenCode was notified; the request may stop."
            if reject
            else "Answers submitted to OpenCode. It is resuming."
        )

    def update_selection(
        self,
        chat_id,
        reference,
        letter,
        question_index,
        option_index=None,
        complete=False,
        expected_question_token=None,
    ):
        """Apply one inline selection to the current question in the active request."""
        request = self.request_store.get_for_chat(reference, chat_id)
        if request is None or request["state"] not in self.request_store.ACTIVE_STATES:
            return "No active request has a pending question."
        if request["state"] == "cancellation_requested":
            return "The active request is stopping; no question answer will be sent."
        session_id = request.get("session_id")
        if not session_id:
            return "The active request has not reached OpenCode yet."
        try:
            pending = self._pending_for_session(session_id)
        except Exception as error:
            print(
                f"question_lookup_failed ref={reference} error_type={type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return "Could not verify the pending question. Use the remote TUI or /cancel."
        presentation = self.presentation_for_request(request)
        if presentation is None or not pending:
            return "No pending question for the active request."
        presentation.sync_questions(pending)
        question = presentation.question_for_letter(letter)
        if question is None:
            return "The question is no longer pending. Use /status to refresh the request."
        question_id = question.get("id")
        if not isinstance(question_id, str) or not any(item.get("id") == question_id for item in pending):
            return "The question is no longer pending. Use /status to refresh the request."
        question_number = presentation.question_number(question_id, question_index)
        if question_number is None:
            return "The question number is unavailable. Use /status to refresh the request."
        if not presentation.is_current_question(question_id, question_index):
            current = presentation.current_question()
            if current is not None:
                return f"Please answer question {current['number']} first."
            return "The question is no longer waiting for an answer. Use /status to refresh the request."
        if (
            expected_question_token is not None
            and presentation.question_token(question_id) != expected_question_token
        ):
            return "The question changed. Use the current request card to answer."
        if complete:
            if not presentation.complete_question_selection(question_id, question_index):
                return "Choose at least one option before submitting this multiple-choice answer."
        elif not presentation.toggle_question_option(question_id, question_index, option_index):
            return "That option is no longer available. Use the current request card."
        self.edit_active_presentation(presentation)
        answers = presentation.answers_for_question(question_id)
        if answers is None:
            item_answer = presentation.answer_for_question_item(question_id, question_index)
            if item_answer is not None:
                partial_answers = [
                    presentation.answer_for_question_item(question_id, index) or []
                    for index in range(len(question.get("questions", [])))
                ]
                self._send_answer_entry(
                    chat_id,
                    reference,
                    presentation,
                    question,
                    answers=partial_answers,
                    waiting_for_more=True,
                    question_indices=[question_index],
                )
                return f"Answer recorded for question {question_number}. Continue with the next question."
            return "Selection updated. Tap Done to submit a multiple-choice answer."
        return self.respond(
            chat_id,
            reference,
            letter,
            answers=answers,
            expected_question_id=question_id,
            expected_question_token=expected_question_token,
        )
