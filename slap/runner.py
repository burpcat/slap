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

import random
import time
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from datetime import timedelta
from pathlib import Path

from slap import doctor, gmass
from slap.latex import WORKDIR_ROOT, recipient_workdir
from slap.queue import due_for_ooo_resend, due_recipients, load_manifest
from slap.tracking import append_event, latest_open_draft_id


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
    """Best-effort estimate only — see module docstring."""
    rows = conn.execute(
        "SELECT persona, current_stage, first_sent_at FROM recipients "
        "WHERE status = 'active' AND first_sent_at IS NOT NULL"
    ).fetchall()
    count = 0
    for row in rows:
        cadence = global_config.personas.get(row["persona"])
        if not cadence:
            continue
        next_stage = row["current_stage"] + 1
        if next_stage > len(cadence):
            continue  # sequence already exhausted
        cumulative_days = sum(cadence[:next_stage])
        fire_date = datetime.fromisoformat(row["first_sent_at"]).date() + timedelta(days=cumulative_days)
        if fire_date == today:
            count += 1
    return count


def cap_headroom(conn, global_config, *, today: date = None) -> int:
    today = today or date.today()
    used = todays_sent_count(conn, today) + _estimate_followups_firing_today(conn, global_config, today)
    return max(0, global_config.schedule.daily_cap - used)


def _send_one(conn, api_key: str, row: dict, *, workdir_root: Path = WORKDIR_ROOT,
              create_draft_fn=gmass.create_draft, send_campaign_fn=gmass.send_campaign) -> bool:
    recipient, campaign = row["recipient"], row["campaign"]
    workdir = recipient_workdir(campaign, recipient, root=workdir_root)

    # Everything that reads/parses staged data (manifest JSON, its keys, the
    # attachment bytes, the cadence-derived campaign_settings) is one
    # exception boundary: a corrupted or partial staged.json — e.g. from a
    # crash mid-write during a prior `send` — must degrade to send_failed for
    # THIS recipient only, never propagate into drain()'s loop and abort
    # every other recipient in the batch (one-recipient blast radius).
    try:
        manifest = load_manifest(workdir)
        attachment_name = manifest["attachment_name"]
        cadence = manifest["cadence"]
        stage_bodies = manifest["stage_bodies"]
        subject = manifest["subject"]
        body = manifest["body"]
        attachment_bytes = (workdir / attachment_name).read_bytes()
        campaign_settings = gmass.build_campaign_settings(cadence, stage_bodies)
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "load_staged_data", "error": str(e)})
        return False

    draft_id = latest_open_draft_id(conn, recipient)
    if draft_id is None:
        try:
            draft = create_draft_fn(
                api_key, recipient=recipient, subject=subject, message=body,
                attachment=(attachment_name, attachment_bytes, "application/pdf"),
            )
        except Exception as e:
            append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                         meta={"stage": "create_draft", "error": str(e)})
            return False
        draft_id = draft["draft_id"]
        # Recorded the instant create_draft returns, BEFORE send_campaign is
        # attempted (§3 idempotency) — a crash/failure past this point is
        # retryable via latest_open_draft_id, never an orphan/double-create.
        append_event(conn, type="draft_created", recipient=recipient, campaign=campaign,
                     stage=0, gmass_draft_id=draft_id)

    try:
        sent = send_campaign_fn(api_key, draft_id, campaign_settings=campaign_settings)
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     gmass_draft_id=draft_id, meta={"stage": "send_campaign", "error": str(e)})
        return False

    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0,
                 gmass_campaign_id=sent["campaign_id"], gmass_draft_id=draft_id,
                 meta={"is_final_stage": len(cadence) == 0})
    return True


def _send_ooo_resend(conn, api_key: str, row: dict, *, workdir_root: Path = WORKDIR_ROOT,
                      create_draft_fn=gmass.create_draft, send_campaign_fn=gmass.send_campaign) -> bool:
    """The OOO counterpart to _send_one (§7, step 10): resends the
    recipient's next stage as a reply threaded into their original
    conversation. Reuses the stage body already sitting in the staged
    manifest from the original send — no new drop/template data needed, and
    no attachment (a threaded follow-up doesn't re-attach the résumé)."""
    recipient, campaign = row["recipient"], row["campaign"]
    workdir = recipient_workdir(campaign, recipient, root=workdir_root)

    try:
        manifest = load_manifest(workdir)
        cadence = manifest["cadence"]
        stage_bodies = manifest["stage_bodies"]
        next_stage = row["current_stage"] + 1
        if next_stage > len(cadence):
            raise RunnerError(
                f"no next stage to resend — current_stage={row['current_stage']}, "
                f"cadence has {len(cadence)} stage(s)"
            )
        stage_body = stage_bodies[next_stage - 1]
        reply_to_campaign_id = row["last_gmass_campaign_id"]
        if not reply_to_campaign_id:
            raise RunnerError("no prior gmass_campaign_id to reply into")
        subject = f"Re: {manifest['subject']}"
        reply_settings = gmass.build_reply_settings(reply_to_campaign_id)
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     meta={"stage": "load_staged_data_ooo", "error": str(e)})
        return False

    draft_id = latest_open_draft_id(conn, recipient)
    if draft_id is None:
        try:
            draft = create_draft_fn(api_key, recipient=recipient, subject=subject, message=stage_body)
        except Exception as e:
            append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                         meta={"stage": "create_draft_ooo", "error": str(e)})
            return False
        draft_id = draft["draft_id"]
        append_event(conn, type="draft_created", recipient=recipient, campaign=campaign,
                     stage=next_stage, gmass_draft_id=draft_id)

    try:
        sent = send_campaign_fn(api_key, draft_id, campaign_settings=reply_settings)
    except Exception as e:
        append_event(conn, type="send_failed", recipient=recipient, campaign=campaign,
                     gmass_draft_id=draft_id, meta={"stage": "send_campaign_ooo", "error": str(e)})
        return False

    append_event(conn, type="requeued", recipient=recipient, campaign=campaign, stage=next_stage,
                 gmass_campaign_id=sent["campaign_id"], gmass_draft_id=draft_id)
    return True


def drain(conn, global_config, api_key: str, *, now: date = None, sleep_fn=time.sleep,
          random_fn=random.uniform, workdir_root: Path = WORKDIR_ROOT,
          create_draft_fn=gmass.create_draft, send_campaign_fn=gmass.send_campaign) -> DrainResult:
    """Drain whatever's queued and due, right now — no window waiting (that's
    wait_for_fire_window's job). Cap-aware, resilient: a preflight failure
    retries then gives up loud (run_failed, queue untouched); a per-email
    failure logs send_failed and moves on (queue stays intact either way)."""
    today = now or date.today()

    error = _preflight_with_retries(
        global_config, conn, global_config.schedule.drain_retries, sleep_fn
    )
    if error is not None:
        append_event(conn, type="run_failed",
                     meta={"error": error, "retry_count": global_config.schedule.drain_retries})
        return DrainResult(ran=False, preflight_error=error)

    append_event(conn, type="run_started")

    headroom = cap_headroom(conn, global_config, today=today)
    # OOO resends (§7) share the exact same cap/gap/preflight/exception
    # handling as initial sends — "fire on the same runner cadence," no
    # special scheduling — so they're just more rows in the same batch.
    due = due_recipients(conn) + due_for_ooo_resend(conn)
    to_send = due[:headroom]

    sent_count = 0
    failed_count = 0
    for i, row in enumerate(to_send):
        if i > 0:
            sleep_fn(random_fn(global_config.schedule.send_delay_min, global_config.schedule.send_delay_max))
        send_fn = _send_ooo_resend if row["status"] == "ooo_requeued" else _send_one
        try:
            ok = send_fn(conn, api_key, row, workdir_root=workdir_root,
                         create_draft_fn=create_draft_fn, send_campaign_fn=send_campaign_fn)
        except Exception as e:
            # Defense in depth: _send_one/_send_ooo_resend already convert
            # their own known failure modes to send_failed, but no bug in
            # either (now or in a future change) should ever be able to
            # crash the whole drain and abort every other recipient in the
            # batch.
            append_event(conn, type="send_failed", recipient=row["recipient"], campaign=row["campaign"],
                         meta={"stage": "unexpected", "error": str(e)})
            ok = False
        if ok:
            sent_count += 1
        else:
            failed_count += 1

    remaining = len(due_recipients(conn)) + len(due_for_ooo_resend(conn))
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
