"""Interactive installer tests (`slap.py init`, post-launch distribution
feature). Uses the same ScriptedInput pattern as tests/test_latex.py's
read_command injection — every prompting function here takes read_line
instead of calling the input() builtin directly, so no monkeypatching of
builtins is needed.
"""
from pathlib import Path

import pytest

from slap import init
from slap.config import load_global_config

REPO_ROOT = Path(__file__).resolve().parent.parent


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


def _copy_example_files(tmp_path):
    (tmp_path / "config.yaml.example").write_text((REPO_ROOT / "config.yaml.example").read_text())
    (tmp_path / ".env.example").write_text((REPO_ROOT / ".env.example").read_text())


# --- _set_scalar_line ---------------------------------------------------

def test_set_scalar_line_replaces_value_and_preserves_comment(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("schedule:\n  daily_cap: 500 # Gmail ceiling; drain stops here\n")
    init._set_scalar_line(path, "daily_cap", "50")
    text = path.read_text()
    assert "daily_cap: 50 # Gmail ceiling; drain stops here" in text


def test_set_scalar_line_preserves_unrelated_lines(tmp_path):
    path = tmp_path / "config.yaml"
    original = (REPO_ROOT / "config.yaml.example").read_text()
    path.write_text(original)
    init._set_scalar_line(path, "from_email", "me@gmail.com")
    text = path.read_text()
    assert "from_email: me@gmail.com" in text
    # every other line (including comments) survives untouched
    for line in original.splitlines():
        if line.strip().startswith("from_email:"):
            continue
        assert line in text.splitlines()


def test_set_scalar_line_raises_for_unknown_key(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("sender:\n  from_email: x@gmail.com\n")
    with pytest.raises(init.InitError):
        init._set_scalar_line(path, "does_not_exist", "value")


# --- .env value helpers ---------------------------------------------------

def test_env_value_round_trip(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GMASS_API_KEY=\n")
    assert init._read_env_value(path, "GMASS_API_KEY") == ""
    init._set_env_value(path, "GMASS_API_KEY", "secret123")
    assert init._read_env_value(path, "GMASS_API_KEY") == "secret123"


# --- step_preflight ---------------------------------------------------------

def test_step_preflight_reports_redis_missing(monkeypatch, capsys):
    monkeypatch.setattr(init.shutil, "which", lambda name: None)
    init.step_preflight()
    out = capsys.readouterr().out
    assert "redis-server: MISSING" in out
    assert "brew install redis" in out


def test_step_preflight_reports_redis_found(monkeypatch, capsys):
    monkeypatch.setattr(init.shutil, "which", lambda name: f"/usr/bin/{name}")
    init.step_preflight()
    out = capsys.readouterr().out
    assert "redis-server: OK" in out


def test_step_preflight_never_blocks_on_missing_redis(monkeypatch):
    # "Check, don't install" (CLAUDE.md): preflight only reports, it must
    # never raise or halt the installer just because an optional dependency
    # (Redis, needed only for the hourly dashboard cache) is absent.
    monkeypatch.setattr(init.shutil, "which", lambda name: None)
    init.step_preflight()  # must not raise


# --- step_sender -----------------------------------------------------------

def test_step_sender_scaffolds_and_writes_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    read_line = ScriptedInput(["me@gmail.com", "Jane Doe"])
    init.step_sender(read_line=read_line)

    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.from_email == "me@gmail.com"
    assert gc.from_name == "Jane Doe"


def test_step_sender_is_idempotent_when_declined(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_sender(read_line=ScriptedInput(["me@gmail.com", "Jane Doe"]))

    # Re-run: decline the overwrite -- must leave the existing values intact
    # and must NOT prompt for new email/name (ScriptedInput would raise if it did).
    init.step_sender(read_line=ScriptedInput(["n"]))
    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.from_email == "me@gmail.com"
    assert gc.from_name == "Jane Doe"


def test_step_sender_overwrites_when_confirmed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_sender(read_line=ScriptedInput(["me@gmail.com", "Jane Doe"]))
    init.step_sender(read_line=ScriptedInput(["y", "other@gmail.com", "Other Name"]))
    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.from_email == "other@gmail.com"
    assert gc.from_name == "Other Name"


def test_step_sender_rejects_invalid_email_and_reprompts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    read_line = ScriptedInput(["not-an-email", "me@gmail.com", "Jane Doe"])
    init.step_sender(read_line=read_line)
    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.from_email == "me@gmail.com"


def test_step_sender_fails_loud_without_example_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(init.InitError):
        init.step_sender(read_line=ScriptedInput([]))


# --- step_gmass_key ----------------------------------------------------

def test_step_gmass_key_scaffolds_and_writes_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_gmass_key(read_line=ScriptedInput(["abc123secretkey"]))
    assert init._read_env_value(tmp_path / ".env", "GMASS_API_KEY") == "abc123secretkey"


def test_step_gmass_key_is_idempotent_when_declined(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_gmass_key(read_line=ScriptedInput(["abc123secretkey"]))
    init.step_gmass_key(read_line=ScriptedInput(["n"]))
    assert init._read_env_value(tmp_path / ".env", "GMASS_API_KEY") == "abc123secretkey"


def test_step_gmass_key_never_prints_full_key(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_gmass_key(read_line=ScriptedInput(["super-secret-value-xyz"]))
    out = capsys.readouterr().out
    assert "super-secret-value-xyz" not in out


# --- step_schedule -----------------------------------------------------

def test_step_schedule_writes_all_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_sender(read_line=ScriptedInput(["me@gmail.com", "Jane Doe"]))
    read_line = ScriptedInput(["10:00", "10:30", "25", "mon,wed,fri"])
    init.step_schedule(read_line=read_line)

    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.schedule.fire_window_start == "10:00"
    assert gc.schedule.fire_window_end == "10:30"
    assert gc.schedule.daily_cap == 25
    assert gc.schedule.active_days == ["mon", "wed", "fri"]


def test_step_schedule_rejects_bad_time_and_reprompts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_sender(read_line=ScriptedInput(["me@gmail.com", "Jane Doe"]))
    read_line = ScriptedInput(["25:99", "09:00", "09:15", "50", "mon,tue"])
    init.step_schedule(read_line=read_line)
    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.schedule.fire_window_start == "09:00"


def test_step_schedule_rejects_invalid_day_and_reprompts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_sender(read_line=ScriptedInput(["me@gmail.com", "Jane Doe"]))
    read_line = ScriptedInput(["09:00", "09:15", "50", "someday,mon", "mon,tue"])
    init.step_schedule(read_line=read_line)
    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.schedule.active_days == ["mon", "tue"]


def test_step_schedule_fresh_install_defaults_to_recommended_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_sender(read_line=ScriptedInput(["me@gmail.com", "Jane Doe"]))
    # blank answers everywhere -> must fall back to init's own recommended
    # defaults (50/day, 09:00-09:15, Mon-Fri), NOT config.yaml.example's
    # own template literal (daily_cap: 500).
    init.step_schedule(read_line=ScriptedInput(["", "", "", ""]), use_recommended_defaults=True)
    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.schedule.fire_window_start == "09:00"
    assert gc.schedule.fire_window_end == "09:15"
    assert gc.schedule.daily_cap == 50
    assert gc.schedule.active_days == ["mon", "tue", "wed", "thu", "fri"]


def test_step_schedule_rerun_defaults_to_current_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_sender(read_line=ScriptedInput(["me@gmail.com", "Jane Doe"]))
    init.step_schedule(read_line=ScriptedInput(["11:00", "11:30", "20", "tue,thu"]), use_recommended_defaults=True)

    # Re-run with blank answers (use_recommended_defaults=False, the
    # default) -- must preserve the values just set, not reset to 50/Mon-Fri.
    init.step_schedule(read_line=ScriptedInput(["", "", "", ""]))
    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.schedule.fire_window_start == "11:00"
    assert gc.schedule.fire_window_end == "11:30"
    assert gc.schedule.daily_cap == 20
    assert gc.schedule.active_days == ["tue", "thu"]


def test_step_schedule_rejects_non_integer_cap_and_reprompts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_sender(read_line=ScriptedInput(["me@gmail.com", "Jane Doe"]))
    read_line = ScriptedInput(["09:00", "09:15", "not-a-number", "50", "mon"])
    init.step_schedule(read_line=read_line)
    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.schedule.daily_cap == 50


# --- step_first_campaign ------------------------------------------------

def test_step_first_campaign_scaffolds_and_passes_doctor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_sender(read_line=ScriptedInput(["me@gmail.com", "Jane Doe"]))
    init.step_schedule(read_line=ScriptedInput(["09:00", "09:15", "50", "mon"]))
    init.step_first_campaign(read_line=ScriptedInput(["y", "example-campaign"]))

    campaign_dir = tmp_path / "campaigns" / "example-campaign"
    assert (campaign_dir / "campaign.yaml").exists()
    assert (campaign_dir / "resume.pdf").exists()

    from slap.config import load_campaign
    gc = load_global_config(tmp_path / "config.yaml")
    campaign = load_campaign("example-campaign", gc, campaigns_dir=tmp_path / "campaigns")

    from slap.doctor import run_campaign_checks
    assert all(r.ok for r in run_campaign_checks(campaign))


def test_step_first_campaign_skipped_when_declined(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init.step_first_campaign(read_line=ScriptedInput(["n"]))
    assert not (tmp_path / "campaigns").exists()


def test_step_first_campaign_never_overwrites_existing_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dest = tmp_path / "campaigns" / "example-campaign"
    dest.mkdir(parents=True)
    (dest / "sentinel.txt").write_text("do not touch")
    init.step_first_campaign(read_line=ScriptedInput(["y", "example-campaign"]))
    assert (dest / "sentinel.txt").read_text() == "do not touch"
    assert not (dest / "campaign.yaml").exists()


# --- step_database -------------------------------------------------------

def test_step_database_creates_empty_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from slap import tracking
    init.step_database()
    assert (tmp_path / "slap.db").exists()
    conn = tracking.connect(tmp_path / "slap.db")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    conn.close()


def test_step_database_leaves_existing_db_untouched(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from slap.tracking import append_event, connect
    conn = connect(tmp_path / "slap.db")
    append_event(conn, type="queued", recipient="a@b.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    conn.close()

    init.step_database()
    conn = connect(tmp_path / "slap.db")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    conn.close()


# --- step_launchd --------------------------------------------------------

def test_step_launchd_skips_on_non_macos(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_sender(read_line=ScriptedInput(["me@gmail.com", "Jane Doe"]))
    gc = load_global_config(tmp_path / "config.yaml")
    monkeypatch.setattr(init.platform, "system", lambda: "Linux")
    init.step_launchd(gc)
    assert "Skipped" in capsys.readouterr().out


def test_step_launchd_prints_plist_on_macos(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    init.step_sender(read_line=ScriptedInput(["me@gmail.com", "Jane Doe"]))
    gc = load_global_config(tmp_path / "config.yaml")
    monkeypatch.setattr(init.platform, "system", lambda: "Darwin")
    init.step_launchd(gc)
    out = capsys.readouterr().out
    assert "com.slap.runner" in out
    assert "launchctl load" in out
    assert "--job sync" in out
    assert "com.slap.sync" in out


# --- full run_init() integration ------------------------------------------

def test_run_init_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    monkeypatch.setattr(init.platform, "system", lambda: "Linux")  # skip launchd step deterministically

    answers = [
        "me@gmail.com", "Jane Doe",          # sender
        "abc123secretkey",                   # gmass key
        "09:00", "09:15", "50", "mon,tue,wed,thu,fri",  # schedule
        "y", "example-campaign",             # first campaign
    ]
    init.run_init(read_line=ScriptedInput(answers))

    assert (tmp_path / "config.yaml").exists()
    assert init._read_env_value(tmp_path / ".env", "GMASS_API_KEY") == "abc123secretkey"
    assert (tmp_path / "campaigns" / "example-campaign" / "campaign.yaml").exists()
    assert (tmp_path / "slap.db").exists()

    from slap.doctor import print_report
    gc = load_global_config(tmp_path / "config.yaml")
    assert print_report(gc) is True


def test_run_init_is_fully_idempotent_on_second_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _copy_example_files(tmp_path)
    monkeypatch.setattr(init.platform, "system", lambda: "Linux")

    first_answers = [
        "me@gmail.com", "Jane Doe", "abc123secretkey",
        "09:00", "09:15", "50", "mon,tue,wed,thu,fri",
        "y", "example-campaign",
    ]
    init.run_init(read_line=ScriptedInput(first_answers))

    # Second run: decline every overwrite/re-scaffold prompt. Must not
    # require any further answers and must leave everything unchanged.
    second_answers = ["n", "n", "10:00", "10:15", "60", "mon", "n"]
    init.run_init(read_line=ScriptedInput(second_answers))

    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.from_email == "me@gmail.com"  # sender untouched (declined)
    assert init._read_env_value(tmp_path / ".env", "GMASS_API_KEY") == "abc123secretkey"  # key untouched
    assert gc.schedule.fire_window_start == "10:00"  # schedule step always re-runs (no idempotency gate)
