"""IMAP reply-detection client — replaces GMass's reply reports.

Under GMass, replies were detected server-side and polled back from
`GET /api/reports/{id}/replies`. With direct SMTP sending there is no such
service, so the app must detect replies itself. `poll_replies()` connects to
the sender's Gmail over IMAP, scans a bounded window of recent inbox messages,
and returns those whose `In-Reply-To`/`References` headers point at one of our
own sent Message-IDs (slap.smtp records every send's Message-ID on its
`sent`/`requeued`/`interaction` event).

**CRITICAL (see CONTROL_SHEET.md / the migration plan): this poll must run
inside the runner's drain, BEFORE follow-ups are selected — not only on
dashboard open.** The runner fires unattended (launchd) when no dashboard is
open, so this poll is the ONLY thing that stops a follow-up going to someone
who already replied. Enforcing stop-on-reply at *fire time* (not display time)
is the whole point of doing reply detection here.

Accepted regression vs. GMass: GMass silently filtered auto-responders/OOO
bounces server-side. Over raw IMAP we do not — a genuine reply and an
auto-reply both thread onto our sent message. The owner's existing manual
OOO-tagging in the dashboard is the safety net for the rare auto-reply that
slips through (see slap.dashboard.tag_reply / §7).

No recipient guard lives here — reads only, and only the owner's own inbox.
The credential is the same Gmail App Password used for SMTP (Gmail enables IMAP
under the same app password).
"""
from __future__ import annotations

import email
import imaplib
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_TIMEOUT = 30

# How many of the most-recent inbox messages to scan per poll. A reply to a
# cold-outreach email lands within days, so a bounded recent window keeps each
# poll fast regardless of total mailbox size — we never need to walk years of
# history to catch a reply to something we sent this week.
DEFAULT_SCAN_LIMIT = 300


class ImapError(Exception):
    """Raised when the IMAP reply poll fails (auth, connection, protocol)."""


@dataclass
class ImapConfig:
    """IMAP read credentials. `user`/`password` are the same Gmail account +
    App Password used for SMTP (Gmail accepts the app password for IMAP too).
    Kept framework-free (no config-file dependency) so imap.py stays as
    unit-testable as smtp.py."""
    host: str
    port: int
    user: str
    password: str
    timeout: int = DEFAULT_TIMEOUT
    mailbox: str = "INBOX"


def _extract_referenced_ids(msg) -> set:
    """Every Message-ID an inbound message threads onto: its `In-Reply-To` plus
    every id in `References`. Only angle-bracketed `<...>` tokens are kept,
    matching how slap.smtp stores sent Message-IDs (email.utils.make_msgid
    always emits `<...>`), so matching is exact and never partial."""
    ids = set()
    for header in ("In-Reply-To", "References"):
        raw = msg.get(header)
        if not raw:
            continue
        for token in raw.split():
            token = token.strip()
            if token.startswith("<") and token.endswith(">"):
                ids.add(token)
    return ids


def _parse_date(date_hdr) -> str | None:
    """A message's Date header as an ISO-8601 string, or None when absent or
    unparseable (never raises — a malformed Date must not drop the message)."""
    if not date_hdr:
        return None
    try:
        return parsedate_to_datetime(date_hdr).isoformat()
    except (TypeError, ValueError):
        return None


def _scan_recent(imap_config: ImapConfig, scan_limit: int, imap_factory):
    """Connect, yield the most-recent `scan_limit` messages' parsed headers
    (newest first), then always release the connection. Shared by poll_replies
    and poll_bounces so both hit the mailbox with one identical, bounded,
    headers-only sweep. Raises `ImapError` on any connection/auth/protocol
    failure; `imap_factory` is the test seam (an `imaplib.IMAP4`-shaped object)."""
    try:
        server = imap_factory(imap_config.host, imap_config.port, timeout=imap_config.timeout)
        try:
            server.login(imap_config.user, imap_config.password)
            server.select(imap_config.mailbox)
            typ, data = server.search(None, "ALL")
            if typ != "OK":
                raise ImapError(f"IMAP search failed: {typ!r}")
            seq_nums = data[0].split()
            # search() returns ascending sequence numbers; scan only the most
            # recent window, newest first.
            for num in reversed(seq_nums[-scan_limit:]):
                typ, msg_data = server.fetch(num, "(BODY.PEEK[HEADER])")
                if typ != "OK" or not msg_data or msg_data[0] is None:
                    continue
                yield email.message_from_bytes(msg_data[0][1])
        finally:
            try:
                server.logout()
            except Exception:
                pass  # a logout failure never masks the real result/error
    except (imaplib.IMAP4.error, OSError) as e:
        raise ImapError(f"IMAP poll failed: {e}") from e


def poll_replies(imap_config: ImapConfig, sent_message_ids, *,
                 scan_limit: int = DEFAULT_SCAN_LIMIT,
                 imap_factory=imaplib.IMAP4_SSL) -> list:
    """Return inbound replies to any of `sent_message_ids`. Each item is a dict:
    `{"in_reply_to": <the sent id replied to>, "reply_message_id": <incoming id>,
      "from": <From header>, "reply_time": <ISO-8601 str or None>}`.

    `sent_message_ids` is the set of every Message-ID this app has ever sent
    (initial + follow-ups + OOO resends + reminds). The downstream dedup key is
    `reply_message_id` (the incoming Message-ID), mirroring how GMass's replyId
    was deduped in the events log.

    An empty `sent_message_ids` short-circuits with no network connection (there
    is nothing to match against). Fail-loud: connection/auth/protocol errors
    raise `ImapError`; the caller decides whether that's fatal or transient."""
    sent = set(sent_message_ids)
    if not sent:
        return []

    replies = []
    for msg in _scan_recent(imap_config, scan_limit, imap_factory):
        hit = _extract_referenced_ids(msg) & sent
        if not hit:
            continue
        # A reply usually References the whole thread; attribute it to the most
        # specific parent (its In-Reply-To when that is one of ours), else any
        # matched id deterministically.
        irt = (msg.get("In-Reply-To") or "").strip()
        in_reply_to = irt if irt in sent else sorted(hit)[0]
        replies.append({
            "in_reply_to": in_reply_to,
            "reply_message_id": (msg.get("Message-ID") or "").strip() or None,
            "from": msg.get("From"),
            "reply_time": _parse_date(msg.get("Date")),
        })
    return replies


def _is_dsn(msg) -> bool:
    """True if a message looks like a delivery-status notification (a bounce):
    a `multipart/report` container, or a From of mailer-daemon/postmaster. Read
    from headers alone (no body fetch)."""
    ctype = (msg.get_content_type() or "").lower()
    frm = (msg.get("From") or "").lower()
    return ctype == "multipart/report" or "mailer-daemon" in frm or "postmaster" in frm


def poll_bounces(imap_config: ImapConfig, recipients, *,
                 scan_limit: int = DEFAULT_SCAN_LIMIT,
                 imap_factory=imaplib.IMAP4_SSL) -> list:
    """Return bounce (DSN) notifications for any of `recipients`. Each item:
    `{"recipient": <failed address, lowercased>, "bounce_reason": <subject>,
      "bounce_message_id": <the DSN's own Message-ID — the dedup key>,
      "bounce_time": <ISO-8601 str or None>}`.

    This restores the "a hard bounce stops the sequence" behaviour GMass's bounce
    report provided and which plain SMTP otherwise loses: Gmail accepts a message
    for an invalid address at submission time and bounces it back asynchronously
    as a DSN, so without this a dead address keeps receiving every follow-up
    stage (wasted cap + a real sender-reputation risk). runner.ingest_bounces
    turns each match into a `bounce` event, flipping the recipient to
    status='bounced' and out of every active-only query.

    Detection is header-only (fast, no body fetch): a message is a bounce when
    `_is_dsn` matches AND its `X-Failed-Recipients` header (which Gmail's own
    mailer-daemon adds) names one of OUR `recipients` — requiring the address to
    be one we actually contacted keeps an unrelated inbox DSN from ever marking
    someone bounced. A DSN whose failed address isn't in `recipients`, or that
    lacks `X-Failed-Recipients` entirely, is skipped (conservative: we never
    fabricate a bounce). Empty `recipients` short-circuits with no connection."""
    wanted = {r.strip().lower() for r in recipients if r and r.strip()}
    if not wanted:
        return []

    bounces = []
    for msg in _scan_recent(imap_config, scan_limit, imap_factory):
        if not _is_dsn(msg):
            continue
        failed_raw = msg.get("X-Failed-Recipients")
        if not failed_raw:
            continue
        dsn_id = (msg.get("Message-ID") or "").strip() or None
        reason = (msg.get("Subject") or "SMTP delivery failure (DSN)").strip()
        bounce_time = _parse_date(msg.get("Date"))
        for addr in (a.strip().lower() for a in failed_raw.split(",")):
            if addr and addr in wanted:
                bounces.append({
                    "recipient": addr,
                    "bounce_reason": reason,
                    "bounce_message_id": dsn_id,
                    "bounce_time": bounce_time,
                })
    return bounces
