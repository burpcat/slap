"""Interaction-log features: LinkedIn-replied toggle, follow-up timer reset."""
from datetime import date, datetime, timezone

import pytest

from slap.dashboard import (
    follow_up_reminders, linkedin_replied_state, mark_followed_up,
    mark_linkedin_replied, reachouts_rows,
)
from slap.tracking import append_event, connect, rebuild


def _ts(y, m, d, hour=12):
    # Noon UTC by default so the LOCAL calendar date (what follow_up_reminders
    # buckets by) matches the y/m/d given across US/EU offsets — a midnight-UTC
    # timestamp would shift to the previous local day west of UTC.
    return datetime(y, m, d, hour, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "test.db")


def _seed_real_lead(conn, recipient="jane@acme.com", campaign="c", real_at=None):
    """A recipient who replied and was tagged Real — an active lead."""
    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": "recruiter", "cadence": [2, 4], "company": "Acme", "role": "Eng"})
    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0,
                 gmass_campaign_id="123", timestamp=_ts(2026, 6, 1))
    append_event(conn, type="reply", recipient=recipient, campaign=campaign, timestamp=_ts(2026, 7, 1))
    append_event(conn, type="reply_reviewed", recipient=recipient, campaign=campaign,
                 meta={"tag": "real"}, timestamp=real_at or _ts(2026, 7, 1))


# --- LinkedIn-replied toggle ------------------------------------------------

def test_mark_linkedin_replied_sets_and_clears(conn):
    _seed_real_lead(conn)
    assert linkedin_replied_state(conn).get("jane@acme.com") is None

    mark_linkedin_replied(conn, "jane@acme.com", True)
    assert linkedin_replied_state(conn)["jane@acme.com"] is True

    mark_linkedin_replied(conn, "jane@acme.com", False)  # toggle off = fresh append
    assert linkedin_replied_state(conn)["jane@acme.com"] is False


def test_mark_linkedin_replied_unknown_recipient_fails_loud(conn):
    with pytest.raises(ValueError):
        mark_linkedin_replied(conn, "ghost@nowhere.com", True)


def test_reachouts_rows_expose_linkedin_replied(conn):
    _seed_real_lead(conn)
    row = next(r for r in reachouts_rows(conn) if r["recipient"] == "jane@acme.com")
    assert row["linkedin_replied"] is False  # default when never toggled

    mark_linkedin_replied(conn, "jane@acme.com", True)
    row = next(r for r in reachouts_rows(conn) if r["recipient"] == "jane@acme.com")
    assert row["linkedin_replied"] is True


def test_interaction_is_append_only_and_rebuild_safe(conn):
    _seed_real_lead(conn)
    mark_linkedin_replied(conn, "jane@acme.com", True)
    mark_followed_up(conn, "jane@acme.com")
    before = {r["recipient"]: dict(r) for r in conn.execute("SELECT * FROM recipients")}
    rebuild(conn)
    after = {r["recipient"]: dict(r) for r in conn.execute("SELECT * FROM recipients")}
    assert before == after  # cache-inert type replays identically


# --- follow-up "mark followed up" timer reset -------------------------------

def test_followed_up_unknown_recipient_fails_loud(conn):
    with pytest.raises(ValueError):
        mark_followed_up(conn, "ghost@nowhere.com")


def test_mark_followed_up_restarts_the_reminder_timer(conn):
    # Marked Real on 2026-07-01 -> ~28 days overdue as of the fixed 'today'.
    _seed_real_lead(conn, real_at=_ts(2026, 7, 1))
    today = date(2026, 7, 29)
    before = follow_up_reminders(conn, today=today)
    assert before[0]["recipient"] == "jane@acme.com"
    assert before[0]["days_since"] == 28  # 2026-07-01 (noon UTC) -> 2026-07-29

    # A follow-up on 2026-07-28 restarts the clock: now only 1 day since.
    append_event(conn, type="interaction", recipient="jane@acme.com", campaign="c",
                 meta={"channel": "followed_up"}, timestamp=_ts(2026, 7, 28))
    after = follow_up_reminders(conn, today=today)
    assert after[0]["days_since"] == 1
    assert after[0]["last_interaction_at"] is not None


def test_linkedin_replied_also_resets_the_reminder_timer(conn):
    # req 4: the timer reset "syncs with linkedin-replied" — a LinkedIn toggle
    # is an interaction too, so it also restarts the clock.
    _seed_real_lead(conn, real_at=_ts(2026, 7, 1))
    today = date(2026, 7, 29)
    mark_linkedin_replied(conn, "jane@acme.com", True)  # appended "now" (real today)
    # Its timestamp is real-now (2026-07-29+), strictly later than 2026-07-01,
    # so days_since collapses toward 0.
    after = follow_up_reminders(conn, today=today)
    assert after[0]["days_since"] <= 1
