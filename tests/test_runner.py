"""Runner tests (Build Order step 9), per SLAP_BUILD_PROMPT.md §13 B:
runner drains; random fire-time lands in the window; 10-15s gap enforced;
cap-aware leaves overflow queued; --now flushes; drain resilience (preflight
failure -> retries -> run_failed, queue intact); idempotency (draft ID
recorded before send, retry-after-draft doesn't double-create).
"""
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from slap.config import GlobalConfig, ScheduleConfig
from slap.queue import due_for_ooo_resend, due_recipients, load_manifest, stage_recipient, tag_ooo
from slap.runner import (
    DrainResult, cap_headroom, drain, wait_for_fire_window, _roll_fire_time,
)
from slap.tracking import append_event, connect, latest_open_draft_id


def make_global_config(tmp_path, *, daily_cap=500, drain_retries=3, send_delay_min=10,
                        send_delay_max=15, api_key_env="GMASS_API_KEY"):
    return GlobalConfig(
        from_email="owner@gmail.com", from_name="Owner", api_key_env=api_key_env,
        personas={"recruiter": [2, 3, 5], "founder": [2, 5, 7], "hiring_manager": [2, 4, 6]},
        schedule=ScheduleConfig(
            fire_window_start="09:00", fire_window_end="09:15",
            send_delay_min=send_delay_min, send_delay_max=send_delay_max,
            daily_cap=daily_cap, drain_retries=drain_retries,
        ),
        # Absolute, tmp_path-scoped — doctor.check_consumer_domains() (step 12)
        # resolves this path directly (no cwd-relative fallback), so this
        # must never resolve to the real repo-root consumer_domains.txt.
        consumer_domains_file=str(tmp_path / "consumer_domains.txt"), path=tmp_path / "config.yaml",
    )


def make_attachment(tmp_path, name="resume.pdf"):
    p = tmp_path / name
    p.write_bytes(b"%PDF-fake")
    return p


def stage_one(conn, tmp_path, recipient="jane@acme.com", persona="recruiter", cadence=None):
    cadence = cadence if cadence is not None else [2, 3, 5]
    return stage_recipient(
        conn, campaign="c", recipient=recipient, persona=persona, cadence=cadence,
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"][:len(cadence)],
        attachment_path=make_attachment(tmp_path, f"{recipient}.pdf"), attachment_name="r.pdf",
        workdir_root=tmp_path / "workdir",
    )


def fake_gmass(draft_id="r-fake", campaign_id=999, create_fails=False, send_fails=False):
    calls = {"create": 0, "send": 0}

    def create_draft_fn(api_key, *, recipient, subject, message, attachment=None):
        calls["create"] += 1
        if create_fails:
            raise RuntimeError("simulated create_draft failure")
        return {"draft_id": draft_id, "raw": {}}

    def send_campaign_fn(api_key, draft_id_arg, *, campaign_settings):
        calls["send"] += 1
        if send_fails:
            raise RuntimeError("simulated send_campaign failure")
        return {"campaign_id": campaign_id, "raw": {}}

    return create_draft_fn, send_campaign_fn, calls


def send_reply_and_tag_ooo(conn, tmp_path, recipient="jane@acme.com", cadence=None):
    """Sets up the precondition for an OOO resend test: stage -> real drain
    (so first_sent_at/last_gmass_campaign_id are genuinely populated, not
    hand-inserted) -> reply -> tag_ooo. Returns the sent campaign_id."""
    stage_one(conn, tmp_path, recipient=recipient, cadence=cadence)
    create_fn, send_fn, _ = fake_gmass(campaign_id=555)
    gc = make_global_config(tmp_path)
    drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
          create_draft_fn=create_fn, send_campaign_fn=send_fn)
    append_event(conn, type="reply", recipient=recipient, campaign="c")
    tag_ooo(conn, recipient)
    return 555


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "test.db")


@pytest.fixture(autouse=True)
def api_key_env(monkeypatch):
    monkeypatch.setenv("GMASS_API_KEY", "fake-key")


# --- drain: happy path -------------------------------------------------

def test_drain_sends_a_staged_recipient(conn, tmp_path):
    gc = make_global_config(tmp_path)
    stage_one(conn, tmp_path)
    create_fn, send_fn, calls = fake_gmass()

    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert result == DrainResult(ran=True, sent=1, failed=0, remaining_queued=0, preflight_error=None)
    assert calls == {"create": 1, "send": 1}
    assert due_recipients(conn) == []


def test_drain_sends_a_recipient_already_contacted_in_an_earlier_campaign(conn, tmp_path):
    # Real BLOCKER repro, end-to-end: dedup hard-warn fires for an
    # already-contacted recipient, owner confirms proceed-anyway, recipient
    # is re-staged for a NEW campaign. drain() must actually send them, not
    # silently report "0 sent, 0 failed, 0 still queued" while a real queued
    # event sits unprocessed in the log forever (recipients.first_sent_at
    # from the EARLIER campaign's send is permanent/first-write-wins and
    # must never be checked as a "not yet sent" proxy for a re-staged cycle).
    gc = make_global_config(tmp_path)
    recipient = "already-contacted@acme.com"
    append_event(conn, type="queued", recipient=recipient, campaign="old-campaign", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient=recipient, campaign="old-campaign", stage=0,
                 gmass_campaign_id="111")

    stage_one(conn, tmp_path, recipient=recipient)  # re-staged for a new campaign ("c")
    create_fn, send_fn, calls = fake_gmass()

    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert result == DrainResult(ran=True, sent=1, failed=0, remaining_queued=0, preflight_error=None)
    assert calls == {"create": 1, "send": 1}


def test_drain_writes_run_started_and_run_completed(conn, tmp_path):
    gc = make_global_config(tmp_path)
    stage_one(conn, tmp_path)
    create_fn, send_fn, _ = fake_gmass()
    drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
          create_draft_fn=create_fn, send_campaign_fn=send_fn)

    types = [dict(r)["type"] for r in conn.execute("SELECT type FROM events ORDER BY id")]
    assert types == ["queued", "run_started", "draft_created", "sent", "run_completed"]


def test_drain_no_due_recipients_still_completes(conn, tmp_path):
    gc = make_global_config(tmp_path)
    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir")
    assert result.ran is True
    assert result.sent == 0


# --- per-email failure resilience ------------------------------------------

def test_per_email_failure_writes_send_failed_and_stays_queued(conn, tmp_path):
    gc = make_global_config(tmp_path)
    stage_one(conn, tmp_path)
    create_fn, send_fn, _ = fake_gmass(send_fails=True)

    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert result.sent == 0
    assert result.failed == 1
    assert len(due_recipients(conn)) == 1  # still queued, nothing lost
    event_types = [dict(r)["type"] for r in conn.execute("SELECT type FROM events ORDER BY id")]
    assert "send_failed" in event_types
    assert "sent" not in event_types


def test_per_email_failure_does_not_abort_the_whole_drain(conn, tmp_path):
    gc = make_global_config(tmp_path)
    stage_one(conn, tmp_path, recipient="fails@acme.com")
    stage_one(conn, tmp_path, recipient="succeeds@acme.com")

    def create_draft_fn(api_key, *, recipient, subject, message, attachment=None):
        if recipient == "fails@acme.com":
            raise RuntimeError("boom")
        return {"draft_id": "r-1", "raw": {}}

    def send_campaign_fn(api_key, draft_id, *, campaign_settings):
        return {"campaign_id": 1, "raw": {}}

    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_draft_fn, send_campaign_fn=send_campaign_fn)
    assert result.sent == 1
    assert result.failed == 1


def test_corrupt_manifest_json_does_not_abort_the_whole_drain(conn, tmp_path):
    # Iron-audit BLOCKER fix: a corrupted staged.json (e.g. from a crash
    # mid-write) must degrade to send_failed for that one recipient, not
    # crash the whole drain and strand every other recipient in the batch.
    stage_one(conn, tmp_path, recipient="corrupt@acme.com")
    stage_one(conn, tmp_path, recipient="fine@acme.com")
    (tmp_path / "workdir" / "c" / "corrupt@acme.com" / "staged.json").write_text("{not valid json")

    gc = make_global_config(tmp_path)
    create_fn, send_fn, calls = fake_gmass()
    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert result.sent == 1
    assert result.failed == 1
    assert calls["create"] == 1  # only for the fine recipient
    # The corrupt recipient stays queued for a human to fix, not lost.
    assert "corrupt@acme.com" in {r["recipient"] for r in due_recipients(conn)}


def test_manifest_missing_key_does_not_abort_the_whole_drain(conn, tmp_path):
    stage_one(conn, tmp_path, recipient="missingkey@acme.com")
    stage_one(conn, tmp_path, recipient="fine@acme.com")
    manifest_path = tmp_path / "workdir" / "c" / "missingkey@acme.com" / "staged.json"
    import json
    manifest = json.loads(manifest_path.read_text())
    del manifest["stage_bodies"]
    manifest_path.write_text(json.dumps(manifest))

    gc = make_global_config(tmp_path)
    create_fn, send_fn, _ = fake_gmass()
    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert result.sent == 1
    assert result.failed == 1


def test_mismatched_cadence_and_stage_bodies_does_not_abort_the_whole_drain(conn, tmp_path):
    # gmass.build_campaign_settings raises GMassError on a length mismatch —
    # confirm that's caught too, not just load_manifest's own exceptions.
    stage_one(conn, tmp_path, recipient="mismatched@acme.com")
    stage_one(conn, tmp_path, recipient="fine@acme.com")
    manifest_path = tmp_path / "workdir" / "c" / "mismatched@acme.com" / "staged.json"
    import json
    manifest = json.loads(manifest_path.read_text())
    manifest["stage_bodies"] = manifest["stage_bodies"][:1]  # cadence has 3, bodies now has 1
    manifest_path.write_text(json.dumps(manifest))

    gc = make_global_config(tmp_path)
    create_fn, send_fn, _ = fake_gmass()
    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert result.sent == 1
    assert result.failed == 1


def test_missing_attachment_file_does_not_abort_the_whole_drain(conn, tmp_path):
    stage_one(conn, tmp_path, recipient="noattachment@acme.com")
    stage_one(conn, tmp_path, recipient="fine@acme.com")
    (tmp_path / "workdir" / "c" / "noattachment@acme.com" / "r.pdf").unlink()

    gc = make_global_config(tmp_path)
    create_fn, send_fn, _ = fake_gmass()
    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert result.sent == 1
    assert result.failed == 1


def test_loop_level_safety_net_survives_a_bug_inside_send_one(conn, tmp_path, monkeypatch):
    # Defense-in-depth: drain()'s per-recipient loop must survive even a bug
    # INSIDE _send_one that escapes its own try/except entirely (e.g. a
    # future regression, or append_event itself raising) — proven here by
    # monkeypatching _send_one to raise directly, independent of its
    # internal exception handling.
    import slap.runner as runner_module
    stage_one(conn, tmp_path, recipient="a@acme.com")
    stage_one(conn, tmp_path, recipient="b@acme.com")

    real_send_one = runner_module._send_one
    calls = []

    def flaky_send_one(conn_arg, api_key, row, **kwargs):
        calls.append(row["recipient"])
        if row["recipient"] == "a@acme.com":
            raise RuntimeError("simulated bug escaping _send_one's own handlers")
        return real_send_one(conn_arg, api_key, row, **kwargs)

    monkeypatch.setattr(runner_module, "_send_one", flaky_send_one)
    gc = make_global_config(tmp_path)
    create_fn, send_fn, _ = fake_gmass()

    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert calls == ["a@acme.com", "b@acme.com"]  # loop continued past the bug
    assert result.sent == 1
    assert result.failed == 1
    types = [dict(r)["type"] for r in conn.execute("SELECT type FROM events ORDER BY id")]
    assert types[-1] == "run_completed"  # drain still finished cleanly


# --- idempotency: draft recorded before send, retry doesn't double-create --

def test_draft_created_recorded_before_send_campaign_is_even_called(conn, tmp_path):
    gc = make_global_config(tmp_path)
    stage_one(conn, tmp_path)
    create_fn, send_fn, _ = fake_gmass(send_fails=True)  # succeeds at create, fails at send

    drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
          create_draft_fn=create_fn, send_campaign_fn=send_fn)

    # The draft_id must be recorded even though send_campaign failed after it.
    assert latest_open_draft_id(conn, "jane@acme.com") == "r-fake"


def test_retry_after_draft_created_does_not_double_create(conn, tmp_path):
    gc = make_global_config(tmp_path)
    stage_one(conn, tmp_path)
    create_fn, send_fn, calls = fake_gmass(send_fails=True)
    drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
          create_draft_fn=create_fn, send_campaign_fn=send_fn)
    assert calls["create"] == 1

    # Second drain attempt (e.g. tomorrow, or a manual --now retry): the
    # recipient is still due (send_failed doesn't remove it from the queue).
    create_fn2, send_fn2, calls2 = fake_gmass()  # this time send succeeds
    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn2, send_campaign_fn=send_fn2)

    assert result.sent == 1
    assert calls2["create"] == 0  # reused the existing open draft, never re-created
    assert calls2["send"] == 1


# --- cap-aware headroom ------------------------------------------------

def test_cap_headroom_full_when_nothing_sent_today(conn, tmp_path):
    gc = make_global_config(tmp_path, daily_cap=500)
    assert cap_headroom(conn, gc, today=date(2026, 1, 1)) == 500


def test_cap_headroom_subtracts_todays_sent_count(conn, tmp_path):
    gc = make_global_config(tmp_path, daily_cap=5)
    today = date(2026, 1, 1)
    ts = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    for i in range(3):
        append_event(conn, type="sent", recipient=f"a{i}@x.com", campaign="c", stage=0, timestamp=ts)
    assert cap_headroom(conn, gc, today=today) == 2


def test_cap_headroom_counts_ooo_resends_too(conn, tmp_path):
    # An OOO resend (requeued) is a real send consuming the same Gmail daily
    # limit as an initial/follow-up 'sent' — must count toward the cap too.
    gc = make_global_config(tmp_path, daily_cap=5)
    ts = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, timestamp=ts)
    append_event(conn, type="requeued", recipient="b@x.com", campaign="c", stage=1,
                 gmass_campaign_id="1", timestamp=ts)
    assert cap_headroom(conn, gc, today=date(2026, 1, 1)) == 3


def test_cap_headroom_ignores_sent_events_from_other_days(conn, tmp_path):
    gc = make_global_config(tmp_path, daily_cap=5)
    yesterday = datetime(2025, 12, 31, 10, 0, tzinfo=timezone.utc)
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, timestamp=yesterday)
    assert cap_headroom(conn, gc, today=date(2026, 1, 1)) == 5


def test_cap_headroom_estimates_followups_firing_today(conn, tmp_path):
    gc = make_global_config(tmp_path, daily_cap=5)
    # recruiter cadence [2,3,5]: stage 0 sent on day 0 -> stage 1 fires day 2.
    first_sent = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=first_sent)
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0,
                 timestamp=first_sent)
    today_stage1_fires = date(2026, 1, 3)  # day 0 + 2 days
    assert cap_headroom(conn, gc, today=today_stage1_fires) == 4  # 5 - 1 estimated follow-up


def test_cap_headroom_never_negative(conn, tmp_path):
    gc = make_global_config(tmp_path, daily_cap=1)
    ts = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    for i in range(3):
        append_event(conn, type="sent", recipient=f"a{i}@x.com", campaign="c", stage=0, timestamp=ts)
    assert cap_headroom(conn, gc, today=date(2026, 1, 1)) == 0


def test_drain_leaves_overflow_queued_beyond_cap(conn, tmp_path):
    gc = make_global_config(tmp_path, daily_cap=1)
    stage_one(conn, tmp_path, recipient="a@acme.com")
    stage_one(conn, tmp_path, recipient="b@acme.com")
    create_fn, send_fn, calls = fake_gmass()

    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert result.sent == 1
    assert result.remaining_queued == 1  # the other one waits for tomorrow
    assert calls["create"] == 1


# --- 10-15s random gap between sends ----------------------------------

def test_gap_delay_applied_between_sends_not_before_the_first(conn, tmp_path):
    gc = make_global_config(tmp_path, send_delay_min=10, send_delay_max=15)
    stage_one(conn, tmp_path, recipient="a@acme.com")
    stage_one(conn, tmp_path, recipient="b@acme.com")
    create_fn, send_fn, _ = fake_gmass()

    sleeps = []
    drain(conn, gc, "fake-key", sleep_fn=lambda s: sleeps.append(s), workdir_root=tmp_path / "workdir",
          create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert len(sleeps) == 1  # 2 recipients -> exactly 1 gap
    assert 10 <= sleeps[0] <= 15


def test_no_gap_delay_for_a_single_send(conn, tmp_path):
    gc = make_global_config(tmp_path)
    stage_one(conn, tmp_path)
    create_fn, send_fn, _ = fake_gmass()
    sleeps = []
    drain(conn, gc, "fake-key", sleep_fn=lambda s: sleeps.append(s), workdir_root=tmp_path / "workdir",
          create_draft_fn=create_fn, send_campaign_fn=send_fn)
    assert sleeps == []


# --- preflight failure -> retry -> run_failed, queue intact -----------------

def test_preflight_failure_retries_then_writes_run_failed(conn, tmp_path, monkeypatch):
    monkeypatch.delenv("GMASS_API_KEY", raising=False)
    gc = make_global_config(tmp_path, drain_retries=3)
    stage_one(conn, tmp_path)

    sleeps = []
    result = drain(conn, gc, "", sleep_fn=lambda s: sleeps.append(s), workdir_root=tmp_path / "workdir")

    assert result.ran is False
    assert result.preflight_error is not None
    assert len(sleeps) == 2  # 3 attempts total -> 2 retry delays in between
    event_types = [dict(r)["type"] for r in conn.execute("SELECT type FROM events ORDER BY id")]
    assert event_types == ["queued", "run_failed"]  # queue completely untouched


def test_preflight_failure_leaves_queue_completely_intact(conn, tmp_path, monkeypatch):
    monkeypatch.delenv("GMASS_API_KEY", raising=False)
    gc = make_global_config(tmp_path, drain_retries=1)
    stage_one(conn, tmp_path)
    drain(conn, gc, "", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir")
    assert len(due_recipients(conn)) == 1


def test_preflight_recovers_within_retries_and_drain_proceeds(conn, tmp_path, monkeypatch):
    # Fails the first check, then "recovers" (env var appears) before retries
    # are exhausted — the drain should proceed normally.
    monkeypatch.delenv("GMASS_API_KEY", raising=False)
    gc = make_global_config(tmp_path, drain_retries=3)
    stage_one(conn, tmp_path)

    attempts = {"n": 0}
    real_sleep_calls = []

    def flaky_sleep(seconds):
        real_sleep_calls.append(seconds)
        attempts["n"] += 1
        if attempts["n"] == 1:
            os.environ["GMASS_API_KEY"] = "fake-key"

    create_fn, send_fn, _ = fake_gmass()
    result = drain(conn, gc, "fake-key", sleep_fn=flaky_sleep, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)
    assert result.ran is True
    assert result.sent == 1


def test_preflight_survives_an_unexpected_exception_and_writes_run_failed(conn, tmp_path):
    # doctor.check_consumer_domains has a real side effect (it seeds a
    # missing file) that can raise — e.g. a customized consumer_domains_file
    # whose parent directory doesn't exist. That must still degrade to a
    # normal preflight failure (retry -> run_failed, queue intact), never an
    # uncaught exception out of drain() (step 12 SHOULD-FIX).
    gc = make_global_config(tmp_path, drain_retries=1)
    gc.consumer_domains_file = str(tmp_path / "missing_dir" / "consumer_domains.txt")
    stage_one(conn, tmp_path)

    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir")

    assert result.ran is False
    assert "unexpected preflight error" in result.preflight_error
    event_types = [dict(r)["type"] for r in conn.execute("SELECT type FROM events ORDER BY id")]
    assert event_types == ["queued", "run_failed"]  # queue completely untouched


# --- random fire-time lands in the configured window -----------------------

def test_roll_fire_time_lands_within_window(tmp_path):
    gc = make_global_config(tmp_path)
    today = date(2026, 1, 1)
    for _ in range(200):
        t = _roll_fire_time(gc.schedule, today)
        assert t.date() == today
        assert (9, 0) <= (t.hour, t.minute) <= (9, 15)


def test_wait_for_fire_window_sleeps_until_target_when_early(tmp_path):
    gc = make_global_config(tmp_path)
    now = datetime(2026, 1, 1, 8, 0)  # before the window
    sleeps = []
    target = wait_for_fire_window(gc.schedule, now_fn=lambda: now, sleep_fn=lambda s: sleeps.append(s),
                                   rng=_FixedRng(0.5))
    assert len(sleeps) == 1
    assert sleeps[0] > 0
    assert (9, 0) <= (target.hour, target.minute) <= (9, 15)


def test_wait_for_fire_window_fires_immediately_if_already_past_it(tmp_path):
    # The launchd wake-catch-up case: woke up after the window already passed.
    gc = make_global_config(tmp_path)
    now = datetime(2026, 1, 1, 9, 20)
    sleeps = []
    wait_for_fire_window(gc.schedule, now_fn=lambda: now, sleep_fn=lambda s: sleeps.append(s))
    assert sleeps == []  # no sleep at all — fires right away


class _FixedRng:
    def __init__(self, fraction):
        self.fraction = fraction

    def uniform(self, a, b):
        return a + self.fraction * (b - a)


# --- OOO re-queue (Build Order step 10) ------------------------------------

def test_drain_processes_ooo_resend_via_combined_due_list(conn, tmp_path):
    reply_to_id = send_reply_and_tag_ooo(conn, tmp_path)
    assert due_for_ooo_resend(conn) != []

    create_fn, send_fn, calls = fake_gmass(draft_id="r-resend", campaign_id=777)
    gc = make_global_config(tmp_path)
    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert result.sent == 1
    assert due_for_ooo_resend(conn) == []  # resolved, no longer due
    # run_started/run_completed have recipient=NULL (run-level events, §5) so
    # they're naturally excluded by this per-recipient filter.
    types = [dict(r)["type"] for r in conn.execute(
        "SELECT type FROM events WHERE recipient = 'jane@acme.com' ORDER BY id")]
    assert types == ["queued", "draft_created", "sent", "reply", "ooo_tagged", "draft_created", "requeued"]


def test_ooo_resend_reuses_staged_stage_body_and_replies_to_original_campaign(conn, tmp_path):
    reply_to_id = send_reply_and_tag_ooo(conn, tmp_path)

    captured = {}

    def create_draft_fn(api_key, *, recipient, subject, message, attachment=None):
        captured["message"] = message
        captured["subject"] = subject
        captured["attachment"] = attachment
        return {"draft_id": "r-resend", "raw": {}}

    def send_campaign_fn(api_key, draft_id, *, campaign_settings):
        captured["campaign_settings"] = campaign_settings
        return {"campaign_id": 777, "raw": {}}

    gc = make_global_config(tmp_path)
    drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
          create_draft_fn=create_draft_fn, send_campaign_fn=send_campaign_fn)

    manifest = load_manifest(tmp_path / "workdir" / "c" / "jane@acme.com")
    assert captured["message"] == manifest["stage_bodies"][0]  # next stage (1) = index 0
    assert captured["attachment"] is None  # no re-attaching the résumé on a threaded reply
    assert captured["campaign_settings"]["sendAsReply"] is True
    assert captured["campaign_settings"]["campaignIdToReplyTo"] == reply_to_id


def test_ooo_resend_advances_current_stage_and_status(conn, tmp_path):
    send_reply_and_tag_ooo(conn, tmp_path)
    create_fn, send_fn, _ = fake_gmass(campaign_id=777)
    gc = make_global_config(tmp_path)
    drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
          create_draft_fn=create_fn, send_campaign_fn=send_fn)

    row = conn.execute("SELECT * FROM recipients WHERE recipient = ?", ("jane@acme.com",)).fetchone()
    assert row["status"] == "active"
    assert row["current_stage"] == 1
    assert row["last_gmass_campaign_id"] == "777"


def test_ooo_resend_retry_reuses_open_draft_no_double_create(conn, tmp_path):
    send_reply_and_tag_ooo(conn, tmp_path)
    create_fn, send_fn, calls = fake_gmass(draft_id="r-resend", send_fails=True)
    gc = make_global_config(tmp_path)
    drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
          create_draft_fn=create_fn, send_campaign_fn=send_fn)
    assert calls["create"] == 1
    assert due_for_ooo_resend(conn) != []  # still due — resend failed

    create_fn2, send_fn2, calls2 = fake_gmass(campaign_id=777)  # this time it succeeds
    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn2, send_campaign_fn=send_fn2)
    assert result.sent == 1
    assert calls2["create"] == 0  # reused the existing open draft
    assert calls2["send"] == 1


def test_ooo_resend_exhausted_cadence_does_not_crash_drain(conn, tmp_path):
    # current_stage already equals len(cadence) — no next stage to resend.
    # Must degrade to send_failed for this one recipient, not crash the batch.
    # (Note: current_stage only advances on OUR OWN sent/requeued events, not
    # on GMass's own silent automatic follow-up firing — so reaching this
    # state for real requires the recipient to have already gone through a
    # prior OOO resend cycle up to the last stage; simulated directly here.)
    stage_one(conn, tmp_path, recipient="exhausted@acme.com", cadence=[2])
    append_event(conn, type="sent", recipient="exhausted@acme.com", campaign="c",
                 stage=0, gmass_campaign_id="1")
    append_event(conn, type="reply", recipient="exhausted@acme.com", campaign="c")
    append_event(conn, type="ooo_tagged", recipient="exhausted@acme.com", campaign="c")
    append_event(conn, type="requeued", recipient="exhausted@acme.com", campaign="c",
                 stage=1, gmass_campaign_id="1")  # current_stage now 1 == len(cadence)
    append_event(conn, type="reply", recipient="exhausted@acme.com", campaign="c")
    tag_ooo(conn, "exhausted@acme.com")  # tagged again — but nothing left to resend

    stage_one(conn, tmp_path, recipient="fine@acme.com")  # a normal recipient in the same batch
    gc = make_global_config(tmp_path)
    create_fn2, send_fn2, _ = fake_gmass()
    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn2, send_campaign_fn=send_fn2)

    assert result.sent == 1  # fine@acme.com
    assert result.failed == 1  # exhausted@acme.com
    event_types = [dict(r)["type"] for r in conn.execute(
        "SELECT type FROM events WHERE recipient = 'exhausted@acme.com' ORDER BY id")]
    assert event_types[-1] == "send_failed"


def test_ooo_resend_missing_campaign_id_does_not_crash_drain(conn, tmp_path):
    # Robustness: an ooo_tagged recipient with no last_gmass_campaign_id
    # (shouldn't happen via the normal flow, but must not crash if it does).
    stage_one(conn, tmp_path, recipient="a@acme.com")
    append_event(conn, type="ooo_tagged", recipient="a@acme.com", campaign="c")

    gc = make_global_config(tmp_path)
    create_fn, send_fn, _ = fake_gmass()
    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)
    assert result.failed == 1


def test_ooo_and_initial_sends_share_the_same_gap_and_cap(conn, tmp_path):
    send_reply_and_tag_ooo(conn, tmp_path, recipient="ooo@acme.com")
    stage_one(conn, tmp_path, recipient="initial@acme.com")

    gc = make_global_config(tmp_path, daily_cap=500)
    create_fn, send_fn, _ = fake_gmass(campaign_id=777)
    sleeps = []
    result = drain(conn, gc, "fake-key", sleep_fn=lambda s: sleeps.append(s),
                    workdir_root=tmp_path / "workdir",
                    create_draft_fn=create_fn, send_campaign_fn=send_fn)

    assert result.sent == 2  # both the OOO resend and the initial send went out
    assert len(sleeps) == 1  # 2 items in the combined batch -> exactly 1 gap


def test_due_for_ooo_resend_repeats_across_multiple_ooo_cycles(conn, tmp_path):
    # A recipient can be tagged OOO more than once over their lifecycle —
    # each cycle should correctly target the NEXT stage after the last one
    # actually sent, not always stage 1.
    send_reply_and_tag_ooo(conn, tmp_path)  # sent stage 0, now due for stage 1 resend
    create_fn, send_fn, _ = fake_gmass(campaign_id=777)
    gc = make_global_config(tmp_path)
    drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
          create_draft_fn=create_fn, send_campaign_fn=send_fn)  # resolves stage 1 resend

    # Reply again, tag OOO again — should now target stage 2.
    append_event(conn, type="reply", recipient="jane@acme.com", campaign="c")
    tag_ooo(conn, "jane@acme.com")

    captured = {}

    def create_draft_fn2(api_key, *, recipient, subject, message, attachment=None):
        captured["message"] = message
        return {"draft_id": "r-resend-2", "raw": {}}

    def send_campaign_fn2(api_key, draft_id, *, campaign_settings):
        captured["campaign_settings"] = campaign_settings
        return {"campaign_id": 888, "raw": {}}

    drain(conn, gc, "fake-key", sleep_fn=lambda s: None, workdir_root=tmp_path / "workdir",
          create_draft_fn=create_draft_fn2, send_campaign_fn=send_campaign_fn2)

    manifest = load_manifest(tmp_path / "workdir" / "c" / "jane@acme.com")
    assert captured["message"] == manifest["stage_bodies"][1]  # stage 2 = index 1
    # Deterministic threading (§7): cycle 2 must reply into cycle 1's resend
    # campaign (777, the MOST RECENT), not the original initial send (555) —
    # these coincide on cycle 1, so only a second cycle can distinguish them.
    assert captured["campaign_settings"]["campaignIdToReplyTo"] == 777
    row = conn.execute("SELECT * FROM recipients WHERE recipient = ?", ("jane@acme.com",)).fetchone()
    assert row["current_stage"] == 2
