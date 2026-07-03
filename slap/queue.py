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
from pathlib import Path

from slap.latex import WORKDIR_ROOT, recipient_workdir
from slap.tracking import append_event

MANIFEST_NAME = "staged.json"


def stage_recipient(conn, *, campaign: str, recipient: str, persona: str, cadence: list,
                     subject: str, body: str, stage_bodies: list,
                     attachment_path: Path, attachment_name: str,
                     workdir_root: Path = WORKDIR_ROOT) -> Path:
    """Write the queued event + staged manifest for one recipient (does not
    send). Returns the recipient's workdir. The attachment is copied into
    the workdir if it isn't already staged there (e.g. the LaTeX loop, step
    8, already staged it in place for latex-enabled campaigns)."""
    workdir = recipient_workdir(campaign, recipient, root=workdir_root)
    staged_attachment = workdir / attachment_name
    if attachment_path.resolve() != staged_attachment.resolve():
        shutil.copyfile(attachment_path, staged_attachment)

    manifest = {
        "campaign": campaign, "recipient": recipient, "persona": persona, "cadence": cadence,
        "subject": subject, "body": body, "stage_bodies": stage_bodies,
        "attachment_name": attachment_name,
    }
    (workdir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": persona})
    return workdir


def load_manifest(workdir: Path) -> dict:
    return json.loads((workdir / MANIFEST_NAME).read_text(encoding="utf-8"))


def due_recipients(conn) -> list:
    """Recipients staged for their initial send but never actually sent yet
    — the runner's stateless 'what's queued and due?' query (§10). Covers
    only stage-0 initial sends; due_for_ooo_resend() covers OOO resends."""
    rows = conn.execute(
        "SELECT * FROM recipients WHERE status = 'active' AND first_sent_at IS NULL "
        "ORDER BY recipient"
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
