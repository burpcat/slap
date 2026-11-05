"""Captured job postings: browser extension → slap → Claude Code (via MCP).

Event-sourced like everything else: one recipientless `lead` event per
lifecycle step, keyed by meta["lead_id"] (derived from the posting URL), with
meta["stage"] ∈ STAGES. A lead's current state is a fold of its events in
order — never stored separately. A lead becomes "drafted" once a `queued`
event carries its lead_id in meta (slap_mcp stages it that way).
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from slap.tracking import append_event

STAGES = ("captured", "matched", "applied", "questions", "answered")


class LeadError(Exception):
    """Fail-loud lead misuse (bad URL, unknown lead)."""


def normalize_url(url: str) -> str:
    """Same posting → same key: drop the fragment, tracking params (utm_*), a
    trailing slash, and host case. Keeps other query params — some job sites put
    the job id there (e.g. ?gh_jid=)."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise LeadError(f"not an http(s) URL: {url!r}")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")])
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/") or "/", query, ""))


def lead_id_for(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:16]


def _events(conn, lead_id: str) -> list:
    rows = conn.execute(
        "SELECT timestamp, meta FROM events WHERE type = 'lead' AND json_extract(meta, '$.lead_id') = ? "
        "ORDER BY id", (lead_id,),
    ).fetchall()
    return [{"timestamp": r["timestamp"], **json.loads(r["meta"])} for r in rows]


def _fold(lead_id: str, events: list, drafted_recipients: list) -> dict:
    lead = {"lead_id": lead_id, "stage": None, "url": "", "jd": "", "company": "", "role": "",
            "captured_at": None, "resume": None, "campaign": None, "applied_resume": None,
            "applied_sha256": None, "applied_at": None, "drafted_for": drafted_recipients,
            "questions": [], "answers": {}}
    for e in events:
        stage = e["stage"]
        if stage in ("captured", "matched", "applied"):  # lifecycle; questions/answers don't move it
            lead["stage"] = stage
        if stage == "captured":
            lead.update(url=e["url"], jd=e["jd"], company=e.get("company", ""), role=e.get("role", ""),
                        captured_at=e["timestamp"])
        elif stage == "matched":
            lead.update(resume=e["resume"], campaign=e["campaign"])
        elif stage == "questions":  # the latest set the extension couldn't fill
            lead["questions"] = e["questions"]
            lead["url"] = lead["url"] or e["url"]
        elif stage == "answered":
            lead["answers"].update(e["answers"])
        elif stage == "applied":
            lead.update(applied_resume=e["resume"], applied_sha256=e.get("resume_sha256"),
                        applied_at=e["timestamp"])
            lead["url"] = lead["url"] or e["url"]
    return lead


def _drafted(conn) -> dict:
    """lead_id -> recipients whose `queued` event carries that lead_id."""
    out: dict = {}
    for r in conn.execute("SELECT recipient, json_extract(meta, '$.lead_id') AS lead_id FROM events "
                          "WHERE type = 'queued' AND json_extract(meta, '$.lead_id') IS NOT NULL ORDER BY id"):
        out.setdefault(r["lead_id"], []).append(r["recipient"])
    return out


def get(conn, lead_id: str) -> dict | None:
    events = _events(conn, lead_id)
    return _fold(lead_id, events, _drafted(conn).get(lead_id, [])) if events else None


def by_url(conn, url: str) -> dict | None:
    return get(conn, lead_id_for(url))


def capture(conn, *, url: str, jd: str, company: str = "", role: str = "") -> tuple[str, bool]:
    """Record a posting. Idempotent per URL: a re-capture returns the existing
    lead_id and appends nothing. Returns (lead_id, created)."""
    if not (jd or "").strip():
        raise LeadError("jd is empty — nothing to match a résumé against")
    lead_id = lead_id_for(url)
    if any(e["stage"] == "captured" for e in _events(conn, lead_id)):
        return lead_id, False
    append_event(conn, type="lead", meta={"lead_id": lead_id, "stage": "captured", "url": normalize_url(url),
                                          "jd": jd.strip(), "company": company.strip(), "role": role.strip()})
    return lead_id, True


def match(conn, lead_id: str, *, resume: str, campaign: str) -> None:
    """Record the résumé + campaign Claude chose. Re-matching appends a new
    event; the latest one wins (the fold)."""
    if get(conn, lead_id) is None:
        raise LeadError(f"unknown lead {lead_id!r}")
    append_event(conn, type="lead", meta={"lead_id": lead_id, "stage": "matched",
                                          "resume": resume, "campaign": campaign})


def mark_applied(conn, *, url: str, resume: str, resume_sha256: str = None) -> str:
    """Record that the owner submitted an application with `resume` (a
    send_tag; the caller validates it). `resume_sha256` pins the exact PDF
    bytes uploaded, so a held email is never released with an edited file.
    Works for a never-captured posting too (apply-first): the applied event
    alone creates the lead. Returns the lead_id."""
    if not (resume or "").strip():
        raise LeadError("resume is empty — record which résumé was actually uploaded")
    lead_id = lead_id_for(url)
    append_event(conn, type="lead", meta={"lead_id": lead_id, "stage": "applied", "url": normalize_url(url),
                                          "resume": resume.strip(), "resume_sha256": resume_sha256})
    return lead_id


def pending(conn) -> list:
    """Leads with a JD that no queued email references yet — Claude's inbox."""
    drafted = _drafted(conn)
    ids = [r[0] for r in conn.execute(
        "SELECT json_extract(meta, '$.lead_id') FROM events WHERE type = 'lead' "
        "AND json_extract(meta, '$.stage') = 'captured' ORDER BY id")]
    return [_fold(i, _events(conn, i), []) for i in ids if i not in drafted]


def record_questions(conn, *, url: str, questions: list) -> str:
    """Form questions the extension couldn't fill from the profile/answer bank,
    queued for Claude Code (slap_pending_questions). Each: {label, kind, options?}.
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


def pending_questions(conn) -> list:
    """Leads with queued questions that still lack an answer."""
    ids = dict.fromkeys(r[0] for r in conn.execute(
        "SELECT json_extract(meta, '$.lead_id') FROM events WHERE type = 'lead' "
        "AND json_extract(meta, '$.stage') = 'questions' ORDER BY id"))
    out = []
    for i in ids:
        lead = get(conn, i)
        open_qs = [q for q in lead["questions"] if q["label"] not in lead["answers"]]
        if open_qs:
            out.append({"lead_id": i, "url": lead["url"], "company": lead["company"], "role": lead["role"],
                        "jd": lead["jd"], "resume": lead["resume"], "questions": open_qs})
    return out
