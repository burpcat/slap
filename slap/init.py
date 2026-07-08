"""Interactive installer (`slap.py init`) — post-launch distribution feature.

Turns a fresh clone of the distributed tree into a working local install:
preflight checks, config.yaml, .env, the owner test-guard, schedule, an
optional first campaign, the tracking DB, and the launchd plist — then runs
doctor and prints the exact self-test address to use for a real first send.

Re-runnable and idempotent ("check, don't install" / "fail loud, never
silent", CLAUDE.md): every step inspects on-disk state first and asks before
overwriting anything real; nothing here is ever auto-installed, only checked
and reported. Bad input is rejected on the spot with a clear message and
re-prompted — never silently accepted or defaulted past.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from slap import display, doctor
from slap.config import VALID_DAYS, ConfigError, load_global_config

CONFIG_PATH = Path("config.yaml")
CONFIG_EXAMPLE_PATH = Path("config.yaml.example")
ENV_PATH = Path(".env")
ENV_EXAMPLE_PATH = Path(".env.example")
CAMPAIGNS_DIR = Path("campaigns")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class InitError(Exception):
    """Raised on a fail-loud structural problem init cannot recover from
    (e.g. a required .example template is missing from the repo)."""


# --- small interactive helpers ----------------------------------------------

def _ask(prompt: str, *, default: str = None, read_line=input) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = read_line(f"{prompt}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        display.warn("  This field is required — please enter a value.")


def _ask_yn(prompt: str, *, default: bool = True, read_line=input) -> bool:
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


def _ask_email(prompt: str, *, default: str = None, read_line=input) -> str:
    while True:
        value = _ask(prompt, default=default, read_line=read_line)
        if _EMAIL_RE.match(value):
            return value
        display.warn(f"  {value!r} doesn't look like a valid email address — try again.")


def _ask_int(prompt: str, *, default: int, min_value: int = None, read_line=input) -> int:
    while True:
        raw = _ask(prompt, default=str(default), read_line=read_line)
        try:
            value = int(raw)
        except ValueError:
            display.warn(f"  {raw!r} is not a whole number — try again.")
            continue
        if min_value is not None and value < min_value:
            display.warn(f"  Must be >= {min_value} — try again.")
            continue
        return value


def _ask_time(prompt: str, *, default: str, read_line=input) -> str:
    while True:
        raw = _ask(prompt, default=default, read_line=read_line)
        if _TIME_RE.match(raw):
            return raw
        display.warn(f"  {raw!r} isn't 24-hour HH:MM time — try again (e.g. 09:00).")


def _ask_days(prompt: str, *, default: list, read_line=input) -> list:
    default_str = ",".join(default)
    while True:
        raw = _ask(f"{prompt} (comma-separated)", default=default_str, read_line=read_line)
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


# --- config.yaml patching (preserves comments/formatting) -------------------

def _set_scalar_line(path: Path, key: str, value: str) -> None:
    """Replaces the value on the first `<indent>key: ...` line in the file,
    preserving indentation and any trailing comment. config.yaml.example's
    schema is small and flat enough that each key init ever writes
    (from_email, from_name, fire_window_start/end, daily_cap, active_days)
    is unique in the file, so no section-scoping is needed — a full
    yaml.safe_load()+dump() round-trip would silently strip every explanatory
    comment in the file, which this avoids."""
    text = path.read_text()
    pattern = re.compile(rf"^([ \t]*{re.escape(key)}:)([^\n#]*)(#.*)?$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        raise InitError(f"{path}: could not find key {key!r} to update.")
    comment = f" {match.group(3)}" if match.group(3) else ""
    new_line = f"{match.group(1)} {value}{comment}"
    path.write_text(text[: match.start()] + new_line + text[match.end() :])


def _read_env_value(path: Path, key: str) -> str:
    for line in path.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line[len(key) + 1 :]
    return ""


def _set_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            path.write_text("\n".join(lines) + "\n")
            return
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")


def _placeholder_pdf() -> bytes:
    """Smallest valid one-page PDF — a scaffold placeholder only, clearly
    meant to be replaced before a real send (doctor still passes with it in
    place, so a fresh init doesn't show a false-negative right after setup)."""
    return (
        b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF"
    )


EXAMPLE_CAMPAIGN_FILES = {
    "campaign.yaml": """persona: recruiter               # -> derives the fixed cadence from config.yaml's personas:
latex:
  enabled: false                 # true = paste + compile a LaTeX resume per recipient at send time
  attachment_name: "Your_Name_Resume.pdf"   # filename the recipient sees
attachment_file: resume.pdf      # used only when latex.enabled is false -- replace this placeholder PDF
fields:
  - { key: email,        label: Email }
  - { key: role_catted,  label: Role }
  - { key: company,      label: Company }
  - { key: req_id,       label: Req ID, optional: true }   # inline, e.g. leading space " (Req #1234)" -- blank drops the whole line it's on
  - { key: contact_name, label: Contact name }
  - { key: byebye,       label: Signoff }
""",
    "initial.txt": """Subject: {{role_catted}} at {{company}}{{req_id}} -- quick intro

hi {{contact_name}},

I came across the {{role_catted}} role at {{company}} and wanted to reach out directly.

<one or two lines on why you're relevant -- keep it short and specific>

Happy to send more detail or get pointed to the right person. Resume attached.

{{byebye}},
Your Name
your-linkedin-or-site-here
""",
    "stage1.txt": """Following up in case this got buried -- still interested in {{role_catted}} at {{company}} if there's a fit.

{{byebye}},
Your Name
""",
    "stage2.txt": """Last note on this one -- if {{role_catted}} at {{company}} isn't the right fit anymore, no worries, and thanks for reading either way.

{{byebye}},
Your Name
""",
    "stage3.txt": """One final check-in on {{role_catted}} at {{company}} -- I'll leave it here after this.

{{byebye}},
Your Name
""",
}


# --- steps -------------------------------------------------------------------

def step_preflight() -> None:
    display.plain("\n== 1. Preflight ==")
    checks = [
        ("python3 (>=3.11)", sys.version_info >= (3, 11),
         f"found {platform.python_version()}",
         f"found {platform.python_version()} — install 3.11+ from python.org or `brew install python@3.12`"),
        ("virtualenv", sys.prefix != sys.base_prefix,
         "active",
         "not detected — recommended (not required): python3 -m venv .venv && source .venv/bin/activate"),
        ("macOS", platform.system() == "Darwin",
         "",
         f"detected {platform.system()} — the unattended runner needs macOS launchd; "
         f"the launchd step later in this installer won't work here"),
        ("xelatex", shutil.which("xelatex") is not None,
         "",
         "not found — only required for campaigns with latex.enabled: true. "
         "Install MacTeX: brew install --cask mactex-no-gui"),
        ("code CLI", shutil.which("code") is not None,
         "",
         "not found — only required for latex campaigns (the compile-loop editor step). "
         "In VS Code: Cmd+Shift+P -> 'Shell Command: Install code command in PATH'"),
    ]
    for name, ok, ok_detail, fail_detail in checks:
        if ok:
            display.success(f"  {name}: OK" + (f" ({ok_detail})" if ok_detail else ""))
        else:
            display.warn(f"  {name}: MISSING — {fail_detail}")
    display.plain("  (Nothing above is auto-installed — check, don't install. Continuing regardless.)")


def step_sender(*, read_line=input) -> None:
    display.plain("\n== 2. Sender ==")
    if CONFIG_PATH.exists():
        try:
            existing = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        except yaml.YAMLError:
            existing = {}
        cur_email = (existing.get("sender") or {}).get("from_email", "")
        cur_name = (existing.get("sender") or {}).get("from_name", "")
        if cur_email and cur_name:
            display.plain(f"  {CONFIG_PATH} already has sender.from_email={cur_email!r}, from_name={cur_name!r}")
            if not _ask_yn("  Overwrite sender settings?", default=False, read_line=read_line):
                return
    else:
        if not CONFIG_EXAMPLE_PATH.exists():
            raise InitError(f"{CONFIG_EXAMPLE_PATH} not found — cannot scaffold {CONFIG_PATH}.")
        CONFIG_PATH.write_text(CONFIG_EXAMPLE_PATH.read_text())
        display.success(f"  Created {CONFIG_PATH} from {CONFIG_EXAMPLE_PATH}.")

    from_email = _ask_email("  Your Gmail address (must be the account your GMass API key is connected to)",
                             read_line=read_line)
    from_name = _ask("  Your name (used as the email sign-off / From name)", read_line=read_line)
    display.warn(
        "  ⚠ from_email MUST be the Gmail account your GMass account is connected to — "
        "GMass sends by relaying through that account."
    )
    _set_scalar_line(CONFIG_PATH, "from_email", from_email)
    _set_scalar_line(CONFIG_PATH, "from_name", from_name)
    display.success(f"  Wrote sender.from_email/from_name to {CONFIG_PATH}.")


def _check_env_gitignored() -> None:
    try:
        result = subprocess.run(["git", "check-ignore", "-q", str(ENV_PATH)])
    except FileNotFoundError:
        display.warn("  git not found on PATH — could not verify .env is gitignored. Check manually.")
        return
    if result.returncode == 0:
        display.success("  .env is gitignored — confirmed.")
    else:
        display.warn(f"  ⚠ {ENV_PATH} does NOT appear to be gitignored! Add it to .gitignore before committing anything.")


def step_gmass_key(*, read_line=input) -> None:
    display.plain("\n== 3. GMass API key ==")
    if ENV_PATH.exists():
        existing = _read_env_value(ENV_PATH, "GMASS_API_KEY")
        if existing:
            masked = f"{existing[:4]}…{existing[-4:]}" if len(existing) > 8 else "…"
            display.plain(f"  {ENV_PATH} already has GMASS_API_KEY set ({masked}, {len(existing)} chars).")
            if not _ask_yn("  Replace it?", default=False, read_line=read_line):
                _check_env_gitignored()
                return
    else:
        if not ENV_EXAMPLE_PATH.exists():
            raise InitError(f"{ENV_EXAMPLE_PATH} not found — cannot scaffold {ENV_PATH}.")
        ENV_PATH.write_text(ENV_EXAMPLE_PATH.read_text())

    key = _ask("  Paste your GMass API key (find it at gmass.co under Settings -> API)", read_line=read_line)
    _set_env_value(ENV_PATH, "GMASS_API_KEY", key)
    # Also set it in THIS process's environment immediately: .env is only
    # loaded into os.environ by python-dotenv at process start (slap.py's
    # own load_dotenv() call), which already ran before this key existed —
    # without this, step_finish's doctor check would show a false FAIL for
    # a key that was just written correctly to disk.
    os.environ["GMASS_API_KEY"] = key
    display.success(f"  Wrote GMASS_API_KEY to {ENV_PATH} ({len(key)} chars — never printed in full).")
    _check_env_gitignored()


def step_owner_guard(global_config) -> None:
    display.plain("\n== 4. Owner test-guard ==")
    local, _, domain = global_config.from_email.partition("@")
    display.success(
        f"  Your safe self-test address is {local}+testmass{{N}}@{domain} — derived live from "
        f"config.yaml's sender.from_email, never a hardcoded address. Use it as the Email field the "
        f"first time you run `send` on any campaign, so a real send can only ever reach your own inbox."
    )


def step_schedule(*, read_line=input, use_recommended_defaults: bool = False) -> None:
    """use_recommended_defaults=True (a brand-new install, config.yaml didn't
    exist before this run) shows the installer's own recommended starting
    point (50/day, 09:00-09:15, Mon-Fri) — NOT config.yaml.example's own
    template literal (daily_cap: 500), which is a generous ceiling, not a
    recommendation. On a re-run against an already-customized config.yaml,
    defaults instead reflect whatever is currently configured, so pressing
    enter through every prompt reproduces the same schedule (idempotent)."""
    display.plain("\n== 5. Schedule ==")
    current = None
    if not use_recommended_defaults:
        try:
            current = load_global_config(CONFIG_PATH).schedule
        except ConfigError:
            current = None
    default_start = current.fire_window_start if current else "09:00"
    default_end = current.fire_window_end if current else "09:15"
    default_cap = current.daily_cap if current else 50
    default_days = current.active_days if current else ["mon", "tue", "wed", "thu", "fri"]

    fire_start = _ask_time("  Fire window start (24h HH:MM)", default=default_start, read_line=read_line)
    fire_end = _ask_time("  Fire window end (24h HH:MM)", default=default_end, read_line=read_line)
    display.plain("  Tip: keep daily volume low for cold-outreach deliverability (50 or under is reasonable).")
    daily_cap = _ask_int("  Daily send cap", default=default_cap, min_value=1, read_line=read_line)
    if daily_cap > 100:
        display.warn(
            f"  ⚠ {daily_cap}/day is high for cold outreach — consider keeping it well under 100 to "
            f"protect your Gmail account's sender reputation."
        )
    active_days = _ask_days("  Active days (runner only fires on these)",
                             default=default_days, read_line=read_line)

    _set_scalar_line(CONFIG_PATH, "fire_window_start", f'"{fire_start}"')
    _set_scalar_line(CONFIG_PATH, "fire_window_end", f'"{fire_end}"')
    _set_scalar_line(CONFIG_PATH, "daily_cap", str(daily_cap))
    _set_scalar_line(CONFIG_PATH, "active_days", "[" + ", ".join(active_days) + "]")
    display.success(
        f"  Wrote schedule: {fire_start}-{fire_end}, daily_cap={daily_cap}, active_days={active_days}."
    )


def step_first_campaign(*, read_line=input) -> None:
    display.plain("\n== 6. First campaign (optional) ==")
    if not _ask_yn("  Scaffold an example campaign folder now?", default=True, read_line=read_line):
        display.plain("  Skipped — see USAGE.md 'Create a new campaign' whenever you're ready.")
        return
    name = _ask("  Campaign folder name", default="example-campaign", read_line=read_line)
    dest = CAMPAIGNS_DIR / name
    if dest.exists():
        display.warn(f"  {dest} already exists — not overwriting. Choose a different name or delete it first.")
        return
    dest.mkdir(parents=True)
    for filename, content in EXAMPLE_CAMPAIGN_FILES.items():
        (dest / filename).write_text(content)
    (dest / "resume.pdf").write_bytes(_placeholder_pdf())
    display.success(
        f"  Scaffolded {dest}/ — edit campaign.yaml/initial.txt/stageN.txt, and replace the placeholder "
        f"resume.pdf with your real resume, before running `send`."
    )


def step_database() -> None:
    display.plain("\n== 7. Database ==")
    from slap import tracking
    existed = tracking.DB_PATH.exists()
    tracking.connect().close()
    if existed:
        display.success(f"  {tracking.DB_PATH} already exists — left untouched.")
    else:
        display.success(f"  Created empty {tracking.DB_PATH}.")


def step_launchd(global_config) -> None:
    display.plain("\n== 8. Launchd (unattended runner) ==")
    if platform.system() != "Darwin":
        display.warn("  Skipped — launchd is macOS-only and this isn't macOS.")
        return
    from slap import launchd
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.slap.runner.plist"
    plist_text = launchd.render_plist(global_config, Path.cwd(), sys.executable)
    display.plain("  Generated plist:\n")
    print(plist_text)
    display.plain(
        f"  To install, run:\n"
        f"    python slap.py plist > {plist_path}\n"
        f"    launchctl load {plist_path}\n\n"
        f"  launchd runs with a bare environment — slap.py runner loads .env itself via "
        f"python-dotenv, so GMASS_API_KEY does not need to be in launchd's own environment.\n\n"
        f"  One-time wake-test procedure: see LAUNCHD.md's 'One-time manual test checklist' — "
        f"this behavior can only be verified on real hardware."
    )


def step_finish(global_config) -> None:
    display.plain("\n== 9. Finish ==")
    ok = doctor.print_report(global_config)
    local, _, domain = global_config.from_email.partition("@")
    display.plain(
        f"\n  Your safe self-test address: {local}+testmass1@{domain} — use this as the Email field "
        f"the first time you run `python slap.py send <campaign>`, so nothing goes to a real lead."
    )
    display.plain(
        "\n  Optional: set RESUME_ARCHIVE_DIR in .env to a folder path and every résumé you send gets "
        "symlinked there as <company>-<role>-<date>.pdf — one place to browse everything you've ever "
        "sent. Off by default and never blocks a send either way — see README.md."
    )
    if ok:
        display.success("\ninit complete — all checks passed. You're ready to `python slap.py send <campaign>`.")
    else:
        display.warn(
            "\ninit complete, but some checks are failing above — fix those before your first real send. "
            "Run `python slap.py doctor` any time to recheck."
        )


def run_init(*, read_line=input) -> None:
    display.plain("slap init — interactive installer\n" + "=" * 40)
    is_fresh_install = not CONFIG_PATH.exists()
    step_preflight()
    step_sender(read_line=read_line)
    step_gmass_key(read_line=read_line)
    try:
        global_config = load_global_config()
    except ConfigError as e:
        raise InitError(f"config.yaml is invalid after setup — {e}") from e
    step_owner_guard(global_config)
    step_schedule(read_line=read_line, use_recommended_defaults=is_fresh_install)
    try:
        global_config = load_global_config()
    except ConfigError as e:
        raise InitError(f"config.yaml is invalid after the schedule step — {e}") from e
    step_first_campaign(read_line=read_line)
    step_database()
    step_launchd(global_config)
    step_finish(global_config)
