"""Tracking store tests (Build Order step 5), per SLAP_BUILD_PROMPT.md §13 B:
events append-only; cache updates on event; rebuild reproduces the cache
identically from the log.
"""
import sqlite3

import pytest

from slap.tracking import (
    TrackingError, _migrate_events_check_constraint, append_event, connect, latest_open_draft_id, rebuild,
)


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "test.db")


def all_events(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id ASC")]


def all_recipients(conn):
    return {r["recipient"]: dict(r) for r in conn.execute("SELECT * FROM recipients")}


def recipient_row(conn, recipient):
    row = conn.execute("SELECT * FROM recipients WHERE recipient = ?", (recipient,)).fetchone()
    return dict(row) if row else None


# --- append-only guarantee -------------------------------------------------

def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    connect(path)
    connect(path)  # must not raise on an already-initialized DB


def test_append_event_returns_new_row_id(conn):
    id1 = append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    id2 = append_event(conn, type="queued", recipient="b@x.com", campaign="c", stage=0)
    assert id2 == id1 + 1


def test_events_table_is_never_updated_or_deleted_in_source():
    # A structural check on the source itself, not just a naming convention —
    # catches a differently-named function issuing raw UPDATE/DELETE against
    # `events`, not just ones named update_event/delete_event.
    import pathlib
    import slap.tracking as tracking_module
    source = pathlib.Path(tracking_module.__file__).read_text().upper()
    assert "UPDATE EVENTS" not in source
    assert "DELETE FROM EVENTS" not in source


def test_rebuild_does_not_modify_events_table(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0)
    before = all_events(conn)
    rebuild(conn)
    after = all_events(conn)
    assert before == after


def test_unknown_event_type_rejected(conn):
    with pytest.raises(TrackingError, match="unknown event type"):
        append_event(conn, type="bogus", recipient="a@x.com", campaign="c")


def test_invalid_type_would_also_be_rejected_at_db_level(conn):
    # Defense in depth: the CHECK constraint protects any insert path, not
    # just append_event's own pre-check.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (timestamp, recipient, campaign, type) VALUES (?, ?, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "a@x.com", "c", "bogus"),
        )


# --- cache updates on event, per event type --------------------------------

def test_queued_creates_active_recipient_with_persona_from_meta(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    row = recipient_row(conn, "a@x.com")
    assert row["status"] == "active"
    assert row["current_stage"] == 0
    assert row["persona"] == "recruiter"
    assert row["first_sent_at"] is None


def test_sent_sets_first_sent_at_once_and_advances_stage(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=_ts(1))
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0,
                 gmass_campaign_id="111", timestamp=_ts(2))
    first = recipient_row(conn, "a@x.com")
    assert first["first_sent_at"] == _ts(2).isoformat()
    assert first["current_stage"] == 0
    assert first["status"] == "active"

    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=1,
                 gmass_campaign_id="111", timestamp=_ts(3))
    second = recipient_row(conn, "a@x.com")
    assert second["first_sent_at"] == _ts(2).isoformat()  # unchanged — first-write-wins
    assert second["current_stage"] == 1
    assert second["last_event_at"] == _ts(3).isoformat()


def test_sent_final_stage_marks_done(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=2,
                 meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=2,
                 meta={"is_final_stage": True})
    assert recipient_row(conn, "a@x.com")["status"] == "done"


def test_reply_sets_status_and_replied_at_once(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="reply", recipient="a@x.com", campaign="c", timestamp=_ts(1))
    row = recipient_row(conn, "a@x.com")
    assert row["status"] == "replied"
    assert row["replied_at"] == _ts(1).isoformat()

    append_event(conn, type="reply", recipient="a@x.com", campaign="c", timestamp=_ts(2))
    row2 = recipient_row(conn, "a@x.com")
    assert row2["replied_at"] == _ts(1).isoformat()  # unchanged — first-write-wins


def test_bounce_sets_bounced_status(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c")
    assert recipient_row(conn, "a@x.com")["status"] == "bounced"


def test_ooo_tagged_sets_ooo_requeued_status(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c")
    assert recipient_row(conn, "a@x.com")["status"] == "ooo_requeued"


def test_requeued_returns_to_active_status(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c")
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c")
    assert recipient_row(conn, "a@x.com")["status"] == "active"


def test_requeued_advances_stage_and_campaign_id_like_sent_does(conn):
    # Step 10: requeued mirrors sent so a successful OOO resend looks the
    # same to the cache as any other send, and status='active' (not
    # 'ooo_requeued') is what naturally removes it from the OOO due-query.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, gmass_campaign_id="1")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c")
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c", stage=1, gmass_campaign_id="2")
    row = recipient_row(conn, "a@x.com")
    assert row["status"] == "active"
    assert row["current_stage"] == 1
    assert row["last_gmass_campaign_id"] == "2"


def test_click_does_not_change_status(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, timestamp=_ts(1))
    append_event(conn, type="click", recipient="a@x.com", campaign="c", timestamp=_ts(2))
    row = recipient_row(conn, "a@x.com")
    assert row["status"] == "active"
    assert row["last_event_at"] == _ts(2).isoformat()


def test_send_failed_does_not_change_status_or_stage(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="send_failed", recipient="a@x.com", campaign="c", meta={"error": "timeout"})
    row = recipient_row(conn, "a@x.com")
    assert row["status"] == "active"
    assert row["current_stage"] == 0


def test_run_level_events_never_touch_recipients_cache(conn):
    append_event(conn, type="run_started")
    append_event(conn, type="run_completed", meta={"sent": 3})
    append_event(conn, type="run_failed", meta={"error": "boom", "retry_count": 3})
    assert all_recipients(conn) == {}
    assert len(all_events(conn)) == 3


def test_draft_created_is_cache_inert(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    before = recipient_row(conn, "a@x.com")
    append_event(conn, type="draft_created", recipient="a@x.com", campaign="c", stage=0,
                 gmass_draft_id="r-1")
    after = recipient_row(conn, "a@x.com")
    assert before == after


def test_reply_reviewed_is_cache_inert(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    before = recipient_row(conn, "a@x.com")
    append_event(conn, type="reply_reviewed", recipient="a@x.com", campaign="c",
                 meta={"tag": "not_interested"})
    after = recipient_row(conn, "a@x.com")
    assert before == after  # status stays 'replied' — real/not_interested aren't cache states


# --- latest_open_draft_id (§3 idempotency support) --------------------------

def test_latest_open_draft_id_none_when_never_drafted(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    assert latest_open_draft_id(conn, "a@x.com") is None


def test_latest_open_draft_id_returns_open_draft(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="draft_created", recipient="a@x.com", campaign="c",
                 stage=0, gmass_draft_id="r-1")
    assert latest_open_draft_id(conn, "a@x.com") == "r-1"


def test_latest_open_draft_id_none_once_sent(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="draft_created", recipient="a@x.com", campaign="c",
                 stage=0, gmass_draft_id="r-1")
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0,
                 gmass_campaign_id="1")
    assert latest_open_draft_id(conn, "a@x.com") is None


def test_latest_open_draft_id_picks_the_newest_after_a_completed_cycle(conn):
    # A second queued/draft_created cycle for the same recipient (e.g. a
    # future OOO resend, step 10) must not resurrect the OLD, already-sent
    # draft as "open".
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="draft_created", recipient="a@x.com", campaign="c",
                 stage=0, gmass_draft_id="r-1")
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0,
                 gmass_campaign_id="1")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=1)
    append_event(conn, type="draft_created", recipient="a@x.com", campaign="c",
                 stage=1, gmass_draft_id="r-2")
    assert latest_open_draft_id(conn, "a@x.com") == "r-2"


def test_latest_open_draft_id_none_once_requeued(conn):
    # requeued (step 10's OOO resend completion marker) must close an open
    # draft exactly like sent does — a real bug found while testing multiple
    # OOO cycles: without this, a SECOND cycle's latest_open_draft_id lookup
    # would wrongly resurrect the FIRST cycle's already-resolved draft.
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c")
    append_event(conn, type="draft_created", recipient="a@x.com", campaign="c",
                 stage=1, gmass_draft_id="r-1")
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c", stage=1,
                 gmass_campaign_id="1")
    assert latest_open_draft_id(conn, "a@x.com") is None


def test_latest_open_draft_id_picks_the_newest_across_two_ooo_cycles(conn):
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c")
    append_event(conn, type="draft_created", recipient="a@x.com", campaign="c",
                 stage=1, gmass_draft_id="r-1")
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c", stage=1,
                 gmass_campaign_id="1")
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c")
    append_event(conn, type="draft_created", recipient="a@x.com", campaign="c",
                 stage=2, gmass_draft_id="r-2")
    assert latest_open_draft_id(conn, "a@x.com") == "r-2"


# --- the core acceptance test -----------------------------------------------

def test_rebuild_reproduces_cache_identically(conn):
    # A realistic, mixed multi-recipient history exercising every one of the
    # 13 event types and every status end-state (active, replied, bounced,
    # done, ooo_requeued — plus a distinct recipient resent out of OOO back
    # to active, so 'requeued' is exercised as a transition too).
    append_event(conn, type="queued", recipient="active@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=_ts(1))
    append_event(conn, type="draft_created", recipient="active@x.com", campaign="c", stage=0,
                 gmass_draft_id="r-1", timestamp=_ts(1.5))
    append_event(conn, type="sent", recipient="active@x.com", campaign="c", stage=0,
                 gmass_campaign_id="1", timestamp=_ts(2))
    append_event(conn, type="click", recipient="active@x.com", campaign="c", timestamp=_ts(3))

    append_event(conn, type="queued", recipient="replied@x.com", campaign="c", stage=0,
                 meta={"persona": "founder"}, timestamp=_ts(1))
    append_event(conn, type="sent", recipient="replied@x.com", campaign="c", stage=0,
                 gmass_campaign_id="2", timestamp=_ts(2))
    append_event(conn, type="reply", recipient="replied@x.com", campaign="c", timestamp=_ts(3))
    append_event(conn, type="reply_reviewed", recipient="replied@x.com", campaign="c",
                 meta={"tag": "real"}, timestamp=_ts(4))

    append_event(conn, type="queued", recipient="bounced@x.com", campaign="c", stage=0,
                 meta={"persona": "hiring_manager"}, timestamp=_ts(1))
    append_event(conn, type="bounce", recipient="bounced@x.com", campaign="c", timestamp=_ts(2))

    append_event(conn, type="queued", recipient="done@x.com", campaign="c", stage=2,
                 meta={"persona": "recruiter"}, timestamp=_ts(1))
    append_event(conn, type="send_failed", recipient="done@x.com", campaign="c",
                 meta={"error": "timeout"}, timestamp=_ts(2))
    append_event(conn, type="sent", recipient="done@x.com", campaign="c", stage=2,
                 meta={"is_final_stage": True}, timestamp=_ts(3))

    # Ends in ooo_requeued — a genuine end-state, not just a mid-replay hop.
    append_event(conn, type="queued", recipient="still_ooo@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=_ts(1))
    append_event(conn, type="reply", recipient="still_ooo@x.com", campaign="c", timestamp=_ts(2))
    append_event(conn, type="ooo_tagged", recipient="still_ooo@x.com", campaign="c", timestamp=_ts(3))

    # Resent out of OOO back to active — exercises 'requeued' as a transition.
    append_event(conn, type="queued", recipient="resent@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=_ts(1))
    append_event(conn, type="reply", recipient="resent@x.com", campaign="c", timestamp=_ts(2))
    append_event(conn, type="ooo_tagged", recipient="resent@x.com", campaign="c", timestamp=_ts(3))
    append_event(conn, type="requeued", recipient="resent@x.com", campaign="c", timestamp=_ts(4))

    append_event(conn, type="run_started", timestamp=_ts(5))
    append_event(conn, type="run_completed", meta={"sent": 6}, timestamp=_ts(6))
    append_event(conn, type="run_failed", meta={"error": "boom", "retry_count": 3}, timestamp=_ts(7))

    assert {e["type"] for e in all_events(conn)} == set(
        ["queued", "draft_created", "sent", "click", "reply", "bounce", "ooo_tagged",
         "requeued", "reply_reviewed", "run_started", "run_completed", "send_failed", "run_failed"]
    )

    live_state = all_recipients(conn)
    assert len(live_state) == 6  # run-level events correctly excluded
    assert live_state["still_ooo@x.com"]["status"] == "ooo_requeued"
    assert live_state["resent@x.com"]["status"] == "active"

    rebuild(conn)
    rebuilt_state = all_recipients(conn)

    assert rebuilt_state == live_state


def test_rebuild_fixes_a_corrupted_cache(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0)
    correct = recipient_row(conn, "a@x.com")

    # Simulate cache corruption/drift that would never happen via append_event.
    conn.execute("UPDATE recipients SET status = 'bounced', current_stage = 99 WHERE recipient = ?",
                 ("a@x.com",))
    conn.commit()
    assert recipient_row(conn, "a@x.com") != correct

    rebuild(conn)
    assert recipient_row(conn, "a@x.com") == correct


def test_rebuild_fixes_a_corrupted_first_write_wins_field(conn):
    # first_sent_at/replied_at are first-write-wins on upsert (COALESCE picks
    # the OLD value if set) — so unlike last-write-wins fields, a corrupted
    # first_sent_at can ONLY be fixed by rebuild's DELETE-then-replay, never
    # by replay alone on top of the stale row. This is what actually proves
    # rebuild() truly regenerates the cache rather than just re-applying
    # last-write-wins updates over whatever was already there.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=_ts(1))
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, timestamp=_ts(2))
    correct = recipient_row(conn, "a@x.com")
    assert correct["first_sent_at"] == _ts(2).isoformat()

    conn.execute("UPDATE recipients SET first_sent_at = ? WHERE recipient = ?",
                 (_ts(99).isoformat(), "a@x.com"))
    conn.commit()
    assert recipient_row(conn, "a@x.com")["first_sent_at"] == _ts(99).isoformat()

    rebuild(conn)
    assert recipient_row(conn, "a@x.com") == correct


def test_stopped_sets_stopped_status(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="stopped", recipient="a@x.com", campaign="c", meta={"scope": "recipient"})
    assert recipient_row(conn, "a@x.com")["status"] == "stopped"


def test_stopped_removes_recipient_from_active_status(conn):
    # 'stopped' must actually flip status away from 'active' — this alone is
    # what removes a stopped recipient from due_recipients()/
    # due_for_ooo_resend() (both require status IN ('active', 'ooo_requeued'))
    # and pipeline()'s followups_scheduled (status == 'active'), with no
    # changes needed to any of those queries.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, gmass_campaign_id="1")
    assert recipient_row(conn, "a@x.com")["status"] == "active"
    append_event(conn, type="stopped", recipient="a@x.com", campaign="c", meta={"scope": "recipient"})
    row = recipient_row(conn, "a@x.com")
    assert row["status"] == "stopped"
    assert row["current_stage"] == 0  # unrelated fields untouched


# --- 'stopped' event type: migration for a pre-existing db --------------

def _old_events_table_sql():
    # The events table's CREATE statement exactly as it looked before the
    # 'stopped' literal was added to the CHECK constraint — used to build a
    # fixture db that looks like a real owner's already-existing slap.db.
    return """
    CREATE TABLE events (
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
    """


def test_connect_migrates_a_pre_existing_db_missing_the_stopped_type(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(_old_events_table_sql())
    raw.execute(
        "INSERT INTO events (timestamp, recipient, campaign, type, stage) VALUES (?, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00+00:00", "a@x.com", "c", "queued", 0),
    )
    raw.commit()
    raw.close()

    # A raw insert of 'stopped' against the OLD db would be rejected —
    # confirms the fixture really does reproduce the pre-migration schema.
    raw = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO events (timestamp, recipient, campaign, type) VALUES (?, ?, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "a@x.com", "c", "stopped"),
        )
    raw.close()

    migrated = connect(path)
    # Pre-existing history survived the migration untouched.
    rows = all_events(migrated)
    assert len(rows) == 1
    assert rows[0]["type"] == "queued" and rows[0]["recipient"] == "a@x.com"

    # 'stopped' now works, and the recipients cache still reflects it.
    append_event(migrated, type="stopped", recipient="a@x.com", campaign="c",
                 meta={"scope": "recipient"})
    assert recipient_row(migrated, "a@x.com")["status"] == "stopped"


def test_connect_migration_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(_old_events_table_sql())
    raw.commit()
    raw.close()

    connect(path).close()
    connect(path).close()  # second connect on an already-migrated db must not raise


class _FlakyConnection(sqlite3.Connection):
    """Test-only sqlite3.Connection subclass (a bare instance can't have
    `.execute` monkeypatched — it's a read-only attribute on the built-in
    C type) that raises on demand for one specific statement, so a
    mid-migration failure can be forced and observed deterministically."""
    fail_on_prefix = None

    def execute(self, sql, *args, **kwargs):
        if self.fail_on_prefix and sql.strip().upper().startswith(self.fail_on_prefix):
            raise RuntimeError("simulated crash mid-migration")
        return super().execute(sql, *args, **kwargs)


def test_migration_rolls_back_cleanly_on_a_mid_migration_failure(tmp_path):
    # An iron-audit BLOCKER fix: the migration must be genuinely atomic (a
    # real explicit transaction, never touched by conn.executescript()'s
    # own implicit-commit-before-DDL behavior — see
    # _migrate_events_check_constraint's own docstring for why that
    # combination silently stranded the entire event log before this fix).
    # Forces a failure AFTER the RENAME has already run (the single most
    # dangerous point — RENAME is DDL and, per that same docstring, used to
    # commit durably on its own) and confirms: (1) the exception propagates
    # rather than being swallowed, (2) the db is left in the ORIGINAL,
    # unmigrated state — not stranded with an empty new `events` table and
    # a hidden `events_pre_stopped_migration` — and (3) a real retry
    # afterward succeeds and preserves every pre-existing row.
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(_old_events_table_sql())
    raw.execute(
        "INSERT INTO events (timestamp, recipient, campaign, type, stage) VALUES (?, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00+00:00", "a@x.com", "c", "queued", 0),
    )
    raw.commit()
    raw.close()

    conn = sqlite3.connect(path, factory=_FlakyConnection)
    conn.row_factory = sqlite3.Row
    conn.fail_on_prefix = "INSERT INTO EVENTS"
    with pytest.raises(RuntimeError, match="simulated crash mid-migration"):
        _migrate_events_check_constraint(conn)
    conn.close()

    # The db must show the ORIGINAL, unmigrated table — not a half-renamed
    # mess — confirming the RENAME itself was rolled back, not just the
    # failed INSERT.
    inspect = sqlite3.connect(path)
    tables = {r[0] for r in inspect.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert tables == {"events", "sqlite_sequence"}
    events_sql = inspect.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()[0]
    assert "'stopped'" not in events_sql
    inspect.close()

    # A real (unpatched) retry succeeds and preserves the pre-existing row.
    retry = connect(path)
    rows = all_events(retry)
    assert len(rows) == 1
    assert rows[0]["type"] == "queued" and rows[0]["recipient"] == "a@x.com"
    append_event(retry, type="stopped", recipient="a@x.com", campaign="c", meta={"scope": "recipient"})
    assert recipient_row(retry, "a@x.com")["status"] == "stopped"


def test_rebuild_removes_a_stale_recipient_row_with_no_backing_events(conn):
    # A recipients row with no events behind it at all (e.g. left over from a
    # deleted/renamed recipient) must be purged by rebuild's full
    # regeneration, not just left in place because replay never touches it.
    conn.execute(
        "INSERT INTO recipients (recipient, campaign, persona, status, current_stage, "
        "last_gmass_campaign_id, first_sent_at, last_event_at, replied_at) "
        "VALUES ('ghost@x.com', 'c', 'recruiter', 'active', 0, NULL, NULL, ?, NULL)",
        (_ts(1).isoformat(),),
    )
    conn.commit()
    assert recipient_row(conn, "ghost@x.com") is not None

    rebuild(conn)
    assert recipient_row(conn, "ghost@x.com") is None


def _ts(n):
    from datetime import datetime, timedelta, timezone
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n)
