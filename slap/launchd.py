"""Generates the launchd .plist for the unattended runner (post-launch
feature — configurable scheduler days). See LAUNCHD.md for install steps.

A single StartCalendarInterval dict can't express "only these weekdays" —
launchd's own answer is an ARRAY of StartCalendarInterval dicts, one per
active day, each carrying the same Hour/Minute (the fire_window_start
anchor) plus that day's Weekday integer. Generated straight from
config.yaml rather than hand-maintained, since active_days already lives
there — one source of truth for the schedule (CLAUDE.md's iron principles).
This eliminates plist/config drift at the source; slap.runner.is_active_day()
is the second line of defense for a plist that wasn't regenerated/reloaded
after an active_days change.
"""
from __future__ import annotations

from pathlib import Path

from slap.config import GlobalConfig

# macOS launchd's own StartCalendarInterval Weekday convention: 0 or 7 =
# Sunday, 1 = Monday, ..., 6 = Saturday (either 0 or 7 is valid for Sunday
# per Apple's docs; 0 is used here).
LAUNCHD_WEEKDAY = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


class LaunchdError(Exception):
    """Raised on fail-loud plist-generation misuse."""


def render_plist(global_config: GlobalConfig, repo_root: Path, python_executable: str, *,
                  label: str = "com.slap.runner") -> str:
    """Renders the full plist XML: one StartCalendarInterval dict per
    active day, all anchored at schedule.fire_window_start's Hour/Minute —
    slap.py runner itself then rolls a random moment within fire_window_
    start/end and sleeps until it, or fires immediately on wake if that
    moment already passed (see runner.wait_for_fire_window)."""
    if not global_config.schedule.active_days:
        # config.py's loader already rejects an empty active_days list, so
        # this is unreachable via the CLI — but ScheduleConfig itself has no
        # such guard, and a directly-constructed empty list here would
        # otherwise silently render a zero-entry StartCalendarInterval array:
        # launchd would accept that plist and simply never fire the job,
        # with no warning at all. Fail loud instead.
        raise LaunchdError("active_days is empty — the generated plist would never fire")

    hour_str, minute_str = global_config.schedule.fire_window_start.split(":")
    hour, minute = int(hour_str), int(minute_str)
    repo_root = Path(repo_root).resolve()

    intervals = "\n".join(
        f"""        <dict>
            <key>Hour</key>
            <integer>{hour}</integer>
            <key>Minute</key>
            <integer>{minute}</integer>
            <key>Weekday</key>
            <integer>{LAUNCHD_WEEKDAY[day]}</integer>
        </dict>"""
        for day in sorted(global_config.schedule.active_days, key=LAUNCHD_WEEKDAY.get)
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python_executable}</string>
        <string>{repo_root / "slap.py"}</string>
        <string>runner</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{repo_root}</string>

    <key>StartCalendarInterval</key>
    <array>
{intervals}
    </array>

    <key>StandardOutPath</key>
    <string>{repo_root / "runner.log"}</string>
    <key>StandardErrorPath</key>
    <string>{repo_root / "runner.err.log"}</string>
</dict>
</plist>
"""


def render_sync_plist(repo_root: Path, python_executable: str, *,
                       label: str = "com.slap.sync", interval_seconds: int = 3600) -> str:
    """Renders the launchd plist for `slap.py sync` — the hourly background
    refresh of the dashboard's Redis-backed GMass-data cache (post-launch
    feature, slap/gmass_cache.py). Deliberately a SEPARATE plist/function
    from render_plist() above, not a shared one: this job's cadence is a
    plain fixed interval ("every hour"), not a calendar-anchored,
    day-of-week-restricted one like the runner's, so `StartInterval`
    (launchd's own idiomatic mechanism for "every N seconds," a single
    integer) is the right fit here — trying to force both jobs' genuinely
    different scheduling shapes through one function/one plist key would
    be more contorted than two small, focused ones. No `active_days`
    restriction either: unlike the outreach runner (which the owner may
    deliberately want to skip on weekends), refreshing a read-only cache
    has no send-volume/deliverability reason to ever pause."""
    repo_root = Path(repo_root).resolve()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python_executable}</string>
        <string>{repo_root / "slap.py"}</string>
        <string>sync</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{repo_root}</string>

    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>

    <key>StandardOutPath</key>
    <string>{repo_root / "sync.log"}</string>
    <key>StandardErrorPath</key>
    <string>{repo_root / "sync.err.log"}</string>
</dict>
</plist>
"""
