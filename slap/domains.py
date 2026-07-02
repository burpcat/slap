"""Domain / recipient dedup + the `domains` command (Build Order step 7).

Derived live from `recipients` (which is itself a derived cache of `events`)
— no new parallel store. See SLAP_BUILD_PROMPT.md §6.

Known gap: the brief's example warning context includes "what role" a prior
contact was for. Role/company/etc. are per-send drop-parsed field values,
which nothing in the current schema persists (events/recipients track
campaign, stage, and status — not the filled-in template fields). Showing
role in the hard-warn context needs the sender (step 9) to stash relevant
drop fields into a queued/sent event's `meta`; until that's wired, context
here is limited to what recipients already tracks: campaign, persona,
status, first_sent_at, replied_at.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CONSUMER_DOMAINS_PATH = Path("consumer_domains.txt")


class DomainsError(Exception):
    """Raised on fail-loud domains-module misuse (e.g. missing consumer_domains.txt)."""


def load_consumer_domains(path: Path = CONSUMER_DOMAINS_PATH) -> set:
    if not path.exists():
        raise DomainsError(
            f"{path} not found. This should be auto-seeded by `doctor` (Build Order "
            f"step 12) — until then, seed it manually per SLAP_BUILD_PROMPT.md §6."
        )
    return {line.strip().lower() for line in path.read_text().splitlines() if line.strip()}


def domain_of(email: str) -> str:
    parts = email.strip().lower().split("@")
    if len(parts) != 2 or not parts[1]:
        raise DomainsError(f"not a single-@ email address: {email!r}")
    return parts[1]


@dataclass
class ContactContext:
    recipient: str
    campaign: str
    persona: str
    status: str
    first_sent_at: str
    replied_at: str


@dataclass
class DedupResult:
    hard_warning: ContactContext = None    # exact recipient already contacted
    soft_warning_domain: str = None        # non-consumer domain with prior contact(s)
    soft_warning_contacts: list = None     # ContactContext list for that domain


def check_recipient(conn, recipient: str, consumer_domains: set) -> DedupResult:
    """Check `recipient` against tracking history before a send. Both the
    exact-recipient (hard) and same-domain (soft) checks warn, never block
    (§6) — the caller decides what to do with the result. The hard warn
    fires regardless of domain, even for consumer domains; the soft warn is
    skipped entirely for consumer domains (mandatory, or it false-warns on
    nearly everyone)."""
    recipient_norm = recipient.strip().lower()
    domain = domain_of(recipient_norm)
    rows = [dict(r) for r in conn.execute("SELECT * FROM recipients")]

    hard = next((r for r in rows if r["recipient"].strip().lower() == recipient_norm), None)
    hard_warning = _to_context(hard) if hard else None

    soft_contacts = []
    if domain not in consumer_domains:
        for r in rows:
            if r["recipient"].strip().lower() == recipient_norm:
                continue  # that's the exact-match case, not "a different person"
            if domain_of(r["recipient"]) == domain:
                soft_contacts.append(_to_context(r))

    return DedupResult(
        hard_warning=hard_warning,
        soft_warning_domain=domain if soft_contacts else None,
        soft_warning_contacts=soft_contacts or None,
    )


def domain_index(conn) -> dict:
    """Group all known recipients by domain — the read-only report backing
    the `domains` command. Regenerated from `recipients` every time; never
    hand-edited, never a source of truth (§6)."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM recipients ORDER BY recipient")]
    index: dict = {}
    for r in rows:
        index.setdefault(domain_of(r["recipient"]), []).append(_to_context(r))
    return index


def _to_context(row: dict) -> ContactContext:
    return ContactContext(
        recipient=row["recipient"], campaign=row["campaign"], persona=row["persona"],
        status=row["status"], first_sent_at=row["first_sent_at"], replied_at=row["replied_at"],
    )
