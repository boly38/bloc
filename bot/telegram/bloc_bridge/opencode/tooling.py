"""Safe tool metadata shared by OpenCode exchange reporting and progress updates."""

import re


_TEST_COMMAND = re.compile(
    r"\b(?:pytest|tox|nox|unittest|cargo\s+test|go\s+test|"
    r"npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+(?:run\s+)?test|"
    r"bun\s+(?:run\s+)?test|mvn\s+test|gradle\s+test|make\s+test)\b",
    re.IGNORECASE,
)
_GH_CLI_COMMAND = re.compile(
    r"\bgh\s+(?:api|auth|issue|pr|repo|run|workflow|release|label|search|gist|secret|variable)\b",
    re.IGNORECASE,
)
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_PROGRESS_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,40}")
_TOOL_PROGRESS_LABELS = {
    "apply_patch": "File patch",
    "bash": "Command",
    "edit": "File edit",
    "gh": "GitHub CLI",
    "glob": "File search",
    "grep": "File search",
    "read": "File read",
    "task": "Subtask",
    "todowrite": "Task tracking",
    "webfetch": "Documentation fetch",
    "write": "File write",
}


def is_test_command(command):
    return isinstance(command, str) and bool(_TEST_COMMAND.search(command))


def is_gh_command(command):
    return isinstance(command, str) and bool(_GH_CLI_COMMAND.search(command))


def tool_names(messages):
    """Return unique tool and recognized CLI names, without inputs or outputs."""
    names = []
    seen = set()
    for entry in messages:
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            message = entry if isinstance(entry, dict) else {}
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            tool_name = part.get("tool")
            if isinstance(tool_name, str) and _TOOL_NAME.fullmatch(tool_name):
                normalized = tool_name.lower()
                if normalized not in seen:
                    names.append(normalized)
                    seen.add(normalized)
            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            tool_input = state.get("input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if tool_name == "bash" and is_gh_command(command) and "gh" not in seen:
                names.append("gh")
                seen.add("gh")
    return names


def tool_call_count(messages):
    count = 0
    for entry in messages:
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            message = entry if isinstance(entry, dict) else {}
        parts = message.get("parts")
        if isinstance(parts, list):
            count += sum(1 for part in parts if isinstance(part, dict) and part.get("type") == "tool")
    return count


def format_tool_usage(names):
    safe_names = [
        name.lower()
        for name in names
        if isinstance(name, str) and _TOOL_NAME.fullmatch(name)
    ]
    return f"Tools used: {', '.join(dict.fromkeys(safe_names)) if safe_names else 'none'}"


def event_tool_metadata(part):
    """Return safe progress metadata for an OpenCode tool part."""
    if not isinstance(part, dict) or part.get("type") != "tool":
        return None, None, False, False
    tool_name = part.get("tool")
    state = part.get("state")
    state = state if isinstance(state, dict) else {}
    tool_input = state.get("input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    is_bash = tool_name == "bash"
    return (
        tool_name,
        state.get("status"),
        is_bash and is_test_command(command),
        is_bash and is_gh_command(command),
    )


def progress_stage(part_type, tool_name=None, tool_status=None, is_test=False, is_gh=False):
    if part_type == "reasoning":
        return "Analysis in progress"
    if part_type != "tool":
        return None

    if is_test:
        label = "Tests"
    elif is_gh:
        label = "GitHub CLI"
    elif isinstance(tool_name, str) and _PROGRESS_TOOL_NAME.fullmatch(tool_name):
        label = _TOOL_PROGRESS_LABELS.get(tool_name.lower(), f"Tool {tool_name}")
    else:
        label = "Tool"
    suffix = {"completed": "complete", "error": "failed"}.get(tool_status, "in progress")
    return f"{label} {suffix}"
