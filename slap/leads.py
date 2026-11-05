"""Captured job postings: browser extension → slap → Claude Code (via MCP).

Event-sourced like everything else: one recipientless `lead` event per
lifecycle step, keyed by meta["lead_id"] (derived from the posting URL), with
meta["stage"] ∈ STAGES. A lead's current state is a fold of its events in
order — never stored separately. A lead becomes "drafted" once a `queued`
event carries its lead_id in meta (slap_mcp stages it that way).
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from slap.tracking import append_event

BATCH_MAX = 25  # ponytail: per-call cap on fetched postings; batch again for more
FETCH_MAX_BYTES = 5_000_000
FETCH_MAX_REDIRECTS = 5
# A step of a posting (apply form, confirmation) folds into the posting's own
# URL. Keep in sync with extension/lib.js STEP.
_STEP = re.compile(r"^(/.*[^/].*?)/(apply|application|confirmation|thanks|thank-you|submitted|success)(/.*)?$", re.I)
_LD_SCRIPT = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)

STAGES = ("captured", "matched", "applied", "questions", "answered")


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
    events = lead_events(conn, lead_id)
    return _fold(lead_id, events, _drafted(conn).get(lead_id, [])) if events else None


def by_url(conn, url: str) -> dict | None:
    return get(conn, lead_id_for(url))


def capture(conn, *, url: str, jd: str, company: str = "", role: str = "") -> tuple[str, bool]:
    """Record a posting. Idempotent per URL: a re-capture returns the existing
    lead_id and appends nothing. Returns (lead_id, created)."""
    if not (jd or "").strip():
        raise LeadError("jd is empty — nothing to match a résumé against")
    lead_id = lead_id_for(url)
    if any(e["stage"] == "captured" for e in lead_events(conn, lead_id)):
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
    return [_fold(i, lead_events(conn, i), []) for i in ids if i not in drafted]


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


def _html_to_text(markup: str) -> str:
    # Decode first: many job sites HTML-escape the description inside JSON-LD (&lt;p&gt;).
    text = re.sub(r"<(br|/p|/li|/div|/h[1-6]|/tr)\b[^>]*>", "\n", html.unescape(markup or ""), flags=re.I)
    text = re.sub(r"<li\b[^>]*>", "\n- ", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return "\n".join(line for line in (" ".join(l.split()) for l in text.split("\n")) if line)


def parse_job_posting(page_html: str) -> dict | None:
    """{role, company, jd} from a page's schema.org JobPosting JSON-LD, or None.
    Mirrors extension/lib.js parseJobPosting for server-side batch import."""
    for raw in _LD_SCRIPT.findall(page_html or ""):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = []
        for n in data if isinstance(data, list) else [data]:
            nodes.extend(n.get("@graph", [n]) if isinstance(n, dict) else [])
        for n in nodes:
            kind = n.get("@type") if isinstance(n, dict) else None
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                desc = n.get("description")
                jd = _html_to_text(desc) if isinstance(desc, str) else ""
                if not jd:
                    continue
                org = n.get("hiringOrganization")
                company = org.get("name", "") if isinstance(org, dict) else (org if isinstance(org, str) else "")
                one_line = lambda v: " ".join(_html_to_text(str(v or "")).split())  # titles are escaped too
                return {"role": one_line(n.get("title")), "company": one_line(company), "jd": jd}
    return None


def _public_host(host: str) -> None:
    """Refuse hosts that resolve to loopback/private/link-local/reserved
    addresses: batch import fetches links Claude passes in, and a JD could
    try to steer that at this machine or the LAN.
    ponytail: resolves before connecting, so a DNS-rebinding race remains;
    pin the resolved IP if that ever matters for a GET-only, JSON-LD-only fetch."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise LeadError(f"can't resolve {host!r} ({e})") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise LeadError(f"refusing to fetch {host!r}: it resolves to non-public address {ip}")


def fetch_page(url: str, *, get=requests.get, check_host=_public_host) -> str:
    """GET a public http(s) page as text: every redirect hop re-checked,
    body capped at FETCH_MAX_BYTES, charset from the header else UTF-8."""
    for _ in range(FETCH_MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise LeadError(f"not an http(s) URL: {url!r}")
        check_host(parts.hostname)
        resp = get(url, timeout=(5, 15), stream=True, allow_redirects=False,
                   headers={"User-Agent": "Mozilla/5.0 (compatible; slap)"})
        if resp.is_redirect:
            url = urljoin(url, resp.headers.get("Location", ""))
            resp.close()
            continue
        resp.raise_for_status()
        body = b""
        for chunk in resp.iter_content(64 * 1024):
            body += chunk
            if len(body) > FETCH_MAX_BYTES:
                resp.close()
                raise LeadError(f"page is larger than {FETCH_MAX_BYTES // 1_000_000} MB")
        charset = "charset=" in resp.headers.get("Content-Type", "").lower() and resp.encoding
        return body.decode(charset or "utf-8", errors="replace")
    raise LeadError(f"more than {FETCH_MAX_REDIRECTS} redirects")


def add_from_urls(conn, urls: list, *, fetch=fetch_page) -> list:
    """Fetch each posting and capture it as a lead. One result per URL —
    {url, lead_id, created, company, role} or {url, error}; one bad link
    never stops the rest."""
    if len(urls) > BATCH_MAX:
        raise LeadError(f"at most {BATCH_MAX} links per batch — got {len(urls)}")
    results = []
    for url in urls:
        try:
            normalize_url(url)
            job = parse_job_posting(fetch(url))
            if job is None:
                raise LeadError("no JobPosting data in the page (it may render in the browser only) — "
                                "open it and use the extension's Send to slap")
            lead_id, created = capture(conn, url=url, **job)
            results.append({"url": url, "lead_id": lead_id, "created": created,
                            "company": job["company"], "role": job["role"]})
        except Exception as e:  # reported per link, loudly; never aborts the batch
            results.append({"url": url, "error": f"{type(e).__name__}: {e}" if not isinstance(e, LeadError) else str(e)})
    return results


def all_leads(conn) -> list:
    """[(lead, events)] for every lead, from ONE pass over lead events (the
    per-lead get() rescans; this is for whole-table views like Companies)."""
    by_id: dict = {}
    for r in conn.execute("SELECT timestamp, meta FROM events WHERE type = 'lead' ORDER BY id"):
        e = {"timestamp": r["timestamp"], **json.loads(r["meta"])}
        by_id.setdefault(e["lead_id"], []).append(e)
    drafted = _drafted(conn)
    return [(_fold(i, evs, drafted.get(i, [])), evs) for i, evs in by_id.items()]
