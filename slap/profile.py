"""The owner's application-form profile: config.yaml's optional `profile:`
section, which the browser extension fills forms from (always after review).

    profile:
      fields:   {first_name: Ada, email: ..., current_company: ..., work_authorization: ...}
      answers:  [{match: "notice period|start date", answer: "Two weeks"}, ...]

`match` is a case-insensitive regex tested against a form question's label
(the extension runs it as a JavaScript regex).

Answers saved from the extension panel ("Save to bank") live in a separate,
slap-managed answers.yaml next to config.yaml, so the hand-written config is
never rewritten. They load after config.yaml's answers, which therefore win.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

LEARNED_FILE = "answers.yaml"

class ProfileError(Exception):
    """A malformed profile (fail loud, never fill from a guess)."""


def parse_profile(raw, path="config.yaml") -> dict:
    """Validate config.yaml's `profile:` section. Absent -> empty profile."""
    if raw is None:
        return {"fields": {}, "answers": []}
    path = f"{path}: profile"
    if not isinstance(raw, dict):
        raise ProfileError(f"{path} must be a mapping with 'fields' and 'answers'")
    fields = raw.get("fields") or {}
    if not isinstance(fields, dict) or not all(isinstance(k, str) and isinstance(v, (str, int, float))
                                               for k, v in fields.items()):
        raise ProfileError(f"{path}: 'fields' must map names to plain values")
    answers = raw.get("answers") or []
    if not isinstance(answers, list):
        raise ProfileError(f"{path}: 'answers' must be a list of {{match, answer}}")
    for i, a in enumerate(answers):
        if not isinstance(a, dict) or not isinstance(a.get("match"), str) or not isinstance(a.get("answer"), str):
            raise ProfileError(f"{path}: answers[{i}] needs string 'match' and 'answer'")
        try:
            re.compile(a["match"], re.I)
        except re.error as e:
            raise ProfileError(f"{path}: answers[{i}].match is not a valid regex ({e})") from e
    return {"fields": {k: str(v) for k, v in fields.items()},
            "answers": [{"match": a["match"], "answer": a["answer"]} for a in answers]}


def load_learned(path: Path) -> list:
    """Answers saved from the panel: a YAML list of {match, answer}. Missing file -> []."""
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return parse_profile({"answers": raw}, str(path))["answers"]


def save_learned(path: Path, phrase: str, answer: str) -> dict:
    """Save one answer, matched on a plain phrase (escaped, so no regex needed).
    Saving the same phrase again replaces its answer. Returns the entry."""
    phrase, answer = (phrase or "").strip(), (answer or "").strip()
    if not phrase or not answer:
        raise ProfileError("both the matching words and the answer are required")
    entry = {"match": re.escape(phrase), "answer": answer}
    entries = [e for e in load_learned(path) if e["match"] != entry["match"]] + [entry]
    path.write_text(yaml.safe_dump(entries, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return entry
