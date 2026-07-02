"""Event-sourced tracking store (Build Order step 5).

One SQLite file, two tables. `events` is the only source of truth and is
append-only — never updated, never deleted. `recipients` is a derived cache
for fast current-state, fully rebuildable by replaying `events` (rebuild()).
See SLAP_BUILD_PROMPT.md §5.

Design decisions not fully pinned down by the brief (documented here and in
CONTROL_SHEET.md, revisit when steps 6/9/10 wire in real callers):

- `run_started`/`run_completed`/`run_failed` describe a drain's own
  lifecycle, not one recipient — they're appended to `events` for the
  dashboard's "Today's runs" panel (§8) but never touch `recipients`.
- The `recipients.status` value 'done' (sequence exhausted, no reply) has no
  dedicated event type in the brief's enum. Convention adopted: a `sent`
  event's `meta` may include `"is_final_stage": true` (the caller knows the
  persona's cadence length at send time) — that marks status='done' instead
  of 'active'. Without it, `sent` always leaves status='active'.
- `persona` isn't a fixed `events` column, so it must ride in a `queued`
  event's `meta` (e.g. `{"persona": "recruiter"}`) — the caller knows it at
  queue time. This keeps `recipients` a pure function of `events` alone
  (rebuildable without consulting live, possibly-since-changed config).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("slap.db")

EVENT_TYPES = {
    "queued", "sent", "click", "reply", "bounce", "ooo_tagged", "requeued",
    "run_started", "run_completed", "send_failed", "run_failed",
}
# Event types describing a runner/drain's own lifecycle, not a specific
# recipient — appended to the log but never applied to the recipients cache.
RUN_LEVEL_TYPES = {"run_started", "run_completed", "run_failed"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    recipient TEXT,
    campaign TEXT,
    type TEXT NOT NULL CHECK (type IN (
        'queued','sent','click','reply','bounce','ooo_tagged','requeued',
        'run_started','run_completed','send_failed','run_failed'
    )),
    stage INTEGER,
    gmass_campaign_id TEXT,
    gmass_draft_id TEXT,
    meta TEXT
);

CREATE TABLE IF NOT EXISTS recipients (
    recipient TEXT PRIMARY KEY,
    campaign TEXT,
    persona TEXT,
    status TEXT,
    current_stage INTEGER,
    last_gmass_campaign_id TEXT,
    first_sent_at TEXT,
    last_event_at TEXT,
    replied_at TEXT
);
"""


class TrackingError(Exception):
    """Raised on fail-loud tracking-store misuse (e.g. an unknown event type)."""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the tracking DB with the schema applied."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def append_event(conn: sqlite3.Connection, *, type: str, recipient: str = None,
                  campaign: str = None, stage: int = None, gmass_campaign_id: str = None,
                  gmass_draft_id: str = None, meta: dict = None, timestamp=None) -> int:
    """Append one event (never updates/deletes existing rows) and apply its
    effect to the recipients cache in the same transaction. Returns the new
    event's id."""
    if type not in EVENT_TYPES:
        raise TrackingError(f"unknown event type {type!r} — must be one of {sorted(EVENT_TYPES)}")
    if type not in RUN_LEVEL_TYPES and not recipient:
        raise TrackingError(f"event type {type!r} requires a recipient")
    if timestamp is not None and timestamp.tzinfo is None:
        raise TrackingError(
            f"timestamp must be timezone-aware UTC (all timestamps are UTC, §5) — "
            f"got a naive datetime {timestamp!r}"
        )
    ts = (timestamp or datetime.now(timezone.utc)).isoformat()
    meta_json = json.dumps(meta) if meta is not None else None
    cur = conn.execute(
        "INSERT INTO events (timestamp, recipient, campaign, type, stage, "
        "gmass_campaign_id, gmass_draft_id, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, recipient, campaign, type, stage, gmass_campaign_id, gmass_draft_id, meta_json),
    )
    event_id = cur.lastrowid
    _apply_event_to_cache(conn, {
        "timestamp": ts, "recipient": recipient, "campaign": campaign, "type": type,
        "stage": stage, "gmass_campaign_id": gmass_campaign_id,
        "gmass_draft_id": gmass_draft_id, "meta": meta,
    })
    conn.commit()
    return event_id


def rebuild(conn: sqlite3.Connection) -> None:
    """Regenerate the recipients cache entirely by replaying events in the
    order they were appended (id ASC). This is the crash-recovery guarantee:
    a rebuilt cache must equal the live one."""
    conn.execute("DELETE FROM recipients")
    for row in conn.execute("SELECT * FROM events ORDER BY id ASC"):
        event = dict(row)
        event["meta"] = json.loads(event["meta"]) if event["meta"] is not None else None
        _apply_event_to_cache(conn, event)
    conn.commit()


def _upsert_recipient(conn, recipient, *, campaign=None, persona=None, status=None,
                       current_stage=None, last_gmass_campaign_id=None,
                       first_sent_at=None, last_event_at=None, replied_at=None) -> None:
    """Insert a recipients row or merge fields into an existing one. Fields
    left as None here mean 'don't change' on conflict, except first_sent_at/
    replied_at which are first-write-wins (once set, never overwritten)."""
    conn.execute(
        """
        INSERT INTO recipients
            (recipient, campaign, persona, status, current_stage,
             last_gmass_campaign_id, first_sent_at, last_event_at, replied_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(recipient) DO UPDATE SET
            campaign = COALESCE(excluded.campaign, recipients.campaign),
            persona = COALESCE(excluded.persona, recipients.persona),
            status = COALESCE(excluded.status, recipients.status),
            current_stage = COALESCE(excluded.current_stage, recipients.current_stage),
            last_gmass_campaign_id = COALESCE(excluded.last_gmass_campaign_id, recipients.last_gmass_campaign_id),
            first_sent_at = COALESCE(recipients.first_sent_at, excluded.first_sent_at),
            last_event_at = COALESCE(excluded.last_event_at, recipients.last_event_at),
            replied_at = COALESCE(recipients.replied_at, excluded.replied_at)
        """,
        (recipient, campaign, persona, status, current_stage, last_gmass_campaign_id,
         first_sent_at, last_event_at, replied_at),
    )


def _apply_event_to_cache(conn: sqlite3.Connection, event: dict) -> None:
    """The single source of truth for how one event changes `recipients`.
    Called by both append_event (incrementally) and rebuild (by replay) with
    identical logic, so a rebuilt cache is guaranteed to equal the live one.
    """
    if event["type"] in RUN_LEVEL_TYPES:
        return

    recipient, campaign, ts = event["recipient"], event["campaign"], event["timestamp"]
    meta = event.get("meta") or {}
    etype = event["type"]

    if etype == "queued":
        _upsert_recipient(conn, recipient, campaign=campaign, persona=meta.get("persona"),
                           status="active", current_stage=event["stage"], last_event_at=ts)
    elif etype == "sent":
        status = "done" if meta.get("is_final_stage") else "active"
        _upsert_recipient(conn, recipient, campaign=campaign, status=status,
                           current_stage=event["stage"],
                           last_gmass_campaign_id=event["gmass_campaign_id"],
                           first_sent_at=ts, last_event_at=ts)
    elif etype in ("send_failed", "click"):
        _upsert_recipient(conn, recipient, campaign=campaign, last_event_at=ts)
    elif etype == "reply":
        _upsert_recipient(conn, recipient, campaign=campaign, status="replied",
                           last_event_at=ts, replied_at=ts)
    elif etype == "bounce":
        _upsert_recipient(conn, recipient, campaign=campaign, status="bounced", last_event_at=ts)
    elif etype == "ooo_tagged":
        _upsert_recipient(conn, recipient, campaign=campaign, status="ooo_requeued", last_event_at=ts)
    elif etype == "requeued":
        _upsert_recipient(conn, recipient, campaign=campaign, status="active", last_event_at=ts)
