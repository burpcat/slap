"""Queue staging (Build Order step 9) + OOO re-queue tagging (step 10).

`send` (prep) stages a recipient by writing a `queued` event plus a staged
manifest into the recipient's workdir — it does NOT send. The runner (fire)
later asks "what's queued and due?" via due_recipients() and drains. The
queue is just more events; there is no separate queue store (§10).

tag_ooo()/due_for_ooo_resend() are the OOO re-queue counterpart (§7):
ooo_tagged is the "due for resend" marker (like queued), requeued is the
completion marker (like sent) — see slap.tracking's module docstring for
why that mapping means no new schema column is needed.

Manual OOO pause (post-launch): tag_ooo() now takes a mandatory
`resume_date` — the owner-chosen date this recipient is expected back,
covering the case where the OOO notice arrived somewhere SLAP/GMass never
saw at all (no detected `reply` to gate on). **This deliberately supersedes
SLAP_BUILD_PROMPT.md §7's original "no special date parsing, no
per-recipient scheduling" line for the OOO re-queue** — per explicit owner
instruction for this specific feature (a manual, unconditional OOO mark
with a real return date is fundamentally incompatible with that constraint;
the override was the whole point of the request, not an oversight of the
brief). due_for_ooo_resend() holds a
recipient's resend until date.today() >= that date; the original
reply-detected recovery had no such wait (resent on the very next drain).
Subsequent stages beyond the first are a CONTINUATION of the same pause,
not a fresh manual re-tag: slap.runner._send_ooo_resend records the next
stage's own due date directly in the `requeued` event's own meta
(`next_resume_date`) rather than appending a second `ooo_tagged` event —
one atomic write per resend, and _apply_event_to_cache's existing
`requeued`/`ooo_tagged` handlers need no changes at all (see
due_for_ooo_resend()'s docstring for the full resolution rule). GMass's
native follow-up timer is BELIEVED suppressed the moment a recipient is
first marked OOO (slap.dashboard.tag_reply calls slap.gmass.
unsubscribe_recipient before ever calling tag_ooo — see that function's
docstring for exactly what's verified vs. still an unconfirmed assumption,
and for why this is account-wide, not per-campaign) — nothing in this
module talks to GMass directly. This module's own guarantee (the pause
window before `resume_date`, and never firing the same stage twice) holds
regardless of whether GMass's native timer actually stays silent — see
slap.gmass.unsubscribe_recipient's docstring for the one part of this
whole feature that isn't fully proven.
"""
from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

from slap import archive, display
from slap.latex import WORKDIR_ROOT, recipient_workdir
from slap.tracking import append_event

MANIFEST_NAME = "staged.json"


def stage_recipient(conn, *, campaign: str, recipient: str, persona: str, cadence: list,
                     subject: str, body: str, stage_bodies: list,
                     attachment_path: Path, attachment_name: str, latex_enabled: bool,
                     company: str = "", role: str = "", req_id: str = "", archive_dir: Path = None,
                     when: date = None, workdir_root: Path = WORKDIR_ROOT,
                     field_values: dict = None) -> Path:
    """Write the queued event + staged manifest for one recipient (does not
    send). Returns the recipient's workdir.

    latex_enabled recipients genuinely have a per-recipient attachment (the
    freshly compiled PDF the LaTeX loop, step 8, already staged in place) —
    that's real per-recipient state, so it stays copied into the workdir.

    Static (latex-disabled) recipients all share the exact same campaign
    resume.pdf — copying it into every recipient's workdir would be false
    per-recipient state (identical bytes duplicated once per send, forever).
    Instead the manifest records `attachment_source`, the shared file's own
    path in campaigns/<name>/ — the runner reads bytes from there directly
    at drain time (see runner._send_one), no per-recipient copy at all.

    `archive_dir` (None unless the owner set RESUME_ARCHIVE_DIR, see
    slap.archive) points a symlink at whichever of the two files above is
    THIS recipient's real, final attachment — this is the one place that
    distinction is already resolved, so archiving hooks in here rather than
    re-deriving it at the call site. Never allowed to fail this function or
    this recipient's staging: a broken/missing archive dir only warns (see
    slap.archive's own docstring), and any unexpected error is caught here
    too, matching the one-recipient-blast-radius guarantee used everywhere
    else sends can partially fail (e.g. runner.drain).

    `company`/`role`/`req_id` also ride in the `queued` event's `meta`
    alongside `persona` (the exact precedent already documented in
    slap.tracking's module docstring: "persona isn't a fixed events column,
    so it must ride in a queued event's meta ... the caller knows it at
    queue time"). These are drop-parsed field values that were previously
    used only for the archive filename and then discarded — persisting them
    is what makes the dashboard's "Reach-outs" page able to filter/show
    company and req_id-present at all, since nothing else in the schema
    tracks them. Additive and backward-compatible: a `queued` event written
    before this change simply lacks these keys, and every reader treats a
    missing key as blank/unknown, never a guessed value.

    `field_values` (post-launch, slap.reload): the raw, pre-fill drop-parsed
    dict (parse_drop()'s return value — every campaign field, not just
    company/role/req_id) — stored so slap.reload.scan() can re-render this
    recipient's subject/body/stage_bodies against a LATER-edited template
    file without ever needing the owner to re-paste the original drop.
    Optional (defaults to None) purely for backward compatibility with
    existing callers/tests written before this field existed; a manifest
    written with `field_values=None` simply can't be reloaded later (see
    slap.reload.scan's own handling of that case — a distinct, actionable
    failure reason, not a crash)."""
    workdir = recipient_workdir(campaign, recipient, root=workdir_root)

    if latex_enabled:
        staged_attachment = workdir / attachment_name
        if attachment_path.resolve() != staged_attachment.resolve():
            shutil.copyfile(attachment_path, staged_attachment)
        attachment_source = None  # None means "read from workdir/attachment_name"
        real_attachment_path = staged_attachment
    else:
        attachment_source = str(attachment_path.resolve())
        real_attachment_path = attachment_path.resolve()

    manifest = {
        "campaign": campaign, "recipient": recipient, "persona": persona, "cadence": cadence,
        "subject": subject, "body": body, "stage_bodies": stage_bodies,
        "attachment_name": attachment_name, "attachment_source": attachment_source,
        "field_values": field_values,
    }
    (workdir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    try:
        archive.archive_resume(real_attachment_path, archive_dir, company=company, role=role, when=when)
    except Exception as e:
        display.warn(f"resume archive: unexpected error archiving for {recipient}: {e}")

    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": persona, "company": company, "role": role, "req_id": req_id})
    return workdir


def load_manifest(workdir: Path) -> dict:
    return json.loads((workdir / MANIFEST_NAME).read_text(encoding="utf-8"))


def due_recipients(conn) -> list:
    """Recipients staged for their initial send but never actually sent yet
    for THAT staged cycle — the runner's stateless 'what's queued and due?'
    query (§10). Covers only stage-0 initial sends; due_for_ooo_resend()
    covers OOO resends.

    Derived from the event log directly (the same "latest relevant event,
    with no later closing event" pattern as due_for_ooo_resend()/
    latest_open_draft_id()/needs_triage()), NOT from recipients.first_sent_at
    — that column is deliberately first-write-wins (the recipient's first
    EVER send, across all campaigns, permanent) and is the wrong thing to
    check here. A recipient already contacted in an earlier campaign (hard
    dedup warn, explicit proceed-anyway per §6's warn-don't-block) gets a
    fresh `queued` event when re-staged for a new campaign, but their
    first_sent_at never changes — checking it would silently exclude them
    from every future drain forever, which is exactly the real BLOCKER this
    fixes (a confirmed proceed-anyway send that vanished: never sent, never
    failed, never counted as still queued). `send_failed` does not count as
    closing a queued cycle — a failed attempt stays due for retry on the
    next drain.

    Second caller (post-launch): `slap.reload.scan()` uses this EXACT query,
    unmodified, as its own "which recipients have nothing sent at all yet"
    eligibility set — not a coincidence, the "queued, no later sent event"
    definition here is precisely "this recipient's staged content has never
    been locked into a real GMass send" (see `slap.reload`'s module
    docstring for why that's the one hard limit on what it can safely
    re-render). Reusing this query instead of re-deriving a similar-but-
    subtly-different one is what makes `slap.reload` immune to the exact
    first_sent_at pitfall this docstring already fixed for the runner —
    a recipient re-staged into a new campaign is correctly still eligible
    for reload against that new campaign's templates, not silently
    excluded forever."""
    rows = conn.execute(
        """
        SELECT r.* FROM recipients r
        WHERE r.status = 'active'
        AND EXISTS (
            SELECT 1 FROM events e
            WHERE e.recipient = r.recipient AND e.type = 'queued'
            AND e.id = (
                SELECT MAX(id) FROM events
                WHERE recipient = r.recipient AND type IN ('queued', 'sent')
            )
        )
        ORDER BY r.recipient
        """
    ).fetchall()
    return [dict(r) for r in rows]


def tag_ooo(conn, recipient: str, resume_date: date) -> None:
    """Marks `recipient` OOO-paused, due for resend once `resume_date`
    arrives — never immediately. Callable for ANY recipient at ANY time,
    regardless of current status or whether SLAP ever detected a reply: the
    real-world trigger for this (an OOO notice landing somewhere SLAP/GMass
    never saw) is itself untethered from any signal SLAP could gate on, so
    this function has no precondition on prior state.

    This is a pure DB write — it does NOT talk to GMass. The caller
    (slap.dashboard.tag_reply) is responsible for calling
    slap.gmass.unsubscribe_recipient() FIRST, before this, so GMass's own
    native follow-up timer is suppressed before any local pause is ever
    recorded (see that function's docstring for why — a local-only pause
    with no working GMass-side suppression would be worse than no pause at
    all)."""
    row = conn.execute("SELECT campaign FROM recipients WHERE recipient = ?", (recipient,)).fetchone()
    campaign = row["campaign"] if row else None
    append_event(conn, type="ooo_tagged", recipient=recipient, campaign=campaign,
                 meta={"resume_date": resume_date.isoformat()})


def _pending_ooo_resume_date(conn, recipient: str, campaign: str):
    """The next OOO-pause-driven resend date still pending for `recipient`
    IN `campaign` specifically, or None if there isn't one (never paused,
    already fully resolved, or paused for a DIFFERENT campaign — see below).
    Reads the LATEST of ooo_tagged/requeued/send_failed for this
    (recipient, campaign) pair — whichever is more recent wins, the same
    "latest event wins" resolution rule slap.dashboard's needs_triage()/
    reply_tags() already establish:

    - Latest is `ooo_tagged`: this is either the ORIGINAL owner-driven pause,
      or a fresh manual re-tag (always allowed, always overrides whatever
      was pending before) — due date is its own `resume_date`. An
      `ooo_tagged` written before this feature existed (no `resume_date` in
      its meta) is treated as immediately due (date.min) — matches the
      ORIGINAL reply-detected recovery's behavior exactly (resend on the
      very next drain), so an already-in-flight OOO tag from before this
      feature shipped is never silently stuck waiting forever.
    - Latest is `requeued`: a prior stage in this same OOO pause already
      fired. If its meta carries `next_resume_date`, the persona's cadence
      still has a stage left and THIS is when it's due (see
      slap.runner._send_ooo_resend). No `next_resume_date` means the
      cadence was already exhausted by that resend — nothing pending.
    - `send_failed` is normally SKIPPED (irrelevant — a transient failed
      attempt stays due for retry on the next drain, matching
      due_recipients()'s identical convention for its own queue), EXCEPT one
      specific, genuinely terminal reason:
      `meta["stage"] == "ooo_cadence_exhausted"` (an iron-audit SHOULD-FIX:
      the persona's cadence has no next stage at all for this recipient —
      retrying can never succeed, since a cadence's length never changes for
      an already-staged recipient — so without this, a recipient marked OOO
      with nothing left to resend would generate a fresh, identical
      send_failed on every single future drain, forever).

    **Campaign-scoped, not just recipient-scoped — an iron-audit BLOCKER
    fix.** The `recipients` cache holds ONE row per recipient, reflecting
    whichever campaign they're MOST RECENTLY associated with (same grain the
    Reach-outs page already documents) — the existing dedup hard-warn
    explicitly allows re-staging an already-contacted recipient into a NEW
    campaign (warn, don't block). Without this scoping, a recipient with a
    pending OOO continuation for an OLD campaign who gets re-staged into a
    NEW one before that continuation resolves would have their OLD
    campaign's pending resend fire using the NEW campaign's `current_stage`/
    workdir/stage bodies but the OLD campaign's `last_gmass_campaign_id` —
    silently sending the new campaign's stage body threaded into the old
    campaign's Gmail conversation, alongside a normal initial send to the
    same recipient in the same drain (a genuine double-send + cross-campaign
    data corruption). Scoping this query to `campaign` (the recipient's
    CURRENT `recipients.campaign`) means a re-staged recipient's dangling
    old-campaign continuation is safely abandoned — it can never resume
    against a campaign it wasn't paused for — rather than corrupting the new
    one."""
    rows = conn.execute(
        "SELECT type, meta FROM events WHERE recipient = ? AND campaign = ? "
        "AND type IN ('ooo_tagged', 'requeued', 'send_failed') ORDER BY id DESC",
        (recipient, campaign),
    ).fetchall()
    for row in rows:
        meta = json.loads(row["meta"]) if row["meta"] else {}
        if row["type"] == "send_failed":
            if meta.get("stage") == "ooo_cadence_exhausted":
                return None  # terminal — nothing pending, ever again
            continue  # a transient failed attempt — keep looking backwards
        if row["type"] == "ooo_tagged":
            resume_date = meta.get("resume_date")
            return date.fromisoformat(resume_date) if resume_date else date.min
        next_resume_date = meta.get("next_resume_date")
        return date.fromisoformat(next_resume_date) if next_resume_date else None
    return None


def due_for_ooo_resend(conn, *, today: date = None) -> list:
    """Recipients due for an OOO-pause resend right now: their latest
    ooo_tagged/requeued/send_failed event FOR THEIR CURRENT CAMPAIGN (see
    _pending_ooo_resume_date) has a pending resume date that's today or
    earlier. Checks BOTH 'ooo_requeued' status (the original pause, or a
    fresh manual re-tag — _apply_event_to_cache's existing ooo_tagged
    handler already sets this) AND 'active' status (a recipient mid-way
    through a multi-stage OOO pause continuation: requeued's existing
    handler always flips status back to 'active' after ANY successful
    resend, unchanged from before this feature — whether there's a FURTHER
    stage still pending is tracked in that same event's meta, not in
    `status`, so no changes were needed to _apply_event_to_cache at all). A
    normal, never-OOO'd active recipient always has no ooo_tagged/requeued
    history at all, so this never produces a false positive for them.

    Deliberately does NOT overlap with due_recipients() even in principle:
    that function requires the recipient's CURRENT campaign to have a fresh,
    never-sent `queued` event; this function requires a pending OOO
    continuation FOR THAT SAME CURRENT campaign specifically (see
    _pending_ooo_resume_date's own docstring) — a freshly re-staged
    recipient has no such history for their new campaign yet, so they can
    never satisfy both at once. drain() still defensively dedupes the two
    lists anyway (belt-and-suspenders, not load-bearing on its own)."""
    today = today or date.today()
    rows = conn.execute(
        "SELECT * FROM recipients WHERE status IN ('ooo_requeued', 'active') ORDER BY recipient"
    ).fetchall()
    due = []
    for row in rows:
        resume_date = _pending_ooo_resume_date(conn, row["recipient"], row["campaign"])
        if resume_date is not None and resume_date <= today:
            due.append(dict(row))
    return due
