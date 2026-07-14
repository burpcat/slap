"""Localhost dashboard (Build Order step 11). See SLAP_BUILD_PROMPT.md §8.

sync_reports() is the on-open GMass poll: "Polls GMass reports on open
(writes new click/reply/bounce/block events), then renders." Every
create_draft() call is for exactly one recipient, so a gmass_campaign_id
maps 1:1 back to a recipient — polling is keyed off every distinct
campaign_id this app has ever recorded, not a live "list my campaigns" call
GMass doesn't offer.

Dedup strategy per report type (none of clicks/bounces/blocks carry a stable
GMass-issued ID in the verified schema, per CONTROL_SHEET.md):
- replies: GMass's own `replyId` (stable, verified in the swagger capture).
- clicks: (url, clickTime) — a recipient clicking the identical link at the
  identical GMass-recorded timestamp twice is not realistically distinct.
- bounces/blocks: (reason, time) — same reasoning.
Each already-recorded item's key is reconstructed from that event's own
`meta` (written using the same field names), so re-polling never re-inserts
the same real-world item as a second event.

**Bounces vs. blocks (found via real usage — see CONTROL_SHEET.md's
"missing second bounce" section for the full investigation)**: GMass
reports these as two entirely separate report categories —
`/api/reports/{id}/bounces` and `/api/reports/{id}/blocks` — with their own
reason/time field names (`bounceReason`/`bounceTime` vs.
`blockReason`/`blockTime`). `_sync_blocks()` polls the second endpoint that
`_sync_bounces()` alone was silently never covering. Both write the SAME
`bounce` event type (not a new one) — see `_sync_blocks()`'s own docstring
for why a genuinely new SQL-level event type was deliberately avoided —
distinguished only by `meta["category"]` (`"bounce"` or `"block"`), the
exact same "one event type, a meta discriminator for the sub-category"
pattern `reply_reviewed`'s `meta["tag"]` already established for
real/ooo/not_interested.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, g, redirect, request, render_template, url_for

from slap import archive, display, domains, gmass, gmass_cache, reload, tracking, ui_state
from slap.config import discover_campaigns
from slap.domains import check_recipient
from slap.queue import (
    QueueError, _pending_ooo_resume_date, due_for_ooo_resend, due_recipients, resend_bounced,
    tag_ooo as _tag_ooo,
)
from slap.runner import cap_headroom
from slap.tracking import append_event

TEMPLATE_FOLDER = str(Path(__file__).parent / "dashboard_templates")


def _all_campaign_ids(conn) -> list:
    """Every distinct GMass campaign this app has ever created, mapped back
    to the recipient/campaign it belongs to."""
    rows = conn.execute(
        "SELECT DISTINCT gmass_campaign_id, recipient, campaign FROM events "
        "WHERE gmass_campaign_id IS NOT NULL AND recipient IS NOT NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def _event_meta_values(conn, recipient: str, event_type: str) -> list:
    rows = conn.execute(
        "SELECT meta FROM events WHERE recipient = ? AND type = ?", (recipient, event_type)
    ).fetchall()
    return [json.loads(r["meta"]) for r in rows if r["meta"]]


def _current_stage(conn, recipient: str):
    row = conn.execute("SELECT current_stage FROM recipients WHERE recipient = ?", (recipient,)).fetchone()
    return row["current_stage"] if row else None


def _sync_replies(conn, api_key: str, campaign_id, recipient: str, campaign: str):
    try:
        items = gmass.get_reports(api_key, campaign_id, "replies")
    except requests.exceptions.RequestException:
        return 0, None  # transient network failure — tolerate, next poll retries
    except gmass.GMassError as e:
        return 0, str(e)  # a real API-level problem (bad key, schema drift) — surface it
    seen = {m["reply_id"] for m in _event_meta_values(conn, recipient, "reply") if "reply_id" in m}
    # Recorded on the reply's own `stage` column (not just in meta) so the
    # dashboard's "reply-by-stage" panel (§8) can group by it directly —
    # stage can't change mid-poll (reply events don't advance current_stage),
    # so it's safe to look up once rather than per item.
    stage = _current_stage(conn, recipient)
    count = 0
    for item in items:
        reply_id = item.get("replyId")
        if reply_id is None or reply_id in seen:
            continue
        append_event(conn, type="reply", recipient=recipient, campaign=campaign, stage=stage,
                     meta={"reply_id": reply_id, "reply_time": item.get("replyTime")})
        seen.add(reply_id)
        count += 1
    return count, None


def _sync_clicks(conn, api_key: str, campaign_id, recipient: str, campaign: str):
    try:
        items = gmass.get_reports(api_key, campaign_id, "clicks")
    except requests.exceptions.RequestException:
        return 0, None
    except gmass.GMassError as e:
        return 0, str(e)
    seen = {(m["url"], m["click_time"]) for m in _event_meta_values(conn, recipient, "click")
            if "url" in m and "click_time" in m}
    stage = _current_stage(conn, recipient)  # §8's "click-by-stage" panel
    count = 0
    for item in items:
        key = (item.get("url"), item.get("clickTime"))
        if key in seen:
            continue
        append_event(conn, type="click", recipient=recipient, campaign=campaign, stage=stage,
                     meta={"url": item.get("url"), "click_time": item.get("clickTime")})
        seen.add(key)
        count += 1
    return count, None


def _bounce_lifecycle_dedup_keys(conn, recipient: str) -> set:
    """Every (reason, time) pair already recorded for this recipient across
    BOTH bounces and blocks — they share the same `bounce` event type and
    the same `bounce_reason`/`bounce_time` meta keys (see module docstring),
    so one shared dedup set correctly prevents re-inserting either kind
    twice, with no risk of a bounce and an unrelated block being conflated
    (their reason/time text never coincidentally matches in practice)."""
    return {(m["bounce_reason"], m["bounce_time"]) for m in _event_meta_values(conn, recipient, "bounce")
            if "bounce_reason" in m and "bounce_time" in m}


def _sync_bounces(conn, api_key: str, campaign_id, recipient: str, campaign: str):
    try:
        items = gmass.get_reports(api_key, campaign_id, "bounces")
    except requests.exceptions.RequestException:
        return 0, None
    except gmass.GMassError as e:
        return 0, str(e)
    seen = _bounce_lifecycle_dedup_keys(conn, recipient)
    count = 0
    for item in items:
        key = (item.get("bounceReason"), item.get("bounceTime"))
        if key in seen:
            continue
        append_event(conn, type="bounce", recipient=recipient, campaign=campaign,
                     meta={"bounce_reason": item.get("bounceReason"), "bounce_time": item.get("bounceTime"),
                           "category": "bounce"})
        seen.add(key)
        count += 1
    return count, None


def _sync_blocks(conn, api_key: str, campaign_id, recipient: str, campaign: str):
    """The `/blocks` report counterpart to _sync_bounces() — found missing
    via real usage (the owner saw two delivery failures, the Bounces widget
    showed only one). GMass reports blocks as an entirely separate category
    from bounces (separate endpoint, separate `blockReason`/`blockTime`
    field names — see slap.gmass.REPORT_TYPES, which already listed
    "blocks" as a valid report type that nothing ever actually polled).

    Writes the SAME `bounce` event type as _sync_bounces() — NOT a new
    `block` type — deliberately: this app's `events.type` column has a SQL
    CHECK constraint baked into every already-existing, populated slap.db
    at table-creation time (see slap/tracking.py's _SCHEMA). Adding a new
    literal event type would require a real ALTER-TABLE-style migration of
    every owner's live database (SQLite has no ALTER TABLE ... ADD CHECK
    VALUE — the only path is a full table rebuild), a live-data-migration
    risk this fix does not need to take on. A block is functionally a dead-
    delivery signal for every purpose this app already treats a bounce as
    one (cleanup eligibility in slap.cleanup, dedup, recipients.status) —
    reusing `bounce` means zero changes needed anywhere else in the app.
    `meta["category"] = "block"` (mirroring reply_reviewed's meta["tag"]
    pattern) is what lets the Bounces widget still show the distinction to
    the owner instead of silently blending the two — see bounces() below."""
    try:
        items = gmass.get_reports(api_key, campaign_id, "blocks")
    except requests.exceptions.RequestException:
        return 0, None
    except gmass.GMassError as e:
        return 0, str(e)
    seen = _bounce_lifecycle_dedup_keys(conn, recipient)
    count = 0
    for item in items:
        key = (item.get("blockReason"), item.get("blockTime"))
        if key in seen:
            continue
        append_event(conn, type="bounce", recipient=recipient, campaign=campaign,
                     meta={"bounce_reason": item.get("blockReason"), "bounce_time": item.get("blockTime"),
                           "category": "block"})
        seen.add(key)
        count += 1
    return count, None


def sync_reports(conn, api_key: str) -> dict:
    """Poll every known campaign for new replies/clicks/bounces/blocks, write
    events for anything not already recorded, and return a summary
    including the UTC "last synced" instant (§8) — convert to local only at
    display time.

    A transient network failure (timeout, connection refused) on one
    campaign's poll is tolerated silently — one campaign's poll failing must
    not block syncing the rest, and the next on-open poll retries it. A real
    API-level problem (bad/expired key, GMass schema drift) raises
    `gmass.GMassError` instead, which is NOT swallowed the same way: it's
    collected into `errors` and surfaced in the dashboard header, so an
    auth failure doesn't silently look like "nothing new" forever.

    `new_bounces` combines both _sync_bounces() and _sync_blocks() counts —
    the top-of-dashboard sync summary just needs "how many new delivery
    failures arrived," not a sub-category breakdown; the per-recipient
    bounce/block distinction is what the Bounces widget itself (bounces(),
    below) surfaces."""
    new_replies = new_clicks = new_bounces = 0
    errors = []
    for row in _all_campaign_ids(conn):
        cid, recipient, campaign = row["gmass_campaign_id"], row["recipient"], row["campaign"]
        count, error = _sync_replies(conn, api_key, cid, recipient, campaign)
        new_replies += count
        if error:
            errors.append(error)
        count, error = _sync_clicks(conn, api_key, cid, recipient, campaign)
        new_clicks += count
        if error:
            errors.append(error)
        count, error = _sync_bounces(conn, api_key, cid, recipient, campaign)
        new_bounces += count
        if error:
            errors.append(error)
        count, error = _sync_blocks(conn, api_key, cid, recipient, campaign)
        new_bounces += count
        if error:
            errors.append(error)
    return {
        "synced_at": datetime.now(timezone.utc),
        "new_replies": new_replies,
        "new_clicks": new_clicks,
        "new_bounces": new_bounces,
        "errors": errors,
    }


# --- panels (§8) -------------------------------------------------------

def _local_date(iso_timestamp: str) -> date:
    """UTC event timestamp (as stored in `events`, §5) -> the LOCAL calendar
    date it falls on. Every place the dashboard buckets events by "day" for
    display must convert to local first, exactly like the `to_local` filter
    does for individual timestamps — otherwise a send made late at night
    local time (e.g. 11pm EDT = 3am UTC the next day) lands in the wrong
    day's panel when compared against a local `date.today()`."""
    dt = datetime.fromisoformat(iso_timestamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date()


def _count_events_on(conn, event_type: str, day: date) -> int:
    rows = conn.execute("SELECT timestamp FROM events WHERE type = ?", (event_type,)).fetchall()
    return sum(1 for r in rows if _local_date(r["timestamp"]) == day)


def _count_events_in_range(conn, event_type: str, start: date, end: date) -> int:
    rows = conn.execute("SELECT timestamp FROM events WHERE type = ?", (event_type,)).fetchall()
    return sum(1 for r in rows if start <= _local_date(r["timestamp"]) <= end)


def _sent_split(conn, start: date, end: date) -> dict:
    """new (stage 0) vs follow-up (stage>0, includes OOO 'requeued' resends)
    sent/requeued events in [start, end] inclusive."""
    rows = conn.execute("SELECT stage, timestamp FROM events WHERE type IN ('sent', 'requeued')").fetchall()
    new_count = follow_up_count = 0
    for row in rows:
        d = _local_date(row["timestamp"])
        if not (start <= d <= end):
            continue
        if row["stage"] == 0:
            new_count += 1
        else:
            follow_up_count += 1
    return {"new": new_count, "follow_up": follow_up_count, "total": new_count + follow_up_count}


def today_strip(conn, global_config, *, today: date = None, campaigns_dir: Path = None) -> dict:
    today = today or date.today()
    sent = _sent_split(conn, today, today)
    daily_cap = global_config.schedule.daily_cap
    # cap_used_pct is driven by the SAME headroom calculation the runner
    # itself enforces (§8: gauge must include follow-ups firing today, not
    # just events already sent) — reusing runner.cap_headroom rather than a
    # second, independent estimate keeps the gauge from ever disagreeing
    # with what the runner will actually do.
    headroom = cap_headroom(conn, global_config, today=today)
    cap_used = daily_cap - headroom
    return {
        "active_campaigns": discover_campaigns(campaigns_dir) if campaigns_dir else discover_campaigns(),
        "sent": sent,
        "daily_cap": daily_cap,
        "cap_used_pct": round(100 * cap_used / daily_cap) if daily_cap else 0,
        "replies_today": _count_events_on(conn, "reply", today),
        "clicks_today": _count_events_on(conn, "click", today),
    }


def this_week(conn, *, today: date = None) -> dict:
    """Rolling 7-day window ending today (not a calendar Mon-Sun week)."""
    today = today or date.today()
    week_start = today - timedelta(days=6)
    return {
        "range_start": week_start,
        "range_end": today,
        "sent": _sent_split(conn, week_start, today),
        "replies": _count_events_in_range(conn, "reply", week_start, today),
        "clicks": _count_events_in_range(conn, "click", week_start, today),
    }


def _count_by_stage(conn, event_type: str) -> dict:
    """Keyed by STRING stage number, not int — an iron-audit BLOCKER fix.
    engagement_intelligence()'s output is cached in Redis via JSON, and
    JSON object keys are always strings; a dict keyed by Python ints
    (`{0: 5}`) silently comes back as `{"0": 5}` after a round-trip, and
    `{"0": 5}.get(0, 0)` (an int lookup, what the template used to do)
    returns 0 — a real, deterministic, SILENT bug that zeroed out these two
    panels on every cache hit (the DOMINANT case — a fresh cache is served
    for ~59 of every 60 minutes). Returning string keys here directly,
    matching the template's now-also-fixed string lookup, means the live
    and JSON-round-tripped versions of this dict are IDENTICAL from the
    start — no discrepancy to accidentally reintroduce later."""
    rows = conn.execute(
        "SELECT stage, COUNT(*) AS c FROM events WHERE type = ? GROUP BY stage", (event_type,)
    ).fetchall()
    return {str(r["stage"]): r["c"] for r in rows}


def _reply_rate_by_persona(conn) -> dict:
    persona_totals: dict = {}
    persona_of: dict = {}
    for row in conn.execute("SELECT recipient, persona FROM recipients WHERE persona IS NOT NULL"):
        persona_totals[row["persona"]] = persona_totals.get(row["persona"], 0) + 1
        persona_of[row["recipient"]] = row["persona"]

    persona_replied: dict = {}
    replied_recipients = {r["recipient"] for r in conn.execute("SELECT DISTINCT recipient FROM events WHERE type = 'reply'")}
    for recipient in replied_recipients:
        persona = persona_of.get(recipient)
        if persona:
            persona_replied[persona] = persona_replied.get(persona, 0) + 1

    return {
        persona: round(100 * persona_replied.get(persona, 0) / total, 1)
        for persona, total in persona_totals.items()
    }


def _time_to_first_reply_distribution(conn) -> dict:
    rows = conn.execute(
        "SELECT first_sent_at, replied_at FROM recipients "
        "WHERE replied_at IS NOT NULL AND first_sent_at IS NOT NULL"
    ).fetchall()
    buckets = {"same_day": 0, "1_2_days": 0, "3_7_days": 0, "8_plus_days": 0}
    for row in rows:
        sent = datetime.fromisoformat(row["first_sent_at"])
        replied = datetime.fromisoformat(row["replied_at"])
        delta_days = (replied - sent).total_seconds() / 86400
        if delta_days < 1:
            buckets["same_day"] += 1
        elif delta_days < 3:
            buckets["1_2_days"] += 1
        elif delta_days < 8:
            buckets["3_7_days"] += 1
        else:
            buckets["8_plus_days"] += 1
    return buckets


def engagement_intelligence(conn) -> dict:
    reply_rate_by_persona = _reply_rate_by_persona(conn)
    reply_by_stage = _count_by_stage(conn, "reply")
    click_by_stage = _count_by_stage(conn, "click")
    time_to_first_reply = _time_to_first_reply_distribution(conn)
    # "no engagement data yet" honestly collapses the three sub-tables
    # instead of showing four rows of zeros before any campaign activity
    # exists. A non-empty reply_rate_by_persona (even at 0%) still counts as
    # real data — it means recipients have actually been contacted, so a 0%
    # rate is informative, not a fabricated placeholder.
    has_data = (
        bool(reply_rate_by_persona) or any(reply_by_stage.values())
        or any(click_by_stage.values()) or any(time_to_first_reply.values())
    )
    return {
        "reply_rate_by_persona": reply_rate_by_persona,
        "reply_by_stage": reply_by_stage,
        "click_by_stage": click_by_stage,
        "time_to_first_reply": time_to_first_reply,
        "has_data": has_data,
    }


def needs_triage(conn) -> list:
    """Recipients who replied but haven't been tagged real/OOO/not-interested
    yet (§8's actionable Replies section). A later `ooo_tagged` or
    `reply_reviewed` event resolves the reply — the same 'any later closing
    event' pattern as due_for_ooo_resend()/latest_open_draft_id()."""
    rows = conn.execute(
        """
        SELECT r1.recipient, r1.campaign, r1.stage, r1.timestamp FROM events r1
        WHERE r1.type = 'reply'
        AND r1.id = (SELECT MAX(id) FROM events WHERE recipient = r1.recipient AND type = 'reply')
        AND NOT EXISTS (
            SELECT 1 FROM events e2
            WHERE e2.recipient = r1.recipient AND e2.type IN ('ooo_tagged', 'reply_reviewed')
            AND e2.id > r1.id
        )
        ORDER BY r1.timestamp DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def actionable_replies(conn, consumer_domains: set) -> list:
    """needs_triage() plus prior-contact domain context (§8: "each row shows
    prior-contact context from the domain history"), reusing step 7's
    dedup check rather than re-deriving the same aggregation."""
    result = []
    for reply in needs_triage(conn):
        result.append({**reply, "dedup_context": check_recipient(conn, reply["recipient"], consumer_domains)})
    return result


def tag_reply(conn, recipient: str, tag: str, *, resume_date: date = None, api_key: str = None,
              unsubscribe_fn=None) -> None:
    """The single underlying action behind BOTH OOO entry points: the
    original reply-tag widget (dashboard.html, gated on a detected `reply`
    event) and the manual "Mark OOO" action on every Reach-outs row
    (reachouts.html, unconditional — any recipient, any time, no reply
    needed). Both hit the exact same `/reply/<recipient>/tag` route, which
    calls this one function — never duplicated.

    'ooo' now requires `resume_date` (the owner-chosen date this recipient
    is expected back) and, before any local state changes, calls
    `unsubscribe_fn` (slap.gmass.unsubscribe_recipient) to suppress GMass's
    own native follow-up timer for this recipient — deliberately FIRST,
    since that's the step that actually prevents a double-send (GMass firing
    a native stage while SLAP separately, later, also fires one manually).
    If that call raises, this function raises too and NOTHING is recorded
    locally — a locally-recorded pause with no working GMass-side
    suppression would be worse than not marking OOO at all: it would look
    "handled" on the dashboard while GMass's native timer stayed fully live.
    See slap.gmass.unsubscribe_recipient's docstring for why this is
    account-wide, not per-campaign.

    'not_interested' now ALSO calls `unsubscribe_fn` first, same as 'ooo' —
    an explicit "not interested" is a stronger stop signal than an
    auto-detected OOO (a temporary pause, not a request to stop), so it gets
    the same GMass-side suppression and the same "nothing recorded locally
    if the call fails" guarantee. 'real' is the one tag left as pure triage
    bookkeeping with no side effect — a genuinely engaged reply should never
    be touched.

    `unsubscribe_fn` defaults to None and is resolved to
    `gmass.unsubscribe_recipient` INSIDE this function body, not as a bound
    default parameter — a default parameter value is captured once, at
    module-import time, which would silently ignore a test's
    `patch("slap.dashboard.gmass.unsubscribe_recipient", ...)` (the patch
    replaces the module attribute; a stale bound-at-def-time reference never
    sees it). Resolving it here instead means every call always sees
    whatever `gmass.unsubscribe_recipient` currently is."""
    if tag not in ("real", "ooo", "not_interested"):
        raise ValueError(f"unknown tag {tag!r} — must be 'real', 'ooo', or 'not_interested'")
    if tag == "ooo":
        if resume_date is None:
            raise ValueError("resume_date is required when tag='ooo'")
        (unsubscribe_fn or gmass.unsubscribe_recipient)(api_key, recipient)
        _tag_ooo(conn, recipient, resume_date)
        return
    if tag == "not_interested":
        (unsubscribe_fn or gmass.unsubscribe_recipient)(api_key, recipient)
    row = conn.execute("SELECT campaign FROM recipients WHERE recipient = ?", (recipient,)).fetchone()
    campaign = row["campaign"] if row else None
    append_event(conn, type="reply_reviewed", recipient=recipient, campaign=campaign, meta={"tag": tag})


def _followups_scheduled(conn, global_config, *, today: date = None) -> dict:
    today = today or date.today()
    tomorrow = today + timedelta(days=1)
    rows = conn.execute(
        "SELECT recipient, persona, current_stage, first_sent_at FROM recipients "
        "WHERE status = 'active' AND first_sent_at IS NOT NULL"
    ).fetchall()
    due_today, due_tomorrow = [], []
    for row in rows:
        cadence = global_config.personas.get(row["persona"])
        if not cadence:
            continue
        next_stage = row["current_stage"] + 1
        if next_stage > len(cadence):
            continue
        cumulative_days = sum(cadence[:next_stage])
        fire_date = _local_date(row["first_sent_at"]) + timedelta(days=cumulative_days)
        entry = {"recipient": row["recipient"], "next_stage": next_stage, "fire_date": fire_date}
        if fire_date == today:
            due_today.append(entry)
        elif fire_date == tomorrow:
            due_tomorrow.append(entry)
    return {"today": due_today, "tomorrow": due_tomorrow}


def pipeline(conn, global_config, *, today: date = None) -> dict:
    rows = conn.execute("SELECT recipient, current_stage FROM recipients WHERE status = 'active'").fetchall()
    by_stage: dict = {}
    for row in rows:
        by_stage.setdefault(row["current_stage"], []).append(row["recipient"])
    return {
        "mid_sequence_by_stage": by_stage,
        "followups_scheduled": _followups_scheduled(conn, global_config, today=today),
    }


def todays_runs(conn, *, today: date = None) -> dict:
    today = today or date.today()
    rows = conn.execute(
        "SELECT timestamp, type, meta FROM events WHERE type IN "
        "('run_started', 'run_completed', 'run_failed') ORDER BY id"
    ).fetchall()
    runs = []
    current = None
    for row in rows:
        if _local_date(row["timestamp"]) != today:
            continue
        meta = json.loads(row["meta"]) if row["meta"] else {}
        if row["type"] == "run_started":
            current = {"fired_at": row["timestamp"], "sent": None, "failed": None,
                       "still_queued": None, "run_failed": False, "error": None, "retry_count": None}
            runs.append(current)
        elif row["type"] == "run_completed" and current is not None:
            current["sent"] = meta.get("sent")
            current["failed"] = meta.get("failed")
            current["still_queued"] = meta.get("remaining_queued")
        elif row["type"] == "run_failed":
            runs.append({"fired_at": row["timestamp"], "sent": None, "failed": None, "still_queued": None,
                        "run_failed": True, "error": meta.get("error"), "retry_count": meta.get("retry_count")})
            current = None

    # A drain that found nothing to do (sent=0, failed=0, nothing left
    # queued) is a passive no-op, not something worth a row on the
    # dashboard — but a real failure (run_failed) or an in-progress/
    # never-completed run (fields still None) is never hidden.
    def _is_zero_activity(run):
        return not run["run_failed"] and run["sent"] == 0 and run["failed"] == 0 and run["still_queued"] == 0

    meaningful_runs = [r for r in runs if not _is_zero_activity(r)]
    capped = meaningful_runs[-8:]
    return {
        "runs": capped,
        "earlier_count": len(meaningful_runs) - len(capped),
        "current_queue_depth": len(due_recipients(conn)) + len(due_for_ooo_resend(conn)),
    }


def _click_details(conn) -> dict:
    """Every recipient's click history, deduped by `url` (keeping the
    earliest click_time seen for each distinct url) and sorted by
    click_time — the shared "which links did they actually click" detail
    reused by both warm_but_silent() (main dashboard) and reachouts_rows()
    (Reach-outs page), so the two can never disagree about it. Built from
    the same url/click_time meta _sync_clicks() already writes on every
    click event; a click recorded with no url (e.g. a bare event seeded
    before that meta existed, or by a test) is simply omitted here, the same
    "never fabricated" convention _recipient_drop_meta() already follows for
    missing meta."""
    rows = conn.execute(
        "SELECT recipient, stage, meta FROM events WHERE type = 'click' ORDER BY id ASC"
    ).fetchall()
    by_recipient: dict = {}
    for row in rows:
        meta = json.loads(row["meta"]) if row["meta"] else {}
        url = meta.get("url")
        if url is None:
            continue
        click_time = meta.get("click_time")
        entry = by_recipient.setdefault(row["recipient"], {})
        existing = entry.get(url)
        if existing is None or (click_time or "") < (existing["click_time"] or ""):
            entry[url] = {"url": url, "stage": row["stage"], "click_time": click_time}
    return {
        recipient: sorted(clicks.values(), key=lambda c: (c["click_time"] or ""))
        for recipient, clicks in by_recipient.items()
    }


def warm_but_silent(conn) -> list:
    """Recipients who clicked a link but have NOT replied — the highest-
    value signal on the dashboard (a click with no reply means the message
    landed and was read, just not answered yet). Depends entirely on the
    click-tracking fix (see CONTROL_SHEET.md's post-launch click-tracking
    section) — stays honestly empty until real click events exist. "Not
    replied" means no reply event ever, not just "not currently in a reply
    state" — once someone has replied at all they're no longer silent, even
    if a later OOO cycle reopened their sequence.

    `clicks` (added post-launch) is each recipient's deduped-by-url click
    detail from _click_details() — `stages_clicked` (bare stage numbers) is
    kept exactly as before for anything already relying on it."""
    click_rows = conn.execute(
        "SELECT recipient, campaign, stage FROM events WHERE type = 'click' ORDER BY recipient, stage"
    ).fetchall()
    replied = {r["recipient"] for r in conn.execute("SELECT DISTINCT recipient FROM events WHERE type = 'reply'")}
    click_details = _click_details(conn)

    by_recipient: dict = {}
    for row in click_rows:
        if row["recipient"] in replied:
            continue
        entry = by_recipient.setdefault(
            row["recipient"], {"recipient": row["recipient"], "campaign": row["campaign"], "stages_clicked": []}
        )
        # A click's stage is only ever None if the recipient wasn't yet in
        # the recipients cache at sync time (shouldn't happen — a click can
        # only follow a sent event, which always upserts the cache) — never
        # render a literal "None" in the stages-clicked list either way.
        if row["stage"] is not None and row["stage"] not in entry["stages_clicked"]:
            entry["stages_clicked"].append(row["stage"])

    for recipient, entry in by_recipient.items():
        entry["clicks"] = click_details.get(recipient, [])

    return sorted(by_recipient.values(), key=lambda e: e["recipient"])


WARM_BUT_SILENT_WIDGET = "warm_but_silent"


def _warm_but_silent_hidden_recipients(conn) -> set:
    """Recipients whose Warm-but-silent row is CURRENTLY hidden — not a bare
    read of ui_state.list_hidden(), because a hide auto-resurfaces once a
    NEWER click has landed since (the point of hiding is "I've seen this,
    nothing new here" — a fresh click is new information worth resurfacing).
    ui_state itself has no opinion on that rule (see its own docstring) —
    this is where it's applied.

    Deliberately uses each `click` event's own `timestamp` column (always a
    reliable, consistently-formatted UTC value SLAP itself wrote at record
    time — see slap/tracking.py's "all timestamps are UTC" rule), NOT
    _click_details()'s `meta.click_time` (GMass's own reported value,
    deduped to the EARLIEST time per distinct url — exactly wrong here: a
    RE-click of a url the recipient already clicked before hiding would be
    silently discarded by that dedup and would never resurface the row).
    `events.timestamp` has no such dedup — every click, including a repeat
    on the same url, advances it."""
    hidden_rows = ui_state.list_hidden(conn, WARM_BUT_SILENT_WIDGET)
    if not hidden_rows:
        return set()
    still_hidden = set()
    for row in hidden_rows:
        recipient, hidden_since = row["recipient"], row["hidden_at"]
        latest_row = conn.execute(
            "SELECT MAX(timestamp) AS latest FROM events WHERE recipient = ? AND type = 'click'",
            (recipient,),
        ).fetchone()
        latest_click = latest_row["latest"] or ""
        if latest_click and latest_click > hidden_since:
            continue  # a newer click since hiding -- resurfaced, not hidden
        still_hidden.add(recipient)
    return still_hidden


def visible_warm_but_silent(conn, rows: list) -> list:
    """`rows` (warm_but_silent()'s output, possibly served from the hourly
    Redis cache) minus whichever recipients are currently hidden. Takes the
    already-computed rows rather than recomputing them so hide/unhide stays
    instant even though the underlying click/reply data can be up to an hour
    stale — hidden-state itself is never cached, it's read fresh every
    request."""
    hidden = _warm_but_silent_hidden_recipients(conn)
    return [r for r in rows if r["recipient"] not in hidden]


def _latest_bounce_meta(conn, recipient: str) -> dict:
    """This recipient's most recent bounce-lifecycle event's full `meta`
    dict (`category` + `bounce_reason`/`bounce_time`) — matches the same
    latest-event-wins convention every other "current state" derivation in
    this app already uses (e.g. reply_tags(), needs_triage()). One query
    backs both the category badge and the reason text (bounces()/
    reachouts_rows()) rather than querying twice for data already fetched
    once. A pre-existing event recorded before `category`/`bounce_reason`
    existed simply lacks those keys — callers default them via `.get()`,
    never guess."""
    row = conn.execute(
        "SELECT meta FROM events WHERE recipient = ? AND type = 'bounce' ORDER BY id DESC LIMIT 1",
        (recipient,),
    ).fetchone()
    if row is None or not row["meta"]:
        return {}
    return json.loads(row["meta"])


def bounces(conn) -> list:
    """Bounced/blocked/undeliverable recipients — already recorded in
    `events` but previously invisible on the dashboard, so a dead address
    could keep getting silently "followed up" forever with no owner
    visibility. GMass reports bounces and blocks as two separate categories
    (see module docstring) — both are surfaced here, distinguished by
    `category` per row, rather than blended into one indistinguishable
    list. `reason` is GMass's own `bounceReason`/`blockReason` text
    (already captured in `meta.bounce_reason` at sync time — see
    _sync_bounces()/_sync_blocks() — but previously never read back out
    for display, so every row just showed a generic "Bounced"/"Blocked"
    label with no detail on WHY). Defaults to "" (never guessed) for a
    pre-existing event recorded before `bounce_reason` existed, or one
    GMass itself returned with no reason text at all."""
    rows = conn.execute(
        "SELECT recipient, campaign, last_event_at FROM recipients "
        "WHERE status = 'bounced' ORDER BY last_event_at DESC"
    ).fetchall()
    result = []
    for r in rows:
        meta = _latest_bounce_meta(conn, r["recipient"])
        result.append({
            **dict(r),
            "category": meta.get("category", "bounce"),
            "reason": meta.get("bounce_reason") or "",
        })
    return result


def companies_contacted(conn, consumer_domains: set, *, today: date = None) -> dict:
    """Distinct non-consumer company domains actually contacted (this week +
    all-time) plus the top companies by headcount — supports DIY dedup
    awareness (§6), built on the same domain_index() the `domains` command
    itself uses (one source of truth). "Contacted" requires an actual send
    (first_sent_at set) — a merely staged-but-never-sent recipient doesn't
    count, or this would overstate real outreach."""
    today = today or date.today()
    week_start = today - timedelta(days=6)
    index = domains.domain_index(conn)

    non_consumer = {}
    for domain, contacts in index.items():
        if domain in consumer_domains:
            continue
        sent_contacts = [c for c in contacts if c.first_sent_at]
        if sent_contacts:
            non_consumer[domain] = sent_contacts

    this_week_domains = {
        d for d, contacts in non_consumer.items()
        if any(week_start <= _local_date(c.first_sent_at) <= today for c in contacts)
    }
    top_companies = sorted(
        ((d, len(contacts)) for d, contacts in non_consumer.items()), key=lambda t: (-t[1], t[0])
    )[:5]

    return {
        "all_time_count": len(non_consumer),
        "this_week_count": len(this_week_domains),
        "top_companies": top_companies,
    }


def next_drain(conn, global_config) -> dict:
    """The next scheduled fire window + current queue depth (§new widget),
    so "N queued, fires ~9am" is visible at a glance. No new scheduling
    logic — just surfaces the existing schedule config plus a live count
    from the same due_recipients()/due_for_ooo_resend() the runner itself
    drains from."""
    return {
        "fire_window_start": global_config.schedule.fire_window_start,
        "fire_window_end": global_config.schedule.fire_window_end,
        "queue_depth": len(due_recipients(conn)) + len(due_for_ooo_resend(conn)),
    }


# --- Reach-outs: all-campaigns, filterable, read-only recipient table ------
#
# Post-launch page, not in the original Build Order. See CONTROL_SHEET.md for
# the full set of findings from investigating the actual schema before
# designing this — the short version: "company"/"req_id" were never
# persisted anywhere before this feature (see slap.queue.stage_recipient's
# docstring for the new, additive `queued`-event-meta capture this depends
# on), and `recipients.status`'s real values (active/done/replied/bounced/
# ooo_requeued) don't match the brief's original queued/sent/failed/bounced
# strawman — "queued" and "failed" are derived here rather than being real
# column values (see reachouts_rows()'s own docstring for exactly how).

def _clicked_recipients(conn) -> set:
    """Every recipient with at least one 'click' event, ever — the exact
    same criterion warm_but_silent() already uses (a click event's mere
    existence), exposed here as a reusable set so this page's 'clicked'
    engagement bucket can never define it differently. Deliberately a new,
    independent function rather than a refactor of warm_but_silent() itself
    (which needs more than a flat set — a per-recipient stage breakdown) —
    test_dashboard.py pins that the two can never disagree on WHICH
    recipients count as clicked."""
    return {r["recipient"] for r in conn.execute("SELECT DISTINCT recipient FROM events WHERE type = 'click'")}


def reply_tags(conn) -> dict:
    """Every recipient's resolved reply-tag status — 'untagged' (replied,
    pending triage), 'ooo', 'real', or 'not_interested' — keyed by
    recipient; a recipient who has never replied at all is simply absent
    from this dict (there's nothing to tag). Mirrors needs_triage()'s exact
    "latest of reply/ooo_tagged/reply_reviewed event wins" resolution rule
    (see that function's own docstring for why that's the right criterion),
    generalized from "is it still open" to "what did it resolve to."
    Deliberately a fresh, independent read of the same event types rather
    than a refactor of needs_triage() itself, to avoid touching that
    already-tested query for an unrelated feature — test_dashboard.py pins
    that a recipient in needs_triage()'s result always maps to 'untagged'
    here, and vice versa, so the two can never silently drift apart."""
    rows = conn.execute(
        "SELECT recipient, type, meta FROM events WHERE type IN ('reply', 'ooo_tagged', 'reply_reviewed') "
        "ORDER BY id ASC"
    ).fetchall()
    latest: dict = {}
    for row in rows:
        latest[row["recipient"]] = row  # ORDER BY id ASC -> last write per recipient wins
    tags = {}
    for recipient, row in latest.items():
        if row["type"] == "reply":
            tags[recipient] = "untagged"
        elif row["type"] == "ooo_tagged":
            tags[recipient] = "ooo"
        elif row["type"] == "reply_reviewed":
            meta = json.loads(row["meta"]) if row["meta"] else {}
            tags[recipient] = meta.get("tag")
    return tags


def _recipient_drop_meta(conn) -> dict:
    """Per recipient, the company/role/req_id captured at their MOST RECENT
    `queued` event (see slap.queue.stage_recipient's docstring for why
    these ride in meta — the exact same precedent already established for
    persona). A recipient staged before this capture existed, or whose drop
    simply left a field blank, has an empty string here — never fabricated,
    never backfilled from anywhere else (e.g. never guessed from a
    rendered email body, which DOES contain the filled-in value as
    unstructured prose but is not a reliable, parseable source of it)."""
    rows = conn.execute(
        "SELECT recipient, meta FROM events WHERE type = 'queued' ORDER BY id ASC"
    ).fetchall()
    result: dict = {}
    for row in rows:
        meta = json.loads(row["meta"]) if row["meta"] else {}
        result[row["recipient"]] = {
            "company": meta.get("company") or "",
            "role": meta.get("role") or "",
            "req_id": meta.get("req_id") or "",
        }
    return result


def _corrected_from_by_recipient(conn) -> dict:
    """Per recipient, the original bounced address this recipient's send was
    corrected from (slap.queue.resend_bounced()'s `corrected_from` meta on
    the new recipient's own `queued` event) — a recipient absent here was
    never created via that path. Kept as its own lookup (rather than a new
    key on _recipient_drop_meta()'s return dict) so that function's
    existing, tested `{company, role, req_id}` shape never changes."""
    rows = conn.execute(
        "SELECT recipient, meta FROM events WHERE type = 'queued' ORDER BY id ASC"
    ).fetchall()
    result: dict = {}
    for row in rows:
        meta = json.loads(row["meta"]) if row["meta"] else {}
        if "corrected_from" in meta:
            result[row["recipient"]] = meta["corrected_from"]
    return result


def _already_corrected_to(conn) -> dict:
    """Reverse of _corrected_from_by_recipient(): per ORIGINAL bounced
    recipient, every new recipient they were later corrected to (in call
    order), each with that new recipient's CURRENT status — an iron-audit
    SHOULD-FIX: without this, a bounced row's Reach-outs detail had no
    memory that a correction already happened, so nothing stopped the owner
    from not noticing and submitting a second, redundant correction for the
    same bounce. This is purely informational and does NOT hide or disable
    the resend form itself (warn, don't block) — the corrected address can
    itself bounce and legitimately need a second correction."""
    corrected_from = _corrected_from_by_recipient(conn)
    reverse: dict = {}
    for new_recipient, original in corrected_from.items():
        reverse.setdefault(original, []).append(new_recipient)
    result: dict = {}
    for original, new_recipients in reverse.items():
        entries = []
        for nr in new_recipients:
            row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", (nr,)).fetchone()
            entries.append({"recipient": nr, "status": row["status"] if row else None})
        result[original] = entries
    return result


def _status_chip(*, status: str, engagement: str, reply_tag, bounce_category, bounce_reason,
                  ooo_resume_date, num_clicks: int) -> dict:
    """One computed `{color, label}` per Reach-outs row — folds status,
    engagement, reply_tag, and bounce category/reason into a single display
    value (see the Reach-outs layout redesign) instead of several columns
    that could silently disagree. `color` follows the badge precedence
    bounced > replied > clicked > none: bounced is the most actionable,
    time-sensitive fact regardless of any PRIOR engagement. `status` and
    `engagement` are independently derived (see reachouts_rows()'s own
    docstring), so a recipient CAN genuinely be both 'bounced' and
    previously 'replied'/'clicked' (e.g. an OOO resend that later bounces) —
    that history isn't lost, it's just not one of the three colors; OOO/
    not-interested/active/done/queued get a label only, no color, since
    none of those is part of the 3-color ask.

    `bounce_category`/`bounce_reason` come straight from _latest_bounce_meta()
    (the raw event meta) — `bounce_category` defaults to "bounce" the same
    way bounces() already does, `bounce_reason` is None/falsy when GMass
    gave no reason text, never fabricated."""
    if status == "bounced":
        label = "Blocked" if bounce_category == "block" else "Bounced"
        if bounce_reason:
            label = f"{label} — {bounce_reason}"
        return {"color": "critical", "label": label}

    if status == "ooo_requeued":
        label = f"OOO — resumes {ooo_resume_date}" if ooo_resume_date else "OOO"
        return {"color": None, "label": label}

    if reply_tag == "not_interested":
        return {"color": None, "label": "Not interested"}

    if engagement == "replied":
        return {"color": "good", "label": "Replied"}

    if engagement == "clicked":
        return {"color": "serious", "label": f"Clicked ({num_clicks})" if num_clicks > 1 else "Clicked"}

    if status in ("done", "active", "queued"):
        return {"color": None, "label": status.capitalize()}
    return {"color": None, "label": status or "—"}


def template_failures() -> list:
    """The Template Failures tab's data: whatever slap.reload's most recent
    `template-reload` run recorded as failed and still unresolved. A plain
    local JSON read (see slap.reload's module docstring for why failures
    live there, not in the events table) — no GMass call, no DB read at all,
    so this can never be stale in the way the GMass-dependent widgets can."""
    return reload.load_failures()


def reachouts_rows(conn) -> list:
    """One row per recipient (the `recipients` cache's own natural grain —
    a recipient's single current row already reflects whichever campaign
    they're most recently associated with), spanning every campaign with no
    restriction — the full read-only dataset behind the Reach-outs page,
    before any filtering. Every category reuses an existing definition
    rather than recomputing one slightly differently:

    - engagement: 'replied' (recipients.replied_at IS NOT NULL — the same
      first-write-wins signal engagement_intelligence()/
      companies_contacted() already rely on) takes priority over 'clicked'
      (_clicked_recipients(), same criterion as warm_but_silent()); else
      'none'.
    - reply_tag: reply_tags() (mirrors needs_triage()'s resolution rule);
      None for a recipient who's never replied.
    - status: recipients.status, EXCEPT a recipient with first_sent_at IS
      NULL is reported as 'queued' rather than 'active' — the raw status
      column alone can't distinguish "just staged, nothing sent yet" from
      "sent at least once, still mid-sequence," both of which are 'active'.
      There is no 'failed' status here on purpose: send_failed is a
      transient per-attempt event, not a resting state — it's always
      retried automatically by the next drain (§11), so a recipient is
      never durably "in a failed state" the way they can durably be
      bounced/replied/done.
    - date: first_sent_at if set, else last_event_at — covers a
      queued-but-never-sent recipient (whose first_sent_at is always None)
      with their queued timestamp instead of leaving them dateless.
    - domain: domains.domain_of(recipient) — always reliable (derived live
      from the recipient's own email), unlike company below.
    - company / req_id_present: _recipient_drop_meta() — only reliable for
      recipients staged after that capture shipped; blank/False for older
      ones, never guessed.
    - ooo_resume_date: ONLY set while status == 'ooo_requeued' — investigated
      before building (see CONTROL_SHEET.md): status and reply_tag already
      correctly reflect an unconditional Mark-OOO with zero prior reply (both
      derive from the same `ooo_tagged` event regardless of any `reply`
      event's existence), so the actual gap was that the resume date itself
      was never surfaced anywhere. Reuses slap.queue._pending_ooo_resume_date
      — the exact same "what's the next OOO-driven due date for this
      recipient's current campaign" logic due_for_ooo_resend() already relies
      on — rather than re-deriving it from event meta a second time here.
      Gated on status=='ooo_requeued' rather than "pending is not None"
      because a mid-multi-stage continuation (status back to 'active'
      between resends, pending still non-None per that function's own
      docstring) is functionally indistinguishable from a normal active
      recipient's next scheduled stage — see slap.runner._send_ooo_resend's
      `next_resume_date` (just the persona's normal inter-stage gap from the
      day that resend fired), so surfacing it as "still OOO" here would be
      misleading. A pre-resume_date-feature `ooo_tagged` event (or any other
      reason the helper returns date.min/None) has no genuine specific date
      to show — left None rather than rendering a fabricated date.min.
      Deliberately NOT sourced from reply_tag: reply_tags() only updates on a
      later reply/ooo_tagged/reply_reviewed event, so once a recipient
      actually resumes (a `requeued` event, not one of those three types)
      their reply_tag stays 'ooo' forever — status is the dimension that
      self-corrects back to normal, which is also why it's the one wired up
      as the "currently OOO" filter (see reachouts.html; already dynamically
      lists every live status value with no template change needed).

    - bounce_category / bounce_reason: ONLY set while status == 'bounced' —
      same "gate the per-recipient lookup query on the status that actually
      implies it" pattern ooo_resume_date above already established. From
      _latest_bounce_meta() (the raw event meta; same source bounces()
      already uses) — this page previously showed a bare "bounced" status
      with no detail on why, same gap the Bounces & Blocks widget had.
    - chip: _status_chip()'s computed `{color, label}` — the single visual
      status indicator the Reach-outs layout redesign uses in place of
      separate Status/Engagement/Reply-tag columns (see that function's own
      docstring for the precedence rule).
    - clicks: this recipient's click detail from _click_details() (deduped
      by url, sorted by time) — empty list if never clicked or no url meta
      was ever captured for their clicks. Same shared helper
      warm_but_silent() uses, so the two never disagree on "what did they
      click."
    - corrected_from / already_corrected_to: bounce-remediation traceability
      (slap.queue.resend_bounced()). corrected_from is the ORIGINAL bounced
      address this row was created to fix (None for anyone not created via
      that path); already_corrected_to is the reverse — every recipient a
      given bounced row was LATER corrected to, each with their current
      status — an iron-audit SHOULD-FIX so a bounced row's own detail shows
      whether it was already resent, rather than silently offering the same
      "Resend to corrected address" action with no memory of prior attempts.
      Purely informational; never hides or disables the resend action
      itself (warn, don't block — a corrected address can itself bounce and
      legitimately need a second correction).
    """
    clicked = _clicked_recipients(conn)
    tags = reply_tags(conn)
    drop_meta = _recipient_drop_meta(conn)
    click_details = _click_details(conn)
    corrected_from_map = _corrected_from_by_recipient(conn)
    already_corrected_map = _already_corrected_to(conn)
    rows = conn.execute("SELECT * FROM recipients").fetchall()

    result = []
    for row in rows:
        recipient = row["recipient"]
        if row["replied_at"]:
            engagement = "replied"
        elif recipient in clicked:
            engagement = "clicked"
        else:
            engagement = "none"

        status = row["status"]
        if row["first_sent_at"] is None and status == "active":
            status = "queued"

        ooo_resume_date = None
        if status == "ooo_requeued":
            pending = _pending_ooo_resume_date(conn, recipient, row["campaign"])
            if pending is not None and pending != date.min:
                ooo_resume_date = pending.isoformat()

        bounce_category = None
        bounce_reason = None
        if status == "bounced":
            bounce_meta = _latest_bounce_meta(conn, recipient)
            bounce_category = bounce_meta.get("category", "bounce")
            bounce_reason = bounce_meta.get("bounce_reason") or None

        meta = drop_meta.get(recipient, {"company": "", "role": "", "req_id": ""})
        row_date = row["first_sent_at"] or row["last_event_at"]
        clicks = click_details.get(recipient, [])

        result.append({
            "recipient": recipient,
            "campaign": row["campaign"],
            "persona": row["persona"],
            "status": status,
            "engagement": engagement,
            "reply_tag": tags.get(recipient),
            "domain": domains.domain_of(recipient),
            "company": meta["company"],
            "req_id_present": bool(meta["req_id"]),
            "date": row_date,
            "ooo_resume_date": ooo_resume_date,
            "bounce_category": bounce_category,
            "bounce_reason": bounce_reason,
            "corrected_from": corrected_from_map.get(recipient),
            "already_corrected_to": already_corrected_map.get(recipient, []),
            "clicks": clicks,
            "chip": _status_chip(status=status, engagement=engagement, reply_tag=tags.get(recipient),
                                  bounce_category=bounce_category, bounce_reason=bounce_reason,
                                  ooo_resume_date=ooo_resume_date,
                                  num_clicks=len(clicks) or (1 if engagement == "clicked" else 0)),
            # Precomputed LOCAL calendar date (YYYY-MM-DD), reusing the same
            # _local_date() conversion todays_runs()/companies_contacted()
            # already use — so the client-side date-range filter (reachouts.
            # html) can do a plain ISO-string compare against an
            # <input type=date> value without redoing timezone math in JS
            # (and without ever risking it disagreeing with this function's
            # own, Python-tested date_start/date_end filtering).
            "date_local": _local_date(row_date).isoformat() if row_date else None,
        })
    return result


def filter_reachouts(rows: list, filters: dict) -> list:
    """AND-combine every provided filter dimension over `rows` (the output
    of reachouts_rows()). Every key in `filters` is optional — absent or
    None means "no constraint on this dimension," never "match nothing."
    This is the one, unit-tested definition of "what counts as a match";
    dashboard_templates/reachouts.html's client-side JS mirrors it for
    interactive filtering (see that template's own comment for why the
    actual filtering happens in the browser rather than round-tripping
    through this function on every interaction — a hard requirement here is
    zero network calls per filter/sort change)."""
    result = rows
    if filters.get("campaign"):
        result = [r for r in result if r["campaign"] == filters["campaign"]]
    if filters.get("persona"):
        result = [r for r in result if r["persona"] == filters["persona"]]
    if filters.get("status"):
        result = [r for r in result if r["status"] == filters["status"]]
    if filters.get("engagement"):
        result = [r for r in result if r["engagement"] == filters["engagement"]]
    if filters.get("reply_tag"):
        result = [r for r in result if r["reply_tag"] == filters["reply_tag"]]
    if filters.get("domain"):
        result = [r for r in result if r["domain"] == filters["domain"]]
    if filters.get("req_id_present") is not None:
        result = [r for r in result if r["req_id_present"] == filters["req_id_present"]]
    if filters.get("date_start"):
        # ISO "YYYY-MM-DD" strings compare correctly lexicographically —
        # the same format reachouts_rows()'s date_local uses and an
        # <input type=date> produces, so this needs no date parsing at all.
        result = [r for r in result if r["date_local"] and r["date_local"] >= filters["date_start"]]
    if filters.get("date_end"):
        result = [r for r in result if r["date_local"] and r["date_local"] <= filters["date_end"]]
    if filters.get("search"):
        needle = filters["search"].strip().lower()
        if needle:
            result = [
                r for r in result
                if needle in r["recipient"].lower() or needle in (r["company"] or "").lower()
            ]
    return result


# --- Redis-backed cache for the GMass-dependent widgets (post-launch) ------
#
# Only these four widgets depend on GMass report data at all (confirmed by
# reading this module, not assumed from names): engagement_intelligence(),
# warm_but_silent(), bounces(), actionable_replies(). Every other widget
# (today_strip/this_week, next_drain, todays_runs, pipeline,
# companies_contacted) is already a pure SQLite read that never called
# GMass either before or after this feature — create_app()'s "/" route
# keeps computing them directly, every load, completely untouched.
#
# "Metrics" (today_strip/this_week) is a genuine edge case worth naming
# explicitly: replies_today/clicks_today count reply/click EVENTS, which
# ARE populated by sync_reports() — so their freshness does depend on when
# a sync last ran, same as the four cached widgets. But today_strip()/
# this_week() don't call GMass themselves (they only read `events`), and
# this dependency already existed before this feature (their numbers were
# always only as fresh as the last sync_reports() call, on-open or
# otherwise) — so nothing about this feature changes their behavior one
# way or the other. Left uncached, computed fresh from SQLite every load,
# same as every other genuinely local-only widget.

def compute_gmass_dependent_data(conn, api_key: str, consumer_domains: set) -> dict:
    """The one place that computes everything the dashboard's four GMass-
    dependent widgets need: runs the EXISTING sync_reports() (completely
    unchanged — polls GMass, writes new click/reply/bounce/block events),
    then computes engagement_intelligence()/warm_but_silent()/bounces()/
    actionable_replies() fresh from the now-updated SQLite. Both the hourly
    `slap.py sync` job and the dashboard's on-open fallback call this exact
    function via gmass_cache.refresh_with_lock() — never two independent
    implementations of "go get fresh GMass data."

    Fully JSON-serializable (no datetime/dataclass objects) so it can be
    written straight into Redis. Renders identically whether it's used
    live or read back from a JSON round-trip: a Jinja template's `.attr`
    access on a plain dict falls back to item access, so
    `{{ r.dedup_context.hard_warning }}` works the same either way
    (verified directly, not assumed)."""
    sync_result = sync_reports(conn, api_key)
    replies = actionable_replies(conn, consumer_domains)
    return {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "sync_result": {
            "synced_at": sync_result["synced_at"].isoformat(),
            "new_replies": sync_result["new_replies"],
            "new_clicks": sync_result["new_clicks"],
            "new_bounces": sync_result["new_bounces"],
            "errors": sync_result["errors"],
        },
        "engagement": engagement_intelligence(conn),
        "warm_but_silent": warm_but_silent(conn),
        "bounces": bounces(conn),
        "replies": [{**r, "dedup_context": dataclasses.asdict(r["dedup_context"])} for r in replies],
    }


def _empty_gmass_data() -> dict:
    """Honest empty state for a narrow bootstrap edge case (see
    get_gmass_dependent_data): no cache has EVER been written yet, and
    another process already holds the refresh lock. Matches this
    dashboard's existing philosophy of an honest empty widget over a
    guessed/fabricated one — for exactly one page load, until the
    in-progress refresh completes and populates the cache for real."""
    return {
        "cached_at": None,
        "sync_result": {"synced_at": None, "new_replies": 0, "new_clicks": 0, "new_bounces": 0, "errors": []},
        "engagement": {
            "reply_rate_by_persona": {}, "reply_by_stage": {}, "click_by_stage": {},
            "time_to_first_reply": {"same_day": 0, "1_2_days": 0, "3_7_days": 0, "8_plus_days": 0},
            "has_data": False,
        },
        "warm_but_silent": [],
        "bounces": [],
        "replies": [],
    }


def _background_refresh(db_path: Path, api_key: str, consumer_domains: set, redis_client) -> None:
    """Runs the SAME refresh_with_lock()/compute_gmass_dependent_data() path
    the hourly `slap.py sync` job uses, but on a daemon thread spawned from
    a dashboard request — see get_gmass_dependent_data's docstring for why
    the request itself no longer waits on this.

    Opens its OWN sqlite connection rather than reusing the request's
    `g.db_conn`: sqlite3 connections default to `check_same_thread=True`
    (unusable from this thread), and even setting that aside,
    `teardown_appcontext` closes the request's connection as soon as that
    request's response finishes sending — almost certainly before a
    multi-second-to-multi-minute GMass sweep completes. `tracking.connect()`
    (not a bare `sqlite3.connect()`) mirrors exactly what `slap.py`'s
    `cmd_sync` already does with its own independent connection — reusing
    an existing pattern, not inventing one.

    Exceptions are caught and printed rather than left to crash silently on
    a background thread with no other visible failure surface — the
    request path that used to run this synchronously would have surfaced a
    failure as a loud request error; this preserves that visibility."""
    conn = tracking.connect(db_path)
    try:
        def do_refresh():
            return compute_gmass_dependent_data(conn, api_key, consumer_domains)
        gmass_cache.refresh_with_lock(redis_client, do_refresh)
    except Exception as e:
        display.error(f"slap dashboard: background GMass refresh failed: {e}")
    finally:
        conn.close()


def _spawn_background_refresh(db_path: Path, api_key: str, consumer_domains: set, redis_client) -> None:
    threading.Thread(
        target=_background_refresh, args=(db_path, api_key, consumer_domains, redis_client), daemon=True,
    ).start()


def get_gmass_dependent_data(api_key: str, consumer_domains: set, redis_client, db_path: Path) -> dict:
    """Orchestrates the dashboard's four GMass-dependent widgets against the
    Redis cache refreshed hourly by `slap.py sync` (slap/gmass_cache.py).
    Never blocks the request on a live GMass poll — a stale/missing/absent
    cache renders whatever's available immediately and heals itself on a
    background thread, never in the request's own critical path.

    - Fresh cache hit: zero GMass calls, zero SQLite recomputation for
      these four widgets — returns the cached blob directly.
    - Stale or missing cache: spawns a background refresh via
      gmass_cache.refresh_with_lock() (the SAME shared, fenced lock both
      `slap.py sync` and this path have always used — safe even if several
      requests land in the stale window and each spawns its own attempt,
      since only one actually runs the sweep) and immediately renders the
      last-known cached data (or an honest empty state if there's none
      yet), tagged so the template can show a "may be stale, refreshing"
      indicator. The request never waits to find out whether ITS spawned
      thread won the lock or another one already had — that distinction
      no longer matters once nothing blocks on the outcome either way.
    - Redis unreachable at read time: does NOT auto-trigger anything — a
      refresh lock is itself a Redis operation, so there is no way to
      coordinate concurrent attempts while Redis is down. An earlier
      version of this function called the live-poll refresh directly in
      this branch, which meant a Redis outage with more than one
      concurrent dashboard load could fire multiple simultaneous,
      *uncoordinated* GMass sweeps — worse than the stale-cache case, which
      can only ever have one attempt actually run per refresh window.
      Renders an honest empty state instead; self-heals via the manual
      "Refresh now" action once Redis is back, or the next hourly tick.

    Every returned dict carries `cache_status` ('fresh' | 'stale_refreshing'
    | 'redis_unavailable') for the template."""
    try:
        cached = gmass_cache.read_cache(redis_client)
    except gmass_cache.RedisUnavailable:
        return {**_empty_gmass_data(), "cache_status": "redis_unavailable"}

    if cached is not None and gmass_cache.is_fresh(cached):
        return {**cached, "cache_status": "fresh"}

    _spawn_background_refresh(db_path, api_key, consumer_domains, redis_client)

    if cached is not None:
        return {**cached, "cache_status": "stale_refreshing"}
    return {**_empty_gmass_data(), "cache_status": "stale_refreshing"}


def _to_local(value) -> str:
    """UTC (a datetime, or an ISO string as stored in `events`) -> local
    display string. Convert to local only at dashboard display (§5)."""
    if value is None:
        return ""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def create_app(db_path: Path, global_config, consumer_domains: set, api_key: str, *,
                redis_client=None) -> Flask:
    """§8: reads SQLite, renders read-only panels except the single write
    action (reply tagging). The four GMass-dependent widgets (engagement
    intelligence, warm-but-silent, bounces, replies-needing-triage) are
    served from the Redis cache slap.py's `sync` command refreshes hourly,
    with an on-open fallback if that cache is stale/missing/unreachable —
    see get_gmass_dependent_data's own docstring. Every other panel is an
    unchanged, direct SQLite read, exactly as before this feature.

    Takes a `db_path`, not an open connection: Flask's dev server (and any
    real WSGI server) dispatches each request on its own thread, and
    sqlite3 connections are only usable on the thread that created them
    (`check_same_thread` defaults to True). A connection opened once at
    startup and closed over by the route functions works for the first
    request and then raises `sqlite3.ProgrammingError` on every request
    handled by a different thread. Each request instead lazily opens its
    own connection (cached on Flask's per-request `g`) and closes it via
    `teardown_appcontext`, the standard Flask SQLite pattern.

    `redis_client` defaults to None and is resolved to a real client (built
    from `global_config.redis_url`) inside this function's own body, not as
    a bound default parameter — unlike a sqlite3 connection, ONE redis.Redis
    client is the correct, thread-safe, connection-pooling way to use it
    across every request (see slap.gmass_cache.redis_client_from_url), so
    it's built once here rather than per-request like get_conn() above."""
    app = Flask(__name__, template_folder=TEMPLATE_FOLDER)
    app.jinja_env.filters["to_local"] = _to_local
    redis_client = redis_client if redis_client is not None else gmass_cache.redis_client_from_url(
        global_config.redis_url
    )
    app.redis_client = redis_client  # exposed as a plain attribute so tests can inspect/seed cache state

    def get_conn():
        if "db_conn" not in g:
            g.db_conn = sqlite3.connect(db_path)
            g.db_conn.row_factory = sqlite3.Row
        return g.db_conn

    @app.teardown_appcontext
    def close_conn(exception=None):
        conn = g.pop("db_conn", None)
        if conn is not None:
            conn.close()

    @app.route("/")
    def index():
        conn = get_conn()
        gmass_data = get_gmass_dependent_data(api_key, consumer_domains, redis_client, db_path)
        # Hide/unhide (post-launch): applied HERE, on top of the possibly-
        # hourly-cached warm_but_silent list, never baked into the cache
        # itself — see visible_warm_but_silent()'s own docstring for why
        # (hidden-state must stay instant, not wait for the next hourly
        # refresh). show_hidden=1 shows every row (hidden ones get an
        # "Unhide" action instead of "Hide") rather than a separate page.
        all_warm_but_silent = gmass_data["warm_but_silent"]
        hidden_recipients = _warm_but_silent_hidden_recipients(conn)
        show_hidden = request.args.get("show_hidden") == "1"
        visible_warm_but_silent_rows = (
            all_warm_but_silent if show_hidden
            else [r for r in all_warm_but_silent if r["recipient"] not in hidden_recipients]
        )
        return render_template(
            "dashboard.html",
            sync_result=gmass_data["sync_result"],
            engagement=gmass_data["engagement"],
            replies=gmass_data["replies"],
            warm_but_silent=visible_warm_but_silent_rows,
            warm_but_silent_hidden_recipients=hidden_recipients,
            warm_but_silent_hidden_count=len(
                [r for r in all_warm_but_silent if r["recipient"] in hidden_recipients]
            ),
            show_hidden=show_hidden,
            bounces=gmass_data["bounces"],
            cache_status=gmass_data["cache_status"],
            today=today_strip(conn, global_config),
            week=this_week(conn),
            pipeline=pipeline(conn, global_config),
            runs=todays_runs(conn),
            companies=companies_contacted(conn, consumer_domains),
            next_drain=next_drain(conn, global_config),
            # Deliberately different from this dashboard's usual "show an
            # honest empty state" default (see reachouts_rows/reply_tags for
            # that default elsewhere): the Template Failures nav link itself
            # is owner-requested to disappear entirely at zero, not render a
            # "0 failures" link — the /template-failures route below still
            # shows a real empty-state page for anyone who navigates there
            # directly regardless of this count.
            template_failures_count=len(template_failures()),
        )

    @app.route("/warm-but-silent/<string:recipient>/hide", methods=["POST"])
    def hide_warm_but_silent(recipient):
        ui_state.hide(get_conn(), recipient, WARM_BUT_SILENT_WIDGET)
        return redirect(url_for("index"))

    @app.route("/warm-but-silent/<string:recipient>/unhide", methods=["POST"])
    def unhide_warm_but_silent(recipient):
        ui_state.unhide(get_conn(), recipient, WARM_BUT_SILENT_WIDGET)
        return redirect(url_for("index", show_hidden=1))

    @app.route("/gmass/refresh", methods=["POST"])
    def gmass_refresh():
        # Manual escalation of get_gmass_dependent_data's own background
        # refresh — e.g. right after tagging a reply, since invalidate()
        # already clears the cache for that action but the auto-refresh
        # otherwise only fires on the NEXT page load. Redirects immediately
        # either way rather than waiting on the sweep, same as the
        # auto-triggered path never blocks the request that spawned it.
        try:
            gmass_cache.ping(redis_client)
        except gmass_cache.RedisUnavailable:
            return redirect(url_for("index"))
        _spawn_background_refresh(db_path, api_key, consumer_domains, redis_client)
        return redirect(url_for("index"))

    @app.route("/reachouts")
    def reachouts():
        # Read-only, local-state-only page (§ Reach-outs): deliberately does
        # NOT call sync_reports() — every other route's on-open GMass poll is
        # optional per the feature's own spec ("if the page itself needs a
        # fresh poll on open, that's fine — but filtering afterward is a pure
        # local-data operation"). Skipping it entirely here is the simplest
        # way to guarantee this page can NEVER make a GMass call, no matter
        # how it's used. Rows are rendered in full (unfiltered) — filtering/
        # sorting happens client-side in the page's own <script>, never a
        # server round-trip, so no pagination or query-param handling is
        # needed here either.
        rows = reachouts_rows(get_conn())
        return render_template("reachouts.html", rows=rows, total_count=len(rows),
                                warning=request.args.get("warning"))

    @app.route("/template-failures")
    def template_failures_page():
        # Always registered regardless of whether any failures currently
        # exist — direct navigation must show a real page (an honest "no
        # failures" empty state), never a 404, even though the nav link to
        # it (index() above) only appears when template_failures_count > 0.
        failures = template_failures()
        return render_template("template_failures.html", failures=failures, total_count=len(failures))

    @app.route("/reply/<string:recipient>/tag", methods=["POST"])
    def reply_tag(recipient):
        # Shared by both OOO entry points (dashboard.html's reply-tag widget
        # and reachouts.html's per-row "Mark OOO" action) — see
        # slap.dashboard.tag_reply's own docstring for why this is one
        # route/one function, not duplicated logic.
        tag = request.form.get("tag", "")
        resume_date_str = request.form.get("resume_date", "").strip()
        resume_date = None
        if resume_date_str:
            try:
                resume_date = date.fromisoformat(resume_date_str)
            except ValueError:
                return f"invalid resume_date {resume_date_str!r} — expected YYYY-MM-DD", 400
        try:
            tag_reply(get_conn(), recipient, tag, resume_date=resume_date, api_key=api_key)
        except ValueError as e:
            return str(e), 400
        except Exception as e:
            # Most likely slap.gmass.unsubscribe_recipient failing (network,
            # invalid key, GMassError) — fail loud with a clear message
            # rather than a bare 500, and confirm nothing was recorded
            # locally either (see tag_reply's docstring: the GMass call runs
            # BEFORE any local event, precisely so a failure here can never
            # leave a false "handled" local pause with no real suppression).
            # Both 'ooo' and 'not_interested' hit this path now (both call
            # unsubscribe_fn first) — the message stays tag-generic.
            return f"could not tag {recipient} ({tag!r}) — GMass suppression call failed, nothing was recorded: {e}", 502
        # Any successful tag can change actionable_replies()'s output (all
        # three tags resolve needs_triage()) — invalidate rather than let
        # the owner's own action sit invisible in a stale cache for up to
        # an hour. Forces the next dashboard load to take the same
        # stale/missing-cache fallback path as any other cache miss, never
        # a separate "partial update" mechanism.
        gmass_cache.invalidate(redis_client)
        redirect_to = request.form.get("redirect_to", "index")
        return redirect(url_for("reachouts" if redirect_to == "reachouts" else "index"))

    @app.route("/reachouts/<string:recipient>/resend", methods=["POST"])
    def resend(recipient):
        # Bounce remediation (post-launch): the Reach-outs row action for a
        # status=='bounced' recipient. Shares slap.queue.resend_bounced()
        # with the `./slap.py bounced` CLI command — see that function's own
        # docstring for why it does NOT run the dedup check itself; this
        # route runs it here, purely for display (informational only, never
        # blocking — the owner has already made an explicit correction by
        # submitting this form).
        corrected_email = request.form.get("corrected_email", "").strip()
        if not corrected_email:
            return "corrected_email is required", 400
        conn = get_conn()
        dedup = check_recipient(conn, corrected_email, consumer_domains)
        warning = None
        if dedup.hard_warning:
            w = dedup.hard_warning
            warning = f"{corrected_email} already contacted — campaign={w.campaign} status={w.status}"
        elif dedup.soft_warning_contacts:
            warning = (f"{len(dedup.soft_warning_contacts)} other contact(s) already on domain "
                       f"{dedup.soft_warning_domain}")
        try:
            resend_bounced(conn, original_recipient=recipient, corrected_email=corrected_email,
                            archive_dir=archive.archive_dir_from_env())
        except QueueError as e:
            return str(e), 400
        return redirect(url_for("reachouts", warning=warning) if warning else url_for("reachouts"))

    return app
