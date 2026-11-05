"""Derived text distilled from source files (résumé cards, voice summaries),
stored in <docs_dir>/derived.yaml with each source's sha256 so an edited
source is flagged stale instead of silently trusted. Never hand-maintained:
Claude Code regenerates entries via the MCP server (slap_save_derived).

    card:<send_tag>: {source: docs/ai.pdf, sha256: ..., text: "..."}
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

DERIVED_FILE = "derived.yaml"
CARD_PREFIX = "card:"


class DerivedError(Exception):
    """derived.yaml is malformed or a source file is missing."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def derived_path(global_config) -> Path:
    return Path(global_config.docs_dir) / DERIVED_FILE


def load_derived(global_config) -> dict:
    path = derived_path(global_config)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise DerivedError(f"{path} must be a mapping of name -> {{source, sha256, text}}")
    return data


def is_stale(entry: dict) -> bool:
    source = Path(entry.get("source", ""))
    return not source.is_file() or sha256_file(source) != entry.get("sha256")


def save_derived(global_config, name: str, source: Path, text: str) -> Path:
    if not Path(source).is_file():
        raise DerivedError(f"source {source} not found")
    derived = load_derived(global_config)
    derived[name] = {"source": str(source), "sha256": sha256_file(source), "text": text}
    path = derived_path(global_config)
    path.write_text(yaml.safe_dump(derived, sort_keys=True, allow_unicode=True), encoding="utf-8")
    return path


def resume_cards(global_config) -> list:
    """One entry per send_tag: {tag, file, card, stale}. A card is stale when
    missing, pointing at another file, or its PDF changed since it was written."""
    derived = load_derived(global_config)
    out = []
    for tag, filename in global_config.send_tags.items():
        entry = derived.get(CARD_PREFIX + tag) or {}
        pdf = Path(global_config.docs_dir) / filename
        stale = not entry or str(pdf) != entry.get("source") or is_stale(entry)
        out.append({"tag": tag, "file": filename, "card": entry.get("text"), "stale": stale})
    return out


_WORD = re.compile(r"[a-z][a-z0-9+#]*(?:[.\-][a-z0-9+#]+)*")
_STOP = set("""and the for with you your our are this that from have has will can not but all any
into over more most such than then them they their there these those who what when where which
while about also been being both each other some very work team role years experience strong""".split())


def _terms(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 2 and w not in _STOP}


def suggest_resume(jd: str, cards: dict) -> dict | None:
    """Local, zero-token résumé hint: the share of each card's distinct terms
    that appear in the JD. {tag, scores} or None when nothing overlaps.
    ponytail: naive term overlap, only a hint for the badge — Claude Code makes
    the real choice; swap in BM25 if the hint is often wrong."""
    jd_terms = _terms(jd)
    scores = {tag: round(len(_terms(card) & jd_terms) / max(len(_terms(card)), 1), 3)
              for tag, card in cards.items() if card}
    if not scores or max(scores.values()) == 0:
        return None
    return {"tag": max(scores, key=scores.get), "scores": scores}
