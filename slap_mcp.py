#!/usr/bin/env python3
"""slap MCP server (stdio): Claude Code is slap's front end, one application
at a time.

The owner pastes a posting into Claude Code and names the campaign, résumé
and contact. Claude captures it, writes the drop, helps with the application
form from the owner's profile, and records the application once the owner
says they submitted it, which is what stages the reach-out:

    slap_capture       capture a posting (JD, campaign, résumé, contact), or
                       update an existing one's campaign/résumé/contact
    slap_application   that application: JD, campaign fields + template, the
                       current drop and email preview, dedup warnings
    slap_save_drop     write the drop (campaign field values); never stages
    slap_applied       the owner submitted the application with résumé X:
                       stages the reach-out to `to` once the drop and contact
                       are set (the ONLY tool that stages)
    slap_profile       the owner's standard form answers (config.yaml
                       profile: + answers.yaml)
    slap_save_answer   save a reusable form answer to answers.yaml
    slap_stars         full star.json entries, the only source of claims

Staging only ever happens for an applied posting (slap.outreach). Keep
slap_applied off the allowlist: its permission prompt, which names the
recipient (`to`), is your confirm.

Register:  claude mcp add --scope user slap -- <repo>/.venv/bin/python <repo>/slap_mcp.py
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from slap import domains, leads, outreach, tracking
from slap.config import ConfigError, load_global_config
from slap.profile import LEARNED_FILE, ProfileError, save_learned

REPO_ROOT = Path(__file__).resolve().parent
STAR_FILE = "star.json"  # inside config.yaml references_dir

server = MCPServer(
    "slap",
    instructions="Work on ONE job application at a time: capture it with slap_capture (or open it with "
                 "slap_application) and stay on it. Don't discuss other applications unless the owner asks. "
                 "Call slap_applied only after the owner says they submitted the application.",
)


def _config():
    try:
        return load_global_config()
    except ConfigError as e:
        raise ToolError(str(e)) from e


def _lead(conn, lead_id):
    lead_id = lead_id or leads.current(conn)
    lead = leads.get(conn, lead_id) if lead_id else None
    if lead is None:
        raise ToolError("no application yet: capture a posting with slap_capture first"
                        if not lead_id else f"unknown application {lead_id!r}")
    return lead


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


def _status(lead) -> str:
    if lead["staged_to"]:
        return f"reach-out staged to {lead['staged_to'][0]}"
    if lead["applied_resume"]:
        return (f"applied with résumé '{lead['applied_resume']}'; reach-out not staged yet "
                f"(slap_applied stages it once the drop and contact are complete)")
    return "not applied yet (the reach-out stages only after the owner applies)"


def _warnings(conn, global_config, lead) -> list:
    """Heads-ups before staging, warn-only: this contact or their company
    domain was already reached out to, or someone at this company was."""
    if lead["staged_to"]:
        return []
    out = outreach.dedup_warnings(conn, global_config, lead["contact_email"]) if lead["contact_email"] else []
    named = set(" ".join(out).split())
    out += [f"Already contacted at {lead['company']}: {c.recipient} (campaign {c.campaign}, {c.status})"
            for c in domains.contacts_at_company(conn, lead["company"])
            if c.recipient != lead["contact_email"] and c.recipient not in named]
    return out


def _application(conn, global_config, lead) -> dict:
    out = {
        "lead_id": lead["lead_id"], "company": lead["company"], "role": lead["role"], "url": lead["url"],
        "jd": lead["jd"], "status": _status(lead),
        "campaign": lead["campaign"] or None, "resume": lead["applied_resume"] or lead["resume"] or None,
        "contact": {"email": lead["contact_email"], "name": lead["contact_name"]},
    }
    try:
        out["warnings"] = _warnings(conn, global_config, lead)
        camp = outreach.campaign_for(lead, global_config)
        if camp is None:
            out["drop"] = "no campaign yet: ask the owner to pick one of campaign_options"
            out["campaign_options"] = outreach.campaign_options(global_config)
        else:
            out["drop_fields"] = [{"key": f.key, "label": f.label, "optional": f.optional, "hint": f.hint}
                                  for f in outreach.drop_fields(camp)]
            out["template"] = {"subject": camp.subject_template, "body": camp.body_template}
            out["drop"] = lead["drop"]
            out["email_preview"] = outreach.public(outreach.render(lead, global_config))
    except outreach.OutreachError as e:
        raise ToolError(str(e)) from e
    try:
        out["stars"] = [{"id": s["id"], "title": s["title"], "tags": s["tags"]} for s in _load_stars(global_config)]
    except ToolError as e:
        out["stars"], out["stars_error"] = [], str(e)
    return out


@server.tool()
def slap_capture(url: str, jd: str = "", company: str = "", role: str = "", campaign: str = "",
                 resume: str = "", contact_email: str = "", contact_name: str = "") -> dict:
    """Capture the posting the owner pasted: its URL, the description text
    (`jd`, verbatim, never summarised), company and role, plus the campaign,
    résumé (a send_tag) and contact the OWNER named. Never pick the campaign,
    résumé or contact yourself: leave them empty and ask. Calling again with
    the same url updates campaign/résumé/contact (omit jd then). Returns the
    application, like slap_application but without the JD."""
    global_config = _config()
    conn = tracking.connect()
    details = {"campaign": campaign, "resume": resume, "contact_email": contact_email, "contact_name": contact_name}
    try:
        existing = leads.by_url(conn, url)
        if existing and existing["applied_resume"] and resume:
            raise ToolError(f"already applied with résumé '{existing['applied_resume']}'; if that's wrong, "
                            f"call slap_applied with the résumé actually uploaded")
        # validated against what the lead already has, so it can't end up with a
        # campaign that doesn't offer its résumé
        current = {"campaign": existing["campaign"], "resume": existing["applied_resume"] or existing["resume"]} \
            if existing else {}
        outreach.check_details({**current, **{k: v for k, v in details.items() if v}}, global_config)
        lead_id, created = leads.capture(conn, url=url, jd=jd, company=company, role=role, **details)
    except (leads.LeadError, outreach.OutreachError) as e:
        raise ToolError(str(e)) from e
    out = _application(conn, global_config, leads.get(conn, lead_id))
    del out["jd"]
    return {"created": created, **out}


@server.tool()
def slap_application(lead_id: str | None = None) -> dict:
    """The one application to work on (default: the one the owner touched
    most recently). Includes the JD, dedup warnings, the campaign's drop
    fields (with hints) and template, the current drop + email preview, and
    the star.json index. Name the company and role back to the owner before
    working."""
    global_config = _config()
    conn = tracking.connect()
    return _application(conn, global_config, _lead(conn, lead_id))


@server.tool()
def slap_save_drop(lead_id: str, fields: dict[str, str]) -> dict:
    """Save the drop (values for the application's drop_fields) and return the
    rendered email and the application's status. Never stages: if the owner
    has already applied, call slap_applied again to stage the reach-out."""
    if not lead_id:
        raise ToolError("lead_id is required; take it from slap_application")
    global_config = _config()
    conn = tracking.connect()
    lead = _lead(conn, lead_id)
    try:
        camp = outreach.campaign_for(lead, global_config)
        if camp is None:
            raise ToolError("this application has no campaign yet; ask the owner to pick one (slap_capture)")
        allowed = {f.key for f in outreach.drop_fields(camp)}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ToolError(f"not drop fields for campaign '{camp.name}': {unknown} (fields: {sorted(allowed)})")
        leads.save_drop(conn, lead["lead_id"], fields)
        lead = leads.get(conn, lead["lead_id"])
        return {"email": outreach.public(outreach.render(lead, global_config)), "status": _status(lead)}
    except (leads.LeadError, outreach.OutreachError) as e:
        raise ToolError(str(e)) from e


@server.tool()
def slap_applied(lead_id: str, resume: str, to: str, acknowledge_warnings: bool = False) -> dict:
    """Record that the OWNER submitted this application, uploading résumé
    `resume` (a send_tag), and stage the reach-out to `to` (the application's
    contact email, so the owner's confirm shows who gets it) if the drop and
    contact are ready. Call it only after the owner says they applied, with
    the résumé they uploaded; call it again after a missing piece is filled
    in. Returns the outreach status: staged | already_staged | incomplete
    (+missing) | busy (+reason) | needs_confirm (+warnings: ask the owner,
    then re-call with acknowledge_warnings=true)."""
    if not lead_id:
        raise ToolError("lead_id is required; take it from slap_application")
    global_config = _config()
    conn = tracking.connect()
    lead = _lead(conn, lead_id)
    try:
        return outreach.apply(conn, global_config, lead["lead_id"], resume, to=to,
                              acknowledge_warnings=acknowledge_warnings)
    except (leads.LeadError, outreach.OutreachError) as e:
        raise ToolError(str(e)) from e


@server.tool()
def slap_profile() -> dict:
    """The owner's standard application answers, for form questions the
    owner pastes: `fields` (name, contact links, location, work
    authorization, sponsorship, ...) and the answer bank `answers` (each
    `match` is a regex naming the questions it answers)."""
    global_config = _config()
    if not global_config.profile["fields"] and not global_config.profile["answers"]:
        raise ToolError(f"no profile yet: add a `profile:` section to {global_config.path} "
                        f"(see config.yaml.example)")
    return global_config.profile


@server.tool()
def slap_save_answer(question: str, answer: str) -> dict:
    """Save a reusable form answer to the owner's answer bank (answers.yaml
    next to config.yaml), only when the owner asks. `question` is the words
    that identify the question (e.g. "notice period"); saving the same words
    again replaces the answer. config.yaml's own answers take priority."""
    global_config = _config()
    try:
        return save_learned(Path(global_config.path).parent / LEARNED_FILE, question, answer)
    except ProfileError as e:
        raise ToolError(str(e)) from e


@server.tool()
def slap_stars(ids: list[str]) -> list[dict]:
    """Full star.json entries for the given ids (the claim source for the drop and answers)."""
    by_id = {s["id"]: s for s in _load_stars(_config())}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ToolError(f"unknown STAR ids: {unknown}")
    return [by_id[i] for i in ids]


def main():
    os.chdir(REPO_ROOT)  # every slap path (config.yaml, campaigns/, slap.db, workdir/) is cwd-relative
    load_dotenv(REPO_ROOT / ".env")
    server.run("stdio")


if __name__ == "__main__":
    main()
