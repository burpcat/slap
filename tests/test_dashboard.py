"""Dashboard tests (Build Order step 11), per SLAP_BUILD_PROMPT.md §8:
on-open poll writes new events; panels render from tracking data;
reply-tag -> OOO -> re-queue fires a send-as-reply.
"""
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from werkzeug.serving import make_server

from slap.config import GlobalConfig, ScheduleConfig
from slap.dashboard import (
    actionable_replies, create_app, engagement_intelligence, needs_triage, pipeline,
    sync_reports, tag_reply, this_week, today_strip, todays_runs,
)
from slap.tracking import append_event, connect


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "test.db")


def make_global_config(*, daily_cap=500, drain_retries=3):
    return GlobalConfig(
        from_email="owner@gmail.com", from_name="Owner", api_key_env="GMASS_API_KEY",
        personas={"recruiter": [2, 3, 5], "founder": [2, 5, 7], "hiring_manager": [2, 4, 6]},
        schedule=ScheduleConfig(fire_window_start="09:00", fire_window_end="09:15",
                                 send_delay_min=10, send_delay_max=15,
                                 daily_cap=daily_cap, drain_retries=drain_retries),
        consumer_domains_file="consumer_domains.txt", path="config.yaml",
    )


def seed_sent_recipient(conn, recipient="jane@acme.com", campaign="c", campaign_id="555"):
    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0,
                 gmass_campaign_id=campaign_id)


# --- sync_reports: dedup + resilience --------------------------------------

def test_sync_reports_writes_new_reply_event(conn):
    seed_sent_recipient(conn)
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if report_type == "replies":
                return [{"emailAddress": "jane@acme.com", "replyId": "r1", "replyTime": "t1"}]
            return []
        mock_get.side_effect = side_effect
        result = sync_reports(conn, "fake-key")

    assert result["new_replies"] == 1
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'reply'")]
    assert len(events) == 1


def test_sync_reports_does_not_reinsert_already_recorded_reply(conn):
    seed_sent_recipient(conn)
    append_event(conn, type="reply", recipient="jane@acme.com", campaign="c",
                 meta={"reply_id": "r1", "reply_time": "t1"})
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if report_type == "replies":
                return [{"emailAddress": "jane@acme.com", "replyId": "r1", "replyTime": "t1"}]
            return []
        mock_get.side_effect = side_effect
        result = sync_reports(conn, "fake-key")

    assert result["new_replies"] == 0
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'reply'")]
    assert len(events) == 1  # still just the one, not duplicated


def test_sync_reports_dedupes_clicks_by_url_and_time(conn):
    seed_sent_recipient(conn)
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if report_type == "clicks":
                return [{"emailAddress": "jane@acme.com", "url": "https://x.com", "clickTime": "t1"}]
            return []
        mock_get.side_effect = side_effect
        r1 = sync_reports(conn, "fake-key")
        r2 = sync_reports(conn, "fake-key")  # same click reported again on next poll

    assert r1["new_clicks"] == 1
    assert r2["new_clicks"] == 0
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'click'")]
    assert len(events) == 1


def test_sync_reports_dedupes_bounces_by_reason_and_time(conn):
    seed_sent_recipient(conn)
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if report_type == "bounces":
                return [{"emailAddress": "jane@acme.com", "bounceReason": "mailbox full", "bounceTime": "t1"}]
            return []
        mock_get.side_effect = side_effect
        r1 = sync_reports(conn, "fake-key")
        r2 = sync_reports(conn, "fake-key")

    assert r1["new_bounces"] == 1
    assert r2["new_bounces"] == 0


def test_sync_reports_distinguishes_different_clicks(conn):
    seed_sent_recipient(conn)
    calls = {"n": 0}
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if report_type == "clicks":
                calls["n"] += 1
                if calls["n"] == 1:
                    return [{"emailAddress": "jane@acme.com", "url": "https://x.com", "clickTime": "t1"}]
                return [{"emailAddress": "jane@acme.com", "url": "https://x.com", "clickTime": "t2"}]
            return []
        mock_get.side_effect = side_effect
        r1 = sync_reports(conn, "fake-key")
        r2 = sync_reports(conn, "fake-key")  # a genuinely later click on the same link

    assert r1["new_clicks"] == 1
    assert r2["new_clicks"] == 1
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'click'")]
    assert len(events) == 2


def test_sync_reports_dedupes_within_a_single_batch_too(conn):
    # If one GMass response somehow contains the same item twice, it must
    # not be inserted as two events (the dedup set is updated as we go, not
    # just checked once against pre-existing events).
    seed_sent_recipient(conn)
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if report_type == "clicks":
                item = {"emailAddress": "jane@acme.com", "url": "https://x.com", "clickTime": "t1"}
                return [item, item]  # duplicate within the same batch
            return []
        mock_get.side_effect = side_effect
        result = sync_reports(conn, "fake-key")

    assert result["new_clicks"] == 1
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'click'")]
    assert len(events) == 1


def test_sync_reports_records_recipients_current_stage_on_reply_and_click(conn):
    seed_sent_recipient(conn)
    append_event(conn, type="sent", recipient="jane@acme.com", campaign="c", stage=1,
                 gmass_campaign_id="555")  # advance current_stage to 1 (e.g. a later OOO resend)
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if report_type == "replies":
                return [{"emailAddress": "jane@acme.com", "replyId": "r1", "replyTime": "t1"}]
            if report_type == "clicks":
                return [{"emailAddress": "jane@acme.com", "url": "https://x.com", "clickTime": "t1"}]
            return []
        mock_get.side_effect = side_effect
        sync_reports(conn, "fake-key")

    reply = dict(conn.execute("SELECT * FROM events WHERE type = 'reply'").fetchone())
    click = dict(conn.execute("SELECT * FROM events WHERE type = 'click'").fetchone())
    assert reply["stage"] == 1
    assert click["stage"] == 1


def test_sync_reports_no_known_campaigns_is_a_noop(conn):
    result = sync_reports(conn, "fake-key")
    assert result == {"synced_at": result["synced_at"], "new_replies": 0, "new_clicks": 0,
                       "new_bounces": 0, "errors": []}


def test_sync_reports_one_campaigns_transient_network_error_does_not_block_others(conn):
    # A transient network failure (timeout, connection refused) is tolerated
    # silently — one campaign's poll failing must not block syncing the
    # rest, and it's not a real problem worth surfacing (the next poll
    # retries it automatically).
    import requests

    seed_sent_recipient(conn, recipient="fails@acme.com", campaign_id="1")
    seed_sent_recipient(conn, recipient="fine@acme.com", campaign_id="2")

    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if cid == "1":
                raise requests.exceptions.ConnectionError("simulated network error")
            if report_type == "replies":
                return [{"emailAddress": "fine@acme.com", "replyId": "r1", "replyTime": "t1"}]
            return []
        mock_get.side_effect = side_effect
        result = sync_reports(conn, "fake-key")

    assert result["new_replies"] == 1  # fine@acme.com's reply still got recorded
    assert result["errors"] == []  # transient failures aren't surfaced as errors


def test_sync_reports_surfaces_gmass_api_errors_without_crashing(conn):
    # A real API-level problem (bad/expired key, GMass schema drift) must
    # NOT be silently swallowed the way a transient network error is —
    # otherwise an invalid API key looks identical to "nothing new" forever.
    # It still must not crash the whole sync or block other campaigns.
    from slap.gmass import GMassError

    seed_sent_recipient(conn, recipient="fails@acme.com", campaign_id="1")
    seed_sent_recipient(conn, recipient="fine@acme.com", campaign_id="2")

    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if cid == "1":
                raise GMassError("GMass reports/replies returned HTTP 401: unauthorized")
            if report_type == "replies":
                return [{"emailAddress": "fine@acme.com", "replyId": "r1", "replyTime": "t1"}]
            return []
        mock_get.side_effect = side_effect
        result = sync_reports(conn, "fake-key")

    assert result["new_replies"] == 1  # fine@acme.com's reply still got recorded
    assert len(result["errors"]) == 3  # one per report type (replies/clicks/bounces) polled for cid=1
    assert any("401" in e for e in result["errors"])


def test_sync_reports_returns_utc_synced_at(conn):
    result = sync_reports(conn, "fake-key")
    assert result["synced_at"].tzinfo is not None
    assert result["synced_at"].utcoffset().total_seconds() == 0


def _ts(day_offset, hour=10):
    # A mid-month anchor gives margin for negative/large offsets in tests.
    return datetime(2026, 1, 15, hour, 0, tzinfo=timezone.utc) + timedelta(days=day_offset)


# --- today_strip / this_week -------------------------------------------

def test_today_strip_counts_new_and_follow_up_sends(conn):
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, timestamp=_ts(0))
    append_event(conn, type="requeued", recipient="b@x.com", campaign="c", stage=1,
                 gmass_campaign_id="1", timestamp=_ts(0))
    strip = today_strip(conn, make_global_config(daily_cap=10), today=date(2026, 1, 15))
    assert strip["sent"] == {"new": 1, "follow_up": 1, "total": 2}
    assert strip["cap_used_pct"] == 20


def test_today_strip_ignores_other_days(conn):
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, timestamp=_ts(-1))
    strip = today_strip(conn, make_global_config(), today=date(2026, 1, 15))
    assert strip["sent"]["total"] == 0


def test_today_strip_counts_replies_and_clicks(conn):
    append_event(conn, type="reply", recipient="a@x.com", campaign="c", timestamp=_ts(0))
    append_event(conn, type="click", recipient="a@x.com", campaign="c", timestamp=_ts(0))
    append_event(conn, type="click", recipient="a@x.com", campaign="c", timestamp=_ts(0))
    strip = today_strip(conn, make_global_config(), today=date(2026, 1, 15))
    assert strip["replies_today"] == 1
    assert strip["clicks_today"] == 2


def test_count_events_on_uses_local_calendar_day_not_utc(conn):
    # Event timestamps are always stored UTC (§5); bucketing by "day" for
    # display must convert to local first — otherwise a send made late at
    # night local time can land in the wrong day's panel. Uses the test
    # machine's REAL local timezone (via .astimezone()) rather than a
    # hardcoded offset, so this is meaningful wherever it runs.
    from slap.dashboard import _count_events_on

    utc_ts = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)  # 2am UTC
    naive_utc_day = utc_ts.date()
    actual_local_day = utc_ts.astimezone().date()
    append_event(conn, type="reply", recipient="a@x.com", campaign="c", timestamp=utc_ts)

    assert _count_events_on(conn, "reply", actual_local_day) == 1
    if naive_utc_day != actual_local_day:
        assert _count_events_on(conn, "reply", naive_utc_day) == 0


def test_today_strip_cap_gauge_includes_followups_firing_today(conn):
    # §8: the daily-cap gauge must include follow-ups firing today, not just
    # events already sent — driving it off runner.cap_headroom (the same
    # function the runner itself uses to decide whether it still has room
    # to send) is what guarantees the two never disagree.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=_ts(-2))
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, timestamp=_ts(-2))
    # recruiter cadence [2,3,5]: stage 0 sent on day -2 -> stage 1 fires day 0 (today).
    strip = today_strip(conn, make_global_config(daily_cap=10), today=date(2026, 1, 15))
    assert strip["sent"]["total"] == 0  # nothing has actually SENT today yet
    assert strip["cap_used_pct"] == 10  # but the gauge reserves headroom for the estimated follow-up


def test_today_strip_active_campaigns_uses_injected_dir(tmp_path, conn):
    (tmp_path / "campaigns" / "coldpost-a").mkdir(parents=True)
    (tmp_path / "campaigns" / "coldpost-a" / "campaign.yaml").write_text("persona: recruiter\n")
    strip = today_strip(conn, make_global_config(), today=date(2026, 1, 15),
                        campaigns_dir=tmp_path / "campaigns")
    assert strip["active_campaigns"] == ["coldpost-a"]


def test_this_week_includes_a_7_day_rolling_window(conn):
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, timestamp=_ts(-6))
    append_event(conn, type="sent", recipient="b@x.com", campaign="c", stage=0, timestamp=_ts(-7))  # outside window
    week = this_week(conn, today=date(2026, 1, 15))
    assert week["sent"]["total"] == 1
    assert week["range_start"] == date(2026, 1, 9)


# --- engagement_intelligence ---------------------------------------------

def test_reply_rate_by_persona(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="queued", recipient="b@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    result = engagement_intelligence(conn)
    assert result["reply_rate_by_persona"]["recruiter"] == 50.0


def test_reply_rate_by_persona_survives_a_later_ooo_resend(conn):
    # A repliER whose status later flips back to 'active' via requeued must
    # still count as "replied" for the rate — status alone would undercount.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c")
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c", stage=1, gmass_campaign_id="1")
    result = engagement_intelligence(conn)
    assert result["reply_rate_by_persona"]["recruiter"] == 100.0


def test_reply_and_click_by_stage(conn):
    append_event(conn, type="reply", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="reply", recipient="b@x.com", campaign="c", stage=1)
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0)
    result = engagement_intelligence(conn)
    assert result["reply_by_stage"] == {0: 1, 1: 1}
    assert result["click_by_stage"] == {0: 1}


def test_time_to_first_reply_distribution_buckets(conn):
    append_event(conn, type="queued", recipient="same_day@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=_ts(0))
    append_event(conn, type="sent", recipient="same_day@x.com", campaign="c", stage=0, timestamp=_ts(0, 9))
    append_event(conn, type="reply", recipient="same_day@x.com", campaign="c", timestamp=_ts(0, 15))

    append_event(conn, type="queued", recipient="week_later@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=_ts(0))
    append_event(conn, type="sent", recipient="week_later@x.com", campaign="c", stage=0, timestamp=_ts(0))
    append_event(conn, type="reply", recipient="week_later@x.com", campaign="c", timestamp=_ts(10))

    result = engagement_intelligence(conn)
    assert result["time_to_first_reply"]["same_day"] == 1
    assert result["time_to_first_reply"]["8_plus_days"] == 1


# --- needs_triage / actionable_replies / tag_reply --------------------

def test_needs_triage_includes_unresolved_reply(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    assert [r["recipient"] for r in needs_triage(conn)] == ["a@x.com"]


def test_needs_triage_excludes_reviewed_reply(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    append_event(conn, type="reply_reviewed", recipient="a@x.com", campaign="c", meta={"tag": "real"})
    assert needs_triage(conn) == []


def test_needs_triage_excludes_ooo_tagged_reply(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c")
    assert needs_triage(conn) == []


def test_needs_triage_reincludes_after_a_second_untagged_reply(conn):
    # A second reply cycle (e.g. after an OOO resend) that hasn't been
    # triaged yet must show up again, even though the FIRST reply was tagged.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c")
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c", stage=1, gmass_campaign_id="1")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    assert [r["recipient"] for r in needs_triage(conn)] == ["a@x.com"]


def test_actionable_replies_includes_domain_context(conn):
    append_event(conn, type="queued", recipient="jane@acme.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="jane@acme.com", campaign="c")
    replies = actionable_replies(conn, consumer_domains=set())
    assert len(replies) == 1
    assert replies[0]["dedup_context"].hard_warning is not None  # already-contacted context


def test_tag_reply_ooo_calls_the_real_requeue_mechanism(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "ooo")
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "ooo_requeued"
    assert needs_triage(conn) == []  # resolved


def test_tag_reply_real_writes_reply_reviewed(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "real")
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'reply_reviewed'")]
    assert len(events) == 1
    assert needs_triage(conn) == []


def test_tag_reply_not_interested_writes_reply_reviewed(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "not_interested")
    assert needs_triage(conn) == []


def test_tag_reply_rejects_unknown_tag(conn):
    with pytest.raises(ValueError, match="unknown tag"):
        tag_reply(conn, "a@x.com", "bogus")


# --- pipeline ---------------------------------------------------------

def test_pipeline_groups_active_recipients_by_stage(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="queued", recipient="b@x.com", campaign="c", stage=1, meta={"persona": "recruiter"})
    result = pipeline(conn, make_global_config())
    assert result["mid_sequence_by_stage"] == {0: ["a@x.com"], 1: ["b@x.com"]}


def test_pipeline_followups_scheduled_today_and_tomorrow(conn):
    # recruiter cadence [2,3,5]: stage 1 fires 2 days after the initial send.
    # today = Mar 1. today@x.com sent Feb 27 -> fires Mar 1 (today).
    # tomorrow@x.com sent Feb 28 -> fires Mar 2 (tomorrow).
    sent_today_target = datetime(2026, 2, 27, 10, 0, tzinfo=timezone.utc)
    sent_tomorrow_target = datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc)
    append_event(conn, type="queued", recipient="today@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=sent_today_target)
    append_event(conn, type="sent", recipient="today@x.com", campaign="c", stage=0,
                 timestamp=sent_today_target)

    append_event(conn, type="queued", recipient="tomorrow@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"}, timestamp=sent_tomorrow_target)
    append_event(conn, type="sent", recipient="tomorrow@x.com", campaign="c", stage=0,
                 timestamp=sent_tomorrow_target)

    result = pipeline(conn, make_global_config(), today=date(2026, 3, 1))
    assert [e["recipient"] for e in result["followups_scheduled"]["today"]] == ["today@x.com"]
    assert [e["recipient"] for e in result["followups_scheduled"]["tomorrow"]] == ["tomorrow@x.com"]


# --- todays_runs --------------------------------------------------------

def test_todays_runs_pairs_started_with_completed(conn):
    append_event(conn, type="run_started", timestamp=_ts(0))
    append_event(conn, type="run_completed", meta={"sent": 3, "failed": 1, "remaining_queued": 2},
                 timestamp=_ts(0, 11))
    result = todays_runs(conn, today=date(2026, 1, 15))
    assert len(result["runs"]) == 1
    run = result["runs"][0]
    assert run["sent"] == 3
    assert run["failed"] == 1
    assert run["still_queued"] == 2
    assert run["run_failed"] is False


def test_todays_runs_shows_run_failed_prominently(conn):
    append_event(conn, type="run_failed", meta={"error": "no api key", "retry_count": 3}, timestamp=_ts(0))
    result = todays_runs(conn, today=date(2026, 1, 15))
    assert result["runs"][0]["run_failed"] is True
    assert result["runs"][0]["error"] == "no api key"
    assert result["runs"][0]["retry_count"] == 3


def test_todays_runs_excludes_other_days(conn):
    append_event(conn, type="run_started", timestamp=_ts(-1))
    result = todays_runs(conn, today=date(2026, 1, 15))
    assert result["runs"] == []


def test_todays_runs_current_queue_depth(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    result = todays_runs(conn, today=date(2026, 1, 15))
    assert result["current_queue_depth"] == 1


# --- create_app: Flask routes ----------------------------------------------
# create_app() takes a db_path, not an open connection — each request opens
# its own sqlite3 connection (see slap/dashboard.py's create_app docstring).
# Flask's test_client() dispatches synchronously on the calling thread, so it
# cannot reproduce the original bug (a connection opened on one thread being
# reused on another, which sqlite3 rejects) — that needs a real running
# server with real request threads, covered separately below.

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # today_strip() calls discover_campaigns() against cwd
    db_path = tmp_path / "test.db"
    connect(db_path).close()
    return create_app(db_path, make_global_config(), consumer_domains=set(), api_key="fake-key")


def test_create_app_index_renders(app):
    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        client = app.test_client()
        resp = client.get("/")
    assert resp.status_code == 200
    assert b"slap dashboard" in resp.data
    assert b"Nothing needs triage." in resp.data


def test_create_app_index_survives_repeated_requests(app):
    # Each request must open (and tear down) its own connection cleanly —
    # a regression guard for accidentally going back to one shared conn.
    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        client = app.test_client()
        for _ in range(3):
            resp = client.get("/")
            assert resp.status_code == 200


def test_create_app_reply_tag_real_resolves_triage_and_redirects(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="carol@x.com", campaign="c", stage=0,
                 meta={"persona": "founder"})
    append_event(conn, type="sent", recipient="carol@x.com", campaign="c", stage=0, gmass_campaign_id="1")
    append_event(conn, type="reply", recipient="carol@x.com", campaign="c", stage=0)
    conn.close()

    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        client = app.test_client()
        resp = client.post("/reply/carol@x.com/tag", data={"tag": "real"})
        assert resp.status_code == 302

        follow = client.get("/")
    assert b"Nothing needs triage." in follow.data


def test_create_app_reply_tag_invalid_returns_400(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="carol@x.com", campaign="c", stage=0,
                 meta={"persona": "founder"})
    append_event(conn, type="sent", recipient="carol@x.com", campaign="c", stage=0, gmass_campaign_id="1")
    append_event(conn, type="reply", recipient="carol@x.com", campaign="c", stage=0)
    conn.close()

    client = app.test_client()
    resp = client.post("/reply/carol@x.com/tag", data={"tag": "bogus"})
    assert resp.status_code == 400


@pytest.mark.slow
def test_dashboard_survives_real_concurrent_request_threads(tmp_path, monkeypatch):
    # Regression test for a real bug caught only via manual browser
    # verification: create_app() used to close over one sqlite3 connection
    # opened at app-creation time. Flask's real dev server (and any real
    # WSGI server) dispatches each request on its own thread, and sqlite3
    # connections are only usable on the thread that created them
    # (check_same_thread defaults to True) — every request after the first
    # one handled on a different thread raised sqlite3.ProgrammingError.
    # Flask's test_client() alone can't catch this since it runs requests
    # synchronously on the calling thread; this spins up a real
    # threaded server on a real socket to prove requests from different
    # threads all succeed.
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "test.db"
    connect(db_path).close()
    app = create_app(db_path, make_global_config(), consumer_domains=set(), api_key="fake-key")

    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        server = make_server("127.0.0.1", 0, app, threaded=True)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            statuses = []
            for _ in range(5):
                try:
                    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/")
                    statuses.append(resp.status)
                except urllib.error.HTTPError as e:
                    statuses.append(e.code)
            assert statuses == [200, 200, 200, 200, 200]
        finally:
            server.shutdown()
            thread.join()
