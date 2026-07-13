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
- `draft_created` (added at step 9) is not in the brief's original §5 enum.
  §3's idempotency rule requires recording the draft ID "the instant step 1
  returns, before step 2 fires" — the original enum had no event type for
  "a GMass draft exists but hasn't been sent yet," which is exactly the
  window a crash/retry needs to detect to avoid orphaning or double-creating
  a draft. It's recipient-scoped but cache-inert (an audit/idempotency
  marker only — `_apply_event_to_cache` no-ops on it), so it doesn't disturb
  the `recipients` status machine.
- `ooo_tagged`/`requeued` (step 10) mirror `queued`/`sent`: `ooo_tagged` is
  the "due for an OOO resend" marker (owner tagged a reply as OOO — a rare
  false-positive safety net, §7); `requeued` is the completion marker,
  written once the app's own resend of the recipient's next stage actually
  succeeds. `requeued` advances `current_stage`/`last_gmass_campaign_id`
  exactly like `sent` does, and flips status back to `'active'` — which is
  *why* no new schema column was needed to track "still pending resend":
  `slap.queue.due_for_ooo_resend()` just queries `status = 'ooo_requeued'`,
  since a successful `requeued` naturally removes a recipient from that set.
- `reply_reviewed` (added at step 11) is not in the brief's original §5 enum.
  §8's dashboard lets the owner tag a reply real/OOO/not-interested; OOO
  already has a real event (`ooo_tagged`) with backend consequences, but
  "real"/"not-interested" have none — they're pure triage bookkeeping, not
  state transitions, since neither is a valid `recipients.status` value.
  Without SOME event marking "the owner already looked at this reply,"
  every reply would show as needing triage on the dashboard forever.
  `reply_reviewed` (meta `{"tag": "real"|"not_interested"}`) is cache-inert,
  like `draft_created` — `slap.dashboard.needs_triage()` finds replies whose
  latest reply-lifecycle event (`reply`/`ooo_tagged`/`reply_reviewed`) is
  still `reply`, the same "any later closing event resolves it" pattern as
  `due_for_ooo_resend()`.
- `template-reload` (post-launch, `slap.reload`) deliberately did NOT get a
  new event type for its per-recipient failure reports, unlike every event
  type documented above. The difference: those all needed to affect the
  `recipients` cache (a real state transition) or be replayable truth. A
  reload failure is neither — it's a disposable diagnostic about the CURRENT
  attempt only, explicitly superseded wholesale by the next attempt (see
  `slap.reload`'s own module docstring for "unresolved failures from the
  most recent run"). More importantly, this table's `type` column has a SQL
  CHECK constraint (below) baked into every already-existing, populated
  `slap.db` at table-creation time — adding a literal value to it needs a
  full table rebuild, not an `ALTER TABLE`, a live-data-migration risk this
  diagnostic-only feature has no reason to take on (the exact same
  reasoning `slap.dashboard._sync_blocks()` already applied once, reusing
  `bounce` + a `meta["category"]` discriminator instead of a new `block`
  type). `slap.reload` instead writes a small JSON file, fully overwritten
  on every run.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("slap.db")

EVENT_TYPES = {
    "queued", "draft_created", "sent", "click", "reply", "bounce", "ooo_tagged",
    "requeued", "reply_reviewed", "run_started", "run_completed", "send_failed", "run_failed",
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
        'queued','draft_created','sent','click','reply','bounce','ooo_tagged','requeued',
        'reply_reviewed','run_started','run_completed','send_failed','run_failed'
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

-- events is append-only and grows forever by design (§5) — these cover the
-- (recipient, type) and bare-type lookups the dashboard/reachouts queries
-- and queue.due_for_ooo_resend()'s per-recipient loop already do, so their
-- cost stays near-flat as history accumulates instead of scaling with total
-- table size. Applied via executescript() on every connect() — idempotent,
-- no separate migration step.
CREATE INDEX IF NOT EXISTS idx_events_recipient_type ON events(recipient, type);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_recipients_status ON recipients(status);
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


def latest_open_draft_id(conn: sqlite3.Connection, recipient: str):
    """The draft_id from the most recent draft_created event for `recipient`
    that has no later `sent`/`requeued` event — i.e. a draft that exists but
    was never confirmed sent. Lets a retry resume without creating an
    orphan/duplicate draft (§3 idempotency). `requeued` (step 10's OOO
    resend completion marker) closes an open draft exactly like `sent`
    does — without it, a SECOND OOO cycle would see the FIRST cycle's
    already-`requeued` draft_created and wrongly treat it as still open.

    Second caller (post-launch): `slap.reload._reload_one` calls this for a
    different reason than `slap.runner._send_one` does. The runner uses a
    non-None result to REUSE an existing draft instead of creating a
    duplicate; `slap.reload` uses it purely as a READ — a non-None result
    means this recipient's initial subject/body are already committed to a
    real GMass draft (from a create_draft that succeeded before a later
    send_campaign failed), so `slap.reload` refuses to rewrite their staged
    manifest at all. Editing it locally at that point would silently
    split-brain the initial send (the next drain reuses the STALE draft
    content unchanged, per this function's whole purpose) against a
    follow-up cadence rebuilt from the newly-edited manifest. Note this is
    recipient-scoped, not campaign-scoped — a recipient re-staged into a new
    campaign while an old campaign's draft is still open will (correctly,
    if conservatively) still be refused a reload for the new campaign too;
    it can never be the reverse (a real open draft going undetected)."""
    rows = conn.execute(
        "SELECT type, gmass_draft_id FROM events WHERE recipient = ? "
        "AND type IN ('draft_created', 'sent', 'requeued') ORDER BY id DESC",
        (recipient,),
    ).fetchall()
    for row in rows:
        if row["type"] in ("sent", "requeued"):
            return None  # already resolved since the last draft_created — nothing open
        if row["type"] == "draft_created":
            return row["gmass_draft_id"]
    return None


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
    elif etype == "draft_created":
        return  # audit/idempotency marker only — no recipients-cache-visible state change
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
        # Mirrors `sent`'s pattern (§7 step 10: ooo_tagged ~ queued, requeued
        # ~ sent) — advances current_stage and last_gmass_campaign_id so the
        # resend is reflected the same way an initial send would be, and so
        # status flipping back to 'active' is what naturally removes this
        # recipient from the "due for OOO resend" query (no new column
        # needed — see slap.queue.due_for_ooo_resend).
        _upsert_recipient(conn, recipient, campaign=campaign, status="active",
                           current_stage=event["stage"],
                           last_gmass_campaign_id=event["gmass_campaign_id"], last_event_at=ts)
    elif etype == "reply_reviewed":
        return  # audit/triage marker only (step 11) — no recipients-cache-visible state change
