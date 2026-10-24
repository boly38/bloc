"""Sanitized diagnostics and cost/token aggregation for OpenCode messages."""

import json
import math
import re
import sys
import time
from datetime import datetime, timezone

from ..bridge_config import COST_DIAGNOSTICS_ENABLED, COST_DIAGNOSTICS_PATH
from .api import session_tree_messages


UTC = timezone.utc


def _diagnostic_metrics(value, path=(), depth=0):
    if depth > 8:
        return {}

    metrics = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isidentifier() or len(key) > 64:
                continue
            item_path = path + (key,)
            if isinstance(item, (dict, list)):
                metrics.update(_diagnostic_metrics(item, item_path, depth + 1))
                continue

            normalized_key = key.lower()
            metric_context = any(marker in segment.lower() for segment in item_path for marker in ("cost", "token", "usage"))
            if isinstance(item, (int, float)) and not isinstance(item, bool) and metric_context:
                if not isinstance(item, float) or math.isfinite(item):
                    metrics[".".join(item_path)] = item
            elif isinstance(item, str) and normalized_key in {
                "model",
                "modelid",
                "provider",
                "providerid",
                "currency",
                "currencycode",
                "agent",
            } and len(item) <= 100:
                metrics[".".join(item_path)] = item
    elif isinstance(value, list):
        for index, item in enumerate(value[:100]):
            metrics.update(_diagnostic_metrics(item, path + (str(index),), depth + 1))
    return metrics


def record_cost_diagnostics(response, exchange_message=None):
    if not COST_DIAGNOSTICS_ENABLED or not isinstance(response, dict):
        return

    info = response.get("info")
    info = info if isinstance(info, dict) else {}
    part_shapes = []
    part_metrics = []
    parts = response.get("parts")
    if isinstance(parts, list):
        for index, part in enumerate(parts[:20]):
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            safe_type = part_type[:40] if isinstance(part_type, str) else "unknown"
            keys = sorted(key for key in part if isinstance(key, str) and key.isidentifier() and len(key) <= 64)[:40]
            part_shapes.append({"type": safe_type, "keys": keys})
            metrics = _diagnostic_metrics(part)
            if metrics:
                part_metrics.append({"index": index, "type": safe_type, "metrics": metrics})

    diagnostic = {
        "timestamp": int(time.time()),
        "response_keys": _safe_keys(response, 40),
        "info_keys": _safe_keys(info, 80),
        "part_shapes": part_shapes,
        "part_metrics": part_metrics,
        "metrics": _diagnostic_metrics(info),
    }
    if exchange_message is not None:
        diagnostic["exchange_message"] = exchange_message
    try:
        with open(COST_DIAGNOSTICS_PATH, "a", encoding="utf-8") as file:
            file.write(json.dumps(diagnostic, sort_keys=True) + "\n")
    except OSError as error:
        print(f"Cost diagnostics write failed ({type(error).__name__}).", file=sys.stderr, flush=True)


def _safe_keys(value, limit):
    return sorted(key for key in value if isinstance(key, str) and key.isidentifier() and len(key) <= 64)[:limit]


def _entry_message(entry):
    if isinstance(entry, dict) and isinstance(entry.get("message"), dict):
        return entry["message"]
    return entry


def _assistant_info(message):
    if not isinstance(message, dict):
        return None
    info = message.get("info")
    if isinstance(info, dict) and info.get("role") == "assistant":
        return info
    return None


def _valid_cost(info):
    cost = info.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        return None
    return float(cost)


def _tokens(info):
    values = info.get("tokens")
    if not isinstance(values, dict):
        return {}
    result = {}
    for name in ("input", "output", "reasoning"):
        value = values.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[name] = value
    cache = values.get("cache")
    if isinstance(cache, dict):
        for name in ("read", "write"):
            value = cache.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[f"cache_{name}"] = value
    return result


def _message_time(info):
    value = info.get("time")
    if not isinstance(value, dict):
        return None
    created = value.get("created")
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created):
        return None
    # OpenCode's DateTimeUtcFromMillis fields are Unix milliseconds.
    return created / 1000 if created > 100_000_000_000 else float(created)


def usage_summary(messages, start_time=None, end_time=None):
    assistant_messages = []
    timestamps_complete = True
    for entry in messages:
        info = _assistant_info(_entry_message(entry))
        if info is None:
            continue
        created = _message_time(info)
        if start_time is not None or end_time is not None:
            if created is None:
                timestamps_complete = False
                continue
            if start_time is not None and created < start_time:
                continue
            if end_time is not None and created >= end_time:
                continue
        assistant_messages.append((info, created))

    costs = [_valid_cost(info) for info, _ in assistant_messages]
    costs_complete = all(cost is not None for cost in costs)
    total = sum(cost for cost in costs if cost is not None) if costs_complete else None
    tokens = {}
    tokens_complete = all(_has_complete_tokens(info) for info, _ in assistant_messages)
    for info, _ in assistant_messages:
        for name, value in _tokens(info).items():
            tokens[name] = tokens.get(name, 0) + value

    agents = {}
    for (info, _), cost in zip(assistant_messages, costs):
        agent = info.get("agent")
        if cost is None or not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z0-9_. -]{1,40}", agent):
            continue
        agents[agent] = agents.get(agent, 0.0) + cost

    return {
        "cost": total,
        "costs_complete": costs_complete,
        "timestamps_complete": timestamps_complete,
        "tokens": tokens,
        "tokens_complete": tokens_complete,
        "agents": agents,
        "message_count": len(assistant_messages),
    }


def _has_complete_tokens(info):
    tokens = info.get("tokens")
    return (
        isinstance(tokens, dict)
        and isinstance(tokens.get("input"), int)
        and not isinstance(tokens.get("input"), bool)
        and tokens["input"] >= 0
        and isinstance(tokens.get("output"), int)
        and not isinstance(tokens.get("output"), bool)
        and tokens["output"] >= 0
    )


def format_cost(cost):
    return f"${cost:.6f}"


def format_observed_cost(messages):
    """Format the cost already reported for messages in an active exchange."""
    assistant_infos = [
        info
        for entry in messages
        if (info := _assistant_info(_entry_message(entry))) is not None
    ]
    costs = [_valid_cost(info) for info in assistant_infos]
    observed = [cost for cost in costs if cost is not None]
    if not observed:
        return None
    return f"Cost observed so far: {format_cost(sum(observed))} (partial)"


def format_exchange_footer(messages):
    usage = usage_summary(messages)
    if usage["message_count"] == 0 or usage["cost"] is None:
        return "\n\nCost: unavailable"
    details = usage["agents"]
    if len(details) > 1:
        breakdown = ", ".join(f"{agent}: {format_cost(cost)}" for agent, cost in sorted(details.items()))
        return f"\n\nCost: {format_cost(usage['cost'])} ({breakdown})"
    return f"\n\nCost: {format_cost(usage['cost'])}"


def cost_report(session_id, now=None):
    messages = session_tree_messages(session_id)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    hour_start = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0).timestamp()
    hour_end = hour_start + 3600
    session_usage = usage_summary(messages)
    hour_usage = usage_summary(messages, hour_start, hour_end)

    session_cost = format_cost(session_usage["cost"]) if session_usage["cost"] is not None else "unavailable"
    if not hour_usage["timestamps_complete"] or hour_usage["cost"] is None:
        hour_cost = "unavailable"
    else:
        hour_cost = format_cost(hour_usage["cost"])
    start_label = datetime.fromtimestamp(hour_start, UTC).strftime("%H:%M")
    end_label = datetime.fromtimestamp(hour_end, UTC).strftime("%H:%M")
    lines = [f"Session: {session_cost}", f"UTC hour ({start_label}–{end_label}): {hour_cost}"]

    if session_usage["cost"] is not None and session_usage["agents"]:
        agents = ", ".join(
            f"{agent}: {format_cost(cost)}" for agent, cost in sorted(session_usage["agents"].items())
        )
        lines.append(f"Agents (session): {agents}")
    if session_usage["tokens"] and session_usage["tokens_complete"]:
        token_parts = []
        for key, label in (
            ("input", "input"),
            ("cache_read", "cached"),
            ("cache_write", "cache write"),
            ("output", "output"),
            ("reasoning", "reasoning"),
        ):
            if key in session_usage["tokens"]:
                token_parts.append(f"{session_usage['tokens'][key]} {label}")
        if token_parts:
            lines.append("Tokens (session): " + ", ".join(token_parts))
    return "\n".join(lines)
