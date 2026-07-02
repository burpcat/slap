"""Tracking store tests (Build Order step 5), per SLAP_BUILD_PROMPT.md §13 B:
events append-only; cache updates on event; rebuild reproduces the cache
identically from the log.
"""
import sqlite3

import pytest

from slap.tracking import TrackingError, append_event, connect, rebuild


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


# --- the core acceptance test -----------------------------------------------

def test_rebuild_reproduces_cache_identically(conn):
    # A realistic, mixed multi-recipient history exercising every one of the
    # 11 event types and every status end-state (active, replied, bounced,
    # done, ooo_requeued — plus a distinct recipient resent out of OOO back
    # to active, so 'requeued' is exercised as a transition too).
    append_event(conn, type="queued", recipient="active@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=_ts(1))
    append_event(conn, type="sent", recipient="active@x.com", campaign="c", stage=0,
                 gmass_campaign_id="1", timestamp=_ts(2))
    append_event(conn, type="click", recipient="active@x.com", campaign="c", timestamp=_ts(3))

    append_event(conn, type="queued", recipient="replied@x.com", campaign="c", stage=0,
                 meta={"persona": "founder"}, timestamp=_ts(1))
    append_event(conn, type="sent", recipient="replied@x.com", campaign="c", stage=0,
                 gmass_campaign_id="2", timestamp=_ts(2))
    append_event(conn, type="reply", recipient="replied@x.com", campaign="c", timestamp=_ts(3))

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
        ["queued", "sent", "click", "reply", "bounce", "ooo_tagged", "requeued",
         "run_started", "run_completed", "send_failed", "run_failed"]
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
