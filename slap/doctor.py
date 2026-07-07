"""Preflight checks (Build Order step 12). See SLAP_BUILD_PROMPT.md §11.

"Fail loud, check-don't-install" (CLAUDE.md): every check here verifies
existing system/config state — none of them install or modify a system
dependency. The ONE deliberate exception, called out explicitly in the
brief, is `consumer_domains.txt`: unlike xelatex/code (real system installs,
left to the human), it's a small data file this project ships a canonical
default for, so a missing one is auto-seeded rather than failed loud (§11:
"seed default if missing").

Two check batteries:
- `run_global_checks()` — campaign-independent (API key, sender fields, DB
  reachable, consumer_domains.txt). Used by standalone `doctor`, `send`'s
  auto-preflight, and `runner.drain()`'s retry-then-run_failed path.
- `run_campaign_checks(campaign)` — the external-state half of "is this
  campaign usable" that `slap.config.load_campaign()` deliberately does NOT
  cover (its own docstring defers this to doctor): attachment_file exists,
  or xelatex/code on PATH when latex is enabled. Used by standalone
  `doctor` (looped over every discovered campaign) and by `send`'s
  auto-preflight for the one target campaign. Deliberately NOT run at drain
  time — by the time a recipient is queued, its campaign already passed
  this check at `send` time and its attachment bytes are already baked into
  that recipient's staged.json; nothing about draining re-touches the live
  campaign.yaml or attachment files, so re-checking them there would be
  meaningless (and ambiguous — a drain batch can span multiple campaigns).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from slap import tracking
from slap.config import CampaignConfig, GlobalConfig

DEFAULT_CONSUMER_DOMAINS = [
    "gmail.com", "outlook.com", "yahoo.com", "icloud.com", "proton.me",
    "protonmail.com", "hotmail.com", "aol.com", "gmx.com", "live.com", "msn.com",
]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def check_api_key(global_config: GlobalConfig) -> CheckResult:
    if not os.environ.get(global_config.api_key_env, "").strip():
        return CheckResult(global_config.api_key_env, False, "not set")
    return CheckResult(global_config.api_key_env, True)


def check_sender_fields(global_config: GlobalConfig) -> CheckResult:
    if not global_config.from_email.strip() or not global_config.from_name.strip():
        return CheckResult("sender fields", False,
                            "sender.from_email and sender.from_name must both be set in config.yaml")
    return CheckResult("sender fields", True)


def check_db(conn=None) -> CheckResult:
    """Reuses an already-open connection when one's given (e.g. from
    runner.drain(), which is mid-run on its own conn) instead of opening a
    second one at the default cwd-relative path — avoids ever touching the
    wrong slap.db just to answer "is the DB reachable"."""
    try:
        if conn is not None:
            conn.execute("SELECT 1")
        else:
            probe = tracking.connect()
            probe.execute("SELECT 1")
            probe.close()
        return CheckResult("SQLite DB", True)
    except Exception as e:
        return CheckResult("SQLite DB", False, str(e))


def check_consumer_domains(global_config: GlobalConfig) -> CheckResult:
    path = Path(global_config.consumer_domains_file)
    if path.exists():
        return CheckResult("consumer_domains.txt", True)
    path.write_text("\n".join(DEFAULT_CONSUMER_DOMAINS) + "\n", encoding="utf-8")
    return CheckResult("consumer_domains.txt", True, "was missing — seeded the default list")


def run_global_checks(global_config: GlobalConfig, conn=None) -> list:
    return [
        check_api_key(global_config),
        check_sender_fields(global_config),
        check_db(conn),
        check_consumer_domains(global_config),
    ]


def check_attachment(campaign: CampaignConfig) -> list:
    if campaign.latex_enabled:
        results = []
        for binary in ("xelatex", "code"):
            found = shutil.which(binary) is not None
            results.append(CheckResult(binary, found, "" if found else f"{binary} not found on PATH"))
        return results
    attachment_path = campaign.path / campaign.attachment_file
    found = attachment_path.exists()
    return [CheckResult("attachment_file", found, "" if found else f"{attachment_path} not found")]


def run_campaign_checks(campaign: CampaignConfig) -> list:
    return check_attachment(campaign)


def _print_check(result: CheckResult, *, indent: str = "") -> None:
    from slap import display
    if result.ok:
        suffix = f" ({result.detail})" if result.detail else ""
        display.success(f"{indent}{result.name}: OK{suffix}")
    else:
        display.error(f"{indent}{result.name}: FAIL — {result.detail}")


def print_report(global_config: GlobalConfig) -> bool:
    """Prints the full doctor report (global checks + every discovered
    campaign's checks) — shared by the standalone `doctor` command and
    `init`'s finish step, so there's exactly one place that defines what
    the report looks like. Returns True if everything passed."""
    from slap import display
    from slap.config import ConfigError, discover_campaigns, load_campaign

    ok = True
    for result in run_global_checks(global_config):
        _print_check(result)
        ok = ok and result.ok

    names = discover_campaigns()
    if not names:
        print("No campaigns found under campaigns/.")
    for name in names:
        try:
            campaign = load_campaign(name, global_config)
        except ConfigError as e:
            display.error(f"campaign '{name}': FAIL — {e}")
            ok = False
            continue
        campaign_results = run_campaign_checks(campaign)
        campaign_ok = all(r.ok for r in campaign_results)
        if campaign_ok:
            display.success(f"campaign '{name}': OK")
        else:
            display.error(f"campaign '{name}': FAIL")
        for result in campaign_results:
            _print_check(result, indent="  ")
        ok = ok and campaign_ok
    return ok
