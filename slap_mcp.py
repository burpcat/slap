#!/usr/bin/env python3
"""slap MCP server (stdio) — the prep half of `slap.py send`, driven by Claude Code.

Claude generates the per-recipient field values itself (via the slap-outreach
skill), previews, and stages through these tools; the unattended runner
(launchd) still fires the queue and owns the follow-up cadence. Pure
composition of existing library functions — no new send logic lives here.

Tools: slap_context · slap_stars · slap_resume_text · slap_save_derived ·
slap_leads · slap_match_lead · slap_pending_questions · slap_answer_questions ·
slap_preview · slap_send. Only slap_send writes to the queue; leave it
un-allowlisted in Claude Code so its permission prompt is the human confirm.

Register:  claude mcp add --scope user slap -- <repo>/.venv/bin/python <repo>/slap_mcp.py
"""
import json
import os
import sys
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pypdf import PdfReader

from slap import archive, derived, doctor, domains, leads, runner, smtp, tracking
from slap.config import ConfigError, discover_campaigns, load_campaign, load_global_config
from slap.queue import held_initials, stage_recipient
from slap.templates import fill_template, merge_config_values

REPO_ROOT = Path(__file__).resolve().parent
STAR_FILE = "star.json"          # inside config.yaml references_dir

server = MCPServer(
    "slap",
    instructions="Cold job outreach through the owner's Gmail. Call slap_context once per session; "
                 "slap_preview before slap_send; never send without showing the owner the preview.",
)

# preview_id -> everything slap_send needs. In-memory on purpose: a restarted
# server forgets previews, so a send can only ever stage what was just shown.
_previews: dict = {}
_send_lock = threading.Lock()


def _config():
    try:
        return load_global_config()
    except ConfigError as e:
        raise ToolError(str(e)) from e


def _campaign(name, global_config):
    try:
        return load_campaign(name, global_config)
    except ConfigError as e:
        raise ToolError(str(e)) from e


def _derived_or_fail(fn, *args):
    try:
        return fn(*args)
    except derived.DerivedError as e:
        raise ToolError(str(e)) from e


def _load_stars(global_config) -> list:
    """star.json's `experiences` list. Only id/title/tags are relied on; every
    other key in an entry is passed through untouched."""
    if global_config.references_dir is None:
        raise ToolError(f"references_dir is not set in {global_config.path} — point it at the folder "
                        f"holding {STAR_FILE}")
    path = global_config.references_dir / STAR_FILE
    if not path.is_file():
        raise ToolError(f"{path} not found (references_dir in {global_config.path})")
    try:
        experiences = json.loads(path.read_text(encoding="utf-8"))["experiences"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise ToolError(f"{path}: expected a JSON object with an 'experiences' list ({e})") from e
    if not isinstance(experiences, list):
        raise ToolError(f"{path}: 'experiences' must be a list")
    for i, entry in enumerate(experiences):
        missing = [k for k in ("id", "title", "tags") if not isinstance(entry, dict) or k not in entry]
        if missing:
            raise ToolError(f"{path}: experiences[{i}] is missing {missing}")
    return experiences


def _resume_path(global_config, tag) -> Path:
    if tag not in global_config.send_tags:
        raise ToolError(f"résumé '{tag}' is not a send_tag in {global_config.path} "
                        f"(known: {sorted(global_config.send_tags)})")
    return global_config.docs_dir / global_config.send_tags[tag]


def _dedup_warnings(global_config, recipient) -> list:
    try:
        consumer = domains.load_consumer_domains(Path(global_config.consumer_domains_file))
    except domains.DomainsError as e:
        raise ToolError(str(e)) from e
    dedup = domains.check_recipient(tracking.connect(), recipient, consumer)
    warnings = []
    if dedup.hard_warning:
        w = dedup.hard_warning
        warnings.append(f"HARD: {recipient} already contacted — campaign={w.campaign} status={w.status} "
                        f"first_sent={w.first_sent_at} replied={'yes' if w.replied_at else 'no'}")
    for c in dedup.soft_warning_contacts or []:
        warnings.append(f"SOFT: same domain {dedup.soft_warning_domain} — {c.recipient} "
                        f"campaign={c.campaign} status={c.status}")
    return warnings


@server.tool()
def slap_context() -> dict:
    """Everything needed to pick a campaign + résumé and write fields: campaigns
    (fields with generation hints), résumé cards (stale = regenerate before use),
    other derived references, the STAR index (id/title/tags), and today's send
    headroom. Call once per session."""
    global_config = _config()
    campaigns = []
    for name in discover_campaigns():
        try:
            c = load_campaign(name, global_config)
        except ConfigError as e:
            campaigns.append({"name": name, "error": str(e)})
            continue
        campaigns.append({
            "name": c.name, "persona": c.persona, "cadence": c.cadence,
            "resumes": list(c.resume_paths),
            "fields": [{"key": f.key, "optional": f.optional, "hint": f.hint} for f in c.fields],
        })

    resumes = _derived_or_fail(derived.resume_cards, global_config)
    references = {name: {"text": e.get("text"), "stale": derived.is_stale(e)}
                  for name, e in _derived_or_fail(derived.load_derived, global_config).items()
                  if not name.startswith(derived.CARD_PREFIX)}

    try:
        stars = [{"id": s["id"], "title": s["title"], "tags": s["tags"]} for s in _load_stars(global_config)]
        stars_error = None
    except ToolError as e:
        stars, stars_error = [], str(e)

    conn = tracking.connect()
    return {
        "campaigns": campaigns, "resumes": resumes, "references": references,
        "stars": stars, "stars_error": stars_error,
        "cap_headroom": runner.cap_headroom(conn, global_config),
    }


@server.tool()
def slap_stars(ids: list[str]) -> list[dict]:
    """Full star.json entries for the given ids (the claim source for bullets)."""
    by_id = {s["id"]: s for s in _load_stars(_config())}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ToolError(f"unknown STAR ids: {unknown}")
    return [by_id[i] for i in ids]


@server.tool()
def slap_resume_text(tag: str) -> str:
    """Plain text of a résumé PDF — only for (re)building its card."""
    path = _resume_path(_config(), tag)
    if not path.is_file():
        raise ToolError(f"{path} not found")
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


@server.tool()
def slap_save_derived(name: str, source: str, text: str) -> str:
    """Store distilled text (e.g. a résumé card as name='card:<tag>', source='resume:<tag>';
    or a voice summary with source = an absolute file path). Records the source's hash
    so slap_context flags it stale when the source changes."""
    global_config = _config()
    if source.startswith("resume:"):
        src = _resume_path(global_config, source.removeprefix("resume:"))
    else:
        src = Path(source).expanduser()
    path = _derived_or_fail(derived.save_derived, global_config, name, src, text)
    return f"saved {name} to {path}"


@server.tool()
def slap_leads() -> dict:
    """Job postings captured from the browser that no email references yet
    (each with its JD), plus staged emails still held for an application."""
    conn = tracking.connect()
    return {"pending": leads.pending(conn), "held": held_initials(conn)}


@server.tool()
def slap_match_lead(lead_id: str, resume: str, campaign: str) -> str:
    """Record the résumé + campaign chosen for a lead, so the browser extension
    uploads that résumé when the owner applies. Re-matching overrides."""
    global_config = _config()
    _resume_path(global_config, resume)
    _campaign(campaign, global_config)
    try:
        leads.match(tracking.connect(), lead_id, resume=resume, campaign=campaign)
    except leads.LeadError as e:
        raise ToolError(str(e)) from e
    return f"lead {lead_id}: résumé '{resume}', campaign '{campaign}'"


@server.tool()
def slap_pending_questions() -> list:
    """Application-form questions the browser extension couldn't fill from
    profile.yaml, per lead (with its JD and matched résumé). Answer each with
    slap_answer_questions; the extension fills them in after you review."""
    return leads.pending_questions(tracking.connect())


@server.tool()
def slap_answer_questions(lead_id: str, answers: dict[str, str]) -> str:
    """Answers keyed by the exact question label from slap_pending_questions.
    Keep every claim inside star.json. For a select/radio question, answer with
    one of its options verbatim."""
    try:
        leads.answer_questions(tracking.connect(), lead_id, answers)
    except leads.LeadError as e:
        raise ToolError(str(e)) from e
    return f"lead {lead_id}: {len(answers)} answer(s) saved"


@server.tool()
def slap_preview(campaign: str, resume: str, fields: dict[str, str], followups: int | None = None,
                 lead_id: str | None = None, hold_until_applied: bool = False) -> dict:
    """Render the exact email that would be staged, plus dedup warnings. Writes
    nothing. `fields` keys are the campaign's field keys; omitted = empty
    (an empty optional field drops its line). `followups` = how many of the
    campaign's follow-up stages to send (default all). `lead_id` links the
    email to a captured posting; `hold_until_applied` (needs lead_id) keeps it
    unsent until the owner applies there. Returns a preview_id for slap_send."""
    global_config = _config()
    hold = False
    if lead_id is not None:
        lead = leads.get(tracking.connect(), lead_id)
        if lead is None:
            raise ToolError(f"unknown lead {lead_id!r}")
        if lead["applied_resume"] and lead["applied_resume"] != resume:
            raise ToolError(f"already applied to this posting with résumé '{lead['applied_resume']}' — "
                            f"the email must attach that one")
        hold = hold_until_applied and not lead["applied_resume"]
    elif hold_until_applied:
        raise ToolError("hold_until_applied needs a lead_id")
    camp = _campaign(campaign, global_config)
    if camp.latex_enabled:
        raise ToolError(f"campaign '{campaign}' is a LaTeX campaign — use `slap.py send {campaign}`")
    if resume not in camp.resume_paths:
        raise ToolError(f"campaign '{campaign}' doesn't offer résumé '{resume}' (offers: {list(camp.resume_paths)})")
    attachment_path = camp.resume_paths[resume]
    if not attachment_path.is_file():
        raise ToolError(f"résumé file {attachment_path} not found")

    known = {f.key for f in camp.fields}
    unknown = sorted(set(fields) - known)
    if unknown:
        raise ToolError(f"unknown field keys {unknown} for campaign '{campaign}' (known: {sorted(known)})")
    values = {f.key: fields.get(f.key, "") for f in camp.fields}
    recipient = values["email"].strip()
    if not recipient:
        raise ToolError("the 'email' field is empty")

    max_n = len(camp.cadence)
    n = max_n if followups is None else followups
    if not 0 <= n <= max_n:
        raise ToolError(f"followups must be 0-{max_n}")

    fill_values = merge_config_values(values, signature=global_config.signature)
    subject = fill_template(camp.subject_template, fill_values, camp.fields)
    body = fill_template(camp.body_template, fill_values, camp.fields)
    stage_bodies = [fill_template(s, fill_values, camp.fields) for s in camp.stage_bodies][:n]
    cadence = camp.cadence[:n]

    warnings = _dedup_warnings(global_config, recipient)

    preview_id = uuid.uuid4().hex[:12]
    _previews[preview_id] = {
        "global_config": global_config, "campaign": camp, "recipient": recipient, "values": values,
        "subject": subject, "body": body, "stage_bodies": stage_bodies, "cadence": cadence,
        "attachment_path": attachment_path, "warnings": warnings,
        "resume": resume, "lead_id": lead_id, "hold": hold,
    }
    return {
        "preview_id": preview_id, "to": recipient, "subject": subject, "body": body,
        "followups": [{"day": d, "body": b} for d, b in zip(cadence, stage_bodies)],
        "attachment": f"{camp.attachment_name} (résumé: {resume})",
        "held_until_applied": hold,
        "warnings": warnings,
        "empty_required": [f.key for f in camp.fields if not f.optional and values[f.key] == ""],
    }


@server.tool()
def slap_send(preview_id: str, acknowledge_warnings: bool = False, now: bool = False) -> dict:
    """Stage exactly what slap_preview rendered. Refuses while the preview has
    dedup warnings unless acknowledge_warnings=true (ask the owner first), and
    refuses outright if the dedup picture changed since the preview (e.g. this
    address was staged in the meantime) — re-preview then. now=true fires
    queued initial sends immediately; otherwise the scheduled runner sends at
    its next window."""
    with _send_lock:  # one stage at a time: no two sends race the same preview/recipient
        return _send(preview_id, acknowledge_warnings, now)


def _send(preview_id, acknowledge_warnings, now):
    p = _previews.get(preview_id)
    if p is None:
        raise ToolError(f"unknown preview_id '{preview_id}' — call slap_preview again")
    global_config, camp, values = p["global_config"], p["campaign"], p["values"]
    current = _dedup_warnings(global_config, p["recipient"])
    if current != p["warnings"]:
        _previews.pop(preview_id)
        raise ToolError("dedup state changed since this preview — call slap_preview again:\n"
                        + "\n".join(current or ["(no warnings now)"]))
    if current and not acknowledge_warnings:
        raise ToolError("preview has dedup warnings — confirm with the owner, then pass "
                        "acknowledge_warnings=true:\n" + "\n".join(current))

    failures = [r for r in doctor.run_global_checks(global_config) + doctor.run_campaign_checks(camp) if not r.ok]
    if failures:
        raise ToolError("doctor preflight failed:\n" + "\n".join(f"- {r.name}: {r.detail}" for r in failures))

    conn = tracking.connect()
    stage_recipient(
        conn, campaign=camp.name, recipient=p["recipient"], persona=camp.persona,
        cadence=p["cadence"], subject=p["subject"], body=p["body"], stage_bodies=p["stage_bodies"],
        attachment_path=p["attachment_path"], attachment_name=camp.attachment_name, latex_enabled=False,
        company=values.get("company", ""), role=values.get("role_catted", ""), req_id=values.get("req_id", ""),
        name=values.get(camp.name_field, "") if camp.name_field else "",
        archive_dir=archive.archive_dir_from_env(), field_values=values,
        extra_meta={"resume": p["resume"], "resume_file": str(Path(p["attachment_path"]).resolve()),
                    **({"lead_id": p["lead_id"]} if p["lead_id"] else {}),
                    **({"hold": "applied"} if p["hold"] else {})},
    )
    del _previews[preview_id]  # one preview stages at most once
    result = {"staged": p["recipient"], "held_until_applied": p["hold"]}
    if now:
        # Same transport config slap.py's _smtp_config builds (not importable: slap.py
        # is shadowed by the slap/ package name).
        smtp_config = smtp.SmtpConfig(host=smtp.DEFAULT_SMTP_HOST, port=smtp.DEFAULT_SMTP_PORT,
                                      user=global_config.from_email,
                                      password=os.environ.get(smtp.PASSWORD_ENV, "").strip())
        drained = runner.drain(conn, global_config, smtp_config, staged_only=True,
                               log_fn=lambda s: print(s, file=sys.stderr))
        result["drain"] = asdict(drained)
    return result


def main():
    os.chdir(REPO_ROOT)  # every slap path (config.yaml, campaigns/, slap.db, workdir/) is cwd-relative
    load_dotenv(REPO_ROOT / ".env")
    server.run("stdio")


if __name__ == "__main__":
    main()
