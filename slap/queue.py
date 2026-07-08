"""Queue staging (Build Order step 9) + OOO re-queue tagging (step 10).

`send` (prep) stages a recipient by writing a `queued` event plus a staged
manifest into the recipient's workdir — it does NOT send. The runner (fire)
later asks "what's queued and due?" via due_recipients() and drains. The
queue is just more events; there is no separate queue store (§10).

tag_ooo()/due_for_ooo_resend() are the OOO re-queue counterpart (§7):
ooo_tagged is the "due for resend" marker (like queued), requeued is the
completion marker (like sent) — see slap.tracking's module docstring for
why that mapping means no new schema column is needed.
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
                     company: str = "", role: str = "", archive_dir: Path = None,
                     when: date = None, workdir_root: Path = WORKDIR_ROOT) -> Path:
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
    else sends can partially fail (e.g. runner.drain)."""
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
    }
    (workdir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    try:
        archive.archive_resume(real_attachment_path, archive_dir, company=company, role=role, when=when)
    except Exception as e:
        display.warn(f"resume archive: unexpected error archiving for {recipient}: {e}")

    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": persona})
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
    next drain."""
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


def tag_ooo(conn, recipient: str) -> None:
    """Owner tags a reply as OOO (§7) — a rare false-positive safety net,
    since GMass normally filters auto-responder replies itself. Marks the
    recipient due for the app's own resend of their next stage, fired later
    on the runner's normal cadence (no special scheduling) — this function
    itself never sends anything."""
    row = conn.execute("SELECT campaign FROM recipients WHERE recipient = ?", (recipient,)).fetchone()
    campaign = row["campaign"] if row else None
    append_event(conn, type="ooo_tagged", recipient=recipient, campaign=campaign)


def due_for_ooo_resend(conn) -> list:
    """Recipients tagged OOO but not yet actually resent. A successful
    resend writes `requeued`, which flips status back to 'active' — so this
    is simply everyone still sitting in 'ooo_requeued', no join needed."""
    rows = conn.execute(
        "SELECT * FROM recipients WHERE status = 'ooo_requeued' ORDER BY recipient"
    ).fetchall()
    return [dict(r) for r in rows]
