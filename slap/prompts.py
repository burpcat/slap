"""Shared interactive-prompt primitives.

Every prompt in this app is a plain `input()`-style call with an injectable
`read_line` parameter (never a curses/questionary/prompt_toolkit dependency —
see requirements.txt) so tests can script deterministic input instead of
driving a real TTY. Originally two separate copies existed (`slap/init.py`'s
`_ask*` family for the installer, `slap.py`'s own `read_paste`/
`PASTE_TERMINATOR` for the drop/LaTeX paste loop) — extracted here once a
second real interactive wizard (`slap/onboard.py`, `onboard-campaign`) needed
the exact same primitives, so both stay byte-for-byte identical instead of
two copies that could quietly drift apart.
"""
from __future__ import annotations

import re

from slap import display
from slap.config import VALID_DAYS

PASTE_TERMINATOR = "<<<EOF>>>"


def read_paste(prompt: str, read_line=input) -> str:
    """Reads a multi-line paste terminated by a line containing only
    PASTE_TERMINATOR, not a blocking read-until-EOF. A real read-until-EOF
    (sys.stdin.read()) would consume the entire stdin stream, leaving
    nothing for any later input() prompt (Add another?, confirmations) to
    read — this works correctly for both a live terminal and piped/scripted
    input, and doesn't rely on TTY-specific Ctrl-D-per-read semantics."""
    print(f"{prompt} (end with a line containing only {PASTE_TERMINATOR}):")
    lines = []
    while True:
        try:
            line = read_line()
        except EOFError:
            break
        if line.strip() == PASTE_TERMINATOR:
            break
        lines.append(line)
    return "\n".join(lines)


def ask(prompt: str, *, default: str = None, read_line=input) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = read_line(f"{prompt}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        display.warn("  This field is required — please enter a value.")


def ask_yn(prompt: str, *, default: bool = True, read_line=input) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        raw = read_line(f"{prompt}{suffix}: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        display.warn("  Please answer y or n.")


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def ask_email(prompt: str, *, default: str = None, read_line=input) -> str:
    while True:
        value = ask(prompt, default=default, read_line=read_line)
        if _EMAIL_RE.match(value):
            return value
        display.warn(f"  {value!r} doesn't look like a valid email address — try again.")


def ask_int(prompt: str, *, default: int, min_value: int = None, read_line=input) -> int:
    while True:
        raw = ask(prompt, default=str(default), read_line=read_line)
        try:
            value = int(raw)
        except ValueError:
            display.warn(f"  {raw!r} is not a whole number — try again.")
            continue
        if min_value is not None and value < min_value:
            display.warn(f"  Must be >= {min_value} — try again.")
            continue
        return value


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def ask_time(prompt: str, *, default: str, read_line=input) -> str:
    while True:
        raw = ask(prompt, default=default, read_line=read_line)
        if _TIME_RE.match(raw):
            return raw
        display.warn(f"  {raw!r} isn't 24-hour HH:MM time — try again (e.g. 09:00).")


def ask_days(prompt: str, *, default: list, read_line=input) -> list:
    default_str = ",".join(default)
    while True:
        raw = ask(f"{prompt} (comma-separated)", default=default_str, read_line=read_line)
        days = [d.strip().lower() for d in raw.split(",") if d.strip()]
        invalid = [d for d in days if d not in VALID_DAYS]
        if invalid:
            display.warn(f"  Not a valid day name: {invalid} — use one of {VALID_DAYS}")
            continue
        if len(set(days)) != len(days):
            display.warn("  Duplicate day(s) in that list — try again.")
            continue
        if not days:
            display.warn("  Need at least one active day.")
            continue
        return days


def placeholder_pdf() -> bytes:
    """Smallest valid one-page PDF — a scaffold placeholder only, clearly
    meant to be replaced before a real send (doctor still passes with it in
    place, so a fresh init/onboard doesn't show a false-negative right after
    setup). `slap.doctor.check_placeholder_resume` compares a campaign's
    `attachment_file` bytes against this EXACT constant to keep warning at
    `send` time for as long as the scaffold hasn't been replaced — both sides
    must stay byte-for-byte identical, which is the whole reason this lives
    in one shared place rather than being duplicated per caller."""
    return (
        b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF"
    )
