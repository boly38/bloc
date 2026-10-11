"""OpenCode message and session-history API helpers."""

import urllib.parse

from bridge_config import OC
from http_api import http


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
    message = details.get("message") or error.get("name") if isinstance(error, dict) else None
    if message:
        raise RuntimeError(f"OpenCode/Opper: {message}")
    raise RuntimeError("OpenCode returned no text")


def session_messages(session_id):
    encoded_id = urllib.parse.quote(session_id)
    messages = http(f"{OC}/session/{encoded_id}/message")
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
