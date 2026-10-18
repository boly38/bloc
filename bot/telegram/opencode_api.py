"""OpenCode message and session-history API helpers."""

import urllib.parse

from bridge_config import OC
import http_api
from http_api import http


class OpenCodeResponseError(RuntimeError):
    """OpenCode returned an assistant error; keep user/provider text out of logs."""

    def __init__(self, error_name, error_code=None):
        self.error_name = error_name if isinstance(error_name, str) else "unknown"
        self.error_code = error_code if isinstance(error_code, (str, int)) else None
        super().__init__("OpenCode returned an error response")


def text_from_response(response):
    text = "\n".join(
        part.get("text", "")
        for part in response.get("parts", [])
        if part.get("type") == "text" and part.get("text")
    ).strip()
    if text:
        return text

    error = response.get("info", {}).get("error", {})
    details = error.get("data", {}) if isinstance(error, dict) else {}
    detail_message = details.get("message") if isinstance(details, dict) else None
    message = detail_message or error.get("name") if isinstance(error, dict) else None
    if message:
        error_code = details.get("code") if isinstance(details, dict) else None
        raise OpenCodeResponseError(error.get("name"), error_code)
    raise RuntimeError("OpenCode returned no text")


def session_messages(session_id, timeout=None):
    encoded_id = urllib.parse.quote(session_id)
    messages = http(f"{OC}/session/{encoded_id}/message", timeout=timeout)
    if not isinstance(messages, list):
        raise RuntimeError("OpenCode returned an invalid message history")
    return messages


def session_tree_messages(session_id):
    """Return messages from a session and all descendants, preserving session IDs internally."""
    messages = []
    visited = set()

    def visit(current_session_id):
        if current_session_id in visited:
            return
        visited.add(current_session_id)
        messages.extend(
            {"session_id": current_session_id, "message": message}
            for message in session_messages(current_session_id)
        )

        encoded_id = urllib.parse.quote(current_session_id)
        children = http(f"{OC}/session/{encoded_id}/children")
        if not isinstance(children, list):
            raise RuntimeError("OpenCode returned an invalid child session list")
        for child in children:
            child_id = child.get("id") if isinstance(child, dict) else None
            if isinstance(child_id, str) and child_id:
                visit(child_id)

    visit(session_id)
    return messages


def submit_async(session_id, message_id, body, timeout):
    """Submit a prompt without waiting for the assistant response."""
    encoded_id = urllib.parse.quote(session_id)
    request_body = dict(body)
    request_body["messageID"] = message_id
    return http(f"{OC}/session/{encoded_id}/prompt_async", request_body, timeout)


def session_status(timeout):
    """Return the server's active session status map."""
    statuses = http(f"{OC}/session/status", timeout=timeout)
    if not isinstance(statuses, dict):
        raise RuntimeError("OpenCode returned an invalid session status map")
    return statuses


def assistant_messages_for_prompt(messages, prompt_message_id):
    """Select the assistant chain rooted at this prompt, preserving message order."""
    parent_ids = {prompt_message_id}
    selected = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("info"), dict):
            continue
        info = message["info"]
        if info.get("role") != "assistant" or info.get("parentID") not in parent_ids:
            continue
        selected.append(message)
        if info.get("id"):
            parent_ids.add(info["id"])
    return selected


def iter_events():
    return http_api.opencode_events()
