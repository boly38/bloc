"""OpenCode v2 HTTP API helpers and message normalization."""

import urllib.parse

from ..bridge_config import OC, TELEGRAM_INSTRUCTIONS
from .. import http_api
from ..http_api import http


_PAGE_SIZE = 200


class OpenCodeResponseError(RuntimeError):
    """OpenCode returned an assistant error; keep user/provider text out of logs."""

    def __init__(self, error_name, error_code=None, status_code=None):
        self.error_name = error_name if isinstance(error_name, str) else "unknown"
        self.error_code = error_code if isinstance(error_code, (str, int)) else None
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        super().__init__("OpenCode returned an error response")


def _data(response, expected_type, description):
    if not isinstance(response, dict):
        raise RuntimeError(f"OpenCode returned an invalid {description}")
    value = response.get("data")
    if not isinstance(value, expected_type):
        raise RuntimeError(f"OpenCode returned an invalid {description}")
    return value


def _session_url(session_id, suffix=""):
    encoded_id = urllib.parse.quote(session_id, safe="")
    return f"{OC}/api/session/{encoded_id}{suffix}"


def create_session(title):
    response = http(f"{OC}/api/session", {"title": title})
    session = _data(response, dict, "session")
    session_id = session.get("id")
    if not isinstance(session_id, str):
        raise RuntimeError("OpenCode returned an invalid session")
    encoded_id = urllib.parse.quote(session_id, safe="")
    http(
        f"{OC}/api/experimental/session/{encoded_id}/instructions/entries/bloc-telegram",
        {"value": TELEGRAM_INSTRUCTIONS},
        method="PUT",
    )
    return session


def raise_response_error(response):
    """Raise a sanitized assistant error from the normalized message representation."""
    info = response.get("info", {}) if isinstance(response, dict) else {}
    error = info.get("error") if isinstance(info, dict) else None
    if error is None:
        return
    if not isinstance(error, dict):
        raise OpenCodeResponseError("unknown")
    details = error.get("data", {})
    if not isinstance(details, dict):
        details = {}
    raise OpenCodeResponseError(
        error.get("name") or error.get("_tag") or error.get("type"),
        details.get("code"),
        details.get("statusCode", error.get("status")),
    )


def text_from_response(response):
    raise_response_error(response)
    parts = response.get("parts", []) if isinstance(response, dict) else []
    text = "\n".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
    ).strip() if isinstance(parts, list) else ""
    if text:
        return text
    raise RuntimeError("OpenCode returned no text")


def session_details(session_id, timeout=None):
    """Return one session's metadata without requesting a model response."""
    return _data(http(_session_url(session_id), timeout=timeout), dict, "session")


def _append_query(url, query):
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urllib.parse.urlencode(query)}"


def _page(url, cursor=None, order=None, timeout=None):
    query = {"limit": str(_PAGE_SIZE)}
    if cursor:
        query["cursor"] = cursor
    elif order:
        query["order"] = order
    response = http(_append_query(url, query), timeout=timeout)
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise RuntimeError("OpenCode returned an invalid paginated response")
    cursor_data = response.get("cursor")
    cursor_data = cursor_data if isinstance(cursor_data, dict) else {}
    return response["data"], cursor_data.get("next")


def _all_pages(url, order="asc", timeout=None):
    result = []
    cursor = None
    visited = set()
    while True:
        values, next_cursor = _page(url, cursor, order if cursor is None else None, timeout)
        result.extend(values)
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in visited:
            return result
        visited.add(next_cursor)
        cursor = next_cursor


def session_children(session_id, timeout=None):
    """Return a session's direct child sessions in chronological order."""
    query = urllib.parse.urlencode({"parentID": session_id})
    return _all_pages(f"{OC}/api/session?{query}", timeout=timeout)


def _normalize_message(message):
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        return None
    role = {"user": "user", "assistant": "assistant"}.get(message["type"])
    info = {
        "id": message.get("id"),
        "role": role,
        "time": message.get("time", {}),
        "cost": message.get("cost"),
        "tokens": message.get("tokens"),
        "agent": message.get("agent"),
        "finish": message.get("finish"),
        "error": message.get("error"),
    }
    parts = []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in ("text", "reasoning"):
                parts.append({"type": part_type, "text": part.get("text", "")})
            elif part_type == "tool":
                state = part.get("state")
                parts.append({
                    "type": "tool",
                    "tool": part.get("name"),
                    "state": state if isinstance(state, dict) else {},
                })
    if message["type"] == "user":
        parts = [{"type": "text", "text": message["text"]}] if isinstance(message.get("text"), str) else []
    return {"info": info, "parts": parts}


def _normalize_session_messages(messages):
    normalized = []
    parent_id = None
    for raw_message in messages:
        message = _normalize_message(raw_message)
        if message is None:
            continue
        info = message["info"]
        if info["role"] == "user":
            parent_id = info.get("id")
        elif info["role"] == "assistant":
            if parent_id:
                info["parentID"] = parent_id
            parent_id = info.get("id") or parent_id
        elif raw_message.get("type") == "idle":
            parent_id = None
        normalized.append(message)
    return normalized


def session_messages(session_id, timeout=None):
    encoded_id = urllib.parse.quote(session_id, safe="")
    messages = _all_pages(f"{OC}/api/session/{encoded_id}/message", timeout=timeout)
    return _normalize_session_messages(messages)


def session_tree_session_ids(session_id, timeout=None):
    """Return one session and all of its descendants without revisiting cycles."""
    result = []
    visited = set()

    def visit(current_id):
        if current_id in visited:
            return
        visited.add(current_id)
        result.append(current_id)
        for child in session_children(current_id, timeout=timeout):
            child_id = child.get("id") if isinstance(child, dict) else None
            if isinstance(child_id, str) and child_id:
                visit(child_id)

    visit(session_id)
    return result


def session_tree_messages(session_id, timeout=None):
    """Return messages from a session and all descendants, preserving session IDs internally."""
    return [
        {"session_id": current_id, "message": message}
        for current_id in session_tree_session_ids(session_id, timeout=timeout)
        for message in session_messages(current_id, timeout=timeout)
    ]


def submit_async(session_id, message_id, body, timeout):
    """Submit one durable prompt without waiting for the assistant response."""
    request_body = dict(body)
    request_body["id"] = message_id
    response = http(_session_url(session_id, "/prompt"), request_body, timeout)
    submitted = _data(response, dict, "prompt submission")
    if submitted.get("id") != message_id:
        raise RuntimeError("OpenCode returned an invalid prompt acknowledgement")
    return submitted


def session_status(timeout):
    """Return the server's active session map in the v2 response envelope."""
    return _data(http(f"{OC}/api/session/active", timeout=timeout), dict, "active session map")


def pending_permissions(session_id=None, timeout=None):
    """Return pending permissions for a session and its descendants."""
    if not isinstance(session_id, str) or not session_id:
        return []
    permissions = []
    for current_id in session_tree_session_ids(session_id, timeout=timeout):
        response = http(_session_url(current_id, "/permission"), timeout=timeout)
        requests = _data(response, list, "permission request list")
        for request in requests:
            if not isinstance(request, dict):
                continue
            permissions.append({
                "id": request.get("id"),
                "sessionID": request.get("sessionID", current_id),
                "permission": request.get("action"),
                "patterns": request.get("resources", []),
                "always": request.get("save", []),
            })
    return permissions


def respond_to_permission(session_id, permission_id, response, timeout=None, message=None):
    """Answer one pending permission request without exposing its IDs to Telegram."""
    if response not in ("once", "always", "reject"):
        raise ValueError("Unsupported permission response")
    encoded_session_id = urllib.parse.quote(session_id, safe="")
    encoded_permission_id = urllib.parse.quote(permission_id, safe="")
    body = {"decision": response}
    if isinstance(message, str) and message:
        body["message"] = message
    result = http(
        f"{OC}/api/session/{encoded_session_id}/permission/{encoded_permission_id}/reply",
        body,
        timeout=timeout,
    )
    return result is None or result is True


def abort_session(session_id, timeout=None):
    """Ask OpenCode to interrupt the current operation in a session."""
    result = http(_session_url(session_id, "/interrupt"), timeout=timeout, method="POST")
    return result is True or (isinstance(result, dict) and result.get("interrupted") is True)


def assistant_messages_for_prompt(messages, prompt_message_id):
    """Select the assistant chain following this user's message in its session timeline."""
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
