"""SMTP client tests — fast, no network calls (a fake smtp_factory is injected).

Replaces the GMass client tests for the send path. Genuine live verification of
this production code (a real relayed self-send) lives in the guarded smoke-test
under probes/, run manually against an owner +testmass address — see
CONTROL_SHEET.md.
"""
import pytest

from slap.smtp import (
    DEFAULT_SMTP_HOST, DEFAULT_SMTP_PORT, SmtpConfig, SmtpError,
    build_message, send_message,
)

CONFIG = SmtpConfig(host=DEFAULT_SMTP_HOST, port=DEFAULT_SMTP_PORT,
                    user="me@gmail.com", password="app-pw")


class FakeSMTP:
    """Minimal stand-in for smtplib.SMTP as a context manager. Records the
    connect args and every call so tests can assert the starttls→login→send
    order without opening a socket."""
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        self.calls.append(("__enter__",))
        return self

    def __exit__(self, *exc):
        self.calls.append(("__exit__",))
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls", context is not None))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append(("send_message",))
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeSMTP.instances = []
    yield


# --- build_message ------------------------------------------------------

def test_build_message_sets_core_headers_and_plaintext_body():
    msg = build_message(sender="me@gmail.com", sender_name="Me",
                        recipient="lead@corp.com", subject="Hello", body="Hi there")
    assert msg["From"] == "Me <me@gmail.com>"
    assert msg["To"] == "lead@corp.com"
    assert msg["Subject"] == "Hello"
    # Plain text on the wire — no HTML part (the linkify/html pass is gone).
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content().strip() == "Hi there"


def test_build_message_generates_message_id_in_sender_domain():
    msg = build_message(sender="me@gmail.com", recipient="x@y.com",
                        subject="s", body="b")
    mid = msg["Message-ID"]
    assert mid.startswith("<") and mid.endswith(">")
    # Domain of the generated id is the sender's own domain, not the local FQDN.
    assert mid.endswith("@gmail.com>")


def test_build_message_without_sender_name_uses_bare_address():
    msg = build_message(sender="me@gmail.com", recipient="x@y.com",
                        subject="s", body="b")
    assert msg["From"] == "me@gmail.com"


def test_build_message_initial_send_has_no_threading_headers():
    msg = build_message(sender="me@gmail.com", recipient="x@y.com",
                        subject="s", body="b")
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None


def test_build_message_followup_threads_via_in_reply_to():
    parent = "<initial-abc@gmail.com>"
    msg = build_message(sender="me@gmail.com", recipient="x@y.com",
                        subject="s", body="b", in_reply_to=parent)
    assert msg["In-Reply-To"] == parent
    # References is seeded from the parent when no explicit chain is given.
    assert msg["References"] == parent


def test_build_message_followup_preserves_explicit_references_chain():
    chain = "<a@gmail.com> <b@gmail.com>"
    msg = build_message(sender="me@gmail.com", recipient="x@y.com", subject="s",
                        body="b", in_reply_to="<b@gmail.com>", references=chain)
    assert msg["References"] == chain
    assert msg["In-Reply-To"] == "<b@gmail.com>"


def test_build_message_encodes_attachment():
    msg = build_message(sender="me@gmail.com", recipient="x@y.com", subject="s",
                        body="b", attachment=("resume.pdf", b"%PDF-1.7 bytes",
                                              "application/pdf"))
    parts = list(msg.iter_attachments())
    assert len(parts) == 1
    att = parts[0]
    assert att.get_filename() == "resume.pdf"
    assert att.get_content_type() == "application/pdf"
    assert att.get_payload(decode=True) == b"%PDF-1.7 bytes"


def test_build_message_uses_supplied_message_id():
    msg = build_message(sender="me@gmail.com", recipient="x@y.com", subject="s",
                        body="b", message_id="<fixed-id@gmail.com>")
    assert msg["Message-ID"] == "<fixed-id@gmail.com>"


# --- send_message -------------------------------------------------------

def test_send_message_performs_starttls_login_send_in_order():
    result = send_message(CONFIG, sender="me@gmail.com", recipient="lead@corp.com",
                          subject="Hi", body="Body", smtp_factory=FakeSMTP)
    server = FakeSMTP.instances[0]
    assert server.host == DEFAULT_SMTP_HOST and server.port == DEFAULT_SMTP_PORT
    ordered = [c[0] for c in server.calls]
    assert ordered == ["__enter__", "starttls", "login", "send_message", "__exit__"]
    # starttls was handed a real SSL context.
    assert ("starttls", True) in server.calls
    assert ("login", "me@gmail.com", "app-pw") in server.calls


def test_send_message_returns_the_wire_message_id():
    result = send_message(CONFIG, sender="me@gmail.com", recipient="lead@corp.com",
                          subject="Hi", body="Body", smtp_factory=FakeSMTP)
    sent = FakeSMTP.instances[0].sent[0]
    assert result["message_id"] == sent["Message-ID"]
    assert result["message_id"].endswith("@gmail.com>")
    assert result["raw"]["recipient"] == "lead@corp.com"


def test_send_message_threads_followup_into_parent():
    send_message(CONFIG, sender="me@gmail.com", recipient="lead@corp.com",
                 subject="Re: Hi", body="Follow-up", in_reply_to="<init@gmail.com>",
                 smtp_factory=FakeSMTP)
    sent = FakeSMTP.instances[0].sent[0]
    assert sent["In-Reply-To"] == "<init@gmail.com>"
    assert sent["References"] == "<init@gmail.com>"


def test_send_message_wraps_smtp_failure_as_smtperror():
    import smtplib

    def boom_factory(*a, **k):
        raise smtplib.SMTPAuthenticationError(535, b"bad app password")

    with pytest.raises(SmtpError) as exc:
        send_message(CONFIG, sender="me@gmail.com", recipient="lead@corp.com",
                     subject="Hi", body="Body", smtp_factory=boom_factory)
    assert "lead@corp.com" in str(exc.value)


def test_send_message_wraps_os_error_as_smtperror():
    def boom_factory(*a, **k):
        raise OSError("connection refused")

    with pytest.raises(SmtpError):
        send_message(CONFIG, sender="me@gmail.com", recipient="lead@corp.com",
                     subject="Hi", body="Body", smtp_factory=boom_factory)
