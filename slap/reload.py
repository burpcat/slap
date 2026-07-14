"""Template reload (post-launch feature): re-render every not-yet-sent
recipient's staged content against whatever the campaign template files
currently say, without needing the owner to re-paste the original drop.

Why this can exist at all, and its one hard limit (confirmed against the
actual send-call code before this was built, per the owner's request — see
slap.gmass.build_campaign_settings): a recipient's ENTIRE follow-up cadence
is baked into GMass's own campaign settings in the single POST /api/campaigns
call that fires at initial send (runner._send_one) — there is no later API
call that could ever update stage 2/3 wording after that point, regardless
of local template edits. So this only ever touches recipients with nothing
sent at all — due_recipients()'s existing "queued, no later sent event"
definition (§10, already correctly scoped per-campaign, not the first-ever-
across-all-campaigns recipients.first_sent_at column — see that function's
own docstring for why the latter would be the wrong thing to check here).

The other hard requirement (also confirmed, not assumed, before building):
staged.json used to only ever store the already-RENDERED subject/body/
stage_bodies text, never the raw drop-parsed field values fill_template()
needs to re-run against a new template. slap.queue.stage_recipient() now
also persists that raw dict (`field_values`) going forward — so a recipient
staged BEFORE this feature shipped has no raw values to reload from at all.
That's reported as its own distinct, actionable failure (not a crash, not a
silent skip) rather than assumed away.

Failures are reported via a small JSON file (FAILURES_PATH), not a new
`events` row: this app's events.type column has a real CHECK constraint
already baked into every existing, populated slap.db (see slap/tracking.py's
_SCHEMA and slap/dashboard.py's _sync_blocks() docstring for the identical
tradeoff already made once, for exactly this reason) — SQLite has no ALTER
TABLE that can add a new literal check value, only a full table rebuild, a
live-data-migration risk this feature does not need to take on. Reusing an
existing event type (e.g. send_failed) wouldn't fit either: several existing
readers (slap.dashboard.todays_runs, slap.queue._pending_ooo_resume_date)
already interpret every event of a given type as meaning something specific,
and a template-reload outcome doesn't cleanly mean any of those things. The
failures file is fully OVERWRITTEN by every scan() + write_failures() pair
(never appended-to) — "unresolved failures from the most recent run" is
exactly what it holds, so a later successful re-run naturally clears an
earlier failure with no separate resolution bookkeeping needed.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from slap.config import CAMPAIGNS_DIR, ConfigError, GlobalConfig, load_campaign
from slap.latex import WORKDIR_ROOT, recipient_workdir
from slap.queue import MANIFEST_NAME, due_recipients, load_manifest
from slap.templates import extract_placeholder_keys, fill_template, merge_config_values
from slap.tracking import latest_open_draft_id

FAILURES_PATH = Path("template_reload_failures.json")


@dataclass
class ReloadChange:
    """One recipient whose re-rendered content differs from what's currently
    staged — the diff a `template-reload` run would apply on confirm.
    `campaign`/`recipient` are enough to locate the workdir again at apply
    time (recipient_workdir); nothing here is written to disk until
    apply_changes() is actually called with it."""
    recipient: str
    campaign: str
    old_subject: str
    new_subject: str
    old_body: str
    new_body: str
    old_stage_bodies: list
    new_stage_bodies: list


@dataclass
class ReloadFailure:
    """One recipient template-reload could not re-render — left completely
    untouched. `missing_fields` is empty for failure reasons that aren't
    about a specific placeholder (no stored field_values at all, or the
    whole campaign's config is currently invalid) — always populated for the
    "template now references a field this recipient has no stored value for"
    case, since that's the one actionable-by-field failure the brief asks
    for."""
    recipient: str
    campaign: str
    reason: str
    missing_fields: list
    attempted_at: str  # ISO-8601 UTC, per §5 "all timestamps are UTC"


@dataclass
class ReloadPlan:
    """scan()'s full result. Read-only by construction: scan() never writes
    anything to disk itself — apply_changes()/write_failures() are separate,
    explicit steps, matching this project's "show a summary before
    committing anything" default."""
    changed: list
    unchanged: list  # list of (recipient, campaign) pairs — already match, nothing to do
    failures: list


def _reload_one(conn, row: dict, global_config: GlobalConfig, campaign_cache: dict, *,
                 workdir_root: Path, campaigns_dir: Path):
    """Re-render ONE recipient's staged content. Returns ("unchanged", None),
    ("changed", ReloadChange), or ("failed", reason, missing_fields) — never
    raises; any unexpected error (corrupt staged.json, permission error, ...)
    becomes its own generic failure, matching the one-recipient-blast-radius
    guarantee used everywhere else a batch operation can partially fail (e.g.
    runner._send_one's identical exception-boundary comment)."""
    recipient, campaign_name = row["recipient"], row["campaign"]
    try:
        # due_recipients() means "no `sent` event yet" — NOT "nothing has
        # happened yet." A recipient whose create_draft succeeded but
        # send_campaign then failed already has a real, open GMass draft
        # (slap.tracking.latest_open_draft_id) — that draft's subject/body
        # were already committed to GMass at draft-creation time and the
        # next drain reuses it as-is (runner._send_one: draft_id is None ->
        # skip create_draft entirely), never recreating it from a
        # subsequently-edited manifest. Rewriting this recipient's staged
        # subject/body here would silently split-brain: the initial email
        # would go out with the STALE pre-edit content baked into that open
        # draft, while build_campaign_settings would build the follow-up
        # stage cadence from the manifest's NEW stage_bodies — exactly the
        # "GMass already locked this in" hazard this whole feature exists to
        # avoid, just one step earlier than the fully-`sent` case. Treated
        # as its own failure rather than silently reloaded.
        if latest_open_draft_id(conn, recipient) is not None:
            return ("failed", "an initial send attempt already created an open GMass draft for this "
                               "recipient — its subject/body are already locked into that draft and "
                               "can't be changed locally; let the next drain resolve it, then re-run "
                               "template-reload", [])

        workdir = recipient_workdir(campaign_name, recipient, root=workdir_root)
        manifest = load_manifest(workdir)

        field_values = manifest.get("field_values")
        if field_values is None:
            return ("failed", "no stored field values — this recipient was staged before "
                               "template-reload existed; re-stage them to enable reload", [])

        if campaign_name not in campaign_cache:
            try:
                campaign_cache[campaign_name] = load_campaign(campaign_name, global_config, campaigns_dir)
            except ConfigError as e:
                campaign_cache[campaign_name] = e
        campaign_or_error = campaign_cache[campaign_name]
        if isinstance(campaign_or_error, ConfigError):
            return ("failed", f"campaign config is currently invalid: {campaign_or_error}", [])
        campaign = campaign_or_error

        fill_values = merge_config_values(field_values, signature=global_config.signature)
        texts = [campaign.subject_template, campaign.body_template, *campaign.stage_bodies]
        used = set()
        for text in texts:
            used |= extract_placeholder_keys(text)
        missing = sorted(used - set(fill_values))
        if missing:
            return ("failed", f"template now references field(s) this recipient has no stored "
                               f"value for: {', '.join(missing)}", missing)

        new_subject = fill_template(campaign.subject_template, fill_values, campaign.fields)
        new_body = fill_template(campaign.body_template, fill_values, campaign.fields)
        new_stage_bodies = [fill_template(s, fill_values, campaign.fields) for s in campaign.stage_bodies]
    except Exception as e:
        return ("failed", f"unexpected error re-rendering: {e}", [])

    if (new_subject, new_body, new_stage_bodies) == (manifest["subject"], manifest["body"], manifest["stage_bodies"]):
        return ("unchanged", None)

    return ("changed", ReloadChange(
        recipient=recipient, campaign=campaign_name,
        old_subject=manifest["subject"], new_subject=new_subject,
        old_body=manifest["body"], new_body=new_body,
        old_stage_bodies=manifest["stage_bodies"], new_stage_bodies=new_stage_bodies,
    ))


def scan(conn, global_config: GlobalConfig, *, workdir_root: Path = WORKDIR_ROOT,
         campaigns_dir: Path = CAMPAIGNS_DIR, now: datetime = None) -> ReloadPlan:
    """Re-render every not-yet-sent recipient's content (due_recipients(),
    §10's own "whatever's queued and due, anywhere" query — no campaign
    argument, exactly the pattern drain() already uses) against whatever the
    campaign template files currently say, RIGHT NOW. Read-only: nothing on
    disk changes until apply_changes() is called with this plan's `changed`
    list. A campaign whose config.yaml/templates are currently broken fails
    every one of ITS recipients with the same reason (loaded once, cached in
    a local dict) without affecting any other campaign's recipients."""
    attempted_at = (now or datetime.now(timezone.utc)).isoformat()
    changed, unchanged, failures = [], [], []
    campaign_cache = {}
    for row in due_recipients(conn):
        outcome, *rest = _reload_one(
            conn, row, global_config, campaign_cache, workdir_root=workdir_root, campaigns_dir=campaigns_dir,
        )
        if outcome == "unchanged":
            unchanged.append((row["recipient"], row["campaign"]))
        elif outcome == "changed":
            changed.append(rest[0])
        else:
            reason, missing_fields = rest
            failures.append(ReloadFailure(
                recipient=row["recipient"], campaign=row["campaign"],
                reason=reason, missing_fields=missing_fields, attempted_at=attempted_at,
            ))
    return ReloadPlan(changed=changed, unchanged=unchanged, failures=failures)


def apply_changes(changes: list, *, workdir_root: Path = WORKDIR_ROOT) -> list:
    """Overwrite ONLY subject/body/stage_bodies in each changed recipient's
    staged.json — cadence, attachment, field_values, everything else about
    their staged send stays exactly as it was (queue position, staged date,
    attachment are untouched because this never rewrites them).

    Returns any apply-time failures (normally empty — each change was already
    re-render-verified during scan(), so a failure here only happens if
    something external interfered between scan() and confirm, e.g. the
    workdir was deleted). One recipient's write failing must not abort
    writing every OTHER already-verified recipient in the same batch — same
    one-recipient-blast-radius guarantee as scan()'s own per-recipient
    boundary (_reload_one)."""
    failures = []
    attempted_at = datetime.now(timezone.utc).isoformat()
    for c in changes:
        try:
            workdir = recipient_workdir(c.campaign, c.recipient, root=workdir_root)
            manifest = load_manifest(workdir)
            manifest["subject"] = c.new_subject
            manifest["body"] = c.new_body
            manifest["stage_bodies"] = c.new_stage_bodies
            (workdir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except Exception as e:
            failures.append(ReloadFailure(
                recipient=c.recipient, campaign=c.campaign,
                reason=f"unexpected error applying reloaded content: {e}",
                missing_fields=[], attempted_at=attempted_at,
            ))
    return failures


def write_failures(failures: list, *, path: Path = FAILURES_PATH) -> None:
    """Fully overwrites the failures report with THIS run's failures — never
    appends, never merges with a prior run's (see module docstring: that's
    precisely what makes "resolved" need no separate bookkeeping)."""
    path.write_text(json.dumps([asdict(f) for f in failures], indent=2), encoding="utf-8")


def load_failures(path: Path = FAILURES_PATH) -> list:
    """The dashboard's read side — a missing file (never run yet, or a fresh
    checkout) means exactly zero failures, not an error."""
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))
