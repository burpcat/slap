"""Drop parsing + local {{key}} template fill (Build Order step 4).

See SLAP_BUILD_PROMPT.md §4 for the exact drop-parser and fill semantics,
preserved from the owner's prior tool. Templates are guaranteed by
slap.config.load_campaign to reference only known field keys (that check
lives there, once — see extract_placeholder_keys), so fill_template does not
re-validate that itself.
"""
from __future__ import annotations

import re

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
# Any {{...}} at all, including malformed ones the well-formed regex above
# would silently miss (a stray space, a hyphenated key, ...).
ANY_PLACEHOLDER_RE = re.compile(r"\{\{([^{}]*)\}\}")


def extract_placeholder_keys(text: str) -> set:
    return set(PLACEHOLDER_RE.findall(text))


def find_malformed_placeholders(text: str) -> set:
    """{{...}} whose inside isn't a plain \\w+ key. fill_template only
    substitutes (and load_campaign's placeholder-key guard only checks)
    well-formed {{key}} placeholders, so a malformed one would otherwise
    survive verbatim, unfilled, into a sent email."""
    return {m for m in ANY_PLACEHOLDER_RE.findall(text) if not re.fullmatch(r"\w+", m)}


def parse_drop(text: str, fields: list) -> dict:
    """Parse a pasted 'Label : value' (or 'key : value') drop into {field.key: value}.

    - split each line on the FIRST colon only (line.partition(':'))
    - strip exactly one separator space after the colon, preserve the rest
    - colon-less lines are ignored
    - each line's left-hand side is matched, case-insensitively, against
      EITHER a field's `label` (the human-readable name, e.g. "Recruiter
      name") OR its `key` (the internal snake_case identifier, e.g.
      "recruiter_name") — a drop can be typed either way. `key` is checked
      first: for a single-word field (e.g. `key: company, label: Company`)
      the two coincide anyway, but where they diverge (any multi-word label)
      matching label-only would silently default a real field to empty
      whenever the drop was typed with the key instead — exactly the
      real-world case this fixes.
    - lines matching neither a known label nor key are ignored
    - fields never matched in the drop default to '' (missing keys default empty)
    - paste-only: no interactive field-by-field entry
    """
    by_key = {f.key.lower(): f.key for f in fields}
    by_label = {f.label.lower(): f.key for f in fields}
    values = {f.key: "" for f in fields}
    for line in text.splitlines():
        label_raw, sep, value_raw = line.partition(":")
        if not sep:
            continue
        lookup = label_raw.strip().lower()
        key = by_key.get(lookup) or by_label.get(lookup)
        if key is None:
            continue
        values[key] = value_raw[1:] if value_raw.startswith(" ") else value_raw
    return values


def fill_template(text: str, values: dict, fields: list) -> str:
    """Fill {{key}} placeholders locally (not GMass merge).

    A line referencing an optional field whose value is empty is dropped
    entirely, cleanly — no whitespace hacks. All other placeholders are
    substituted from `values`.
    """
    optional_keys = {f.key for f in fields if f.optional}
    out_lines = []
    for line in text.splitlines():
        keys_in_line = PLACEHOLDER_RE.findall(line)
        if any(k in optional_keys and values.get(k, "") == "" for k in keys_in_line):
            continue
        out_lines.append(PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], line))
    return "\n".join(out_lines)
