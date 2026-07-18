"""Interactive campaign-onboarding wizard tests (`slap.py onboard-campaign`,
post-launch feature). Same ScriptedInput pattern as tests/test_init.py's
read_line injection — every prompting function here takes read_line instead
of calling the input() builtin directly, so no monkeypatching of builtins is
needed.
"""
import re
from pathlib import Path

import pytest
import yaml

from slap import onboard
from slap.config import ConfigError, load_campaign, load_global_config
from slap.doctor import run_campaign_checks

REPO_ROOT = Path(__file__).resolve().parent.parent


def _plain(out: str) -> str:
    """Strips ANSI codes and collapses whitespace/line-wraps, so a long
    rich-printed message can be substring-matched regardless of the
    console's width or color state (rich soft-wraps a single long
    console.print() call across multiple lines, re-opening/closing style
    codes at each wrap point — see tests/test_display.py's identical
    ANSI-stripping precedent)."""
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", out).split())


class ScriptedInput:
    """Feeds a fixed sequence of answers to read_line, like a scripted
    terminal session. Raises if the script runs out (a step asked more
    questions than expected) rather than hanging or returning garbage."""
    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, prompt=""):
        if not self.answers:
            raise AssertionError(f"ScriptedInput exhausted; no answer left for prompt: {prompt!r}")
        return self.answers.pop(0)


def _copy_config(tmp_path, *, extra_persona_yaml: str = "") -> None:
    """Real config.yaml.example, patched with 'Test Owner', optionally with
    an extra persona line appended to the personas: block (used to prove the
    wizard's follow-up count tracks the CHOSEN persona's cadence length, not
    a hardcoded number)."""
    text = (REPO_ROOT / "config.yaml.example").read_text().replace("<Owner Name>", "Test Owner")
    if extra_persona_yaml:
        text = text.replace(
            "personas: # cadences are FIXED per persona\n",
            f"personas: # cadences are FIXED per persona\n{extra_persona_yaml}",
        )
    (tmp_path / "config.yaml").write_text(text)


INITIAL_TEMPLATE = (
    "Subject: {{role_catted}} at {{company}} -- quick intro\n"
    "\n"
    "Hi {{contact_name}},\n"
    "\n"
    "Wanted to reach out about the {{role_catted}} role at {{company}}.\n"
    "\n"
    "{{byebye}},\n"
    "{{signature}}\n"
    "<<<EOF>>>"
)


def _stage_body(text: str) -> str:
    return f"{text}\n<<<EOF>>>"


# --- _ask_campaign_name -------------------------------------------------

def test_ask_campaign_name_rejects_existing_folder_and_reprompts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "campaigns" / "taken").mkdir(parents=True)
    name = onboard._ask_campaign_name(read_line=ScriptedInput(["taken", "free"]))
    assert name == "free"


# --- _ask_persona --------------------------------------------------------

def test_ask_persona_rejects_unknown_and_reprompts(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _copy_config(tmp_path)
    gc = load_global_config(tmp_path / "config.yaml")
    persona = onboard._ask_persona(gc, read_line=ScriptedInput(["not-a-persona", "recruiter"]))
    assert persona == "recruiter"
    out = capsys.readouterr().out
    assert "recruiter" in out and "hiring_manager" in out and "founder" in out


# --- _compose_initial_template --------------------------------------------

def test_compose_initial_template_accepts_valid_paste():
    subject, body, raw = onboard._compose_initial_template(read_line=ScriptedInput(INITIAL_TEMPLATE.split("\n")))
    assert subject == "{{role_catted}} at {{company}} -- quick intro"
    assert "Hi {{contact_name}}," in body
    assert raw.splitlines()[0] == "Subject: {{role_catted}} at {{company}} -- quick intro"


def test_compose_initial_template_rejects_missing_subject_and_reprompts():
    bad = "Hi there\n<<<EOF>>>"
    lines = bad.split("\n") + INITIAL_TEMPLATE.split("\n")
    subject, body, raw = onboard._compose_initial_template(read_line=ScriptedInput(lines))
    assert subject == "{{role_catted}} at {{company}} -- quick intro"


def test_compose_initial_template_rejects_malformed_placeholder_and_reprompts():
    bad = "Subject: hi {{bad-key}}\n\nbody\n<<<EOF>>>"
    lines = bad.split("\n") + INITIAL_TEMPLATE.split("\n")
    subject, body, raw = onboard._compose_initial_template(read_line=ScriptedInput(lines))
    assert subject == "{{role_catted}} at {{company}} -- quick intro"


# --- field declaration ----------------------------------------------------

def test_detect_and_declare_fields_prompts_only_for_new_keys():
    fields = {"email": {"key": "email", "label": "Email", "optional": False}}
    read_line = ScriptedInput([
        "", "",       # company: label default, optional default
        "", "y",      # req_id: label default, optional=True
    ])
    onboard._detect_and_declare_fields("hi {{company}}{{req_id}}", fields, read_line=read_line)
    assert set(fields) == {"email", "company", "req_id"}
    assert fields["company"]["label"] == "Company"
    assert fields["req_id"]["optional"] is True


def test_detect_and_declare_fields_skips_config_sourced_keys():
    fields = {"email": {"key": "email", "label": "Email", "optional": False}}
    # {{signature}} is config-sourced -- must NOT prompt at all (ScriptedInput
    # would raise if it did, since no answers are provided).
    onboard._detect_and_declare_fields("bye {{signature}}", fields, read_line=ScriptedInput([]))
    assert set(fields) == {"email"}


def test_detect_and_declare_fields_skips_already_declared_keys():
    fields = {"email": {"key": "email", "label": "Email", "optional": False},
              "company": {"key": "company", "label": "Company", "optional": False}}
    onboard._detect_and_declare_fields("hi {{company}}", fields, read_line=ScriptedInput([]))
    assert fields["company"]["label"] == "Company"  # untouched, no re-prompt


def test_offer_more_fields_adds_until_declined():
    fields = {"email": {"key": "email", "label": "Email", "optional": False}}
    read_line = ScriptedInput([
        "y", "contact_name", "", "",   # add contact_name (default label/optional)
        "n",                          # stop
    ])
    onboard._offer_more_fields(fields, read_line=read_line)
    assert set(fields) == {"email", "contact_name"}


def test_offer_more_fields_rejects_invalid_key_syntax_and_reprompts():
    fields = {"email": {"key": "email", "label": "Email", "optional": False}}
    read_line = ScriptedInput(["y", "bad-key!", "good_key", "", "", "n"])
    onboard._offer_more_fields(fields, read_line=read_line)
    assert "good_key" in fields
    assert "bad-key!" not in fields


def test_offer_more_fields_rejects_duplicate_key_and_reprompts():
    fields = {"email": {"key": "email", "label": "Email", "optional": False}}
    read_line = ScriptedInput(["y", "email", "new_key", "", "", "n"])
    onboard._offer_more_fields(fields, read_line=read_line)
    assert set(fields) == {"email", "new_key"}


# --- _compose_stage_bodies -------------------------------------------------

def test_compose_stage_bodies_asks_for_exactly_n():
    fields = {"email": {"key": "email", "label": "Email", "optional": False}}
    lines = (
        _stage_body("follow up one").split("\n")
        + _stage_body("follow up two").split("\n")
    )
    bodies = onboard._compose_stage_bodies(2, fields, read_line=ScriptedInput(lines))
    assert bodies == ["follow up one", "follow up two"]


def test_compose_stage_bodies_declares_new_fields_found_inline():
    fields = {"email": {"key": "email", "label": "Email", "optional": False}}
    lines = _stage_body("hi {{nickname}}").split("\n") + ["", ""]  # body, then label/optional defaults
    bodies = onboard._compose_stage_bodies(1, fields, read_line=ScriptedInput(lines))
    assert bodies == ["hi {{nickname}}"]
    assert "nickname" in fields


def test_compose_stage_bodies_rejects_malformed_placeholder_and_reprompts():
    fields = {"email": {"key": "email", "label": "Email", "optional": False}}
    lines = (
        _stage_body("bad {{not ok}}").split("\n")
        + _stage_body("good body").split("\n")
    )
    bodies = onboard._compose_stage_bodies(1, fields, read_line=ScriptedInput(lines))
    assert bodies == ["good body"]


# --- _review ---------------------------------------------------------------

def test_review_shows_placeholders_and_fields(capsys):
    fields = {"email": {"key": "email", "label": "Email", "optional": False},
              "company": {"key": "company", "label": "Company", "optional": True}}
    ok = onboard._review("Subject {{company}}", "body {{company}}", ["stage one"], fields,
                          read_line=ScriptedInput(["y"]))
    assert ok is True
    out = capsys.readouterr().out
    assert "{{company}}" in out
    assert "optional" in out


def test_review_returns_false_on_decline():
    fields = {"email": {"key": "email", "label": "Email", "optional": False}}
    ok = onboard._review("subj", "body", [], fields, read_line=ScriptedInput(["n"]))
    assert ok is False


# --- _ask_latex_and_attachment ----------------------------------------------

def test_ask_latex_and_attachment_latex_enabled_skips_resume_prompt():
    result = onboard._ask_latex_and_attachment(read_line=ScriptedInput(["y", "MyResume.pdf"]))
    assert result == {"latex_enabled": True, "attachment_name": "MyResume.pdf",
                       "attachment_file": None, "resume_bytes": None}


def test_ask_latex_and_attachment_static_copies_real_pdf(tmp_path):
    real_pdf = tmp_path / "real.pdf"
    real_pdf.write_bytes(b"%PDF-1.4 real content")
    result = onboard._ask_latex_and_attachment(
        read_line=ScriptedInput(["n", "Resume.pdf", str(real_pdf)])
    )
    assert result["latex_enabled"] is False
    assert result["attachment_file"] == "resume.pdf"
    assert result["resume_bytes"] == b"%PDF-1.4 real content"


def test_ask_latex_and_attachment_static_blank_path_falls_back_to_placeholder():
    result = onboard._ask_latex_and_attachment(read_line=ScriptedInput(["n", "Resume.pdf", ""]))
    assert result["resume_bytes"] == onboard.placeholder_pdf()


def test_ask_latex_and_attachment_static_bad_path_falls_back_to_placeholder_with_warning(capsys):
    result = onboard._ask_latex_and_attachment(
        read_line=ScriptedInput(["n", "Resume.pdf", "/no/such/file.pdf"])
    )
    assert result["resume_bytes"] == onboard.placeholder_pdf()
    assert "Could not read" in capsys.readouterr().out


# --- _write_campaign + load_campaign round-trip -----------------------------

def test_write_campaign_produces_a_valid_loadable_campaign(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_config(tmp_path)
    gc = load_global_config(tmp_path / "config.yaml")
    fields = {
        "email": {"key": "email", "label": "Email", "optional": False},
        "company": {"key": "company", "label": "Company", "optional": False},
        "req_id": {"key": "req_id", "label": "Req ID", "optional": True},
    }
    dest = onboard._write_campaign(
        "my-camp", persona="recruiter",
        raw_initial_text="Subject: hi {{company}}{{req_id}}\n\nbody {{company}} {{signature}}",
        stage_bodies_raw=["follow 1 {{signature}}", "follow 2 {{signature}}", "follow 3 {{signature}}"],
        fields=fields, latex_enabled=False, attachment_name="Resume.pdf",
        attachment_file="resume.pdf", resume_bytes=b"%PDF-1.4 x",
    )
    assert dest.resolve() == (tmp_path / "campaigns" / "my-camp").resolve()
    campaign = load_campaign("my-camp", gc, campaigns_dir=tmp_path / "campaigns")
    assert campaign.persona == "recruiter"
    assert campaign.cadence == [2, 3, 5]
    assert all(r.ok for r in run_campaign_checks(campaign))

    written = yaml.safe_load((dest / "campaign.yaml").read_text())
    assert written["fields"][2] == {"key": "req_id", "label": "Req ID", "optional": True}


def test_write_campaign_latex_enabled_writes_no_resume_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_config(tmp_path)
    fields = {"email": {"key": "email", "label": "Email", "optional": False}}
    dest = onboard._write_campaign(
        "latex-camp", persona="founder", raw_initial_text="Subject: hi\n\nbody {{signature}}",
        stage_bodies_raw=["f1 {{signature}}", "f2 {{signature}}", "f3 {{signature}}"],
        fields=fields, latex_enabled=True, attachment_name="Resume.pdf",
        attachment_file=None, resume_bytes=None,
    )
    assert not (dest / "resume.pdf").exists()


# --- full run_onboard_campaign() integration --------------------------------

def _full_answers(*, persona="recruiter", followups=3, latex_answer="n",
                   attachment_name="", resume_path=""):
    return (
        ["my-campaign", persona]
        + INITIAL_TEMPLATE.split("\n")
        + ["", "",   # byebye
           "", "",   # company
           "", "",   # contact_name
           "", ""]   # role_catted
        + [""]       # no more fields
        + sum((_stage_body(f"follow-up {i}").split("\n") for i in range(1, followups + 1)), [])
        + ["y"]      # review: look good?
        + [latex_answer, attachment_name, resume_path]
    )


def test_run_onboard_campaign_end_to_end_writes_valid_campaign(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_config(tmp_path)

    onboard.run_onboard_campaign(read_line=ScriptedInput(_full_answers()))

    gc = load_global_config(tmp_path / "config.yaml")
    campaign = load_campaign("my-campaign", gc, campaigns_dir=tmp_path / "campaigns")
    assert campaign.persona == "recruiter"
    assert len(campaign.stage_bodies) == 3
    assert all(r.ok for r in run_campaign_checks(campaign))

    fields_declared = {f.key for f in campaign.fields}
    assert fields_declared == {"email", "byebye", "company", "contact_name", "role_catted"}


def test_run_onboard_campaign_follow_up_count_tracks_persona_cadence_length(tmp_path, monkeypatch):
    # Real owner's config.yaml has an actual undocumented 2-stage persona
    # ("vibe: [2, 4]") -- reused here to prove the wizard asks for exactly
    # the CHOSEN persona's cadence length, not a hardcoded 3.
    monkeypatch.chdir(tmp_path)
    _copy_config(tmp_path, extra_persona_yaml="  vibe: { stages: [2, 4] }\n")
    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.personas["vibe"] == [2, 4]

    onboard.run_onboard_campaign(read_line=ScriptedInput(_full_answers(persona="vibe", followups=2)))

    campaign = load_campaign("my-campaign", gc, campaigns_dir=tmp_path / "campaigns")
    assert campaign.cadence == [2, 4]
    assert len(campaign.stage_bodies) == 2


def test_run_onboard_campaign_aborted_at_review_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_config(tmp_path)
    answers = _full_answers()
    review_index = len(answers) - 4  # the "y" for "Look good?" -- see _full_answers
    answers[review_index] = "n"
    onboard.run_onboard_campaign(read_line=ScriptedInput(answers[: review_index + 1]))
    assert not (tmp_path / "campaigns" / "my-campaign").exists()


def test_run_onboard_campaign_never_overwrites_existing_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_config(tmp_path)
    dest = tmp_path / "campaigns" / "taken"
    dest.mkdir(parents=True)
    (dest / "sentinel.txt").write_text("do not touch")

    answers = _full_answers()
    answers[0] = "taken"
    answers.insert(1, "my-campaign")  # reprompted after the collision
    onboard.run_onboard_campaign(read_line=ScriptedInput(answers))

    assert (dest / "sentinel.txt").read_text() == "do not touch"
    assert not (dest / "campaign.yaml").exists()
    assert (tmp_path / "campaigns" / "my-campaign" / "campaign.yaml").exists()


def test_run_onboard_campaign_placeholder_resume_warns_via_doctor(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _copy_config(tmp_path)
    onboard.run_onboard_campaign(read_line=ScriptedInput(_full_answers()))
    out = _plain(capsys.readouterr().out)
    assert "resume placeholder: FAIL" in out
    assert "replace it with your real résumé" in out


def test_run_onboard_campaign_fails_loud_without_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(onboard.OnboardError):
        onboard.run_onboard_campaign(read_line=ScriptedInput([]))
