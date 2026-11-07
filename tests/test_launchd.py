"""launchd .plist generation tests (post-launch feature: configurable
scheduler days). A single StartCalendarInterval dict can't express "only
these weekdays" — the generated plist must use an ARRAY, one dict per
active day, each anchored at schedule.fire_window_start's Hour/Minute.
"""
import plistlib

import pytest

from slap.config import GlobalConfig, ScheduleConfig
from slap.launchd import LAUNCHD_WEEKDAY, LaunchdError, render_plist, render_sync_plist


def make_global_config(*, fire_window_start="09:00", active_days=None):
    return GlobalConfig(
        from_email="owner@gmail.com", from_name="Owner", api_key_env="GMASS_API_KEY",
        personas={"recruiter": [2, 3, 5]},
        schedule=ScheduleConfig(fire_window_start=fire_window_start, fire_window_end="09:15",
                                 send_delay_min=10, send_delay_max=15, daily_cap=500, drain_retries=3,
                                 active_days=active_days or ["mon", "tue", "wed", "thu", "fri"]),
        consumer_domains_file="consumer_domains.txt", path="config.yaml",
    )


def test_render_plist_is_valid_plist_xml(tmp_path):
    xml = render_plist(make_global_config(), tmp_path, "/usr/bin/python3")
    parsed = plistlib.loads(xml.encode())
    assert parsed["Label"] == "com.slap.runner"


def test_render_plist_start_calendar_interval_is_an_array_not_a_dict(tmp_path):
    xml = render_plist(make_global_config(), tmp_path, "/usr/bin/python3")
    parsed = plistlib.loads(xml.encode())
    assert isinstance(parsed["StartCalendarInterval"], list)


def test_render_plist_one_entry_per_active_day(tmp_path):
    xml = render_plist(make_global_config(active_days=["mon", "wed", "fri"]), tmp_path, "/usr/bin/python3")
    parsed = plistlib.loads(xml.encode())
    intervals = parsed["StartCalendarInterval"]
    assert len(intervals) == 3
    weekdays = {entry["Weekday"] for entry in intervals}
    assert weekdays == {LAUNCHD_WEEKDAY["mon"], LAUNCHD_WEEKDAY["wed"], LAUNCHD_WEEKDAY["fri"]}


def test_render_plist_weekday_mapping_matches_launchd_convention(tmp_path):
    # Apple's own convention: 1=Monday ... 6=Saturday, (0 or 7)=Sunday.
    assert LAUNCHD_WEEKDAY["mon"] == 1
    assert LAUNCHD_WEEKDAY["fri"] == 5
    assert LAUNCHD_WEEKDAY["sat"] == 6
    assert LAUNCHD_WEEKDAY["sun"] == 0


def test_render_plist_every_entry_shares_the_fire_window_start_anchor(tmp_path):
    xml = render_plist(make_global_config(fire_window_start="14:37", active_days=["mon", "tue", "sat"]),
                        tmp_path, "/usr/bin/python3")
    parsed = plistlib.loads(xml.encode())
    for entry in parsed["StartCalendarInterval"]:
        assert entry["Hour"] == 14
        assert entry["Minute"] == 37


def test_render_plist_uses_the_given_python_executable_and_absolute_slap_py_path(tmp_path):
    xml = render_plist(make_global_config(), tmp_path, "/some/venv/bin/python")
    parsed = plistlib.loads(xml.encode())
    args = parsed["ProgramArguments"]
    assert args[0] == "/some/venv/bin/python"
    assert args[1] == str((tmp_path / "slap.py").resolve())
    assert args[2] == "runner"


def test_render_plist_working_directory_is_the_absolute_repo_root(tmp_path):
    xml = render_plist(make_global_config(), tmp_path, "/usr/bin/python3")
    parsed = plistlib.loads(xml.encode())
    assert parsed["WorkingDirectory"] == str(tmp_path.resolve())


def test_render_plist_log_paths_are_under_the_repo_root(tmp_path):
    xml = render_plist(make_global_config(), tmp_path, "/usr/bin/python3")
    parsed = plistlib.loads(xml.encode())
    assert parsed["StandardOutPath"] == str((tmp_path / "runner.log").resolve())
    assert parsed["StandardErrorPath"] == str((tmp_path / "runner.err.log").resolve())


def test_render_plist_single_active_day(tmp_path):
    xml = render_plist(make_global_config(active_days=["sun"]), tmp_path, "/usr/bin/python3")
    parsed = plistlib.loads(xml.encode())
    assert len(parsed["StartCalendarInterval"]) == 1
    assert parsed["StartCalendarInterval"][0]["Weekday"] == 0


def test_render_plist_fails_loud_on_empty_active_days(tmp_path):
    # config.py's loader already rejects an empty active_days list, but
    # ScheduleConfig itself has no such guard — a directly-constructed empty
    # list must never silently produce a zero-entry StartCalendarInterval
    # array (launchd would accept it and just never fire the job).
    gc = make_global_config()
    gc.schedule.active_days = []
    with pytest.raises(LaunchdError, match="active_days is empty"):
        render_plist(gc, tmp_path, "/usr/bin/python3")


# --- render_sync_plist (post-launch feature: Redis-backed dashboard cache) --
# A plain fixed hourly interval, deliberately NOT the runner's calendar/
# weekday-restricted shape — a separate function/plist, not a shared one
# (see that function's own docstring for why).

def test_render_sync_plist_is_valid_plist_xml(tmp_path):
    xml = render_sync_plist(tmp_path, "/usr/bin/python3")
    parsed = plistlib.loads(xml.encode())
    assert parsed["Label"] == "com.slap.sync"


def test_render_sync_plist_uses_start_interval_not_calendar_interval(tmp_path):
    xml = render_sync_plist(tmp_path, "/usr/bin/python3")
    parsed = plistlib.loads(xml.encode())
    assert parsed["StartInterval"] == 3600
    assert "StartCalendarInterval" not in parsed


def test_render_sync_plist_default_interval_is_one_hour(tmp_path):
    xml = render_sync_plist(tmp_path, "/usr/bin/python3")
    parsed = plistlib.loads(xml.encode())
    assert parsed["StartInterval"] == 3600


def test_render_sync_plist_custom_interval(tmp_path):
    xml = render_sync_plist(tmp_path, "/usr/bin/python3", interval_seconds=1800)
    parsed = plistlib.loads(xml.encode())
    assert parsed["StartInterval"] == 1800


def test_render_sync_plist_program_arguments_invoke_sync_subcommand(tmp_path):
    xml = render_sync_plist(tmp_path, "/some/venv/bin/python")
    parsed = plistlib.loads(xml.encode())
    args = parsed["ProgramArguments"]
    assert args[0] == "/some/venv/bin/python"
    assert args[1] == str((tmp_path / "slap.py").resolve())
    assert args[2] == "sync"


def test_render_sync_plist_working_directory_and_log_paths(tmp_path):
    xml = render_sync_plist(tmp_path, "/usr/bin/python3")
    parsed = plistlib.loads(xml.encode())
    assert parsed["WorkingDirectory"] == str(tmp_path.resolve())
    assert parsed["StandardOutPath"] == str((tmp_path / "sync.log").resolve())
    assert parsed["StandardErrorPath"] == str((tmp_path / "sync.err.log").resolve())


def test_render_sync_plist_and_render_plist_use_different_labels(tmp_path):
    # Two independent jobs must never collide under launchd's own Label
    # namespace (~/Library/LaunchAgents).
    sync_xml = render_sync_plist(tmp_path, "/usr/bin/python3")
    runner_xml = render_plist(make_global_config(), tmp_path, "/usr/bin/python3")
    assert plistlib.loads(sync_xml.encode())["Label"] != plistlib.loads(runner_xml.encode())["Label"]
