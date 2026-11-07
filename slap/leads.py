"""Job postings the owner works through in Claude Code (slap_mcp).

Event-sourced like everything else: one recipientless `lead` event per step,
keyed by meta["lead_id"] (derived from the posting URL), meta["stage"] in
STAGES. A lead's current state is a fold of its events, never stored apart.

    captured   the posting (JD, role, company, URL), optionally with the
               details below
    details    campaign, résumé, contact email/name (the owner's picks; latest wins)
    drafted    the drop: campaign field values Claude wrote for this lead
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
# URL, so pasting either one finds the same lead.
_STEP = re.compile(r"^(/.*[^/].*?)/(apply|application|confirmation|thanks|thank-you|submitted|success)(/.*)?$", re.I)

STAGES = ("captured", "details", "drafted", "applied")
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
            "applied_resume": None, "applied_sha256": None,
            "applied_at": None, "staged_to": staged_to, "last_activity": None}
    for e in events:
        stage = e["stage"]
        lead["last_activity"] = e["timestamp"]
        if stage == "applied" or (stage == "captured" and lead["stage"] != "applied"):
            lead["stage"] = stage  # the lifecycle; capturing after applying keeps "applied"
        if stage == "captured":
            lead.update(url=e["url"], jd=e["jd"], company=e.get("company", ""), role=e.get("role", ""),
                        captured_at=e["timestamp"])
        if stage in ("captured", "details"):
            lead.update({k: e[k] for k in DETAIL_KEYS if e.get(k)})
        elif stage == "drafted":
            lead.update(drop=e["fields"], drop_at=e["timestamp"])
        elif stage == "applied":
            lead.update(applied_resume=e["resume"], applied_sha256=e.get("resume_sha256"),
                        applied_at=e["timestamp"])
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


def capture(conn, *, url: str, jd: str = "", company: str = "", role: str = "", **details) -> tuple[str, bool]:
    """Record a posting (plus any details the owner gave). Idempotent per URL:
    a re-capture appends nothing, though new details are recorded, so the JD
    is only needed the first time. The caller validates campaign/résumé
    names. Returns (lead_id, created)."""
    clean = _clean_details(details)
    lead_id = lead_id_for(url)
    if any(e["stage"] == "captured" for e in lead_events(conn, lead_id)):
        if clean:
            set_details(conn, lead_id, **clean)
        return lead_id, False
    if not (jd or "").strip():
        raise LeadError("jd is empty — pass the posting's description text")
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


def mark_applied(conn, lead_id: str, *, resume: str, resume_sha256: str = None) -> None:
    """Record that the owner submitted this application with `resume` (a
    send_tag; the caller validates it). `resume_sha256` pins the PDF bytes.
    Re-marking with the same résumé appends nothing; the same résumé with
    different bytes is refused, since the company has the earlier PDF."""
    if not (resume or "").strip():
        raise LeadError("resume is empty — record which résumé was actually uploaded")
    lead = get(conn, lead_id)
    if lead is None or not lead["captured_at"]:
        raise LeadError(f"unknown lead {lead_id!r} — capture the posting first")
    if lead["applied_resume"] == resume.strip():
        if lead["applied_sha256"] == resume_sha256:
            return
        if lead["applied_sha256"]:
            raise LeadError(f"résumé '{resume.strip()}' changed since you applied with it, and the company has "
                            f"the earlier PDF; restore that file, or record the résumé you actually uploaded")
    if lead["staged_to"]:
        raise LeadError(f"the reach-out is already staged with résumé '{lead['applied_resume']}'; "
                        f"it can't switch to '{resume.strip()}' now")
    append_event(conn, type="lead", meta={"lead_id": lead_id, "stage": "applied",
                                          "resume": resume.strip(), "resume_sha256": resume_sha256})


def current(conn) -> str | None:
    """The application the owner is working on: the lead with the latest event."""
    row = conn.execute("SELECT json_extract(meta, '$.lead_id') FROM events WHERE type = 'lead' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def all_leads(conn) -> list:
    """[(lead, events)] for every lead, from ONE pass over lead events (the
    per-lead get() rescans; this is for whole-table views like Companies)."""
    by_id: dict = {}
    for r in conn.execute("SELECT timestamp, meta FROM events WHERE type = 'lead' ORDER BY id"):
        e = {"timestamp": r["timestamp"], **json.loads(r["meta"])}
        by_id.setdefault(e["lead_id"], []).append(e)
    drafted = _staged(conn)
    return [(_fold(i, evs, drafted.get(i, [])), evs) for i, evs in by_id.items()]
