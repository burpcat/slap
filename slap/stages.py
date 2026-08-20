"""Pure cadence-schedule math — the single source of truth for "when does a
follow-up fire" and "what stage is a recipient in."

GMass fires follow-up stages 1..N server-side with no read-back API (see
CONTROL_SHEET.md / runner.py's module docstring), so the app never records a
per-stage `sent` event for GMass's own automatic follow-ups. Every "is a stage
due / has it likely fired" question is therefore a best-effort ESTIMATE derived
from `first_sent_at` + the recipient's own recorded `cadence` (day-deltas
between consecutive stages) versus the current date.

That estimate used to be recomputed by hand in four places (runner cap
headroom, dashboard "due today" panel, cleanup window, OOO resend). This module
centralizes the primitive so those callers — and the Reach-outs stage/next-shoot
columns — can never disagree on the arithmetic. All functions are pure: they
take dates + a cadence list and touch neither the DB nor config.

Cadence semantics (unchanged from the callers this replaced): `cadence[i]` is
the day gap from the PREVIOUS stage, so the absolute offset of the Nth
follow-up from the initial send is the cumulative `sum(cadence[:N])`. Stage
numbering is 1-based for follow-ups (stage 1 = first follow-up); "stage 0" is
the initial send itself. All dates are LOCAL calendar dates, matching the
callers (which key off `_local_date(first_sent_at)` / `first_sent_at.date()`).
"""
from __future__ import annotations

from datetime import date, timedelta


def stage_fire_date(first_sent_at: date, cadence: list[int], stage: int) -> date:
    """Absolute local fire date of the Nth follow-up (1-based): stage=1 is the
    first follow-up. Equivalent to the callers' old
    `first_sent_at + timedelta(days=sum(cadence[:stage]))`."""
    return first_sent_at + timedelta(days=sum(cadence[:stage]))


def cadence_window_days(cadence: list[int]) -> int:
    """Total days from the initial send until the last follow-up would fire —
    the full cadence window. Same value cleanup.classify_recipient used as
    `sum(cadence)` to decide a sequence has run its course."""
    return sum(cadence)


def estimate_current_stage(first_sent_at: date, cadence: list[int], today: date,
                           floor: int = 0) -> int:
    """Number of follow-up stages estimated to have fired by `today`
    (0 = only the initial send has gone out). Counts every stage whose
    cumulative fire date is on or before `today`.

    `floor` clamps the result up to a known-fired stage (pass a recipient's
    recorded `current_stage`), so an OOO-advanced recipient — whose stages
    fired on the app's own resend cadence, not the original calendar offsets —
    never reads BELOW the stage the event log already confirms.
    """
    fired = 0
    for i in range(1, len(cadence) + 1):
        if stage_fire_date(first_sent_at, cadence, i) <= today:
            fired += 1
        else:
            break
    return max(fired, floor)


def next_fire_date(first_sent_at: date, cadence: list[int], today: date,
                   from_stage: int = 0) -> date | None:
    """Local fire date of the next follow-up strictly after `today`, or None
    when the whole cadence window has already elapsed (sequence exhausted).

    A stage whose fire date is exactly `today` is treated as already fired
    (consistent with estimate_current_stage counting `<= today`), so it's the
    following stage that's "next". `from_stage` skips stages already known to
    have fired (e.g. an OOO-advanced recipient's recorded `current_stage`).
    """
    for i in range(1, len(cadence) + 1):
        if i <= from_stage:
            continue
        fd = stage_fire_date(first_sent_at, cadence, i)
        if fd > today:
            return fd
    return None


def stage_label(stage: int) -> str:
    """Human label for a stage index: 0 -> "initial", n -> "stageN"
    (matching the Reach-outs Status column's spec: initial, stage1, stage2, …)."""
    return "initial" if stage <= 0 else f"stage{stage}"
