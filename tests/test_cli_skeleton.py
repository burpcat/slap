"""Smoke tests for the slap.py CLI skeleton (Build Order step 2).

Scoped strictly to what step 2 builds: argparse dispatch and fail-loud stubs.
Does not test config/templates/tracking/etc. — those land in later steps.
"""
import subprocess
import sys
from pathlib import Path

SLAP_PY = Path(__file__).resolve().parent.parent / "slap.py"
ALL_COMMANDS = ["list", "send", "dashboard", "doctor", "domains", "rebuild"]
# 'list' (step 3) and 'rebuild' (step 5) are wired to real implementations
# and are no longer stubs — see their dedicated tests below.
NO_ARG_COMMANDS = ["dashboard", "doctor", "domains"]


def run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(SLAP_PY), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_help_lists_all_subcommands():
    result = run("--help")
    assert result.returncode == 0
    for cmd in ALL_COMMANDS:
        assert cmd in result.stdout


def test_no_arg_commands_fail_loud_not_implemented():
    for cmd in NO_ARG_COMMANDS:
        result = run(cmd)
        assert result.returncode != 0, f"{cmd} should exit non-zero"
        assert "not yet implemented" in result.stderr, f"{cmd} stderr: {result.stderr!r}"


def test_send_without_campaign_is_argparse_usage_error():
    result = run("send")
    assert result.returncode == 2
    assert "not yet implemented" not in result.stderr


def test_send_with_campaign_hits_stub():
    result = run("send", "somecampaign")
    assert result.returncode != 0
    assert "not yet implemented" in result.stderr


def test_list_fails_loud_without_config():
    # No config.yaml is committed at repo root (owner-supplied, not shipped as
    # real data) — 'list' must fail loud with a clear message, not a traceback.
    result = run("list")
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
