"""UI-only dismissal state (post-launch feature — e.g. "hide" on a
Warm-but-silent row).

Deliberately NOT an `events` type or a `recipients` cache column: hiding a
row is a dashboard display preference, not something that actually happened
to the recipient in the outreach sense — appending it as an event would
pollute that append-only truth log with cosmetic state (see
slap.tracking's own "one SQLite file, two tables, `events` is the only
source of truth" docstring), and a new event type would need a real
SQL-CHECK-constraint migration of every owner's live database — the exact
cost slap.dashboard._sync_blocks() already documents as the reason it
reuses the `bounce` event type instead of adding a `block` one.

This is a third, small, purpose-built table living in the SAME slap.db file
(one local state file, even though it's genuinely a third table) — kept in
its own module rather than folded into tracking.py's schema, so that
module's "one SQLite file, two tables" docstring claim about the
event-sourced model stays accurate: this is explicitly NOT a variant of
`recipients`/`events`, it's UI state with no append-only or rebuild-from-
events guarantee at all — hiding something and unhiding it later has no
history, on purpose.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ui_state (
    recipient TEXT NOT NULL,
    widget TEXT NOT NULL,
    hidden_at TEXT NOT NULL,
    PRIMARY KEY (recipient, widget)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def hide(conn: sqlite3.Connection, recipient: str, widget: str, *, when: datetime = None) -> None:
    """Marks `recipient` hidden on `widget` as of now (or `when`) —
    idempotent re-hide just bumps `hidden_at` to the new time."""
    ensure_schema(conn)
    ts = (when or datetime.now(timezone.utc)).isoformat()
    conn.execute(
        "INSERT INTO ui_state (recipient, widget, hidden_at) VALUES (?, ?, ?) "
        "ON CONFLICT(recipient, widget) DO UPDATE SET hidden_at = excluded.hidden_at",
        (recipient, widget, ts),
    )
    conn.commit()


def unhide(conn: sqlite3.Connection, recipient: str, widget: str) -> None:
    ensure_schema(conn)
    conn.execute("DELETE FROM ui_state WHERE recipient = ? AND widget = ?", (recipient, widget))
    conn.commit()


def hidden_at(conn: sqlite3.Connection, recipient: str, widget: str):
    """This recipient's `hidden_at` timestamp for `widget`, or None if not
    hidden at all."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT hidden_at FROM ui_state WHERE recipient = ? AND widget = ?", (recipient, widget)
    ).fetchone()
    return row["hidden_at"] if row else None


def list_hidden(conn: sqlite3.Connection, widget: str) -> list:
    """Every currently-hidden `{recipient, hidden_at}` row for `widget`,
    most-recently-hidden first. Callers that need to auto-resurface a row
    (e.g. slap.dashboard's "a newer click un-hides it" rule) do that
    comparison themselves — this module has no opinion on what should
    override a hide, only on storing/reading the hide itself."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT recipient, hidden_at FROM ui_state WHERE widget = ? ORDER BY hidden_at DESC", (widget,)
    ).fetchall()
    return [dict(r) for r in rows]
