"""OpenCode message and session-history API helpers."""

import urllib.parse

from ..bridge_config import OC
from .. import http_api
from ..http_api import http


class OpenCodeResponseError(RuntimeError):
    """OpenCode returned an assistant error; keep user/provider text out of logs."""

    def __init__(self, error_name, error_code=None, status_code=None):
        self.error_name = error_name if isinstance(error_name, str) else "unknown"
        self.error_code = error_code if isinstance(error_code, (str, int)) else None
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        super().__init__("OpenCode returned an error response")


def raise_response_error(response):
    """Raise on an assistant error, retaining only structured diagnostic fields."""
    error = response.get("info", {}).get("error")
    if error is None:
        return
    if not isinstance(error, dict):
        raise OpenCodeResponseError("unknown")
    details = error.get("data", {})
    if not isinstance(details, dict):
        details = {}
    raise OpenCodeResponseError(error.get("name"), details.get("code"), details.get("statusCode"))


def text_from_response(response):
    raise_response_error(response)
    text = "\n".join(
        part.get("text", "")
        for part in response.get("parts", [])
        if part.get("type") == "text" and part.get("text")
    ).strip()
    if text:
        return text

    raise RuntimeError("OpenCode returned no text")


def session_messages(session_id, timeout=None):
    encoded_id = urllib.parse.quote(session_id)
    messages = http(f"{OC}/session/{encoded_id}/message", timeout=timeout)
    if not isinstance(messages, list):
        raise RuntimeError("OpenCode returned an invalid message history")
    return messages


def session_details(session_id, timeout=None):
    """Return one session's metadata without requesting a model response."""
    encoded_id = urllib.parse.quote(session_id)
    session = http(f"{OC}/session/{encoded_id}", timeout=timeout)
    if not isinstance(session, dict):
        raise RuntimeError("OpenCode returned an invalid session")
    return session


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


def pending_permissions(timeout=None):
    """Return OpenCode's pending permission requests for the current project."""
    permissions = http(f"{OC}/permission", timeout=timeout)
    if not isinstance(permissions, list):
        raise RuntimeError("OpenCode returned an invalid permission request list")
    return permissions


def respond_to_permission(session_id, permission_id, response, timeout=None):
    """Answer one pending permission request without exposing its IDs to Telegram."""
    if response not in ("once", "always", "reject"):
        raise ValueError("Unsupported permission response")
    encoded_session_id = urllib.parse.quote(session_id, safe="")
    encoded_permission_id = urllib.parse.quote(permission_id, safe="")
    result = http(
        f"{OC}/session/{encoded_session_id}/permissions/{encoded_permission_id}",
        {"response": response},
        timeout=timeout,
    )
    if not isinstance(result, bool):
        raise RuntimeError("OpenCode returned an invalid permission response")
    return result


def abort_session(session_id, timeout=None):
    """Ask OpenCode to stop the current operation in a session."""
    encoded_id = urllib.parse.quote(session_id)
    result = http(f"{OC}/session/{encoded_id}/abort", {}, timeout=timeout)
    if not isinstance(result, bool):
        raise RuntimeError("OpenCode returned an invalid abort result")
    return result


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
