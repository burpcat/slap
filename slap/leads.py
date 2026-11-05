"""Captured job postings: browser extension -> slap -> Claude Code (MCP).

Event-sourced like everything else: one recipientless `lead` event per step,
keyed by meta["lead_id"] (derived from the posting URL), meta["stage"] in
STAGES. A lead's current state is a fold of its events, never stored apart.

    captured   the extension captured the posting (JD, role, company, URL),
               optionally with the details below
    details    campaign, résumé, contact email/name (from the panel; latest wins)
    drafted    the drop: campaign field values Claude wrote for this lead
    questions  form questions the extension couldn't fill (latest set wins)
    answered   Claude's answers to those questions
    applied    the owner applied; this is what lets the reach-out stage
               (slap.outreach)

A lead is "staged" once a `queued` event carries its lead_id in meta.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from slap.tracking import append_event

# A step of a posting (apply form, confirmation) folds into the posting's own
# URL. Keep in sync with extension/lib.js STEP.
_STEP = re.compile(r"^(/.*[^/].*?)/(apply|application|confirmation|thanks|thank-you|submitted|success)(/.*)?$", re.I)

STAGES = ("captured", "details", "drafted", "questions", "answered", "applied")
DETAIL_KEYS = ("campaign", "resume", "contact_email", "contact_name")


class LeadError(Exception):
    """Fail-loud lead misuse (bad URL, unknown lead)."""


def normalize_url(url: str) -> str:
    """Same posting → same key: drop the fragment, tracking params (utm_*), a
    trailing slash, and host case; fold an apply/confirmation step (and its
    query) into the posting URL. Keeps other query params on the posting itself
    — some job sites put the job id there (e.g. ?gh_jid=)."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise LeadError(f"not an http(s) URL: {url!r}")
    path, query = parts.path, parts.query
    step = _STEP.match(path)
    if step:
        path, query = step.group(1), ""
    query = urlencode([(k, v) for k, v in parse_qsl(query) if not k.lower().startswith("utm_")])
    return urlunsplit((parts.scheme, parts.netloc.lower(), path.rstrip("/") or "/", query, ""))


def lead_id_for(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:16]


def lead_events(conn, lead_id: str) -> list:
    rows = conn.execute(
        "SELECT timestamp, meta FROM events WHERE type = 'lead' AND json_extract(meta, '$.lead_id') = ? "
        "ORDER BY id", (lead_id,),
    ).fetchall()
    return [{"timestamp": r["timestamp"], **json.loads(r["meta"])} for r in rows]


def _fold(lead_id: str, events: list, staged_to: list) -> dict:
    lead = {"lead_id": lead_id, "stage": None, "url": "", "jd": "", "company": "", "role": "",
            "captured_at": None, **{k: "" for k in DETAIL_KEYS}, "drop": {}, "drop_at": None,
            "questions": [], "answers": {}, "applied_resume": None, "applied_sha256": None,
            "applied_at": None, "staged_to": staged_to, "last_activity": None}
    for e in events:
        stage = e["stage"]
        lead["last_activity"] = e["timestamp"]
        if stage == "applied" or (stage == "captured" and lead["stage"] != "applied"):
            lead["stage"] = stage  # the lifecycle the panel shows; capturing after applying keeps "applied"
        if stage == "captured":
            lead.update(url=e["url"], jd=e["jd"], company=e.get("company", ""), role=e.get("role", ""),
                        captured_at=e["timestamp"])
        if stage in ("captured", "details"):
            lead.update({k: e[k] for k in DETAIL_KEYS if e.get(k)})
        elif stage == "drafted":
            lead.update(drop=e["fields"], drop_at=e["timestamp"])
        elif stage == "questions":
            lead["questions"] = e["questions"]
            lead["url"] = lead["url"] or e["url"]
        elif stage == "answered":
            lead["answers"].update(e["answers"])
        elif stage == "applied":
            lead.update(applied_resume=e["resume"], applied_sha256=e.get("resume_sha256"),
                        applied_at=e["timestamp"])
            lead["url"] = lead["url"] or e["url"]
    return lead


def _staged(conn) -> dict:
    """lead_id -> recipients whose `queued` event carries that lead_id."""
    out: dict = {}
    for r in conn.execute("SELECT recipient, json_extract(meta, '$.lead_id') AS lead_id FROM events "
                          "WHERE type = 'queued' AND json_extract(meta, '$.lead_id') IS NOT NULL ORDER BY id"):
        out.setdefault(r["lead_id"], []).append(r["recipient"])
    return out


def get(conn, lead_id: str) -> dict | None:
    events = lead_events(conn, lead_id)
    return _fold(lead_id, events, _staged(conn).get(lead_id, [])) if events else None


def by_url(conn, url: str) -> dict | None:
    return get(conn, lead_id_for(url))


def _clean_details(details: dict) -> dict:
    unknown = sorted(set(details) - set(DETAIL_KEYS))
    if unknown:
        raise LeadError(f"unknown lead details {unknown} (known: {list(DETAIL_KEYS)})")
    clean = {k: str(v).strip() for k, v in details.items() if v is not None and str(v).strip()}
    email = clean.get("contact_email", "")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise LeadError(f"contact email {email!r} doesn't look like an email address")
    return clean


def capture(conn, *, url: str, jd: str, company: str = "", role: str = "", **details) -> tuple[str, bool]:
    """Record a posting (plus any details from the panel). Idempotent per URL:
    a re-capture appends nothing, though new details are recorded. The caller
    validates campaign/résumé names. Returns (lead_id, created)."""
    if not (jd or "").strip():
        raise LeadError("jd is empty — capture the posting page, not the form")
    clean = _clean_details(details)
    lead_id = lead_id_for(url)
    if any(e["stage"] == "captured" for e in lead_events(conn, lead_id)):
        if clean:
            set_details(conn, lead_id, **clean)
        return lead_id, False
    append_event(conn, type="lead", meta={"lead_id": lead_id, "stage": "captured", "url": normalize_url(url),
                                          "jd": jd.strip(), "company": company.strip(), "role": role.strip(),
                                          **clean})
    return lead_id, True


def set_details(conn, lead_id: str, **details) -> None:
    """Campaign / résumé / contact for a lead, editable until it's staged."""
    lead = get(conn, lead_id)
    if lead is None:
        raise LeadError(f"unknown lead {lead_id!r}")
    if lead["staged_to"]:
        raise LeadError(f"the reach-out for this posting is already staged to {lead['staged_to'][0]}")
    clean = _clean_details(details)
    if clean:
        append_event(conn, type="lead", meta={"lead_id": lead_id, "stage": "details", **clean})


def save_drop(conn, lead_id: str, fields: dict) -> None:
    """The campaign field values Claude wrote for this lead (the caller
    validates keys against the campaign). Latest drop wins until staged."""
    lead = get(conn, lead_id)
    if lead is None:
        raise LeadError(f"unknown lead {lead_id!r}")
    if lead["staged_to"]:
        raise LeadError(f"the reach-out for this posting is already staged to {lead['staged_to'][0]}")
    if not isinstance(fields, dict) or not all(isinstance(v, str) for v in fields.values()):
        raise LeadError("drop fields must map field keys to strings")
    append_event(conn, type="lead", meta={"lead_id": lead_id, "stage": "drafted", "fields": fields})


def mark_applied(conn, *, url: str, resume: str, resume_sha256: str = None) -> str:
    """Record that the owner submitted an application with `resume` (a
    send_tag; the caller validates it). `resume_sha256` pins the PDF bytes.
    Works for a never-captured posting too (apply-first). Re-marking with the
    same résumé appends nothing. Returns the lead_id."""
    if not (resume or "").strip():
        raise LeadError("resume is empty — record which résumé was actually uploaded")
    lead_id = lead_id_for(url)
    lead = get(conn, lead_id)
    if lead and lead["applied_resume"] == resume.strip() and lead["applied_sha256"] == resume_sha256:
        return lead_id
    if lead and lead["staged_to"]:
        raise LeadError(f"the reach-out is already staged with résumé '{lead['applied_resume']}'; "
                        f"it can't switch to '{resume.strip()}' now")
    append_event(conn, type="lead", meta={"lead_id": lead_id, "stage": "applied", "url": normalize_url(url),
                                          "resume": resume.strip(), "resume_sha256": resume_sha256})
    return lead_id


def current(conn) -> str | None:
    """The application the owner is working on: the lead with the latest event."""
    row = conn.execute("SELECT json_extract(meta, '$.lead_id') FROM events WHERE type = 'lead' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def record_questions(conn, *, url: str, questions: list) -> str:
    """Form questions the extension couldn't fill from the profile/answer bank,
    queued for Claude Code (slap_application). Each: {label, kind, options?}.
    The latest set replaces earlier ones. Returns the lead_id."""
    if not isinstance(questions, list) or not questions:
        raise LeadError("questions must be a non-empty list")
    clean = []
    for q in questions:
        label = (q.get("label") if isinstance(q, dict) else "") or ""
        if not label.strip():
            raise LeadError(f"each question needs a label — got {q!r}")
        options = q.get("options") or []
        if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
            raise LeadError(f"question {label!r}: options must be a list of strings")
        clean.append({"label": label.strip(), "kind": str(q.get("kind") or "text"), "options": options})
    lead_id = lead_id_for(url)
    append_event(conn, type="lead", meta={"lead_id": lead_id, "stage": "questions",
                                          "url": normalize_url(url), "questions": clean})
    return lead_id


def answer_questions(conn, lead_id: str, answers: dict) -> None:
    """Claude's answers, keyed by question label exactly as queued. Unknown
    labels are refused so a typo can't silently never fill."""
    lead = get(conn, lead_id)
    if lead is None:
        raise LeadError(f"unknown lead {lead_id!r}")
    asked = {q["label"] for q in lead["questions"]}
    unknown = sorted(set(answers) - asked)
    if unknown:
        raise LeadError(f"not among this lead's queued questions: {unknown} (asked: {sorted(asked)})")
    if not all(isinstance(v, str) and v.strip() for v in answers.values()):
        raise LeadError("every answer must be a non-empty string")
    append_event(conn, type="lead", meta={"lead_id": lead_id, "stage": "answered", "answers": answers})


def all_leads(conn) -> list:
    """[(lead, events)] for every lead, from ONE pass over lead events (the
    per-lead get() rescans; this is for whole-table views like Companies)."""
    by_id: dict = {}
    for r in conn.execute("SELECT timestamp, meta FROM events WHERE type = 'lead' ORDER BY id"):
        e = {"timestamp": r["timestamp"], **json.loads(r["meta"])}
        by_id.setdefault(e["lead_id"], []).append(e)
    drafted = _staged(conn)
    return [(_fold(i, evs, drafted.get(i, [])), evs) for i, evs in by_id.items()]
