"""The owner's application-form profile (profile.yaml, gitignored): standard
field values plus an answer bank the browser extension fills forms from.
Read-only here; the owner (or Claude Code) edits the file.

    fields:   {first_name: Ada, email: ..., linkedin: ..., work_authorization: ...}
    answers:  [{match: "notice period|start date", answer: "Two weeks"}, ...]

`match` is a case-insensitive regex tested against a form question's label.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

PROFILE_PATH = Path("profile.yaml")


class ProfileError(Exception):
    """profile.yaml is missing or malformed (fail loud, never fill from a guess)."""


def load_profile(path: Path = PROFILE_PATH) -> dict:
    if not path.exists():
        raise ProfileError(f"{path} not found — copy profile.yaml.example to {path} and fill it in")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
