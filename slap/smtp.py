"""SMTP send client — replaces the GMass API client (slap/gmass.py).

Sends directly through Gmail's SMTP relay using an app password, replacing
GMass's two-call `POST /api/campaigndrafts` + `POST /api/campaigns` flow with a
single `smtplib` send. Because SMTP has no draft/send split, the two-call
idempotency dance is gone: `send_message()` is one atomic network action per
recipient, and the caller records the returned `Message-ID` — the threading +
reply-match key that replaces `gmass_campaign_id`/`gmass_draft_id` — the instant
it returns.

**Threading** (`In-Reply-To` / `References` headers) replaces GMass's
`sendAsReply`/`campaignIdToReplyTo`: a follow-up or OOO resend passes
`in_reply_to=<the recipient's initial Message-ID>` so it threads
deterministically into the same Gmail conversation, exactly as the old
`build_reply_settings(campaignIdToReplyTo=...)` did — but with a real RFC822
header instead of a GMass campaign id.

**Body is plain text** (`text/plain`). GMass's `_plain_text_to_html()` / linkify
pass existed only so its `clickTracking` had an `<a href>` to rewrite; click
tracking has no free SMTP equivalent and is intentionally dropped. Campaign
`.txt` templates stay plain text — now on the wire too.

**No recipient guard lives here.** Production sends to real leads, so — exactly
like `gmass.create_draft` before it — this module sends wherever it is told. The
self-send-only safety rule for *live* testing is enforced by the guarded
smoke-test script (`probes/`, reusing `probes/run.py`'s `_guard`), never by a
config knob on this module.
"""
from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

# Gmail's submission relay. STARTTLS on 587 (not implicit TLS on 465) — the
# path `smtplib.SMTP(...).starttls()` takes below.
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587

# The .env var holding the Gmail App Password (loaded into os.environ by
# python-dotenv). Replaces GMASS_API_KEY. A module constant (not a config knob
# yet) so slap.py and doctor.py agree on the name during the GMass->SMTP
# migration; config.yaml may later override it (step 7).
PASSWORD_ENV = "GMAIL_APP_PASSWORD"

# Every real network call this module makes is bounded by this timeout, the
# same discipline gmass.DEFAULT_TIMEOUT enforced for the HTTP client: an
# unbounded smtplib connect/login could otherwise hang a whole drain (an
# unattended launchd run has no one watching to Ctrl-C it).
DEFAULT_TIMEOUT = 30


class SmtpError(Exception):
    """Raised when an SMTP send fails (auth, connection, or transport error).

    Mirrors gmass.GMassError so the runner's existing per-recipient
    `except (…Error)` → `send_failed` event path is a near-drop-in swap."""


@dataclass
class SmtpConfig:
    """Transport credentials for the send. `user` is the Gmail account used as
    the SMTP login name (usually equal to the sender address); `password` is a
    Gmail **App Password**, not the account password. Kept as a plain dataclass
    with no config-file dependency so smtp.py stays as framework-free and
    unit-testable as gmass.py was."""
    host: str
    port: int
    user: str
    password: str
    timeout: int = DEFAULT_TIMEOUT


def build_message(*, sender: str, recipient: str, subject: str, body: str,
                  sender_name: str = None, attachment: tuple = None,
                  in_reply_to: str = None, references: str = None,
                  message_id: str = None, date: str = None) -> EmailMessage:
    """Construct the outbound EmailMessage. Split out from send_message() so
    tests (and the runner, if it ever needs the Message-ID before sending) can
    inspect the exact wire message without opening a socket.

    `message_id` defaults to a fresh RFC822 id in the sender's own domain (so a
    Gmail-relayed message carries a plausible `@gmail.com` id, not the local
    host's FQDN that `make_msgid()` would otherwise leak). `attachment`, when
    given, is `(filename, bytes, content_type)` — the same tuple shape
    `gmass.create_draft` accepted."""
    msg = EmailMessage()
    domain = sender.partition("@")[2] or None
    msg["Message-ID"] = message_id or make_msgid(domain=domain)
    msg["Date"] = date or formatdate(localtime=True)
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)  # text/plain; us-ascii or utf-8 chosen automatically

    # Threading: an initial send sets neither header; a follow-up/OOO resend
    # passes in_reply_to (the initial Message-ID). References chains the whole
    # thread — if the caller doesn't supply an explicit chain, seed it with the
    # message we're replying to, which is enough for Gmail to thread correctly.
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    elif references:
        msg["References"] = references

    if attachment:
        fname, fbytes, ctype = attachment
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(fbytes, maintype=maintype or "application",
                           subtype=subtype or "octet-stream", filename=fname)
    return msg


def send_message(smtp_config: SmtpConfig, *, sender: str, recipient: str,
                 subject: str, body: str, sender_name: str = None,
                 attachment: tuple = None, in_reply_to: str = None,
                 references: str = None, smtp_factory=smtplib.SMTP) -> dict:
    """Send one email over SMTP and return `{"message_id": "<...>", "raw": {…}}`.

    The returned `message_id` is the value the caller MUST persist (via
    `slap.tracking.append_event(..., message_id=...)`) to thread later
    follow-ups and to match inbound replies over IMAP — it replaces the
    `gmass_campaign_id`/`gmass_draft_id` a GMass send used to return.

    `smtp_factory` is a dependency-injection seam (matching the runner's
    `create_draft_fn=`/`sleep_fn=` style): production leaves it as
    `smtplib.SMTP`; tests pass a fake exposing `starttls`/`login`/`send_message`
    so no socket is ever opened.

    Fail-loud: any `smtplib`/socket error is re-raised as `SmtpError` with the
    recipient in context, so one recipient's failure surfaces as a
    `send_failed` event and the drain moves on — never a silent drop."""
    msg = build_message(sender=sender, recipient=recipient, subject=subject,
                        body=body, sender_name=sender_name, attachment=attachment,
                        in_reply_to=in_reply_to, references=references)
    message_id = msg["Message-ID"]
    try:
        with smtp_factory(smtp_config.host, smtp_config.port,
                          timeout=smtp_config.timeout) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(smtp_config.user, smtp_config.password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise SmtpError(f"SMTP send to {recipient!r} failed: {e}") from e
    return {"message_id": message_id,
            "raw": {"recipient": recipient, "subject": subject}}
