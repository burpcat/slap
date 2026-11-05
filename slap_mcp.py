#!/usr/bin/env python3
"""slap MCP server (stdio) for Claude Code: one application at a time.

The browser extension captures a posting (with its campaign, résumé and
contact) and records the application; Claude's job is narrow and scoped to
ONE application, by default the one the owner touched most recently:

    slap_application       that application: JD, campaign fields + template,
                           the current drop and email preview, open form questions
    slap_save_drop         write the drop (campaign field values); if the owner
                           already applied, this stages the reach-out
    slap_answer_questions  answer that application's open form questions
    slap_stars             full star.json entries, the only source of claims

Staging only ever happens for an applied posting (slap.outreach). Leave
slap_save_drop off the allowlist so its permission prompt is your confirm.

Register:  claude mcp add --scope user slap -- <repo>/.venv/bin/python <repo>/slap_mcp.py
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from slap import leads, outreach, tracking
from slap.config import ConfigError, load_global_config

REPO_ROOT = Path(__file__).resolve().parent
STAR_FILE = "star.json"  # inside config.yaml references_dir

server = MCPServer(
    "slap",
    instructions="Work on ONE job application at a time: call slap_application first and stay on the "
                 "application it returns. Don't discuss other applications unless the owner asks.",
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
        raise ToolError("no application yet: capture a posting with the slap browser extension first"
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
        return f"applied with résumé '{lead['applied_resume']}'; reach-out not staged yet"
    return "not applied yet (the reach-out stages only after the owner applies)"


@server.tool()
def slap_application(lead_id: str | None = None) -> dict:
    """The one application to work on (default: the one the owner touched
    most recently in the browser). Includes the JD, the campaign's drop
    fields (with hints) and template, the current drop + email preview, open
    form questions, and the star.json index. Name the company and role back
    to the owner before working."""
    global_config = _config()
    conn = tracking.connect()
    lead = _lead(conn, lead_id)
    out = {
        "lead_id": lead["lead_id"], "company": lead["company"], "role": lead["role"], "url": lead["url"],
        "jd": lead["jd"], "status": _status(lead),
        "campaign": lead["campaign"] or None, "resume": lead["applied_resume"] or lead["resume"] or None,
        "contact": {"email": lead["contact_email"], "name": lead["contact_name"]},
        "open_questions": [q for q in lead["questions"] if q["label"] not in lead["answers"]],
    }
    try:
        camp = outreach.campaign_for(lead, global_config)
        if camp is None:
            out["drop"] = "no campaign yet: the owner picks one in the extension panel"
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
def slap_save_drop(lead_id: str, fields: dict[str, str], acknowledge_warnings: bool = False) -> dict:
    """Save the drop (values for the application's drop_fields) and return the
    rendered email. If the owner already applied, this also stages the
    reach-out; with dedup warnings it refuses until acknowledge_warnings=true
    (ask the owner first)."""
    if not lead_id:
        raise ToolError("lead_id is required; take it from slap_application")
    global_config = _config()
    conn = tracking.connect()
    lead = _lead(conn, lead_id)
    try:
        camp = outreach.campaign_for(lead, global_config)
        if camp is None:
            raise ToolError("this application has no campaign yet; ask the owner to pick one in the panel")
        allowed = {f.key for f in outreach.drop_fields(camp)}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ToolError(f"not drop fields for campaign '{camp.name}': {unknown} (fields: {sorted(allowed)})")
        leads.save_drop(conn, lead["lead_id"], fields)
        lead = leads.get(conn, lead["lead_id"])
        result = {"email": outreach.public(outreach.render(lead, global_config))}
        result["outreach"] = outreach.stage(conn, global_config, lead["lead_id"],
                                            acknowledge_warnings=acknowledge_warnings)
    except (leads.LeadError, outreach.OutreachError) as e:
        raise ToolError(str(e)) from e
    return result


@server.tool()
def slap_answer_questions(lead_id: str, answers: dict[str, str]) -> str:
    """Answers keyed by the exact question label from slap_application's
    open_questions. Keep every claim inside star.json. For a select/radio
    question, answer with one of its options verbatim. The extension fills
    them in after the owner reviews."""
    if not lead_id:
        raise ToolError("lead_id is required; take it from slap_application")
    try:
        leads.answer_questions(tracking.connect(), lead_id, answers)
    except leads.LeadError as e:
        raise ToolError(str(e)) from e
    return f"{len(answers)} answer(s) saved; the owner clicks Prepare answers in the panel to see them"


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
