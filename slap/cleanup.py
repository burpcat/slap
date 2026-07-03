"""Stale-PDF cleanup (post-launch feature, not in the original Build Order).

Compiled résumé PDFs are the heavy files in workdir/ — this identifies which
ones are safe to reclaim without ever risking a PDF an active or replied
sequence still needs. Classification is deliberately NOT a file-age (mtime)
delete: every signal comes from the append-only `events` log, cross-referenced
per (campaign, recipient), never from file timestamps and never from the
`recipients` cache alone (the cache is proven rebuild-equivalent to events,
but this gates an irreversible delete, so it re-derives from the source of
truth directly rather than trusting a derived column for a new purpose it
wasn't specifically built for — see queue.due_recipients()'s docstring for
the exact class of bug that pattern previously caused).

Non-obvious architectural fact this logic hinges on: per CLAUDE.md, "Follow-
ups are GMass's job... We build no follow-up scheduler." The app sets the
WHOLE cadence in one send_campaign() call and then has zero visibility into
GMass silently firing stages 1-3 on its own servers — there is no event for
"GMass finished the cadence." So `sent.meta.is_final_stage` is only ever True
when a persona's cadence is empty (never, for any persona currently
configured). For a normal active recipient, the only honest way to know the
follow-up sequence has run its course (rather than still being mid-flight on
GMass's side) is: the recipient's full persona cadence window (sum of the
cadence's stage-day offsets) has elapsed since first_sent_at with no reply.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from slap.config import GlobalConfig
from slap.latex import WORKDIR_ROOT
from slap.queue import MANIFEST_NAME, load_manifest

DEFAULT_MIN_DAYS_IDLE = 15


@dataclass
class Verdict:
    status: str  # "eligible" | "not_yet" | "undetermined"
    reason: str
    days_idle: int = None


@dataclass
class CleanupCandidate:
    campaign: str
    recipient: str
    workdir: Path
    pdf_path: Path
    hash_path: Path
    days_idle: int
    reason: str


@dataclass
class UndeterminedRecipient:
    campaign: str
    recipient: str
    workdir: Path
    reason: str


@dataclass
class CleanupReport:
    eligible: list
    undetermined: list


def _recipient_events(conn, campaign: str, recipient: str) -> list:
    rows = conn.execute(
        "SELECT type, stage, timestamp, meta FROM events "
        "WHERE campaign = ? AND recipient = ? ORDER BY id ASC",
        (campaign, recipient),
    ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["meta"] = json.loads(event["meta"]) if event["meta"] is not None else {}
        events.append(event)
    return events


def classify_recipient(conn, campaign: str, recipient: str, global_config: GlobalConfig, *,
                        min_days_idle: int = DEFAULT_MIN_DAYS_IDLE, now: datetime = None) -> Verdict:
    """Determine whether one (campaign, recipient)'s staged PDF is safe to
    delete, purely from the raw events table. Never looks at file mtime.

    status="eligible"     -> safe to delete (all three conditions hold)
    status="not_yet"      -> determinable, but still needed (active/pending/replied)
    status="undetermined" -> can't tell (fail loud, skip — never guess)
    """
    now = now or datetime.now(timezone.utc)
    events = _recipient_events(conn, campaign, recipient)

    if not events:
        return Verdict("undetermined", "no events found for this recipient — cannot determine state")

    last_event_at = max(datetime.fromisoformat(e["timestamp"]) for e in events)
    days_idle = (now - last_event_at).days
    if days_idle < min_days_idle:
        return Verdict("not_yet", f"only {days_idle}d idle (needs {min_days_idle}d+)", days_idle)

    if any(e["type"] == "reply" for e in events):
        return Verdict("not_yet", "recipient replied — PDF never eligible for cleanup", days_idle)

    open_ooo = any(e["type"] == "ooo_tagged" for e in events) and not any(
        e["type"] == "requeued" for e in events
    )
    if open_ooo:
        return Verdict("not_yet", "OOO resend pending — PDF still needed", days_idle)

    if any(e["type"] == "bounce" for e in events):
        return Verdict("eligible", f"bounced (sequence dead), idle {days_idle}d", days_idle)

    sent_or_requeued = [e for e in events if e["type"] in ("sent", "requeued")]
    if not sent_or_requeued:
        return Verdict("not_yet", "never actually sent (only queued/failed) — PDF still needed for retry", days_idle)

    latest_send = max(sent_or_requeued, key=lambda e: e["timestamp"])
    if latest_send["meta"].get("is_final_stage"):
        return Verdict("eligible", f"final stage confirmed sent, idle {days_idle}d", days_idle)

    # Normal case: GMass owns firing the remaining follow-up stages silently
    # (no event exists for "GMass finished the cadence") — the only honest
    # signal that no further stage is still pending is that the recipient's
    # FULL persona cadence window has elapsed since their first send.
    first_sent = min(
        (e for e in events if e["type"] == "sent"), key=lambda e: e["timestamp"], default=None
    )
    if first_sent is None:
        return Verdict("not_yet", "no initial 'sent' event found — PDF still needed", days_idle)

    persona_rows = [e for e in events if e["type"] == "queued" and e["meta"].get("persona")]
    persona = persona_rows[0]["meta"]["persona"] if persona_rows else None
    if persona is None or persona not in global_config.personas:
        return Verdict("undetermined", f"persona {persona!r} not found in config — cannot verify cadence")

    cadence = global_config.personas[persona]
    window_days = sum(cadence)
    first_sent_at = datetime.fromisoformat(first_sent["timestamp"])
    elapsed_days = (now - first_sent_at).days
    if elapsed_days < window_days:
        return Verdict(
            "not_yet", f"still within {persona} cadence window ({elapsed_days}/{window_days}d elapsed)", days_idle
        )

    return Verdict(
        "eligible", f"{persona} cadence window elapsed ({elapsed_days}/{window_days}d), idle {days_idle}d", days_idle
    )


def find_cleanup_candidates(conn, global_config: GlobalConfig, *,
                             min_days_idle: int = DEFAULT_MIN_DAYS_IDLE,
                             now: datetime = None, workdir_root: Path = WORKDIR_ROOT) -> CleanupReport:
    """Scans workdir/<campaign>/<recipient>/staged.json for every currently
    staged recipient with a PDF still on disk, classifies each via
    classify_recipient(), and returns the eligible/undetermined split. Pure
    read — deletes nothing. Recipients classified "not_yet" (still active,
    pending, or replied) are simply omitted — they aren't candidates and
    aren't a problem, so they don't need reporting."""
    now = now or datetime.now(timezone.utc)
    eligible, undetermined = [], []

    for manifest_path in sorted(workdir_root.glob(f"*/*/{MANIFEST_NAME}")):
        workdir = manifest_path.parent
        campaign, recipient = workdir.parent.name, workdir.name
        manifest = load_manifest(workdir)
        pdf_path = workdir / manifest["attachment_name"]
        if not pdf_path.exists():
            continue  # nothing heavy staged here (already cleaned, or latex-loop aborted)

        verdict = classify_recipient(conn, campaign, recipient, global_config,
                                      min_days_idle=min_days_idle, now=now)
        if verdict.status == "eligible":
            eligible.append(CleanupCandidate(
                campaign=campaign, recipient=recipient, workdir=workdir,
                pdf_path=pdf_path, hash_path=pdf_path.with_name(pdf_path.name + ".hash"),
                days_idle=verdict.days_idle, reason=verdict.reason,
            ))
        elif verdict.status == "undetermined":
            undetermined.append(UndeterminedRecipient(
                campaign=campaign, recipient=recipient, workdir=workdir, reason=verdict.reason,
            ))

    return CleanupReport(eligible=eligible, undetermined=undetermined)


def delete_eligible(candidates: list) -> list:
    """Deletes the PDF + its .pdf.hash sidecar for each candidate. Only ever
    called on candidates that already came from find_cleanup_candidates()'s
    `eligible` list — this function does no classification of its own, so it
    can never delete something classify_recipient() didn't already clear.
    resume.tex is never referenced here and is therefore never at risk.
    Missing sidecar files (e.g. a latex-off campaign has no .hash) are fine —
    delete what exists, don't fail loud over an expected absence."""
    deleted = []
    for candidate in candidates:
        candidate.pdf_path.unlink(missing_ok=True)
        candidate.hash_path.unlink(missing_ok=True)
        deleted.append(candidate)
    return deleted
