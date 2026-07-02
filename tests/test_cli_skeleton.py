"""Smoke tests for the slap.py CLI skeleton (Build Order step 2).

Scoped strictly to what step 2 builds: argparse dispatch and fail-loud stubs.
Does not test config/templates/tracking/etc. — those land in later steps.
"""
import subprocess
import sys
from pathlib import Path

SLAP_PY = Path(__file__).resolve().parent.parent / "slap.py"
NO_ARG_COMMANDS = ["list", "dashboard", "doctor", "domains", "rebuild"]


def run(*args):
    return subprocess.run(
        [sys.executable, str(SLAP_PY), *args],
        capture_output=True,
        text=True,
    )


def test_help_lists_all_subcommands():
    result = run("--help")
    assert result.returncode == 0
    for cmd in [*NO_ARG_COMMANDS, "send"]:
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
