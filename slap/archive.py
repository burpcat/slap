"""Résumé archive: a durable, browsable record of every résumé PDF ever
staged (post-launch feature, not in the original Build Order).

Populated with **symlinks, not copies** — the real bytes live in exactly one
place (the per-recipient compiled PDF in `workdir/<campaign>/<recipient>/`
for LaTeX campaigns, or the shared `campaigns/<name>/<attachment_file>` for
static ones), consistent with CLAUDE.md's "one source of truth" rule.

`RESUME_ARCHIVE_DIR` is read directly from the environment — loaded into
`os.environ` by python-dotenv the same way `GMASS_API_KEY` is (slap.py's
`load_dotenv()` call already covers it). Unlike `GMASS_API_KEY`, there's no
`gmass.api_key_env`-style indirection: no reason exists to let the owner
rename this variable, so there's no second config.yaml knob for it.

Warn-don't-block (CLAUDE.md iron rule): every public function here either
returns cleanly or returns None/empty on failure — nothing raises out of
`archive_resume()`. A missing/unset `RESUME_ARCHIVE_DIR` means archiving is
off; a set-but-broken one (doesn't exist, not writable) is logged as a
warning and skipped. Either way the caller's send must proceed — see
`slap.queue.stage_recipient()`, which wraps the call in its own exception
boundary for defense in depth (one-recipient blast radius).
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from slap import display

ENV_VAR = "RESUME_ARCHIVE_DIR"


def archive_dir_from_env() -> Path | None:
    raw = os.environ.get(ENV_VAR, "").strip()
    return Path(raw) if raw else None


def _slugify(value: str) -> str:
    slug = value.strip().lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", slug)


def is_valid_dir(archive_dir: Path) -> bool:
    return archive_dir.is_dir() and os.access(archive_dir, os.W_OK)


def archive_resume(real_path: Path, archive_dir: Path | None, *, company: str, role: str,
                    when: date = None) -> Path | None:
    """Create (or confirm-idempotent) a symlink in `archive_dir` pointing at
    `real_path`, named `<company>-<role>-<date>.pdf` (slugified). Returns the
    symlink path on success, or None if archiving is off or failed.

    Collisions: a same-name symlink already pointing at the SAME resolved
    target is left alone (idempotent re-stage). One pointing elsewhere gets
    `-2`, `-3`, ... appended. A non-symlink file occupying the name is
    treated the same as a collision — never overwritten.
    """
    if archive_dir is None:
        return None
    if not is_valid_dir(archive_dir):
        display.warn(
            f"resume archive: {ENV_VAR}={archive_dir} does not exist or isn't writable — "
            f"skipping archive symlink for this recipient."
        )
        return None

    when = when or date.today()
    company_slug, role_slug = _slugify(company), _slugify(role)
    if not company_slug or not role_slug:
        display.warn(
            f"resume archive: company and/or role is empty for this recipient — the "
            f"archive filename will be missing that part (check the campaign's field "
            f"keys line up with 'company'/'role_catted')."
        )
    base = f"{company_slug}-{role_slug}-{when.isoformat()}"
    target = real_path.resolve()

    n = 1
    while True:
        name = f"{base}.pdf" if n == 1 else f"{base}-{n}.pdf"
        candidate = archive_dir / name
        if candidate.is_symlink():
            if candidate.resolve() == target:
                return candidate  # already archived — idempotent re-stage
            n += 1
            continue
        if candidate.exists():
            n += 1  # a real (non-symlink) file occupies this name — never overwrite
            continue
        try:
            candidate.symlink_to(target)
        except OSError as e:
            display.warn(f"resume archive: could not create symlink {candidate}: {e}")
            return None
        return candidate


def find_broken_symlinks(archive_dir: Path | None) -> list:
    """Symlinks inside `archive_dir` whose target no longer exists —
    surfaced by `slap.py doctor` so a `cleanup`-reclaimed PDF (or any other
    deleted target) shows up as loud, visible staleness instead of silently
    rotting in the archive."""
    if archive_dir is None or not archive_dir.is_dir():
        return []
    return sorted(p for p in archive_dir.iterdir() if p.is_symlink() and not p.exists())


def resolve_live_targets(archive_dir: Path | None) -> set:
    """Resolved target paths of every currently-non-dangling symlink in
    `archive_dir`. Used by `slap.cleanup`'s delete guard (the archive/cleanup
    tension, resolved as option (a) in CONTROL_SHEET.md): a PDF still
    referenced by a live archive symlink is kept, not reclaimed, so `cleanup`
    can never silently defeat the archive."""
    if archive_dir is None or not archive_dir.is_dir():
        return set()
    targets = set()
    for entry in archive_dir.iterdir():
        if entry.is_symlink() and entry.exists():
            targets.add(entry.resolve())
    return targets
