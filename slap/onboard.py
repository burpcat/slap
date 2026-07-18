"""Interactive campaign scaffolding wizard (`slap.py onboard-campaign`).

Creating a campaign today means hand-writing `campaign.yaml`/`initial.txt`/
`stageN.txt` from scratch, or copying `slap/init.py`'s fixed example scaffold
and editing it. This wizard walks the owner through composing a real
template, declaring its variables, reviewing everything rendered with
placeholders highlighted, then filling in the rest of `campaign.yaml` — and
writes a folder that `load_campaign`/`doctor` accept, because every rule
either enforces is checked live here first.

One ordering is forced by the app's own architecture, not a stylistic
choice: a campaign's number of `stageN.txt` files must exactly equal its
persona's cadence length (`slap.config._validate_stage_files`), so persona
must be picked (step 2) before the follow-up bodies can be authored (step 5)
— everything else follows the owner's own requested order (template, then
variables, then a combined review, then the rest of campaign.yaml).

Every prompt here is a plain `input()`-style call with an injectable
`read_line`, reusing `slap.prompts`'s shared primitives — the same
convention `slap/init.py` already established, not a new interaction model.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from slap import display, doctor
from slap.config import CAMPAIGNS_DIR, ConfigError, load_campaign, load_global_config, parse_initial_txt_text
from slap.prompts import ask, ask_yn, placeholder_pdf, read_paste
from slap.templates import CONFIG_SOURCED_KEYS, extract_placeholder_keys, find_malformed_placeholders

_KEY_RE = re.compile(r"^\w+$")


class OnboardError(Exception):
    """Raised on a fail-loud problem the wizard cannot recover from (e.g.
    config.yaml itself is missing/invalid — nothing about a broken global
    config can be fixed by this wizard)."""


# --- step 1: campaign name ---------------------------------------------------

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _ask_campaign_name(*, read_line=input) -> str:
    while True:
        name = ask("\nCampaign folder name", read_line=read_line)
        if not _NAME_RE.match(name):
            display.warn("  Use only letters, digits, underscores, and dashes — no slashes or spaces "
                         "(this becomes a real campaigns/<name> folder name).")
            continue
        dest = CAMPAIGNS_DIR / name
        if dest.exists():
            display.warn(f"  {dest} already exists — choose a different name or delete it first.")
            continue
        return name


# --- step 2: persona (fixes the follow-up count for step 5) -----------------

def _ask_persona(global_config, *, read_line=input) -> str:
    names = sorted(global_config.personas)
    display.plain("\nAvailable personas (fixed cadences, from config.yaml):")
    for n in names:
        display.plain(f"  {n}: {global_config.personas[n]}")
    while True:
        persona = ask("Persona", read_line=read_line)
        if persona in global_config.personas:
            return persona
        display.warn(f"  {persona!r} isn't defined in config.yaml's personas: block — choose one of {names}.")


# --- step 3: initial template ------------------------------------------------

def _compose_initial_template(*, read_line=input) -> tuple:
    """Returns (subject, body, raw_text). `raw_text` is written to
    initial.txt byte-for-byte later — subject/body (parsed from it via the
    exact same rule `load_campaign` will enforce) are only used for live
    field-detection and the review panel."""
    display.plain(
        "\nPaste your initial email template. First line must be 'Subject: ...', then a "
        "blank line, then the body. Use {{key}} for anything that varies per recipient "
        "(e.g. {{company}}), and {{signature}} for your configured sign-off."
    )
    while True:
        raw_text = read_paste("Initial template", read_line=read_line)
        malformed = find_malformed_placeholders(raw_text)
        if malformed:
            display.warn(
                f"  Malformed placeholder(s) {sorted(malformed)} — a {{{{...}}}} may only contain "
                f"letters/digits/underscore. Try again."
            )
            continue
        try:
            subject, body = parse_initial_txt_text(raw_text, ctx="initial.txt")
        except ConfigError as e:
            display.warn(f"  {e}")
            continue
        return subject, body, raw_text


# --- step 4: variables --------------------------------------------------------

def _declare_field(key: str, *, read_line=input) -> dict:
    default_label = key.replace("_", " ").title()
    label = ask(f"  Label for {{{{{key}}}}} (shown when pasting a drop)", default=default_label, read_line=read_line)
    optional = ask_yn(
        f"  Is {{{{{key}}}}} optional (blank drops its WHOLE line, not just the placeholder)?",
        default=False, read_line=read_line,
    )
    return {"key": key, "label": label, "optional": optional}


def _detect_and_declare_fields(text: str, fields: dict, *, read_line=input) -> None:
    """Mutates `fields` (an ordered dict of key -> field-dict) in place,
    prompting for any newly-seen {{key}} not yet declared and not a
    CONFIG_SOURCED_KEYS constant (currently just {{signature}}, filled from
    config.yaml, never declared as a campaign field)."""
    for key in sorted(extract_placeholder_keys(text)):
        if key in CONFIG_SOURCED_KEYS or key in fields:
            continue
        display.plain(f"\nNew variable found: {{{{{key}}}}}")
        fields[key] = _declare_field(key, read_line=read_line)


def _offer_more_fields(fields: dict, *, read_line=input) -> None:
    """Fields not yet used in the initial template (e.g. only needed in a
    follow-up authored later, in step 5) can be declared ahead of time here
    — step 5 also catches any still-undeclared {{key}} inline, so this is
    purely a convenience, never the only chance to declare one."""
    while ask_yn("\nAny more fields to declare now (e.g. ones you'll use in a follow-up)?",
                  default=False, read_line=read_line):
        while True:
            key = ask("  Field key (e.g. contact_name)", read_line=read_line).strip()
            if not _KEY_RE.match(key):
                display.warn("  A field key may only contain letters/digits/underscore — try again.")
                continue
            if key in fields or key in CONFIG_SOURCED_KEYS:
                display.warn(f"  {key!r} is already declared (or config-sourced) — try again.")
                continue
            break
        fields[key] = _declare_field(key, read_line=read_line)


# --- step 5: follow-ups -------------------------------------------------------

def _compose_stage_bodies(n: int, fields: dict, *, read_line=input) -> list:
    bodies = []
    for i in range(1, n + 1):
        display.plain(f"\nFollow-up {i} body (no subject line — it threads as a reply into the same conversation).")
        while True:
            text = read_paste(f"Follow-up {i}", read_line=read_line)
            malformed = find_malformed_placeholders(text)
            if malformed:
                display.warn(
                    f"  Malformed placeholder(s) {sorted(malformed)} — a {{{{...}}}} may only contain "
                    f"letters/digits/underscore. Try again."
                )
                continue
            _detect_and_declare_fields(text, fields, read_line=read_line)
            bodies.append(text)
            break
    return bodies


# --- step 6: review -----------------------------------------------------------

def _review(subject: str, body: str, stage_bodies: list, fields: dict, *, read_line=input) -> bool:
    sections = [(f"Initial — Subject: {subject}", body)]
    for i, s in enumerate(stage_bodies, start=1):
        sections.append((f"Follow-up {i}", s))
    display.template_review_panel(sections)
    display.plain("\nDeclared fields:")
    for f in fields.values():
        opt = "  (optional — blank drops its whole line)" if f["optional"] else ""
        display.plain(f"  {{{{{f['key']}}}}} -> \"{f['label']}\"{opt}")
    return ask_yn("\nLook good?", default=True, read_line=read_line)


# --- step 7: rest of campaign.yaml --------------------------------------------

def _ask_latex_and_attachment(*, read_line=input) -> dict:
    """Returns {"latex_enabled", "attachment_name", "attachment_file",
    "resume_bytes"} — resume_bytes/attachment_file are None for a
    latex-enabled campaign (the résumé is pasted fresh per recipient at
    send time, never a static file in the campaign folder)."""
    latex_enabled = ask_yn(
        "\nDoes this campaign need a per-recipient compiled LaTeX résumé (paste + xelatex at send time)?",
        default=False, read_line=read_line,
    )
    attachment_name = ask(
        "Attachment filename the recipient will see (e.g. Firstname_Lastname_Resume.pdf)",
        default="Resume.pdf", read_line=read_line,
    )
    if latex_enabled:
        return {"latex_enabled": True, "attachment_name": attachment_name,
                "attachment_file": None, "resume_bytes": None}

    path_str = ask(
        "Path to an existing résumé PDF to copy in (leave blank to scaffold a placeholder for now)",
        default="", read_line=read_line,
    ).strip()
    resume_bytes = None
    if path_str:
        src = Path(path_str).expanduser()
        try:
            resume_bytes = src.read_bytes()
        except OSError as e:
            display.warn(f"  Could not read {src} ({e}) — scaffolding a placeholder instead; replace it before sending.")
    if resume_bytes is None:
        display.warn(
            "  Scaffolding a placeholder résumé.pdf — `doctor`/`send` will keep warning about it "
            "until you replace it with a real résumé."
        )
        resume_bytes = placeholder_pdf()
    return {"latex_enabled": False, "attachment_name": attachment_name,
            "attachment_file": "resume.pdf", "resume_bytes": resume_bytes}


# --- step 8: write + validate --------------------------------------------------

def _write_campaign(name: str, *, persona: str, raw_initial_text: str, stage_bodies_raw: list,
                     fields: dict, latex_enabled: bool, attachment_name: str,
                     attachment_file: str, resume_bytes: bytes) -> Path:
    dest = CAMPAIGNS_DIR / name
    dest.mkdir(parents=True)

    campaign_dict = {
        "persona": persona,
        "latex": {"enabled": latex_enabled, "attachment_name": attachment_name},
        "fields": [
            {"key": f["key"], "label": f["label"], **({"optional": True} if f["optional"] else {})}
            for f in fields.values()
        ],
    }
    if attachment_file:
        campaign_dict["attachment_file"] = attachment_file
    (dest / "campaign.yaml").write_text(yaml.safe_dump(campaign_dict, sort_keys=False, default_flow_style=False))

    initial_text = raw_initial_text if raw_initial_text.endswith("\n") else raw_initial_text + "\n"
    (dest / "initial.txt").write_text(initial_text)

    for i, body_text in enumerate(stage_bodies_raw, start=1):
        text = body_text if body_text.endswith("\n") else body_text + "\n"
        (dest / f"stage{i}.txt").write_text(text)

    if not latex_enabled:
        (dest / "resume.pdf").write_bytes(resume_bytes)

    return dest


# --- entry point ---------------------------------------------------------------

def run_onboard_campaign(*, read_line=input) -> None:
    try:
        global_config = load_global_config()
    except ConfigError as e:
        raise OnboardError(str(e)) from e

    display.plain("slap onboard-campaign — interactive campaign wizard\n" + "=" * 40)

    name = _ask_campaign_name(read_line=read_line)
    persona = _ask_persona(global_config, read_line=read_line)
    cadence = global_config.personas[persona]

    subject, body, raw_initial_text = _compose_initial_template(read_line=read_line)

    fields = {"email": {"key": "email", "label": "Email", "optional": False}}
    display.plain("\n'email' was added automatically — every campaign needs it to know who to send to.")
    _detect_and_declare_fields(raw_initial_text, fields, read_line=read_line)
    _offer_more_fields(fields, read_line=read_line)

    display.plain(
        f"\nPersona {persona!r} needs {len(cadence)} follow-up(s), {len(cadence)} days apart: {cadence}."
    )
    stage_bodies_raw = _compose_stage_bodies(len(cadence), fields, read_line=read_line)

    if not _review(subject, body, stage_bodies_raw, fields, read_line=read_line):
        display.warn("\nAborted — nothing written.")
        return

    rest = _ask_latex_and_attachment(read_line=read_line)

    dest = _write_campaign(
        name, persona=persona, raw_initial_text=raw_initial_text, stage_bodies_raw=stage_bodies_raw,
        fields=fields, **rest,
    )

    try:
        campaign = load_campaign(name, global_config)
    except ConfigError as e:
        raise OnboardError(
            f"{dest} was written but failed validation — this shouldn't happen (every rule was already "
            f"checked live during authoring above); fix by hand or delete the folder and rerun: {e}"
        ) from e

    display.success(f"\nScaffolded {dest}/.")
    campaign_results = doctor.run_campaign_checks(campaign) + [doctor.check_placeholder_resume(campaign)]
    ok = True
    for result in campaign_results:
        doctor.print_check(result, indent="  ")
        ok = ok and result.ok
    if ok:
        display.success(f"\nAll checks passed — ready for `python slap.py send {name}`.")
    else:
        display.warn(
            f"\nSome checks above need attention before sending to real recipients — "
            f"`python slap.py doctor` any time to recheck."
        )
