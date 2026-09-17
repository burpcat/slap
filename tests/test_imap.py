"""IMAP reply-detection tests — fast, no network (a fake IMAP factory injected)."""
import imaplib

import pytest

from slap.imap import ImapConfig, ImapError, poll_replies

CFG = ImapConfig(host="imap.gmail.com", port=993, user="me@gmail.com", password="pw")


def _raw(message_id, in_reply_to=None, references=None, frm="lead@corp.com",
         date="Wed, 17 Sep 2026 10:00:00 -0400", subject="Re: Hi"):
    lines = [f"From: {frm}", f"Message-ID: {message_id}", f"Subject: {subject}", f"Date: {date}"]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        lines.append(f"References: {references}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class FakeIMAP:
    """Stand-in for imaplib.IMAP4_SSL. `messages` is a list of raw header
    byte-strings; sequence numbers are 1-based indices into it."""
    def __init__(self, messages, *, fail_login=False, fail_search=False):
        self._messages = messages
        self._fail_login, self._fail_search = fail_login, fail_search
        self.logged_out = False

    def factory(self):
        def _make(host, port, timeout=None):
            return self
        return _make

    def login(self, user, password):
        if self._fail_login:
            raise imaplib.IMAP4.error("bad credentials")

    def select(self, mailbox):
        return ("OK", [str(len(self._messages)).encode()])

    def search(self, charset, *criteria):
        if self._fail_search:
            return ("NO", [b""])
        return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(self._messages)))])

    def fetch(self, num, spec):
        idx = int(num) - 1
        if idx < 0 or idx >= len(self._messages):
            return ("NO", [None])
        return ("OK", [(b"%s (BODY[HEADER])" % num, self._messages[idx])])

    def logout(self):
        self.logged_out = True


def test_poll_replies_matches_on_in_reply_to():
    fake = FakeIMAP([_raw("<reply-1@corp.com>", in_reply_to="<init@gmail.com>")])
    out = poll_replies(CFG, {"<init@gmail.com>"}, imap_factory=fake.factory())
    assert out == [{
        "in_reply_to": "<init@gmail.com>",
        "reply_message_id": "<reply-1@corp.com>",
        "from": "lead@corp.com",
        "reply_time": "2026-09-17T10:00:00-04:00",
    }]
    assert fake.logged_out is True  # connection always released


def test_poll_replies_matches_via_references_when_no_in_reply_to():
    fake = FakeIMAP([_raw("<r2@corp.com>", references="<other@gmail.com> <init@gmail.com>")])
    out = poll_replies(CFG, {"<init@gmail.com>"}, imap_factory=fake.factory())
    assert len(out) == 1
    assert out[0]["in_reply_to"] == "<init@gmail.com>"  # attributed to the matched id


def test_poll_replies_ignores_unrelated_messages():
    fake = FakeIMAP([
        _raw("<newsletter@spam.com>"),  # no threading headers
        _raw("<x@corp.com>", in_reply_to="<not-ours@gmail.com>"),  # threads onto something else
    ])
    assert poll_replies(CFG, {"<init@gmail.com>"}, imap_factory=fake.factory()) == []


def test_poll_replies_empty_sent_set_short_circuits_without_connecting():
    def exploding_factory(*a, **k):
        raise AssertionError("must not connect when there is nothing to match")
    assert poll_replies(CFG, set(), imap_factory=exploding_factory) == []


def test_poll_replies_prefers_in_reply_to_over_references_for_attribution():
    # References lists two of ours; In-Reply-To picks the specific parent.
    fake = FakeIMAP([_raw("<r@corp.com>", in_reply_to="<stage1@gmail.com>",
                          references="<init@gmail.com> <stage1@gmail.com>")])
    out = poll_replies(CFG, {"<init@gmail.com>", "<stage1@gmail.com>"},
                       imap_factory=fake.factory())
    assert out[0]["in_reply_to"] == "<stage1@gmail.com>"


def test_poll_replies_wraps_login_failure_as_imaperror():
    fake = FakeIMAP([], fail_login=True)
    with pytest.raises(ImapError):
        poll_replies(CFG, {"<init@gmail.com>"}, imap_factory=fake.factory())


def test_poll_replies_raises_on_failed_search():
    fake = FakeIMAP([_raw("<r@corp.com>", in_reply_to="<init@gmail.com>")], fail_search=True)
    with pytest.raises(ImapError):
        poll_replies(CFG, {"<init@gmail.com>"}, imap_factory=fake.factory())


def test_poll_replies_missing_date_yields_none_reply_time():
    # A reply with no parseable Date header must still be returned (just with
    # reply_time=None), never dropped or crashing the poll.
    raw = b"From: x@y.com\r\nMessage-ID: <r@corp.com>\r\nIn-Reply-To: <init@gmail.com>\r\n\r\n"
    fake = FakeIMAP([raw])
    out = poll_replies(CFG, {"<init@gmail.com>"}, imap_factory=fake.factory())
    assert out[0]["reply_time"] is None
