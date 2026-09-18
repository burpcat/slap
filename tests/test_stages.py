"""Pure cadence-schedule math (slap.stages) — the shared source of truth for
stage estimation and next-follow-up timing."""
from datetime import date

from slap import stages

SENT = date(2026, 1, 1)          # initial send
CADENCE = [2, 5, 9]              # deltas -> follow-ups at +2, +7, +16 days


def test_stage_fire_date_is_cumulative():
    assert stages.stage_fire_date(SENT, CADENCE, 1) == date(2026, 1, 3)   # +2
    assert stages.stage_fire_date(SENT, CADENCE, 2) == date(2026, 1, 8)   # +2+5
    assert stages.stage_fire_date(SENT, CADENCE, 3) == date(2026, 1, 17)  # +2+5+9


def test_cadence_window_days():
    assert stages.cadence_window_days(CADENCE) == 16
    assert stages.cadence_window_days([]) == 0


def test_estimate_current_stage_initial_before_first_followup():
    # day of send and the day before stage 1 -> still "initial"
    assert stages.estimate_current_stage(SENT, CADENCE, SENT) == 0
    assert stages.estimate_current_stage(SENT, CADENCE, date(2026, 1, 2)) == 0


def test_estimate_current_stage_counts_fired_stages():
    assert stages.estimate_current_stage(SENT, CADENCE, date(2026, 1, 3)) == 1   # stage1 fired today
    assert stages.estimate_current_stage(SENT, CADENCE, date(2026, 1, 7)) == 1
    assert stages.estimate_current_stage(SENT, CADENCE, date(2026, 1, 8)) == 2   # stage2 fired
    assert stages.estimate_current_stage(SENT, CADENCE, date(2026, 1, 17)) == 3  # final fired


def test_estimate_current_stage_exhausted_caps_at_len():
    assert stages.estimate_current_stage(SENT, CADENCE, date(2026, 6, 1)) == 3


def test_estimate_current_stage_empty_cadence_is_initial():
    assert stages.estimate_current_stage(SENT, [], date(2026, 6, 1)) == 0


def test_estimate_current_stage_floor_never_reads_below_recorded():
    # elapsed time suggests stage 0, but the event log recorded stage 2 (OOO)
    assert stages.estimate_current_stage(SENT, CADENCE, SENT, floor=2) == 2
    # floor never pulls DOWN a higher elapsed estimate
    assert stages.estimate_current_stage(SENT, CADENCE, date(2026, 1, 17), floor=1) == 3


def test_next_fire_date_walks_to_next_unfired_stage():
    assert stages.next_fire_date(SENT, CADENCE, SENT) == date(2026, 1, 3)          # stage1 next
    assert stages.next_fire_date(SENT, CADENCE, date(2026, 1, 3)) == date(2026, 1, 8)  # stage1 today -> stage2 next
    assert stages.next_fire_date(SENT, CADENCE, date(2026, 1, 8)) == date(2026, 1, 17)


def test_next_fire_date_none_when_window_elapsed():
    assert stages.next_fire_date(SENT, CADENCE, date(2026, 1, 17)) is None
    assert stages.next_fire_date(SENT, CADENCE, date(2026, 6, 1)) is None


def test_next_fire_date_from_stage_skips_already_fired():
    # from_stage=2 skips stages 1 & 2 even if their dates are in the future
    assert stages.next_fire_date(SENT, CADENCE, SENT, from_stage=2) == date(2026, 1, 17)
    assert stages.next_fire_date(SENT, CADENCE, SENT, from_stage=3) is None


def test_next_fire_date_empty_cadence_is_none():
    assert stages.next_fire_date(SENT, [], SENT) is None


def test_stage_label_mapping():
    assert stages.stage_label(0) == "initial"
    assert stages.stage_label(1) == "stage1"
    assert stages.stage_label(3) == "stage3"
    assert stages.stage_label(-1) == "initial"
