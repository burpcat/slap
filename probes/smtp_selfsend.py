#!/usr/bin/env python3
"""Guarded live SMTP + IMAP smoke test — the ONLY sanctioned way to send a REAL
email from this app during verification of the SMTP/IMAP migration.

SAFETY (non-negotiable, not a flag, not overridable): `send` refuses any
recipient that is not an owner Gmail plus-tag (``local+testmassN@domain``,
derived from config.yaml's ``sender.from_email``), raising BEFORE any network
call. This mirrors the guard the deleted GMass Phase-0 probes used — a clone
with a different owner's config is automatically guarded to THEIR address.

Requires ``GMAIL_APP_PASSWORD`` in the environment (loaded from .env). Usage:

  python probes/smtp_selfsend.py send [N]       # send an initial to +testmass{N} (default 1)
  python probes/smtp_selfsend.py send-followup  # send a threaded follow-up into that thread
  python probes/smtp_selfsend.py poll-replies   # IMAP-poll for a reply to any message we sent
  python probes/smtp_selfsend.py poll-bounces   # IMAP-poll (read-only) for bounce DSNs

Verify the whole loop end to end:
  1. `send`               → a real email lands in your own +testmass inbox.
  2. `send-followup`      → a follow-up lands IN THE SAME Gmail conversation.
                            Open it, "Show original", and confirm the follow-up's
                            In-Reply-To + References point at the earlier message(s)
                            — exactly what runner._send_followup emits in production.
  3. reply to the thread from Gmail, then `poll-replies` → reports the reply
                            detected, i.e. stop-on-reply would fire.
This exercises the real slap.smtp + slap.imap code paths the unit tests can only
mock. State (the accumulated Message-ID chain) is written to
probes/.smoke_state.json, so send-followup threads exactly as production does.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from slap import imap, smtp  # noqa: E402
from slap.config import ConfigError, load_global_config  # noqa: E402

STATE = Path(__file__).resolve().parent / ".smoke_state.json"


def _owner_parts():
    """(global_config, local, domain) from config.yaml's sender.from_email —
    fail loud, never fall back to a default address (the guard must always
    follow whoever owns this install)."""
    try:
        gc = load_global_config()
    except ConfigError as e:
        sys.exit(f"FAIL: could not load config.yaml to build the safety guard — {e}")
    local, sep, domain = gc.from_email.partition("@")
    if not sep or not local or not domain:
        sys.exit(f"FAIL: sender.from_email={gc.from_email!r} is not a valid email address.")
    return gc, local, domain


def _guard(recipient, local, domain):
    """Raise before any network call if `recipient` is not an owner self-test
    address. THE safety boundary — do not weaken."""
    if not re.match(rf"^{re.escape(local)}\+testmass\d+@{re.escape(domain)}$", recipient or ""):
        raise RuntimeError(
            f"SAFETY GUARD: refusing to target {recipient!r}. This smoke test may only "
            f"send to {local}+testmass{{N}}@{domain}. No network call was made."
        )
    return recipient


def _creds(gc):
    pw = os.environ.get(smtp.PASSWORD_ENV, "").strip()
    if not pw:
        sys.exit(f"FAIL: {smtp.PASSWORD_ENV} not set — put your Gmail App Password in .env.")
    smtp_cfg = smtp.SmtpConfig(smtp.DEFAULT_SMTP_HOST, smtp.DEFAULT_SMTP_PORT, gc.from_email, pw)
    imap_cfg = imap.ImapConfig(imap.DEFAULT_IMAP_HOST, imap.DEFAULT_IMAP_PORT, gc.from_email, pw)
    return smtp_cfg, imap_cfg


def cmd_send(n):
    load_dotenv()
    gc, local, domain = _owner_parts()
    recipient = _guard(f"{local}+testmass{n}@{domain}", local, domain)  # guard BEFORE creds/network
    smtp_cfg, _ = _creds(gc)
    result = smtp.send_message(
        smtp_cfg, sender=gc.from_email, sender_name=gc.from_name, recipient=recipient,
        subject="slap SMTP smoke test",
        body=("This is a live SMTP smoke test from slap. Reply to this email to verify "
              "IMAP reply detection.\n\n" + (gc.signature or "")),
    )
    STATE.write_text(json.dumps({"recipient": recipient, "subject": "slap SMTP smoke test",
                                 "chain": [result["message_id"]]}))
    print(f"SENT to {recipient}")
    print(f"  Message-ID: {result['message_id']}")
    print("  Next: python probes/smtp_selfsend.py send-followup   (threads a reply into this)")


def cmd_send_followup():
    load_dotenv()
    gc, local, domain = _owner_parts()
    if not STATE.exists():
        sys.exit("FAIL: no prior send recorded — run `send` first.")
    st = json.loads(STATE.read_text())
    recipient = _guard(st["recipient"], local, domain)  # guard the saved recipient too
    chain = st["chain"]
    smtp_cfg, _ = _creds(gc)
    # Thread exactly as runner._send_followup does: In-Reply-To = the latest
    # message, References = the whole accumulated chain, "Re:" the subject.
    in_reply_to = chain[-1]
    references = " ".join(chain)
    result = smtp.send_message(
        smtp_cfg, sender=gc.from_email, sender_name=gc.from_name, recipient=recipient,
        subject=f"Re: {st['subject']}", in_reply_to=in_reply_to, references=references,
        body="Threaded follow-up from slap's smoke test — this should land in the SAME "
             "Gmail conversation as the initial send.\n\n" + (gc.signature or ""),
    )
    st["chain"].append(result["message_id"])
    STATE.write_text(json.dumps(st))
    print(f"SENT FOLLOW-UP to {recipient} (should thread into the existing conversation)")
    print(f"  In-Reply-To: {in_reply_to}")
    print(f"  References:  {references}")
    print(f"  Message-ID:  {result['message_id']}")
    print("  Check Gmail: the follow-up should appear inside the original thread. "
          "'Show original' to confirm the headers.")


def cmd_poll_replies():
    load_dotenv()
    gc, *_ = _owner_parts()
    _, imap_cfg = _creds(gc)
    if not STATE.exists():
        sys.exit("FAIL: no prior send recorded — run `send` first.")
    st = json.loads(STATE.read_text())
    replies = imap.poll_replies(imap_cfg, set(st["chain"]))  # a reply to ANY message we sent
    if replies:
        print(f"REPLY DETECTED to {st['recipient']}:")
        for r in replies:
            print(f"  from={r['from']}  reply_id={r['reply_message_id']}  time={r['reply_time']}")
        print("  -> stop-on-reply would fire: this recipient flips to status='replied' and "
              "receives no further follow-ups.")
    else:
        print(f"No reply detected yet for the thread {st['chain']}.")
        print("  Reply to the test email from Gmail, then re-run (IMAP can lag a few seconds).")


def cmd_poll_bounces():
    load_dotenv()
    gc, local, domain = _owner_parts()
    _, imap_cfg = _creds(gc)
    # Read-only scan for DSNs affecting the test addresses. Self-sends never
    # bounce, so this normally prints nothing — it's here to exercise the same
    # read path ingest_bounces uses, against a real inbox, with zero risk.
    test_addrs = {f"{local}+testmass{i}@{domain}" for i in range(1, 20)}
    bounces = imap.poll_bounces(imap_cfg, test_addrs)
    if bounces:
        for b in bounces:
            print(f"BOUNCE: {b['recipient']} — {b['bounce_reason']} ({b['bounce_time']})")
    else:
        print("No bounce DSNs found for the test addresses (expected — self-sends don't bounce).")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "send":
        cmd_send(sys.argv[2] if len(sys.argv) > 2 else "1")
    elif cmd == "send-followup":
        cmd_send_followup()
    elif cmd == "poll-replies":
        cmd_poll_replies()
    elif cmd == "poll-bounces":
        cmd_poll_bounces()
    else:
        sys.exit(f"unknown command {cmd!r}\n{__doc__}")


if __name__ == "__main__":
    main()
