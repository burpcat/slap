"""Saved, reusable follow-up ("Remind") templates — authored content on disk.

A "Remind" is a one-off follow-up the owner sends to a warm-but-silent /
LinkedIn-replied / real lead. Saved templates live as plain files under a
project-root `followups/` directory, one file per template:

    Title: Quick nudge on the Acme role
    <blank line>
    Hi {{name}}, just circling back ...

This is the EXACT precedent set by campaign body files (`campaigns/<name>/
initial.txt`, `stageN.txt`): reusable message prose is authored content on
disk, discovered by presence (no central registry), never rows in the
append-only `events` table. The events table stays the source of truth about
*what was actually sent to whom and when* (a Remind send records an
`interaction` event, see slap.tracking); this directory is the authored
*material*, the same category as every other `.txt` template the app already
keeps as files. So it is NOT a "parallel store" in the iron sense — nothing
here is derivable from events, and nothing derivable from events is kept here.

The Title:-line + blank-line-separator shape mirrors `initial.txt`'s
Subject:-line rule (slap.config.parse_initial_txt_text) so the two parse rules
stay recognizably the same. A body may contain `{{...}}` placeholders, but this
module does not fill them — the send path snapshots the resolved body verbatim
into its `interaction` event, and the UI warns the owner to edit before sending
(the "body is unchanged" warning), because a saved template is generic by
design.
"""
from __future__ import annotations

import re
from pathlib import Path

FOLLOWUPS_DIR = Path("followups")


class FollowupError(Exception):
    """Raised on fail-loud follow-up-store misuse (bad file, slug clash)."""


def parse_followup_text(text: str, *, ctx: str = "followup") -> tuple:
    """Parse a saved-follow-up file's text into (title, body).

    Same shape as slap.config.parse_initial_txt_text, but keyed on `Title:`
    instead of `Subject:` (a follow-up is a reply body, threaded onto the
    original campaign — it carries no new subject line of its own)."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("Title:"):
        raise FollowupError(f"{ctx}: first line must be 'Title: ...'")
    if len(lines) < 2 or lines[1] != "":
        raise FollowupError(f"{ctx}: second line must be blank (Title line + blank-line separator)")
    title = lines[0].removeprefix("Title:").lstrip(" ")
    if not title.strip():
        raise FollowupError(f"{ctx}: Title must not be empty")
    body = "\n".join(lines[2:])
    return title, body


def slugify(title: str) -> str:
    """A filesystem-safe slug derived from a human title. Deterministic so the
    same title always maps to the same file (and thus a re-save of the same
    title is caught as a clash rather than silently creating a near-duplicate)."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    if not slug:
        raise FollowupError(f"title {title!r} produces an empty slug — use some letters/digits")
    return slug


def followup_path(slug: str, followups_dir: Path = FOLLOWUPS_DIR) -> Path:
    return followups_dir / f"{slug}.txt"


def discover_followups(followups_dir: Path = FOLLOWUPS_DIR) -> list:
    """Every `<slug>.txt` under `followups_dir`, parsed to {slug, title, path}.

    Presence-based discovery (like discover_campaigns): a missing directory is
    simply an empty list, not an error — the owner may never have saved one."""
    if not followups_dir.exists():
        return []
    out = []
    for path in sorted(followups_dir.glob("*.txt")):
        title, _ = parse_followup_text(path.read_text(encoding="utf-8"), ctx=str(path))
        out.append({"slug": path.stem, "title": title, "path": path})
    return out


def load_followup(slug: str, followups_dir: Path = FOLLOWUPS_DIR) -> dict:
    """Load one saved follow-up by slug -> {slug, title, body, path}. Fail loud
    if it doesn't exist (the CLI/API resolves a slug the owner picked; a missing
    file means a stale reference, which should surface, not silently no-op)."""
    path = followup_path(slug, followups_dir)
    if not path.exists():
        raise FollowupError(f"no saved follow-up {slug!r} at {path}")
    title, body = parse_followup_text(path.read_text(encoding="utf-8"), ctx=str(path))
    return {"slug": slug, "title": title, "body": body, "path": path}


def save_followup(title: str, body: str, followups_dir: Path = FOLLOWUPS_DIR) -> dict:
    """Persist a new saved follow-up. Fail loud if a file for this title's slug
    already exists — never a silent overwrite (same instinct as
    slap.onboard's refusal to reuse an existing campaign folder name). Returns
    the same {slug, title, body, path} shape as load_followup."""
    slug = slugify(title)
    path = followup_path(slug, followups_dir)
    if path.exists():
        raise FollowupError(
            f"a saved follow-up {slug!r} already exists at {path} — pick a different title "
            f"(saved follow-ups are never silently overwritten)"
        )
    followups_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(f"Title: {title}\n\n{body}", encoding="utf-8")
    return {"slug": slug, "title": title, "body": body, "path": path}
