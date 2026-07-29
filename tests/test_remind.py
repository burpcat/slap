"""One-shot Remind: queue, due-set, threaded send, idempotency, eligibility."""
from datetime import datetime, timezone

import pytest

from slap import dashboard
from slap.queue import QueueError, due_for_remind, queue_remind
from slap.runner import _send_remind
from slap.tracking import append_event, connect, latest_open_draft_id


def _ts(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


def _seed_sent(conn, recipient="a@x.com", campaign="c", campaign_id="555"):
    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": "recruiter", "cadence": []})
    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0,
                 gmass_campaign_id=campaign_id)


def _make_real_lead(conn, recipient="a@x.com", campaign="c"):
    _seed_sent(conn, recipient, campaign)
    append_event(conn, type="reply", recipient=recipient, campaign=campaign)
    append_event(conn, type="reply_reviewed", recipient=recipient, campaign=campaign, meta={"tag": "real"})


class _CapturingGmass:
    def __init__(self):
        self.sent_settings = None

    def create_draft(self, api_key, *, recipient, subject, message, attachment=None):
        self.subject, self.message, self.attachment = subject, message, attachment
        return {"draft_id": "d1"}

    def send_campaign(self, api_key, draft_id, *, campaign_settings):
        self.sent_settings = campaign_settings
        return {"campaign_id": "999"}


# --- queue + due-set --------------------------------------------------------

def test_queue_remind_then_due(conn):
    _seed_sent(conn)
    queue_remind(conn, "a@x.com", "Just circling back", followup="nudge")
    due = due_for_remind(conn)
    assert len(due) == 1
    assert due[0]["body"] == "Just circling back"
    assert due[0]["campaign_id_to_reply_to"] == "555"
    assert due[0]["followup"] == "nudge"


def test_remind_sent_closes_the_due_entry(conn):
    _seed_sent(conn)
    queue_remind(conn, "a@x.com", "body")
    append_event(conn, type="interaction", recipient="a@x.com", campaign="c",
                 meta={"channel": "remind_sent"})
    assert due_for_remind(conn) == []


def test_queue_remind_unknown_recipient_fails_loud(conn):
    with pytest.raises(QueueError):
        queue_remind(conn, "ghost@x.com", "body")


def test_queue_remind_without_prior_send_fails_loud(conn):
    # queued but never sent -> no last_gmass_campaign_id -> nothing to thread onto.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter", "cadence": []})
    with pytest.raises(QueueError):
        queue_remind(conn, "a@x.com", "body")


# --- threaded send + idempotency -------------------------------------------

def test_send_remind_sends_threaded_reply_and_records_marker(conn):
    _seed_sent(conn)
    queue_remind(conn, "a@x.com", "circle back body")
    pending = due_for_remind(conn)[0]
    fake = _CapturingGmass()

    ok = _send_remind(conn, "key", pending, create_draft_fn=fake.create_draft,
                      send_campaign_fn=fake.send_campaign)
    assert ok is True
    assert fake.message == "circle back body"
    assert fake.attachment is None  # threaded reply carries no attachment
    # sent as a reply into the original campaign (build_reply_settings).
    assert fake.sent_settings is not None
    # remind_sent marker recorded, closing the due entry.
    assert due_for_remind(conn) == []


def test_remind_sent_closes_the_draft_for_later_sends(conn):
    _seed_sent(conn)
    queue_remind(conn, "a@x.com", "body")
    pending = due_for_remind(conn)[0]
    fake = _CapturingGmass()
    _send_remind(conn, "key", pending, create_draft_fn=fake.create_draft,
                 send_campaign_fn=fake.send_campaign)
    # The Remind's own draft is closed (remind_sent carries its draft id) — a
    # later send must NOT reuse a stale Remind draft.
    assert latest_open_draft_id(conn, "a@x.com") is None


def test_send_remind_retries_without_double_creating_a_draft(conn):
    _seed_sent(conn)
    queue_remind(conn, "a@x.com", "body")
    pending = due_for_remind(conn)[0]

    created = {"n": 0}

    def create_draft(api_key, *, recipient, subject, message, attachment=None):
        created["n"] += 1
        return {"draft_id": "d1"}

    def failing_send(api_key, draft_id, *, campaign_settings):
        raise RuntimeError("gmass down")

    def ok_send(api_key, draft_id, *, campaign_settings):
        return {"campaign_id": "999"}

    assert _send_remind(conn, "k", pending, create_draft_fn=create_draft, send_campaign_fn=failing_send) is False
    # Retry: the open draft is reused, not recreated (idempotency).
    assert _send_remind(conn, "k", pending, create_draft_fn=create_draft, send_campaign_fn=ok_send) is True
    assert created["n"] == 1


# --- eligibility gate (dashboard.queue_remind_for) --------------------------

def test_queue_remind_for_rejects_ineligible_recipient(conn):
    _seed_sent(conn)  # sent, but not warm-but-silent / linkedin / real
    with pytest.raises(ValueError):
        dashboard.queue_remind_for(conn, "a@x.com", "body")


def test_queue_remind_for_allows_a_real_lead(conn):
    _make_real_lead(conn)
    dashboard.queue_remind_for(conn, "a@x.com", "body")
    assert len(due_for_remind(conn)) == 1


def test_queue_remind_for_allows_a_linkedin_replied_recipient(conn):
    _seed_sent(conn)
    dashboard.mark_linkedin_replied(conn, "a@x.com", True)
    dashboard.queue_remind_for(conn, "a@x.com", "body")
    assert len(due_for_remind(conn)) == 1
