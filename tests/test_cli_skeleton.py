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
from pathlib import Path

SLAP_PY = Path(__file__).resolve().parent.parent / "slap.py"
ALL_COMMANDS = ["list", "send", "dashboard", "doctor", "domains", "rebuild", "runner"]


def run(*args, cwd=None, env=None):
    # Deterministic regardless of the ambient shell's color-forcing env vars
    # (e.g. FORCE_COLOR, set by some terminal/CI wrappers) — tests assert on
    # exact plain-text stdout/stderr content, and rich's colorization (see
    # slap/display.py) must never leak ANSI codes into that output here,
    # same as it must never leak into an actual sent email body.
    full_env = dict(env) if env is not None else dict(os.environ)
    full_env.pop("FORCE_COLOR", None)
    full_env.pop("CLICOLOR_FORCE", None)
    full_env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, str(SLAP_PY), *args],
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
