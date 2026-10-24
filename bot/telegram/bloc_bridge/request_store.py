"""Durable metadata for Telegram requests, without storing prompt contents."""

import os
import sqlite3
import time
from contextlib import contextmanager

from .bridge_config import REQUESTS_PATH


ACTIVE_STATES = frozenset(
    {
        "acknowledging",
        "queued",
        "submitting",
        "running",
        "cancellation_requested",
        "recovering",
        "uncertain",
        "delivering",
    }
)

TERMINAL_STATES = frozenset(
    {
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "rejected",
        "interrupted",
        "acknowledgement_unknown",
        "delivery_unknown",
    }
)

ALLOWED_TRANSITIONS = {
    "acknowledging": frozenset(("acknowledging", "queued", "failed", "acknowledgement_unknown")),
    "queued": frozenset(("queued", "submitting", "delivering", "failed", "interrupted", "rejected")),
    "submitting": frozenset(("submitting", "running", "delivering", "uncertain", "failed")),
    "running": frozenset(("running", "cancellation_requested", "recovering", "delivering", "uncertain", "failed")),
    "cancellation_requested": frozenset(
        ("cancellation_requested", "running", "delivering", "cancelled", "timed_out", "uncertain")
    ),
    "recovering": frozenset(("recovering", "running", "uncertain", "failed")),
    "uncertain": frozenset(("uncertain", "recovering", "running", "failed", "cancelled", "timed_out")),
    "delivering": frozenset(
        ("delivering", "completed", "failed", "cancelled", "timed_out", "delivery_unknown")
    ),
}
for _terminal_state in TERMINAL_STATES:
    ALLOWED_TRANSITIONS[_terminal_state] = frozenset((_terminal_state,))


class InvalidTransition(RuntimeError):
    """Raised when a request attempts a state transition outside its lifecycle."""


def _connect():
    directory = os.path.dirname(REQUESTS_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(REQUESTS_PATH, timeout=10)
    # The database contains private chat and OpenCode correlation identifiers.
    os.chmod(REQUESTS_PATH, 0o600)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


@contextmanager
def _connection():
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    """Create the request table and its lookup indexes if they do not exist."""
    with _connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        # Store correlation and lifecycle metadata only; prompt and response text stay elsewhere.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                reference TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                update_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                acknowledgement_id INTEGER,
                session_id TEXT,
                prompt_message_id TEXT,
                detail TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                completed_at REAL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS requests_chat_state ON requests(chat_id, state, created_at)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS handled_updates (update_id TEXT PRIMARY KEY, created_at REAL NOT NULL)"
        )


def create(reference, chat_id, update_id, state="acknowledging", detail=None):
    """Create one request for a Telegram update; return its row and whether it was new."""
    if state not in ALLOWED_TRANSITIONS:
        raise InvalidTransition(f"unknown initial state {state}")
    now = time.time()
    with _connection() as connection:
        # Telegram may redeliver an update until the next poll confirms its offset.
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO requests
                (reference, chat_id, update_id, state, detail, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (reference, str(chat_id), str(update_id), state, detail, now, now),
        )
        row = connection.execute(
            "SELECT * FROM requests WHERE update_id = ?", (str(update_id),)
        ).fetchone()
    return dict(row), cursor.rowcount == 1


def get(reference):
    with _connection() as connection:
        row = connection.execute(
            "SELECT * FROM requests WHERE reference = ?", (reference,)
        ).fetchone()
    return dict(row) if row is not None else None


def get_for_chat(reference, chat_id):
    with _connection() as connection:
        row = connection.execute(
            "SELECT * FROM requests WHERE reference = ? AND chat_id = ?",
            (reference, str(chat_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def get_by_update_id(update_id):
    with _connection() as connection:
        row = connection.execute(
            "SELECT * FROM requests WHERE update_id = ?", (str(update_id),)
        ).fetchone()
    return dict(row) if row is not None else None


def claim_update(update_id):
    """Persistently deduplicate a control-command Telegram update."""
    with _connection() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO handled_updates (update_id, created_at) VALUES (?, ?)",
            (str(update_id), time.time()),
        )
    return cursor.rowcount == 1


def update(reference, **fields):
    """Update request metadata without allowing arbitrary column names or values."""
    allowed = {
        "state",
        "acknowledgement_id",
        "session_id",
        "prompt_message_id",
        "detail",
        "started_at",
        "completed_at",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return get(reference)

    requested_state = updates.get("state")
    if requested_state is not None:
        return transition(reference, requested_state, **{key: value for key, value in updates.items() if key != "state"})

    updates["updated_at"] = time.time()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [reference]
    with _connection() as connection:
        connection.execute(
            f"UPDATE requests SET {assignments} WHERE reference = ?", values
        )
        row = connection.execute(
            "SELECT * FROM requests WHERE reference = ?", (reference,)
        ).fetchone()
    return dict(row) if row is not None else None


def transition(reference, new_state, **fields):
    """Atomically validate and apply one request lifecycle transition."""
    allowed = {
        "acknowledgement_id",
        "session_id",
        "prompt_message_id",
        "detail",
        "started_at",
        "completed_at",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    updates["state"] = new_state
    updates["updated_at"] = time.time()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with _connection() as connection:
        row = connection.execute(
            "SELECT state FROM requests WHERE reference = ?", (reference,)
        ).fetchone()
        if row is None:
            return None
        current_state = row["state"]
        if new_state not in ALLOWED_TRANSITIONS.get(current_state, ()):
            raise InvalidTransition(f"cannot transition from {current_state} to {new_state}")
        values = list(updates.values()) + [reference, current_state]
        connection.execute(
            f"UPDATE requests SET {assignments} WHERE reference = ? AND state = ?",
            values,
        )
        row = connection.execute(
            "SELECT * FROM requests WHERE reference = ?", (reference,)
        ).fetchone()
    return dict(row) if row is not None else None


def update_if_state(reference, expected_states, **fields):
    """Atomically update a request only if it is still in an expected state."""
    allowed = {
        "state",
        "acknowledgement_id",
        "session_id",
        "prompt_message_id",
        "detail",
        "started_at",
        "completed_at",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return get(reference)
    requested_state = updates.get("state")
    updates.pop("state", None)
    updates["updated_at"] = time.time()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    expected = tuple(expected_states)
    placeholders = ", ".join("?" for _ in expected)
    values = list(updates.values()) + [reference, *expected]
    with _connection() as connection:
        if requested_state is not None:
            current = connection.execute(
                "SELECT state FROM requests WHERE reference = ? AND state IN ({} )".format(placeholders),
                (reference, *expected),
            ).fetchone()
            if current is None:
                row = connection.execute(
                    "SELECT * FROM requests WHERE reference = ?", (reference,)
                ).fetchone()
                return dict(row) if row is not None else None
            if requested_state not in ALLOWED_TRANSITIONS.get(current["state"], ()):
                raise InvalidTransition(f"cannot transition from {current['state']} to {requested_state}")
            updates["state"] = requested_state
            assignments = ", ".join(f"{key} = ?" for key in updates)
            values = list(updates.values()) + [reference, *expected]
        connection.execute(
            f"UPDATE requests SET {assignments} WHERE reference = ? AND state IN ({placeholders})",
            values,
        )
        row = connection.execute(
            "SELECT * FROM requests WHERE reference = ?", (reference,)
        ).fetchone()
    return dict(row) if row is not None else None


def active_for_chat(chat_id):
    placeholders = ", ".join("?" for _ in ACTIVE_STATES)
    with _connection() as connection:
        rows = connection.execute(
            f"""SELECT * FROM requests WHERE chat_id = ? AND state IN ({placeholders})
                ORDER BY created_at""",
            (str(chat_id), *sorted(ACTIVE_STATES)),
        ).fetchall()
    return [dict(row) for row in rows]


def active_requests():
    placeholders = ", ".join("?" for _ in ACTIVE_STATES)
    with _connection() as connection:
        rows = connection.execute(
            f"SELECT * FROM requests WHERE state IN ({placeholders}) ORDER BY created_at",
            tuple(sorted(ACTIVE_STATES)),
        ).fetchall()
    return [dict(row) for row in rows]
