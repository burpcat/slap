"""Smoke tests for the slap.py CLI skeleton (Build Order step 2).

Originally scoped to argparse dispatch and fail-loud stubs; every one of the
six commands has since graduated to a real implementation (list: step 3,
rebuild: step 5, domains: step 7, send/runner: step 9, dashboard: step 11,
doctor: step 12) — see each command's dedicated test file. What's left here
is the top-level dispatch smoke test plus each command's own
fail-loud-before-doing-anything-real path — 'dashboard' and 'runner' in
particular are NOT exercised beyond that, since a successful run either
blocks forever (Flask's app.run()) or can sleep for minutes
(wait_for_fire_window).
"""
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

SLAP_PY = Path(__file__).resolve().parent.parent / "slap.py"
ALL_COMMANDS = ["list", "send", "dashboard", "doctor", "domains", "rebuild", "runner", "cleanup", "plist", "init"]


def run(*args, cwd=None, env=None, input=None):
    # Deterministic regardless of the ambient shell's color-forcing env vars
    # (e.g. FORCE_COLOR, set by some terminal/CI wrappers) — tests assert on
    # exact plain-text stdout/stderr content, and rich's colorization (see
    # slap/display.py) must never leak ANSI codes into that output here,
    # same as it must never leak into an actual sent email body.
    full_env = dict(env) if env is not None else dict(os.environ)
    full_env.pop("FORCE_COLOR", None)
    full_env.pop("CLICOLOR_FORCE", None)
    full_env["NO_COLOR"] = "1"
    # Hermetic against a real developer .env: slap.py's own load_dotenv() call
    # walks UP from slap.py's file location looking for a .env — when tests
    # run from a worktree nested under the real repo (as they do here), that
    # walk can find the real, non-test .env and pick up a real
    # RESUME_ARCHIVE_DIR, silently writing real symlinks into a real folder
    # outside this test's cwd/tmp_path sandbox. Forcing it blank here (not
    # popped — python-dotenv's load_dotenv(override=False) only skips a key
    # ALREADY present in os.environ, and a blank value still counts as
    # present) makes every subprocess spawned via run() archiving-off by
    # construction, regardless of what any real .env on this machine says.
    full_env.setdefault("RESUME_ARCHIVE_DIR", "")
    return subprocess.run(
        [sys.executable, str(SLAP_PY), *args],
        input=input,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=full_env,
        timeout=10,  # defensive: 'runner' must never reach a real sleep in these tests
    )


def test_help_lists_all_subcommands():
    result = run("--help")
    assert result.returncode == 0
    for cmd in ALL_COMMANDS:
        assert cmd in result.stdout


def test_send_without_campaign_is_argparse_usage_error():
    result = run("send")
    assert result.returncode == 2
    assert "not yet implemented" not in result.stderr


def test_send_fails_loud_for_unknown_campaign(tmp_path):
    # Fails during load_campaign, before ever reading stdin — safe to run
    # via subprocess without needing to script any interactive input.
    (tmp_path / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )
    result = run("send", "somecampaign", cwd=tmp_path)
    assert result.returncode != 0
    assert "not found" in result.stderr
    assert "Traceback" not in result.stderr


def test_send_fails_loud_when_doctor_preflight_fails(tmp_path):
    # A structurally valid campaign (load_campaign succeeds) but doctor's
    # preflight fails (missing attachment) — must fail loud BEFORE ever
    # reading stdin for the interactive drop-paste loop.
    (tmp_path / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )
    campaign = tmp_path / "campaigns" / "coldpost"
    campaign.mkdir(parents=True)
    (campaign / "campaign.yaml").write_text(
        "persona: recruiter\n"
        "latex: { enabled: false, attachment_name: r.pdf }\n"
        "attachment_file: resume.pdf\n"
        "fields:\n  - { key: email, label: Email }\n"
    )
    (campaign / "initial.txt").write_text("Subject: Hi\n\nBody\n")
    for i in (1, 2, 3):
        (campaign / f"stage{i}.txt").write_text(f"stage {i}\n")
    # resume.pdf deliberately never created.

    env_with_key = {**os.environ, "GMASS_API_KEY": "fake-key"}
    result = run("send", "coldpost", cwd=tmp_path, env=env_with_key)
    assert result.returncode != 0
    assert "doctor preflight failed" in result.stderr
    assert "attachment_file" in result.stderr
    assert "Traceback" not in result.stderr


def test_send_never_leaks_ansi_into_the_staged_message_even_with_color_forced(tmp_path):
    # HARD REQUIREMENT: colorization (slap/display.py) is display-only. This
    # deliberately runs with color FORCED ON (the opposite of every other
    # test in this file, which forces color OFF for deterministic
    # assertions) to maximize the chance of exposing a leak if the preview
    # panel / hard-warn text ever accidentally shared a variable with the
    # actual subject/body — then proves the staged manifest (the exact
    # artifact later fed to gmass.create_draft's subject/message) contains
    # zero ANSI escape bytes.
    (tmp_path / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )
    (tmp_path / "consumer_domains.txt").write_text(
        (Path(__file__).resolve().parent.parent / "consumer_domains.txt").read_text()
    )
    campaign = tmp_path / "campaigns" / "coldpost"
    campaign.mkdir(parents=True)
    (campaign / "campaign.yaml").write_text(
        "persona: recruiter\n"
        "latex: { enabled: false, attachment_name: r.pdf }\n"
        "attachment_file: resume.pdf\n"
        "fields:\n  - { key: email, label: Email }\n  - { key: company, label: Company }\n"
    )
    (campaign / "resume.pdf").write_bytes(b"%PDF-fake")
    (campaign / "initial.txt").write_text("Subject: Hi from {{company}}\n\nHello {{company}} team\n")
    for i in (1, 2, 3):
        (campaign / f"stage{i}.txt").write_text(f"stage {i}\n")

    recipient = "jane@acme.com"
    # Pre-seed a prior send so the dedup HARD WARN fires (exercising the
    # red-styled warning text, a separate display call from the preview
    # panel — both must independently never touch subject/body).
    from slap.tracking import append_event, connect
    connect(tmp_path / "slap.db").close()
    conn = connect(tmp_path / "slap.db")
    append_event(conn, type="queued", recipient=recipient, campaign="prior-campaign", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient=recipient, campaign="prior-campaign", stage=0,
                 gmass_campaign_id="1")
    conn.close()

    drop = f"Email: {recipient}\nCompany: Acme\n"
    scripted_stdin = f"{drop}\nEOF\ny\ny\nn\n"  # drop, proceed-anyway, stage-this-send, no-more

    env_with_key = {**os.environ, "GMASS_API_KEY": "fake-key", "FORCE_COLOR": "1"}
    env_with_key.pop("NO_COLOR", None)
    env_with_key.setdefault("RESUME_ARCHIVE_DIR", "")  # hermetic — see run()'s own comment above
    result = subprocess.run(
        [sys.executable, str(SLAP_PY), "send", "coldpost"],
        input=scripted_stdin, capture_output=True, text=True, cwd=tmp_path, env=env_with_key, timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "\x1b" in result.stdout  # sanity check: color really was active for this run
    assert "Staged" in result.stdout

    manifest_path = tmp_path / "workdir" / "coldpost" / recipient / "staged.json"
    manifest = json.loads(manifest_path.read_text())
    assert "\x1b" not in manifest["subject"]
    assert "\x1b" not in manifest["body"]
    assert all("\x1b" not in body for body in manifest["stage_bodies"])
    assert manifest["subject"] == "Hi from Acme"
    assert manifest["body"] == "Hello Acme team"


def _setup_three_field_campaign(tmp_path):
    (tmp_path / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )
    (tmp_path / "consumer_domains.txt").write_text(
        (Path(__file__).resolve().parent.parent / "consumer_domains.txt").read_text()
    )
    campaign = tmp_path / "campaigns" / "coldpost"
    campaign.mkdir(parents=True)
    (campaign / "campaign.yaml").write_text(
        "persona: recruiter\n"
        "latex: { enabled: false, attachment_name: r.pdf }\n"
        "attachment_file: resume.pdf\n"
        "fields:\n"
        "  - { key: email, label: Email }\n"
        "  - { key: company, label: Company }\n"
        "  - { key: req_id, label: Req ID, optional: true }\n"
    )
    (campaign / "resume.pdf").write_bytes(b"%PDF-fake")
    (campaign / "initial.txt").write_text("Subject: Hi from {{company}}\n\nHello {{company}} team\n")
    for i in (1, 2, 3):
        (campaign / f"stage{i}.txt").write_text(f"stage {i}\n")
    return campaign


def test_send_warns_about_empty_declared_fields_but_does_not_block(tmp_path):
    # Pre-preview validation warning: display-only, never gates the send.
    _setup_three_field_campaign(tmp_path)
    recipient = "jane@acme.com"
    drop = f"Email: {recipient}\nCompany: Acme\n"  # Req ID deliberately omitted -- stays empty
    scripted_stdin = f"{drop}\nEOF\ny\nn\n"  # drop, stage-this-send, no-more

    env_with_key = {**os.environ, "GMASS_API_KEY": "fake-key"}
    result = run("send", "coldpost", cwd=tmp_path, env=env_with_key, input=scripted_stdin)

    assert result.returncode == 0, result.stderr
    assert "empty fields: req_id" in result.stdout
    assert "Staged" in result.stdout  # non-blocking: the send still proceeds despite the warning


def test_send_no_empty_fields_warning_when_everything_is_filled(tmp_path):
    _setup_three_field_campaign(tmp_path)
    recipient = "jane@acme.com"
    drop = f"Email: {recipient}\nCompany: Acme\nReq ID: 6900\n"
    scripted_stdin = f"{drop}\nEOF\ny\nn\n"

    env_with_key = {**os.environ, "GMASS_API_KEY": "fake-key"}
    result = run("send", "coldpost", cwd=tmp_path, env=env_with_key, input=scripted_stdin)

    assert result.returncode == 0, result.stderr
    assert "empty fields" not in result.stdout
    assert "Staged" in result.stdout


def test_doctor_fails_loud_without_config(tmp_path):
    result = run("doctor", cwd=tmp_path)
    assert result.returncode != 0
    assert "config.yaml: FAIL" in result.stdout
    assert "Traceback" not in (result.stdout + result.stderr)


def test_doctor_reports_missing_api_key(tmp_path):
    (tmp_path / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )
    env_without_key = {**os.environ, "GMASS_API_KEY": ""}
    result = run("doctor", cwd=tmp_path, env=env_without_key)
    assert result.returncode != 0
    assert "GMASS_API_KEY: FAIL" in result.stdout


def test_doctor_seeds_missing_consumer_domains_and_passes(tmp_path):
    (tmp_path / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )
    env_with_key = {**os.environ, "GMASS_API_KEY": "fake-key"}
    result = run("doctor", cwd=tmp_path, env=env_with_key)
    assert result.returncode == 0
    assert "All checks passed." in result.stdout
    seeded = tmp_path / "consumer_domains.txt"
    assert seeded.exists()
    assert "gmail.com" in seeded.read_text()


def test_doctor_reports_campaign_attachment_issues(tmp_path):
    (tmp_path / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )
    (tmp_path / "consumer_domains.txt").write_text(
        (Path(__file__).resolve().parent.parent / "consumer_domains.txt").read_text()
    )
    broken = tmp_path / "campaigns" / "broken-campaign"
    broken.mkdir(parents=True)
    (broken / "campaign.yaml").write_text(
        "persona: recruiter\n"
        "latex: { enabled: false, attachment_name: r.pdf }\n"
        "attachment_file: resume.pdf\n"
        "fields:\n  - { key: email, label: Email }\n"
    )
    (broken / "initial.txt").write_text("Subject: Hi\n\nBody\n")
    for i in (1, 2, 3):
        (broken / f"stage{i}.txt").write_text(f"stage {i}\n")
    # resume.pdf deliberately never created.

    env_with_key = {**os.environ, "GMASS_API_KEY": "fake-key"}
    result = run("doctor", cwd=tmp_path, env=env_with_key)
    assert result.returncode != 0
    assert "campaign 'broken-campaign': FAIL" in result.stdout
    assert "attachment_file: FAIL" in result.stdout


def test_runner_fails_loud_without_config(tmp_path):
    # Must fail during load_global_config, before ever reaching
    # wait_for_fire_window (which can sleep for minutes) — critical for this
    # test to actually be fast and not hang the suite.
    result = run("runner", cwd=tmp_path)
    assert result.returncode != 0
    assert "config.yaml" in result.stderr
    assert not (tmp_path / "slap.db").exists()


def test_runner_skips_draining_on_an_inactive_day(tmp_path):
    # The active_days guard must fire BEFORE tracking.connect()/
    # wait_for_fire_window — critical for this test to stay fast (no real
    # sleep) and to prove no DB/queue side effect happens on a skipped day.
    all_days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today_abbr = all_days[date.today().weekday()]
    other_days = [d for d in all_days if d != today_abbr]
    config_text = (
        (Path(__file__).resolve().parent.parent / "config.yaml.example").read_text()
        .replace("<Owner Name>", "Test Owner")
        .replace("active_days: [mon, tue, wed, thu, fri]", f"active_days: [{', '.join(other_days)}]")
    )
    (tmp_path / "config.yaml").write_text(config_text)
    result = run("runner", cwd=tmp_path)
    assert result.returncode == 0
    assert "not an active day" in (result.stdout + result.stderr).lower()
    assert not (tmp_path / "slap.db").exists()


def test_plist_fails_loud_without_config(tmp_path):
    result = run("plist", cwd=tmp_path)
    assert result.returncode != 0
    assert "config.yaml" in result.stderr


def test_plist_prints_a_valid_plist_for_this_repo(tmp_path):
    (tmp_path / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )
    result = run("plist", cwd=tmp_path)
    assert result.returncode == 0
    assert "com.slap.runner" in result.stdout
    assert "<key>StartCalendarInterval</key>" in result.stdout
    assert str(tmp_path.resolve()) in result.stdout  # WorkingDirectory/paths resolved to THIS config's repo root
    assert sys.executable in result.stdout  # the interpreter that ran slap.py, not a hardcoded path

    import plistlib
    parsed = plistlib.loads(result.stdout.encode())
    assert isinstance(parsed["StartCalendarInterval"], list)
    assert len(parsed["StartCalendarInterval"]) == 5  # config.yaml.example's active_days: mon-fri


def test_dashboard_fails_loud_without_config(tmp_path):
    # Must fail during load_global_config, before ever reaching
    # dashboard.create_app()/app.run() (which would block the test forever
    # serving on 127.0.0.1:5000).
    result = run("dashboard", cwd=tmp_path)
    assert result.returncode != 0
    assert "config.yaml" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "slap.db").exists()


def test_dashboard_fails_loud_without_api_key(tmp_path):
    # config.yaml + consumer_domains.txt both present so the ONLY missing
    # thing is the API key — must fail loud before ever reaching
    # tracking.connect()/create_app()/app.run().
    (tmp_path / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )
    (tmp_path / "consumer_domains.txt").write_text(
        (Path(__file__).resolve().parent.parent / "consumer_domains.txt").read_text()
    )
    # Explicitly set to "" rather than deleted: slap.py's load_dotenv() finds
    # the real repo-root .env (it searches from slap.py's own file location,
    # not the subprocess cwd) and, with its default override=False, would
    # silently repopulate a *deleted* key from that real file — but never
    # overwrites a key that's already present, even as an empty string.
    env_without_key = {**os.environ, "GMASS_API_KEY": ""}
    result = run("dashboard", cwd=tmp_path, env=env_without_key)
    assert result.returncode != 0
    assert "GMASS_API_KEY" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "slap.db").exists()


def test_list_fails_loud_without_config(tmp_path):
    # config.yaml is owner-filled-in, real local data (gitignored, not shipped) —
    # isolate cwd so this doesn't depend on whether a real one happens to exist
    # at the actual repo root right now.
    result = run("list", cwd=tmp_path)
    assert result.returncode != 0
    assert "config.yaml" in result.stderr
    assert "Traceback" not in result.stderr


def test_list_reports_broken_campaign_inline_and_continues(tmp_path):
    # Owner-confirmed behavior: one malformed campaign.yaml prints an inline
    # ERROR line and 'list' still reports the rest, exiting 0 (it did its job
    # of reporting) rather than aborting the whole command.
    (tmp_path / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )

    good = tmp_path / "campaigns" / "good-campaign"
    good.mkdir(parents=True)
    (good / "campaign.yaml").write_text(
        "persona: recruiter\n"
        "latex: { enabled: false, attachment_name: r.pdf }\n"
        "attachment_file: resume.pdf\n"
        "fields:\n  - { key: email, label: Email }\n"
    )
    (good / "initial.txt").write_text("Subject: Hi\n\nBody\n")
    for i in (1, 2, 3):
        (good / f"stage{i}.txt").write_text(f"stage {i}\n")

    broken = tmp_path / "campaigns" / "broken-campaign"
    broken.mkdir(parents=True)
    (broken / "campaign.yaml").write_text(
        "persona: recruiter\n"
        "latex: { enabled: false, attachment_name: r.pdf }\n"
        "attachment_file: resume.pdf\n"
        "fields:\n  - { key: email, label: Email }\n"
    )
    (broken / "initial.txt").write_text("No subject line here\n")
    for i in (1, 2, 3):
        (broken / f"stage{i}.txt").write_text(f"stage {i}\n")

    result = run("list", cwd=tmp_path)
    assert result.returncode == 0
    assert "Traceback" not in result.stderr
    assert "good-campaign  persona=recruiter  latex off" in result.stdout
    assert "broken-campaign: ERROR" in result.stdout


def test_rebuild_works_on_a_fresh_db(tmp_path):
    # Run in an isolated cwd — 'rebuild' creates slap.db in the CWD, which
    # must never land in the actual repo root as a side effect of testing.
    result = run("rebuild", cwd=tmp_path)
    assert result.returncode == 0
    assert "0 recipients" in result.stdout
    assert "0 events" in result.stdout
    assert (tmp_path / "slap.db").exists()


def test_domains_works_on_a_fresh_db(tmp_path):
    # Isolated cwd for the same reason as rebuild above — 'domains' also
    # creates slap.db in the CWD via tracking.connect().
    (tmp_path / "consumer_domains.txt").write_text(
        (Path(__file__).resolve().parent.parent / "consumer_domains.txt").read_text()
    )
    result = run("domains", cwd=tmp_path)
    assert result.returncode == 0
    assert "No contacts tracked yet." in result.stdout
    assert (tmp_path / "slap.db").exists()


def test_domains_fails_loud_without_consumer_domains_file(tmp_path):
    result = run("domains", cwd=tmp_path)
    assert result.returncode != 0
    assert "consumer_domains.txt" in result.stderr
    assert not (tmp_path / "slap.db").exists()  # fails before ever touching the DB


def test_init_end_to_end_via_cli(tmp_path):
    # Real subprocess invocation of `python slap.py init`, scripted stdin for
    # every prompt in order: sender (email, name), gmass key, schedule (start,
    # end, cap, days), then decline the optional campaign scaffold.
    (tmp_path / "config.yaml.example").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example").read_text()
    )
    (tmp_path / ".env.example").write_text(
        (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    )
    answers = "\n".join([
        "me@gmail.com", "Jane Doe",
        "fake-gmass-key-value",
        "09:00", "09:15", "50", "mon,tue,wed,thu,fri",
        "n",
    ]) + "\n"
    result = run("init", cwd=tmp_path, input=answers)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "init complete" in result.stdout
    assert "fake-gmass-key-value" not in result.stdout  # API key never echoed in full

    assert (tmp_path / "config.yaml").exists()
    assert (tmp_path / ".env").exists()
    assert (tmp_path / "slap.db").exists()

    from slap.config import load_global_config
    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.from_email == "me@gmail.com"
    assert gc.schedule.daily_cap == 50


def test_init_is_idempotent_on_second_cli_run(tmp_path):
    (tmp_path / "config.yaml.example").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example").read_text()
    )
    (tmp_path / ".env.example").write_text(
        (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    )
    first_answers = "\n".join([
        "me@gmail.com", "Jane Doe", "fake-gmass-key-value",
        "09:00", "09:15", "50", "mon,tue,wed,thu,fri", "n",
    ]) + "\n"
    result1 = run("init", cwd=tmp_path, input=first_answers)
    assert result1.returncode == 0, result1.stdout + result1.stderr

    # Second run: decline sender/key overwrite, re-answer schedule, decline campaign again.
    second_answers = "\n".join(["n", "n", "09:00", "09:15", "50", "mon,tue,wed,thu,fri", "n"]) + "\n"
    result2 = run("init", cwd=tmp_path, input=second_answers)
    assert result2.returncode == 0, result2.stdout + result2.stderr
    assert "Traceback" not in result2.stderr

    from slap.config import load_global_config
    gc = load_global_config(tmp_path / "config.yaml")
    assert gc.from_email == "me@gmail.com"  # untouched by the declined second run


# --- résumé reuse on the domain soft-warn (post-launch, latex-off campaigns only) ---

def _setup_reuse_campaign(tmp_path):
    (tmp_path / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )
    (tmp_path / "consumer_domains.txt").write_text(
        (Path(__file__).resolve().parent.parent / "consumer_domains.txt").read_text()
    )
    campaign = tmp_path / "campaigns" / "coldpost"
    campaign.mkdir(parents=True)
    (campaign / "campaign.yaml").write_text(
        "persona: recruiter\n"
        "latex: { enabled: false, attachment_name: AvinashArutla.pdf }\n"
        "attachment_file: resume.pdf\n"
        "fields:\n"
        "  - { key: email,       label: Email }\n"
        "  - { key: company,     label: Company }\n"
        "  - { key: role_catted, label: Role }\n"
    )
    (campaign / "resume.pdf").write_bytes(b"%PDF-default-resume")
    (campaign / "initial.txt").write_text("Subject: Hi {{company}}\n\nHello {{company}} re {{role_catted}}\n")
    for i in (1, 2, 3):
        (campaign / f"stage{i}.txt").write_text(f"stage {i}\n")
    return campaign


def _seed_prior_contact_same_domain(tmp_path, *, recipient="other@acme.com", campaign="coldpost"):
    from slap.tracking import append_event, connect
    conn = connect(tmp_path / "slap.db")
    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0,
                 gmass_campaign_id="1")
    conn.close()


def _write_valid_pdf(path):
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as f:
        writer.write(f)


def test_send_offers_resume_reuse_on_soft_warn_and_accepted(tmp_path):
    _setup_reuse_campaign(tmp_path)
    _seed_prior_contact_same_domain(tmp_path)

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    original = tmp_path / "original.pdf"
    _write_valid_pdf(original)
    (archive_dir / "acme-founding-engineer-2026-06-01.pdf").symlink_to(original)

    recipient = "jane@acme.com"
    drop = f"Email: {recipient}\nCompany: Acme\nRole: Staff Engineer\n"
    scripted_stdin = f"{drop}\nEOF\ny\n1\ny\nn\n"  # drop, proceed-anyway, reuse #1, stage, no-more

    env = {**os.environ, "GMASS_API_KEY": "fake-key", "RESUME_ARCHIVE_DIR": str(archive_dir)}
    result = run("send", "coldpost", cwd=tmp_path, env=env, input=scripted_stdin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused from acme-founding-engineer-2026-06-01.pdf" in result.stdout
    assert "Staged" in result.stdout

    staged = tmp_path / "workdir" / "coldpost" / recipient / "AvinashArutla.pdf"
    assert staged.exists()
    assert staged.read_bytes() == original.read_bytes()
    assert original.exists()  # original archive target untouched

    today = date.today().isoformat()
    new_entry = archive_dir / f"acme-staff-engineer-{today}.pdf"
    assert new_entry.is_symlink()
    assert new_entry.resolve() == staged.resolve()  # a fresh entry for THIS send, not the reused one


def test_send_offers_resume_reuse_multiple_matches_selects_correct_one(tmp_path):
    _setup_reuse_campaign(tmp_path)
    _seed_prior_contact_same_domain(tmp_path)

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _write_valid_pdf(first)
    _write_valid_pdf(second)
    # alphabetical: "acme-founding..." sorts before "acme-recruiter..."
    (archive_dir / "acme-founding-engineer-2026-06-01.pdf").symlink_to(first)
    (archive_dir / "acme-recruiter-2026-05-01.pdf").symlink_to(second)

    recipient = "jane@acme.com"
    drop = f"Email: {recipient}\nCompany: Acme\nRole: Staff Engineer\n"
    scripted_stdin = f"{drop}\nEOF\ny\n2\ny\nn\n"  # pick the 2nd listed match

    env = {**os.environ, "GMASS_API_KEY": "fake-key", "RESUME_ARCHIVE_DIR": str(archive_dir)}
    result = run("send", "coldpost", cwd=tmp_path, env=env, input=scripted_stdin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 previous résumé(s) found" in result.stdout
    assert "reused from acme-recruiter-2026-05-01.pdf" in result.stdout

    staged = tmp_path / "workdir" / "coldpost" / recipient / "AvinashArutla.pdf"
    assert staged.read_bytes() == second.read_bytes()


def test_send_soft_warn_no_archive_matches_falls_through_no_prompt(tmp_path):
    _setup_reuse_campaign(tmp_path)
    _seed_prior_contact_same_domain(tmp_path)

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    other = tmp_path / "other.pdf"
    _write_valid_pdf(other)
    (archive_dir / "othercorp-swe-2026-06-01.pdf").symlink_to(other)  # no match for "Acme"

    recipient = "jane@acme.com"
    drop = f"Email: {recipient}\nCompany: Acme\nRole: Staff Engineer\n"
    scripted_stdin = f"{drop}\nEOF\ny\ny\nn\n"  # drop, proceed-anyway, stage, no-more (NO reuse prompt)

    env = {**os.environ, "GMASS_API_KEY": "fake-key", "RESUME_ARCHIVE_DIR": str(archive_dir)}
    result = run("send", "coldpost", cwd=tmp_path, env=env, input=scripted_stdin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "résumé(s) found" not in result.stdout
    assert "Attachment: AvinashArutla.pdf" in result.stdout
    assert "Staged" in result.stdout


def test_send_soft_warn_archive_dir_unset_falls_through_no_prompt_no_error(tmp_path):
    _setup_reuse_campaign(tmp_path)
    _seed_prior_contact_same_domain(tmp_path)

    recipient = "jane@acme.com"
    drop = f"Email: {recipient}\nCompany: Acme\nRole: Staff Engineer\n"
    scripted_stdin = f"{drop}\nEOF\ny\ny\nn\n"

    env = {**os.environ, "GMASS_API_KEY": "fake-key"}  # RESUME_ARCHIVE_DIR left unset
    result = run("send", "coldpost", cwd=tmp_path, env=env, input=scripted_stdin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "résumé(s) found" not in result.stdout
    assert "Staged" in result.stdout


def test_send_declining_resume_reuse_uses_default(tmp_path):
    campaign = _setup_reuse_campaign(tmp_path)
    _seed_prior_contact_same_domain(tmp_path)

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    original = tmp_path / "original.pdf"
    _write_valid_pdf(original)
    (archive_dir / "acme-founding-engineer-2026-06-01.pdf").symlink_to(original)

    recipient = "jane@acme.com"
    drop = f"Email: {recipient}\nCompany: Acme\nRole: Staff Engineer\n"
    scripted_stdin = f"{drop}\nEOF\ny\n0\ny\nn\n"  # drop, proceed-anyway, DECLINE reuse, stage, no-more

    env = {**os.environ, "GMASS_API_KEY": "fake-key", "RESUME_ARCHIVE_DIR": str(archive_dir)}
    result = run("send", "coldpost", cwd=tmp_path, env=env, input=scripted_stdin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Attachment: AvinashArutla.pdf" in result.stdout
    assert "reused from" not in result.stdout
    assert "Staged" in result.stdout

    # Static-campaign default path: no per-recipient copy, shared source recorded instead.
    workdir = tmp_path / "workdir" / "coldpost" / recipient
    assert not (workdir / "AvinashArutla.pdf").exists()
    manifest = json.loads((workdir / "staged.json").read_text())
    assert manifest["attachment_source"] == str((campaign / "resume.pdf").resolve())


def test_send_reuse_of_broken_archive_entry_fails_loud_for_that_recipient_only(tmp_path):
    _setup_reuse_campaign(tmp_path)
    _seed_prior_contact_same_domain(tmp_path)

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    gone = tmp_path / "gone.pdf"
    _write_valid_pdf(gone)
    (archive_dir / "acme-founding-engineer-2026-06-01.pdf").symlink_to(gone)
    gone.unlink()  # dangling target by the time it's picked

    recipient1, recipient2 = "jane@acme.com", "jill@acme.com"
    scripted_stdin = "\n".join([
        f"Email: {recipient1}", "Company: Acme", "Role: Staff Engineer", "",
        "EOF",
        "y",   # proceed anyway (soft-warn, recipient1)
        "1",   # reuse the (broken) match -> fails loud, this recipient is skipped
        "y",   # cmd_send's "Add another?"
        f"Email: {recipient2}", "Company: Acme", "Role: Staff Engineer", "",
        "EOF",
        "y",   # proceed anyway (soft-warn, recipient2 -- jane was never staged, so still just 'other@acme.com')
        "0",   # decline reuse this time
        "y",   # Stage this send?
        "n",   # Add another? -> end
        "",
    ])

    env = {**os.environ, "GMASS_API_KEY": "fake-key", "RESUME_ARCHIVE_DIR": str(archive_dir)}
    result = run("send", "coldpost", cwd=tmp_path, env=env, input=scripted_stdin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Could not reuse" in result.stdout

    assert not (tmp_path / "workdir" / "coldpost" / recipient1 / "staged.json").exists()
    assert (tmp_path / "workdir" / "coldpost" / recipient2 / "staged.json").exists()


# --- email signature moved into config.yaml (post-launch feature) ----------

SIGNATURE_TEXT = "Avinash Arutla\nhttps://www.linkedin.com/in/avinasharutla/"


def _setup_signature_campaign(tmp_path, *, config_signature=SIGNATURE_TEXT, signature_key_present=True):
    config_text = (
        (Path(__file__).resolve().parent.parent / "config.yaml.example")
        .read_text()
        .replace("<Owner Name>", "Test Owner")
    )
    if signature_key_present:
        # Replace the whole example |- block (3 indented lines) with a
        # single-line signature so the test's expected string is exact and
        # simple to assert on, regardless of the example's own placeholder text.
        lines = config_text.splitlines()
        out, skip = [], False
        for line in lines:
            if line.strip().startswith("signature:"):
                out.append(f'signature: "{config_signature}"'.replace("\n", "\\n"))
                skip = True
                continue
            if skip and line.startswith("  "):
                continue  # part of the old |- block, drop it
            skip = False
            out.append(line)
        config_text = "\n".join(out) + "\n"
    else:
        # Strip the signature key (and its indented block) entirely.
        lines = config_text.splitlines()
        out, skip = [], False
        for line in lines:
            if line.strip().startswith("signature:"):
                skip = True
                continue
            if skip and (line.startswith("  ") or not line.strip()):
                continue
            skip = False
            out.append(line)
        config_text = "\n".join(out) + "\n"
    (tmp_path / "config.yaml").write_text(config_text)
    (tmp_path / "consumer_domains.txt").write_text(
        (Path(__file__).resolve().parent.parent / "consumer_domains.txt").read_text()
    )

    campaign = tmp_path / "campaigns" / "coldpost"
    campaign.mkdir(parents=True)
    (campaign / "campaign.yaml").write_text(
        "persona: recruiter\n"
        "latex: { enabled: false, attachment_name: r.pdf }\n"
        "attachment_file: resume.pdf\n"
        "fields:\n"
        "  - { key: email,   label: Email }\n"
        "  - { key: company, label: Company }\n"
        "  - { key: byebye,  label: Signoff }\n"
    )
    (campaign / "resume.pdf").write_bytes(b"%PDF-fake")
    # initial.txt: the "replace an existing hardcoded sign-off" path.
    (campaign / "initial.txt").write_text(
        "Subject: Hi {{company}}\n\nBody text about {{company}}.\n\n{{byebye}},\n{{signature}}\n"
    )
    # stage1.txt: the "add to a template with NO prior sign-off at all" path.
    (campaign / "stage1.txt").write_text("Just checking in, no news yet.\n\n{{signature}}\n")
    (campaign / "stage2.txt").write_text("Second follow-up.\n\n{{signature}}\n")
    (campaign / "stage3.txt").write_text("Last one from me.\n\n{{signature}}\n")
    return campaign


def test_send_substitutes_configured_signature_in_initial_and_stage_files(tmp_path):
    _setup_signature_campaign(tmp_path)
    recipient = "jane@acme.com"
    drop = f"Email: {recipient}\nCompany: Acme\nSignoff: Best\n"
    scripted_stdin = f"{drop}\nEOF\ny\nn\n"  # drop, stage, no-more

    env = {**os.environ, "GMASS_API_KEY": "fake-key"}
    result = run("send", "coldpost", cwd=tmp_path, env=env, input=scripted_stdin)

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((tmp_path / "workdir" / "coldpost" / recipient / "staged.json").read_text())
    assert manifest["body"] == f"Body text about Acme.\n\nBest,\n{SIGNATURE_TEXT}"
    # stage1/2/3 had NO sign-off at all before this feature -- confirms the
    # ADDITION path, not just the replacement path.
    assert manifest["stage_bodies"][0] == f"Just checking in, no news yet.\n\n{SIGNATURE_TEXT}"
    assert manifest["stage_bodies"][1] == f"Second follow-up.\n\n{SIGNATURE_TEXT}"
    assert manifest["stage_bodies"][2] == f"Last one from me.\n\n{SIGNATURE_TEXT}"


def test_send_fails_loud_when_signature_missing_before_reading_stdin(tmp_path):
    _setup_signature_campaign(tmp_path, signature_key_present=False)
    env = {**os.environ, "GMASS_API_KEY": "fake-key"}
    # No `input=` at all -- if this didn't fail before ever reading stdin,
    # the subprocess would hang waiting for a drop paste and the test would
    # time out instead of returning promptly.
    result = run("send", "coldpost", cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "signature" in result.stderr
    assert "Traceback" not in result.stderr


def test_send_empty_signature_renders_blank_with_no_error(tmp_path):
    _setup_signature_campaign(tmp_path, config_signature="")
    recipient = "jane@acme.com"
    drop = f"Email: {recipient}\nCompany: Acme\nSignoff: Best\n"
    scripted_stdin = f"{drop}\nEOF\ny\nn\n"

    env = {**os.environ, "GMASS_API_KEY": "fake-key"}
    result = run("send", "coldpost", cwd=tmp_path, env=env, input=scripted_stdin)

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((tmp_path / "workdir" / "coldpost" / recipient / "staged.json").read_text())
    assert manifest["body"] == "Body text about Acme.\n\nBest,\n"
    assert manifest["stage_bodies"][0] == "Just checking in, no news yet.\n\n"
