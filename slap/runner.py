"""Queue drain + unattended runner (Build Order step 9).

Split prep (interactive, `send`) from fire (unattended, this module) per
§10. The runner is stateless — it asks the DB "what's queued and due?"
(slap.queue.due_recipients) and drains. No separate queue store.

Design decisions the brief leaves implicit (documented here and in
CONTROL_SHEET.md):

- `drain_retries` applies to PREFLIGHT failures specifically, not per-email
  sends — §11 is explicit ("a preflight failure → retry per drain_retries,
  then run_failed"). A per-email failure just writes `send_failed` and moves
  on to the next recipient (no immediate retry); it's naturally retried by
  the next scheduled drain or a manual `--now`.
- The fire-window (`fire_window_start`/`end`) is interpreted in LOCAL time,
  not UTC — it's a human scheduling preference ("send around 9am"), unlike
  event-log timestamps which are always UTC (§5). launchd's
  StartCalendarInterval also fires in local system time.
- "Counting follow-ups firing today" for cap headroom is a best-effort
  ESTIMATE: GMass fires stages 2/3 server-side with no API to ask "what
  fires today," so this estimates each active, already-sent recipient's next
  stage date as cumulative persona-cadence days from `first_sent_at`. Real
  GMass timing (time-of-day, `skipWeekends`, etc.) can differ.
- Preflight here (step 12) runs doctor's GLOBAL checks only (API key, sender
  fields, DB reachable, consumer_domains.txt present-or-seeded) — NOT the
  per-campaign attachment/xelatex/code checks. By the time a recipient is
  queued, its campaign already passed those at `send` time and its
  attachment bytes are already baked into that recipient's staged.json; a
  drain batch can also span multiple campaigns, so there's no single
  "current campaign" to re-check anyway.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from datetime import time as dt_time
from datetime import timedelta
from pathlib import Path

from slap import doctor, imap, smtp, stages
from slap.latex import WORKDIR_ROOT, recipient_workdir
from slap.queue import (
    due_for_followup, due_for_ooo_resend, due_for_remind, due_recipients, load_manifest,
)
from slap.tracking import append_event


class RunnerError(Exception):
    """Raised on fail-loud runner misuse."""


# date.weekday(): 0=Monday ... 6=Sunday — matches slap.config.VALID_DAYS' spelling.
_WEEKDAY_ABBR = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def is_active_day(schedule, *, today: date = None) -> bool:
    """Guard for the UNATTENDED runner only (§new: configurable scheduler
    days) — not applied to a manual `send --now`, which is an explicit human
    action that should never be silently skipped by a scheduling preference.
    Correct even if the launchd plist and config.yaml's active_days have
    drifted out of sync (e.g. active_days edited without regenerating/
    reloading the plist) — this is the second line of defense; the plist
    generator (slap/launchd.py) is the first, since it only ever emits
    StartCalendarInterval entries for days config.yaml actually lists. Local
    calendar day, matching the fire-window's own local-time interpretation
    (see module docstring)."""
    today = today or date.today()
    return _WEEKDAY_ABBR[today.weekday()] in schedule.active_days


def next_fire_moment(schedule, *, now: datetime = None) -> datetime:
    """Next LOCAL datetime the unattended runner is expected to drain: today at
    fire_window_start if today is an active day and that time hasn't yet passed,
    otherwise the first active day after today, at fire_window_start. Walks
    forward day-by-day reusing is_active_day — no launchd/plist coupling.

    Used to estimate a queued (not-yet-sent) recipient's "next shoot" on the
    Reach-outs page; like every other timing estimate in this module it's
    best-effort (a Mac asleep through the window fires on wake, the send moment
    is randomized within the window), never a promise. Local time throughout,
    matching is_active_day / the fire window's own local-time interpretation."""
    now = now or datetime.now()
    start_h, start_m = (int(x) for x in schedule.fire_window_start.split(":"))
    today = now.date()
    todays_fire = datetime.combine(today, dt_time(start_h, start_m))
    if is_active_day(schedule, today=today) and now <= todays_fire:
        return todays_fire
    # Walk forward to the next active day. Bounded at a full week so a
    # misconfigured (empty) active_days can never spin forever — config
    # validation already requires it non-empty, this is just belt-and-braces.
    d = today
    for _ in range(7):
        d += timedelta(days=1)
        if is_active_day(schedule, today=d):
            break
    return datetime.combine(d, dt_time(start_h, start_m))


def last_run_event_at(conn) -> datetime | None:
    """Local-time timestamp of the most recent run_started/run_completed/
    run_failed event, or None if the runner has never fired at all (a fresh
    install — not staleness, just nothing yet to compare against)."""
    row = conn.execute(
        "SELECT timestamp FROM events WHERE type IN "
        "('run_started', 'run_completed', 'run_failed') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    ts = datetime.fromisoformat(row["timestamp"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone()


def staleness_warning(conn, schedule, *, today: date = None, now: datetime = None) -> str | None:
    """Detects a SILENT runner failure — e.g. launchd itself failing to spawn
    the job (macOS EX_CONFIG), which writes zero events at all, unlike an
    in-app preflight/send failure that at least logs `run_failed` (already
    surfaced by the dashboard's "today's runs" panel). Found via a real
    incident: the launchd LaunchAgent stopped firing for a week with no
    run_failed event anywhere, so the queue silently piled up with nothing
    on the dashboard calling it out.

    Walks forward, in LOCAL time (matching is_active_day/the fire window's
    own local-time interpretation), from the day after the last recorded run
    event to today. If any active day in that span has already closed its
    fire window with still no newer run event, the runner has gone silent.
    Returns None when healthy, or when the runner has never fired yet."""
    today = today or date.today()
    now = now or datetime.now().astimezone()
    last_at = last_run_event_at(conn)
    if last_at is None:
        return None
    last_date = last_at.date()
    d = last_date + timedelta(days=1)
    while d <= today:
        if is_active_day(schedule, today=d):
            end_h, end_m = (int(x) for x in schedule.fire_window_end.split(":"))
            window_closed = d < today or now.time() >= dt_time(end_h, end_m)
            if window_closed:
                days_late = (today - last_date).days
                plural = "s" if days_late != 1 else ""
                return (f"No runner activity since {last_date.isoformat()} ({days_late} day{plural} ago) — "
                        f"expected a drain on {d.isoformat()}. The launchd job may have silently stopped "
                        f"firing; check `launchctl print gui/<uid>/com.slap.runner` for a stuck/failed job.")
        d += timedelta(days=1)
    return None


@dataclass
class DrainResult:
    ran: bool                  # False if preflight failed and nothing ran at all
    sent: int = 0
    failed: int = 0
    remaining_queued: int = 0
    preflight_error: str = None


def _preflight(global_config, conn=None) -> str:
    """Doctor's global checks (step 12) — API key, sender fields, DB
    reachable, consumer_domains.txt present-or-seeded. Returns a combined
    error string, or None if every check passed. `conn`, when given, is
    reused for the DB-reachable check instead of opening a second
    connection at the default cwd-relative path (see doctor.check_db).

    One check has a real side effect (check_consumer_domains seeds a
    missing file) that can raise (e.g. an unwritable/missing parent dir for
    a customized consumer_domains_file) — that must still degrade to a
    normal preflight failure string, never an uncaught exception, so §11's
    "retry then run_failed, queue intact" guarantee holds unconditionally."""
    try:
        failures = [r for r in doctor.run_global_checks(global_config, conn) if not r.ok]
    except Exception as e:
        return f"unexpected preflight error: {e}"
    if not failures:
        return None
    return "; ".join(f"{r.name}: {r.detail}" for r in failures)


def _preflight_with_retries(global_config, conn, drain_retries: int, sleep_fn) -> str:
    error = _preflight(global_config, conn)
    attempts = 1
    while error is not None and attempts < drain_retries:
        sleep_fn(2)
        error = _preflight(global_config, conn)
        attempts += 1
    return error


def todays_sent_count(conn, today: date) -> int:
    """Every real send that fired today — both initial/follow-up sends
    (`sent`) and OOO resends (`requeued`), since both consume the same
    Gmail daily-send ceiling. Public: also used by slap.dashboard's
    "sent today" panel, which needs this exact same count to stay
    consistent with what the cap actually enforces."""
    rows = conn.execute(
        "SELECT timestamp FROM events WHERE type IN ('sent', 'requeued')"
    ).fetchall()
    return sum(1 for r in rows if datetime.fromisoformat(r["timestamp"]).date() == today)


def _estimate_followups_firing_today(conn, global_config, today: date) -> int:
    """Best-effort estimate only — see module docstring.

    Prefers each recipient's own recorded `cadence` (set at stage time via
    `slap.queue.stage_recipient`, possibly truncated from the persona's full
    default by a per-send override — see slap.tracking's module docstring)
    over the persona lookup; a `queued` event written before that column
    existed leaves it NULL, so this falls back to the persona default exactly
    like before for older recipients."""
    rows = conn.execute(
        "SELECT persona, current_stage, first_sent_at, cadence FROM recipients "
        "WHERE status = 'active' AND first_sent_at IS NOT NULL"
    ).fetchall()
    count = 0
    for row in rows:
        cadence = json.loads(row["cadence"]) if row["cadence"] else global_config.personas.get(row["persona"])
        if not cadence:
            continue
        next_stage = row["current_stage"] + 1
        if next_stage > len(cadence):
            continue  # sequence already exhausted
        fire_date = stages.stage_fire_date(
            datetime.fromisoformat(row["first_sent_at"]).date(), cadence, next_stage)
        if fire_date == today:
            count += 1
    return count


def cap_headroom(conn, global_config, *, today: date = None) -> int:
    today = today or date.today()
    used = todays_sent_count(conn, today) + _estimate_followups_firing_today(conn, global_config, today)
    return max(0, global_config.schedule.daily_cap - used)


def _sent_message_id_owners(conn) -> dict:
    """Map every Message-ID this app has sent -> (recipient, campaign), so a
    matched inbound reply can be attributed to the recipient it replied to.
    Covers every event type that records a message_id: initial/follow-up
    `sent`, OOO `requeued`, and remind `interaction`. A later send's id
    overwrites an earlier one for the same recipient, which is fine — the map
    is keyed by the unique per-message id, and all of a recipient's ids resolve
    back to that same recipient."""
    owners = {}
    for row in conn.execute(
        "SELECT recipient, campaign, message_id FROM events "
        "WHERE message_id IS NOT NULL AND type IN ('sent', 'requeued', 'interaction')"
    ):
        owners[row["message_id"]] = (row["recipient"], row["campaign"])
    return owners


def _existing_reply_ids(conn) -> set:
    """Every inbound reply Message-ID already recorded (dedup key), read from
    `reply` events' meta — the same dedup discipline dashboard._sync_replies
    used with GMass's replyId."""
    seen = set()
    for row in conn.execute("SELECT meta FROM events WHERE type = 'reply'"):
        meta = json.loads(row["meta"]) if row["meta"] else {}
        rid = meta.get("reply_id")
        if rid:
            seen.add(rid)
    return seen


def ingest_replies(conn, imap_config, *, poll_replies_fn=imap.poll_replies) -> int:
    """Poll IMAP for replies to any of our sent messages and append a `reply`
    event for each new one (deduped by the inbound Message-ID). Returns the
    count of new replies written.

    This is the SMTP replacement for GMass's server-side reply reports. It is
    called at the START of a drain — BEFORE follow-ups are selected — so
    stop-on-reply is enforced at *fire time*: a recipient whose reply is
    ingested here flips to status 'replied' (the `reply` cache handler), which
    removes them from slap.queue.due_for_followup. The dashboard's on-open poll
    calls it too, so replies also surface there. Raises imap.ImapError on a
    connection/auth failure (the caller decides whether that's fatal)."""
    owners = _sent_message_id_owners(conn)
    if not owners:
        return 0
    replies = poll_replies_fn(imap_config, set(owners))
    seen = _existing_reply_ids(conn)
    new = 0
    for r in replies:
        reply_id = r.get("reply_message_id")
        if not reply_id or reply_id in seen:
            continue
        owner = owners.get(r.get("in_reply_to"))
        if owner is None:
            continue
        recipient, campaign = owner
        stage_row = conn.execute(
            "SELECT current_stage FROM recipients WHERE recipient = ?", (recipient,)
        ).fetchone()
        stage = stage_row["current_stage"] if stage_row else None
        append_event(conn, type="reply", recipient=recipient, campaign=campaign, stage=stage,
                     meta={"reply_id": reply_id, "reply_time": r.get("reply_time")})
        seen.add(reply_id)
        new += 1
    return new


def _sent_recipient_campaigns(conn) -> dict:
    """lowercased recipient -> (original-cased recipient, campaign) for every
    recipient in the cache, so a matched bounce DSN can be attributed and carry
    its campaign on the `bounce` event. Lowercased key because a DSN's
    X-Failed-Recipients casing needn't match how we stored the address."""
    owners = {}
    for row in conn.execute("SELECT recipient, campaign FROM recipients WHERE recipient IS NOT NULL"):
        owners[row["recipient"].lower()] = (row["recipient"], row["campaign"])
    return owners


def ingest_bounces(conn, imap_config, *, poll_bounces_fn=imap.poll_bounces) -> int:
    """Poll IMAP for delivery-failure DSNs and append a `bounce` event for each
    new one (which flips the recipient to status='bounced', removing them from
    every active-only query — no more follow-ups to a dead address). Returns the
    count of new bounces written.

    This restores the stop-on-bounce behaviour GMass's bounce report gave and
    that plain SMTP loses: Gmail accepts a message for an invalid address then
    bounces it back asynchronously, so without this a hard-bounced recipient
    keeps getting every stage (wasted cap + sender-reputation risk). Deduped so
    a recipient is marked bounced at most once: by the DSN's own Message-ID when
    present, and — belt-and-suspenders for a DSN lacking one — by the recipient
    already having a bounce event. Called from the drain (before follow-ups) so a
    bounce stops the sequence the same drain it's detected."""
    owners = _sent_recipient_campaigns(conn)
    if not owners:
        return 0
    bounces = poll_bounces_fn(imap_config, set(owners))
    seen_dsn = set()
    already_bounced = set()
    for row in conn.execute("SELECT recipient, meta FROM events WHERE type = 'bounce'"):
        if row["recipient"]:
            already_bounced.add(row["recipient"])
        meta = json.loads(row["meta"]) if row["meta"] else {}
        if meta.get("dsn_message_id"):
            seen_dsn.add(meta["dsn_message_id"])
    new = 0
    for b in bounces:
        owner = owners.get((b.get("recipient") or "").lower())
        if owner is None:
            continue
        recipient, campaign = owner
        dsn_id = b.get("bounce_message_id")
        if (dsn_id and dsn_id in seen_dsn) or recipient in already_bounced:
            continue
        append_event(conn, type="bounce", recipient=recipient, campaign=campaign,
                     meta={"bounce_reason": b.get("bounce_reason"),
                           "bounce_time": b.get("bounce_time"),
                           "category": "bounce", "dsn_message_id": dsn_id})
        if dsn_id:
            seen_dsn.add(dsn_id)
        already_bounced.add(recipient)
        new += 1
    return new


def _thread_references(conn, recipient: str) -> str:
    """The RFC 5322 References chain for the next threaded message to
    `recipient`: every Message-ID we've already sent them, in send order,
    space-joined. A strict mail client threads a late follow-up back to the
    original by intersecting References sets, so carrying the WHOLE chain (not
    just the immediate parent, which In-Reply-To already gives) keeps the
    conversation grouped even where In-Reply-To alone wouldn't. Derived live
    from the append-only log — no parallel state to keep in sync. Returns None
    when the recipient has no prior send (the initial send has no chain)."""
    rows = conn.execute(
        "SELECT message_id FROM events WHERE recipient = ? AND message_id IS NOT NULL "
        "AND type IN ('sent', 'requeued', 'interaction') ORDER BY id ASC",
        (recipient,),
    ).fetchall()
    ids = [r["message_id"] for r in rows]
    return " ".join(ids) if ids else None


def _send_one(conn, smtp_config, row: dict, *, workdir_root: Path = WORKDIR_ROOT,
              send_message_fn=smtp.send_message, sender_name: str = None) -> bool:
    recipient, campaign = row["recipient"], row["campaign"]
    workdir = recipient_workdir(campaign, recipient, root=workdir_root)

    # Everything that reads/parses staged data (manifest JSON, its keys, the
    # attachment bytes) is one exception boundary: a corrupted or partial
    # staged.json — e.g. from a crash mid-write during a prior `send` — must
    # degrade to send_failed for THIS recipient only, never propagate into
    # drain()'s loop and abort every other recipient in the batch
    # (one-recipient blast radius).
    try:
        manifest = load_manifest(workdir)
        attachment_name = manifest["attachment_name"]
        # cadence is read only to mark the sequence final when it's empty; the
        # follow-up STAGES are no longer configured on this send (GMass fired
        # them server-side from stageNDays; the app now fires them itself via
        # slap.queue.due_for_followup / runner._send_followup). subject/body
        # are read fresh at drain time, so `slap.py template-reload` still
        # picks up a rewritten manifest with zero changes needed here.
        cadence = manifest["cadence"]
        subject = manifest["subject"]
        body = manifest["body"]
        # attachment_source (static/latex-disabled campaigns): the shared
        # campaigns/<name>/<attachment_file> path — read fresh at drain
        # time, never copied per-recipient (identical bytes for everyone).
        # Absent/None (latex-enabled campaigns, and any staged.json written
        # before this field existed): read the per-recipient compiled PDF
        # already sitting in this recipient's own workdir, as before.
        # attachment_name is None (no sentinel file) for a no-attachment
        # `send custom` (mode 4) — send with no attachment at all.
        if attachment_name is None:
            attachment_arg = None
        else:
            attachment_source = manifest.get("attachment_source")
            attachment_path = Path(attachment_source) if attachment_source else workdir / attachment_name
            attachment_arg = (attachment_name, attachment_path.read_bytes(), "application/pdf")
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "load_staged_data", "error": str(e)})
        return False

    # One atomic SMTP send — no draft/campaign two-call, so no draft_created
    # idempotency marker: the GMass draft-reuse dance is gone. A send that
    # raises did so BEFORE the message reached Gmail (auth/connection/RCPT
    # errors are all pre-transmission), so a send_failed is always safe to
    # retry on the next drain — the recipient stays `queued` and due. The
    # returned Message-ID is recorded on the `sent` event as the threading +
    # reply-match key (see slap.smtp / slap.tracking).
    try:
        sent = send_message_fn(
            smtp_config, sender=smtp_config.user, sender_name=sender_name,
            recipient=recipient, subject=subject, body=body, attachment=attachment_arg,
        )
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "send_message", "error": str(e)})
        return False

    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0,
                 message_id=sent["message_id"], meta={"is_final_stage": len(cadence) == 0})
    return True


def _send_ooo_resend(conn, smtp_config, row: dict, *, workdir_root: Path = WORKDIR_ROOT,
                      send_message_fn=smtp.send_message, sender_name: str = None,
                      today: date = None) -> bool:
    """The OOO counterpart to _send_one (§7, step 10): resends the
    recipient's next stage as a reply threaded into their original
    conversation (In-Reply-To the recipient's stored message_id). Reuses the
    stage body already sitting in the staged manifest from the original send —
    no new drop/template data needed, and no attachment (a threaded follow-up
    doesn't re-attach the résumé).

    Manual OOO-pause continuation (post-launch): under SMTP the app owns the
    ENTIRE follow-up cadence itself (there is no external GMass timer to
    suppress or race — that whole concern, and the account-wide unsubscribe
    lever it needed, is gone), so if the cadence still has a stage left after
    this send, THIS function schedules it: records `next_resume_date` in this
    same `requeued` event's own meta (one atomic write, no second event needed
    — see slap.queue.due_for_ooo_resend/_pending_ooo_resume_date for how that's
    read back), anchored to `today` (the date this stage actually fired) plus
    the persona's own inter-stage gap — a continuation of the existing
    sequence, never a restart from stage 1.

    A cadence-exhausted recipient (no next stage at all) is a TERMINAL
    condition, not a transient one — retrying can never succeed, since a
    cadence's length never changes for an already-staged recipient. Marked
    with its own `send_failed` meta discriminator
    (`{"stage": "ooo_cadence_exhausted"}`) so slap.queue.
    _pending_ooo_resume_date recognizes it as CLOSING the pending OOO state
    (an iron-audit SHOULD-FIX) — without this, a recipient marked OOO with
    nothing left to resend (newly reachable via the unconditional Reach-outs
    "Mark OOO" action, e.g. a single-stage persona or a recipient already at
    their final stage) would generate an identical send_failed on every
    single future drain, forever."""
    recipient, campaign = row["recipient"], row["campaign"]
    workdir = recipient_workdir(campaign, recipient, root=workdir_root)

    try:
        manifest = load_manifest(workdir)
        cadence = manifest["cadence"]
        stage_bodies = manifest["stage_bodies"]
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "load_staged_data_ooo", "error": str(e)})
        return False

    next_stage = row["current_stage"] + 1
    if next_stage > len(cadence):
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "ooo_cadence_exhausted",
                           "error": f"no next stage to resend — current_stage={row['current_stage']}, "
                                    f"cadence has {len(cadence)} stage(s)"})
        return False

    try:
        stage_body = stage_bodies[next_stage - 1]
        reply_to_message_id = row["message_id"]
        if not reply_to_message_id:
            raise RunnerError("no prior message_id to thread the OOO resend into")
        subject = f"Re: {manifest['subject']}"
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "load_staged_data_ooo", "error": str(e)})
        return False

    try:
        sent = send_message_fn(
            smtp_config, sender=smtp_config.user, sender_name=sender_name,
            recipient=recipient, subject=subject, body=stage_body,
            in_reply_to=reply_to_message_id, references=_thread_references(conn, recipient),
        )
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "send_message_ooo", "error": str(e)})
        return False

    next_resume_date = None
    if next_stage < len(cadence):
        next_resume_date = (today or date.today()) + timedelta(days=cadence[next_stage])

    append_event(conn, type="requeued", recipient=recipient, campaign=campaign, stage=next_stage,
                 message_id=sent["message_id"],
                 meta={"next_resume_date": next_resume_date.isoformat()} if next_resume_date else None)
    return True


def _send_followup(conn, smtp_config, row: dict, *, workdir_root: Path = WORKDIR_ROOT,
                   send_message_fn=smtp.send_message, sender_name: str = None) -> bool:
    """Fire the recipient's next normal-cadence follow-up stage as a reply
    threaded into their original conversation. This is the app-side replacement
    for a GMass server-side stage: the same reply-in-thread shape as
    _send_ooo_resend (In-Reply-To the recipient's stored message_id, reusing the
    staged stage body, no re-attached résumé), but on the NORMAL cadence — so it
    records a `sent` event advancing current_stage (not a `requeued`), and marks
    the sequence `done` via is_final_stage when the last stage fires.

    Selection/idempotency is owned by slap.queue.due_for_followup (which gates on
    fire date and current_stage); this function just sends the stage that query
    identified. The cadence-exhausted guard here is pure defense in depth — the
    due query already excludes exhausted recipients."""
    recipient, campaign = row["recipient"], row["campaign"]
    workdir = recipient_workdir(campaign, recipient, root=workdir_root)

    try:
        manifest = load_manifest(workdir)
        cadence = manifest["cadence"]
        stage_bodies = manifest["stage_bodies"]
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "load_staged_data_followup", "error": str(e)})
        return False

    next_stage = row["current_stage"] + 1
    if next_stage > len(cadence):
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "followup_cadence_exhausted",
                           "error": f"no next stage — current_stage={row['current_stage']}, "
                                    f"cadence has {len(cadence)} stage(s)"})
        return False

    try:
        stage_body = stage_bodies[next_stage - 1]
        reply_to_message_id = row["message_id"]
        if not reply_to_message_id:
            raise RunnerError("no prior message_id to thread the follow-up into")
        subject = f"Re: {manifest['subject']}"
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "load_staged_data_followup", "error": str(e)})
        return False

    try:
        sent = send_message_fn(
            smtp_config, sender=smtp_config.user, sender_name=sender_name,
            recipient=recipient, subject=subject, body=stage_body,
            in_reply_to=reply_to_message_id, references=_thread_references(conn, recipient),
        )
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "send_message_followup", "error": str(e)})
        return False

    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=next_stage,
                 message_id=sent["message_id"], meta={"is_final_stage": next_stage == len(cadence)})
    return True


def _send_remind(conn, smtp_config, row: dict, *, workdir_root: Path = WORKDIR_ROOT,
                 send_message_fn=smtp.send_message, sender_name: str = None) -> bool:
    """Fire a one-shot Remind (Engagement/reach-out Remind action) as a reply
    threaded into the recipient's original conversation — the same app-initiated
    reply-in-thread shape as _send_ooo_resend, but using the externally-authored
    body SNAPSHOTTED into the queued event (slap.queue.queue_remind), never a
    persona-cadence stage, and WITHOUT advancing current_stage or touching
    status (a Remind is a nudge, not a sequence transition). No attachment
    (a threaded follow-up doesn't re-attach the résumé, same as OOO).

    One atomic SMTP send (no draft two-call): a send that raises did so before
    the message reached Gmail, so it's safe to retry on the next drain. The
    completion marker is a `remind_sent` interaction carrying the sent
    Message-ID — which removes this recipient from due_for_remind() so the same
    Remind can never fire twice."""
    recipient, campaign = row["recipient"], row["campaign"]
    try:
        body = row["body"]
        reply_to_message_id = row.get("reply_to_message_id")
        if not reply_to_message_id:
            raise RunnerError("no prior message_id to thread the remind into")
        subject_ref = (row.get("subject_ref") or "").strip()
        subject = f"Re: {subject_ref}" if subject_ref else "Re: following up"
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "load_remind_data", "error": str(e)})
        return False

    try:
        sent = send_message_fn(
            smtp_config, sender=smtp_config.user, sender_name=sender_name,
            recipient=recipient, subject=subject, body=body,
            in_reply_to=reply_to_message_id, references=_thread_references(conn, recipient),
        )
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "send_message_remind", "error": str(e)})
        return False

    append_event(conn, type="interaction", recipient=recipient, campaign=campaign,
                 message_id=sent["message_id"],
                 meta={"channel": "remind_sent", "followup": row.get("followup")})
    return True


def drain(conn, global_config, smtp_config, *, now: date = None, sleep_fn=time.sleep,
          random_fn=random.uniform, workdir_root: Path = WORKDIR_ROOT,
          send_message_fn=smtp.send_message, imap_config=None,
          poll_replies_fn=imap.poll_replies, poll_bounces_fn=imap.poll_bounces,
          log_fn=print) -> DrainResult:
    """Drain whatever's queued and due, right now — no window waiting (that's
    wait_for_fire_window's job). Cap-aware, resilient: a preflight failure
    retries then gives up loud (run_failed, queue untouched); a per-email
    failure logs send_failed and moves on (queue stays intact either way).

    `log_fn` prints one line per recipient as each send attempt resolves —
    real-time progress instead of the previous total silence until the final
    summary line, which made a multi-minute batch (throttled by
    send_delay_min/max) indistinguishable from a hang. Defaults to the
    builtin `print` so every existing caller (`cmd_runner`, `send --now`)
    gets this for free; unattended launchd runs land these same lines in
    runner.log right alongside the existing summary, which the dashboard's
    Logs page also surfaces."""
    today = now or date.today()

    error = _preflight_with_retries(
        global_config, conn, global_config.schedule.drain_retries, sleep_fn
    )
    if error is not None:
        append_event(conn, type="run_failed",
                     meta={"error": error, "retry_count": global_config.schedule.drain_retries})
        return DrainResult(ran=False, preflight_error=error)

    append_event(conn, type="run_started")

    # Stop-on-reply at FIRE TIME (see slap.imap's module docstring / the hard
    # correctness rule): poll IMAP for replies and record them BEFORE selecting
    # follow-ups, so a recipient who replied since the last drain leaves
    # 'active' status here and is never picked up by due_for_followup below.
    # imap_config is None only in tests / degraded runs that inject replies
    # directly. If the poll itself fails, we must NOT fire follow-ups this drain
    # (we can't confirm nobody replied) — but initial sends and reminds still
    # go, since those don't depend on reply state.
    followups_enabled = True
    if imap_config is not None:
        try:
            ingest_replies(conn, imap_config, poll_replies_fn=poll_replies_fn)
        except imap.ImapError as e:
            followups_enabled = False
            log_fn(f"reply poll failed — skipping follow-ups this drain: {e}")
        # Bounce detection is a best-effort safety net (stop mailing dead
        # addresses), not the primary stop signal — a bounce-poll failure is
        # logged but does NOT skip follow-ups the way a reply-poll failure does.
        try:
            ingest_bounces(conn, imap_config, poll_bounces_fn=poll_bounces_fn)
        except imap.ImapError as e:
            log_fn(f"bounce poll failed (best-effort, proceeding): {e}")

    headroom = cap_headroom(conn, global_config, today=today)
    # OOO resends (§7) share the exact same cap/gap/preflight/exception
    # handling as initial sends — "fire on the same runner cadence," no
    # special scheduling — so they're just more rows in the same batch.
    #
    # Dispatch is tagged by WHICH due-list a row came from, not by
    # row["status"]: a recipient mid-way through a multi-stage manual
    # OOO-pause continuation (post-launch feature) shows status='active'
    # between stages (see slap.queue.due_for_ooo_resend's docstring) — the
    # same status a perfectly normal, never-OOO'd recipient has — so status
    # alone can no longer tell the two apart.
    #
    # Defense in depth against a double-send (iron-audit BLOCKER): the two
    # due-lists are designed to never overlap (see due_for_ooo_resend's own
    # docstring — its campaign-scoping is the actual fix), but a recipient
    # already dispatched to _send_one this drain is explicitly excluded from
    # the OOO list too, belt-and-suspenders, so a future change to either
    # query can never resend to the same recipient twice in one batch.
    due_initial = due_recipients(conn)
    due_ooo = due_for_ooo_resend(conn, today=today)
    # Follow-up stages (§10): under SMTP the app fires stages 1..N itself (GMass
    # used to fire them server-side). due_for_followup selects the next
    # normal-cadence stage that's due today and excludes any OOO-managed
    # recipient (owned by the OOO list above on its pause schedule), so the two
    # never fire the same stage — dispatched via _send_followup on this same
    # cap-bounded batch, just like every other send.
    due_followup = due_for_followup(conn, global_config, today=today) if followups_enabled else []
    # Reminds (one-shot manual nudges) fire on the same runner cadence as
    # everything else — just more rows in the same cap-bounded batch, dispatched
    # via _send_remind. A recipient already dispatched this drain (initial/OOO/
    # follow-up) is excluded, belt-and-suspenders against any double-send.
    due_remind = due_for_remind(conn)
    initial_recipients = {row["recipient"] for row in due_initial}
    ooo_recipients = {row["recipient"] for row in due_ooo}
    followup_recipients = {row["recipient"] for row in due_followup}
    due = ([(row, _send_one) for row in due_initial]
           + [(row, _send_ooo_resend) for row in due_ooo if row["recipient"] not in initial_recipients]
           + [(row, _send_followup) for row in due_followup
              if row["recipient"] not in initial_recipients and row["recipient"] not in ooo_recipients]
           + [(row, _send_remind) for row in due_remind
              if row["recipient"] not in initial_recipients and row["recipient"] not in ooo_recipients
              and row["recipient"] not in followup_recipients])
    to_send = due[:headroom]

    sent_count = 0
    failed_count = 0
    for i, (row, send_fn) in enumerate(to_send):
        if i > 0:
            sleep_fn(random_fn(global_config.schedule.send_delay_min, global_config.schedule.send_delay_max))
        kwargs = {"workdir_root": workdir_root, "send_message_fn": send_message_fn,
                  "sender_name": global_config.from_name}
        if send_fn is _send_ooo_resend:
            kwargs["today"] = today
        # _send_one/_send_remind take no extra kwargs beyond the common ones.
        # (The old gmass_allowed_days/skip_holidays that configured GMass's
        # server-side follow-up firing are gone — the app now owns follow-up
        # scheduling itself in slap.queue.due_for_followup, which is where any
        # send-window restriction now belongs.)
        try:
            ok = send_fn(conn, smtp_config, row, **kwargs)
        except Exception as e:
            # Defense in depth: _send_one/_send_ooo_resend already convert
            # their own known failure modes to send_failed, but no bug in
            # either (now or in a future change) should ever be able to
            # crash the whole drain and abort every other recipient in the
            # batch.
            append_event(conn, type="send_failed", recipient=row["recipient"], campaign=row["campaign"],
                         meta={"stage": "unexpected", "error": str(e)})
            ok = False
        log_fn(f"[{i + 1}/{len(to_send)}] {row['recipient']} ({row['campaign']}) "
               f"-> {'sent' if ok else 'FAILED'}")
        if ok:
            sent_count += 1
        else:
            failed_count += 1

    remaining = (len(due_recipients(conn)) + len(due_for_ooo_resend(conn, today=today))
                 + len(due_for_followup(conn, global_config, today=today)) + len(due_for_remind(conn)))
    append_event(conn, type="run_completed",
                 meta={"sent": sent_count, "failed": failed_count, "remaining_queued": remaining})
    return DrainResult(ran=True, sent=sent_count, failed=failed_count, remaining_queued=remaining)


def _roll_fire_time(schedule, today: date, rng=random) -> datetime:
    start_h, start_m = (int(x) for x in schedule.fire_window_start.split(":"))
    end_h, end_m = (int(x) for x in schedule.fire_window_end.split(":"))
    start = datetime.combine(today, dt_time(start_h, start_m))
    end = datetime.combine(today, dt_time(end_h, end_m))
    span = max(0.0, (end - start).total_seconds())
    return start + timedelta(seconds=rng.uniform(0, span))


def wait_for_fire_window(schedule, *, now_fn=datetime.now, sleep_fn=time.sleep, rng=random) -> datetime:
    """Sleep until a random moment in today's fire window (local time), or
    return immediately if that moment already passed — the launchd
    wake-catch-up case: a Mac asleep through the window should fire on wake,
    not wait for tomorrow."""
    now = now_fn()
    target = _roll_fire_time(schedule, now.date(), rng)
    if now < target:
        sleep_fn((target - now).total_seconds())
    return target
