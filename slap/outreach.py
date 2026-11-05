"""A captured lead's reach-out email: rendered from its campaign's templates
plus the drop Claude wrote and the contact from the panel, and staged ONLY
once the owner has applied (the hard rule of the extension flow).

Callers: slap.api (the panel's Applied button) and slap_mcp (saving a drop
after the application was already recorded). Both route through stage(),
which is idempotent per lead and serialised across processes by a file lock,
so the same posting can never be staged twice.
"""
from __future__ import annotations

import fcntl
import hashlib
import shutil
from pathlib import Path

from slap import archive, doctor, domains, leads
from slap.config import ConfigError, load_campaign
from slap.latex import WORKDIR_ROOT, recipient_workdir
from slap.queue import due_recipients, stage_recipient
from slap.templates import fill_template, merge_config_values

LOCK_FILE = ".stage.lock"  # inside WORKDIR_ROOT


class OutreachError(Exception):
    """Config/setup problem that stops a reach-out (fail loud)."""


def dedup_warnings(conn, global_config, recipient: str) -> list:
    try:
        consumer = domains.load_consumer_domains(Path(global_config.consumer_domains_file))
    except domains.DomainsError as e:
        raise OutreachError(str(e)) from e
    dedup = domains.check_recipient(conn, recipient, consumer)
    warnings = []
    if dedup.hard_warning:
        w = dedup.hard_warning
        warnings.append(f"Already contacted {recipient} (campaign {w.campaign}, {w.status}"
                        f"{', replied' if w.replied_at else ''})")
    for c in dedup.soft_warning_contacts or []:
        warnings.append(f"Same company domain: {c.recipient} (campaign {c.campaign}, {c.status})")
    return warnings


def campaign_for(lead, global_config):
    if not lead["campaign"]:
        return None
    try:
        camp = load_campaign(lead["campaign"], global_config)
    except ConfigError as e:
        raise OutreachError(str(e)) from e
    if camp.latex_enabled:
        raise OutreachError(f"campaign '{camp.name}' compiles LaTeX per recipient; the extension flow "
                            f"needs a static-résumé campaign")
    return camp


def drop_fields(camp) -> list:
    """The fields Claude writes. Email and the greeting name come from the
    panel's contact, so they're not part of the drop."""
    return [f for f in camp.fields if f.key not in ("email", camp.name_field)]


def render(lead, global_config) -> dict:
    """The email as it would be staged: {to, subject, body, followups,
    attachment, resume, missing}. `missing` names what still blocks staging
    (campaign, contact email, required drop fields)."""
    camp = campaign_for(lead, global_config)
    if camp is None:
        return {"missing": ["campaign"]}
    keys = {f.key for f in camp.fields}
    values = dict.fromkeys(keys, "")
    for key, captured in (("company", lead["company"]), ("role_catted", lead["role"].lower())):
        if key in keys:  # sensible defaults from the capture; the drop overrides them
            values[key] = captured
    values.update({k: v for k, v in lead["drop"].items() if k in keys})
    values["email"] = lead["contact_email"]
    if camp.name_field:
        values[camp.name_field] = lead["contact_name"] or values.get(camp.name_field) or "there"
    missing = [] if lead["contact_email"] else ["contact email"]
    missing += [f.key for f in drop_fields(camp) if not f.optional and not values.get(f.key, "").strip()]

    resume = lead["applied_resume"] or lead["resume"] or next(iter(camp.resume_paths), None)
    if resume not in camp.resume_paths:
        raise OutreachError(f"campaign '{camp.name}' doesn't offer résumé '{resume}' "
                            f"(offers: {list(camp.resume_paths)})")
    fill = merge_config_values(values, signature=global_config.signature)
    return {
        "to": lead["contact_email"], "campaign": camp.name, "resume": resume,
        "attachment": f"{camp.attachment_name} (résumé: {resume})",
        "subject": fill_template(camp.subject_template, fill, camp.fields),
        "body": fill_template(camp.body_template, fill, camp.fields),
        "followups": [{"day": d, "body": fill_template(b, fill, camp.fields)}
                      for d, b in zip(camp.cadence, camp.stage_bodies)],
        "missing": missing,
        "_camp": camp, "_values": values,
    }


def public(rendered: dict) -> dict:
    return {k: v for k, v in rendered.items() if not k.startswith("_")}


def stage(conn, global_config, lead_id: str, *, acknowledge_warnings: bool = False,
          workdir_root: Path = WORKDIR_ROOT) -> dict:
    """Stage this lead's reach-out if it's ready. Returns {"status": ...}:
    staged | already_staged | not_applied | incomplete (+missing) |
    busy (+reason) | needs_confirm (+warnings). Never stages twice, and
    snapshots the applied résumé's bytes so a later edit in docs/ can't
    change what the company receives."""
    workdir_root.mkdir(parents=True, exist_ok=True)
    with open(workdir_root / LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # dashboard + MCP server are separate processes
        lead = leads.get(conn, lead_id)
        if lead is None:
            raise OutreachError(f"unknown lead {lead_id!r}")
        if lead["staged_to"]:
            return {"status": "already_staged", "to": lead["staged_to"][0]}
        if not lead["applied_resume"]:
            return {"status": "not_applied"}
        r = render(lead, global_config)
        if r["missing"]:
            return {"status": "incomplete", "missing": r["missing"]}
        if r["to"] in {row["recipient"] for row in due_recipients(conn)}:
            return {"status": "busy", "reason": f"{r['to']} already has an unsent reach-out queued; "
                                                f"stage this one after that sends"}
        warnings = dedup_warnings(conn, global_config, r["to"])
        if warnings and not acknowledge_warnings:
            return {"status": "needs_confirm", "warnings": warnings, "email": public(r)}
        camp, values = r["_camp"], r["_values"]
        failures = [c for c in doctor.run_global_checks(global_config, conn) + doctor.run_campaign_checks(camp)
                    if not c.ok]
        if failures:
            raise OutreachError("doctor preflight failed: " + "; ".join(f"{c.name}: {c.detail}" for c in failures))
        attachment = camp.resume_paths[r["resume"]]
        if not lead["applied_sha256"]:
            raise OutreachError("this application has no recorded résumé hash; click Applied again in the panel")
        if _sha256(attachment) != lead["applied_sha256"]:
            raise OutreachError(f"résumé file {attachment} changed since you applied; the email would attach "
                                f"a different PDF than the company got")
        # Snapshot the exact bytes you applied with: the runner reads this copy at
        # send time, not docs/, which you may re-export before the next drain.
        snapshot = recipient_workdir(camp.name, r["to"], root=workdir_root) / "applied-resume.pdf"
        shutil.copyfile(attachment, snapshot)
        attachment = snapshot
        stage_recipient(
            conn, campaign=camp.name, recipient=r["to"], persona=camp.persona, cadence=camp.cadence,
            subject=r["subject"], body=r["body"], stage_bodies=[f["body"] for f in r["followups"]],
            attachment_path=attachment, attachment_name=camp.attachment_name, latex_enabled=False,
            company=lead["company"], role=values.get("role_catted", lead["role"]),
            name=values.get(camp.name_field, "") if camp.name_field else "",
            archive_dir=archive.archive_dir_from_env(), field_values=values, workdir_root=workdir_root,
            extra_meta={"lead_id": lead_id, "resume": r["resume"]},
        )
        return {"status": "staged", "to": r["to"]}


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
