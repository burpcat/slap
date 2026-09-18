"""One-shot Remind: queue, due-set, threaded send, idempotency, eligibility."""
from datetime import datetime, timezone

import pytest

from slap import dashboard
from slap.queue import QueueError, due_for_remind, queue_remind
from slap.runner import _send_remind
from slap.smtp import SmtpConfig
from slap.tracking import append_event, connect

SMTP = SmtpConfig(host="smtp.gmail.com", port=587, user="owner@gmail.com", password="pw")


def _ts(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


def _seed_sent(conn, recipient="a@x.com", campaign="c", message_id="<sent-555@gmail.com>"):
    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": "recruiter", "cadence": []})
    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0,
                 message_id=message_id)


def _make_real_lead(conn, recipient="a@x.com", campaign="c"):
    _seed_sent(conn, recipient, campaign)
    append_event(conn, type="reply", recipient=recipient, campaign=campaign)
    append_event(conn, type="reply_reviewed", recipient=recipient, campaign=campaign, meta={"tag": "real"})


class _CapturingSmtp:
    """Captures the outbound send instead of hitting SMTP."""
    def send_message(self, smtp_config, *, sender, recipient, subject, body,
                     sender_name=None, attachment=None, in_reply_to=None, references=None):
        self.subject, self.body, self.attachment = subject, body, attachment
        self.in_reply_to = in_reply_to
        return {"message_id": "<remind-1@gmail.com>", "raw": {}}


# --- queue + due-set --------------------------------------------------------

def test_queue_remind_then_due(conn):
    _seed_sent(conn)
    queue_remind(conn, "a@x.com", "Just circling back", followup="nudge")
    due = due_for_remind(conn)
    assert len(due) == 1
    assert due[0]["body"] == "Just circling back"
    assert due[0]["reply_to_message_id"] == "<sent-555@gmail.com>"
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
    # queued but never sent -> no message_id -> nothing to thread onto.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter", "cadence": []})
    with pytest.raises(QueueError):
        queue_remind(conn, "a@x.com", "body")


# --- threaded send + idempotency -------------------------------------------

def test_send_remind_sends_threaded_reply_and_records_marker(conn):
    _seed_sent(conn)
    queue_remind(conn, "a@x.com", "circle back body")
    pending = due_for_remind(conn)[0]
    fake = _CapturingSmtp()

    ok = _send_remind(conn, SMTP, pending, send_message_fn=fake.send_message)
    assert ok is True
    assert fake.body == "circle back body"
    assert fake.attachment is None  # threaded reply carries no attachment
    # threaded into the recipient's original send (In-Reply-To its message_id).
    assert fake.in_reply_to == "<sent-555@gmail.com>"
    # remind_sent marker recorded, closing the due entry.
    assert due_for_remind(conn) == []


def test_send_remind_retry_after_send_failure_sends_once(conn):
    _seed_sent(conn)
    queue_remind(conn, "a@x.com", "body")
    pending = due_for_remind(conn)[0]

    def failing_send(smtp_config, **kw):
        raise RuntimeError("smtp down")

    sent = {"n": 0}

    def ok_send(smtp_config, **kw):
        sent["n"] += 1
        return {"message_id": "<r@gmail.com>", "raw": {}}

    # A send failure leaves the remind pending (retryable), no marker written.
    assert _send_remind(conn, SMTP, pending, send_message_fn=failing_send) is False
    assert len(due_for_remind(conn)) == 1
    # Retry succeeds and fires exactly once, closing the due entry.
    assert _send_remind(conn, SMTP, pending, send_message_fn=ok_send) is True
    assert sent["n"] == 1
    assert due_for_remind(conn) == []


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
