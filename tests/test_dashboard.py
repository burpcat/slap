"""Dashboard tests (Build Order step 11), per SLAP_BUILD_PROMPT.md §8:
on-open poll writes new events; panels render from tracking data;
reply-tag -> OOO -> re-queue fires a send-as-reply.
"""
import json
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
    _clicked_recipients, _recipient_drop_meta, actionable_replies, bounces, companies_contacted,
    create_app, engagement_intelligence, filter_reachouts, needs_triage, next_drain, pipeline,
    reachouts_rows, reply_tags, sync_reports, tag_reply, this_week, today_strip, todays_runs,
    warm_but_silent,
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
                                 daily_cap=daily_cap, drain_retries=drain_retries,
                                 active_days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"]),
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


def test_sync_reports_records_a_block_that_bounces_alone_would_miss(conn):
    # Reproduces the actual reported bug: GMass classifies a delivery
    # failure as a BLOCK (a separate report category/endpoint from
    # bounces, with its own blockReason/blockTime fields), and the owner
    # saw it as a real failure but the dashboard never recorded it — because
    # sync_reports() used to poll only /bounces, never /blocks. If this test
    # only mocked "bounces" and left "blocks" returning [], it would pass
    # even with the bug still present — it must return a REAL item for
    # "blocks" and nothing for "bounces" to actually exercise the gap.
    seed_sent_recipient(conn, recipient="blocked@acme.com")
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if report_type == "blocks":
                return [{"emailAddress": "blocked@acme.com", "blockReason": "554 rejected", "blockTime": "t1"}]
            return []  # bounces (and everything else) genuinely empty for this recipient
        mock_get.side_effect = side_effect
        result = sync_reports(conn, "fake-key")

    assert result["new_bounces"] == 1  # combined bounce+block counter — see sync_reports()'s docstring
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'bounce'")]
    assert len(events) == 1
    meta = json.loads(events[0]["meta"])
    assert meta["category"] == "block"
    assert meta["bounce_reason"] == "554 rejected"


def test_sync_reports_dedupes_blocks_by_reason_and_time(conn):
    seed_sent_recipient(conn)
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if report_type == "blocks":
                return [{"emailAddress": "jane@acme.com", "blockReason": "security policy", "blockTime": "t1"}]
            return []
        mock_get.side_effect = side_effect
        r1 = sync_reports(conn, "fake-key")
        r2 = sync_reports(conn, "fake-key")

    assert r1["new_bounces"] == 1
    assert r2["new_bounces"] == 0


def test_sync_reports_records_both_a_bounce_and_a_block_for_different_recipients(conn):
    seed_sent_recipient(conn, recipient="bounced@x.com", campaign_id="1")
    seed_sent_recipient(conn, recipient="blocked@x.com", campaign_id="2")
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        def side_effect(api_key, cid, report_type):
            if cid == "1" and report_type == "bounces":
                return [{"emailAddress": "bounced@x.com", "bounceReason": "mailbox full", "bounceTime": "t1"}]
            if cid == "2" and report_type == "blocks":
                return [{"emailAddress": "blocked@x.com", "blockReason": "spam policy", "blockTime": "t2"}]
            return []
        mock_get.side_effect = side_effect
        result = sync_reports(conn, "fake-key")

    assert result["new_bounces"] == 2
    events = {r["recipient"]: json.loads(r["meta"])["category"]
              for r in conn.execute("SELECT recipient, meta FROM events WHERE type = 'bounce'")}
    assert events == {"bounced@x.com": "bounce", "blocked@x.com": "block"}


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
    assert len(result["errors"]) == 4  # one per report type (replies/clicks/bounces/blocks) polled for cid=1
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


def test_engagement_intelligence_has_data_false_when_nothing_happened_yet(conn):
    # Before any campaign activity, the three sub-tables would otherwise
    # show four rows of zeros — has_data lets the template collapse them to
    # a single honest "no data yet" line instead.
    result = engagement_intelligence(conn)
    assert result["has_data"] is False


def test_engagement_intelligence_has_data_true_once_a_persona_has_been_contacted(conn):
    # Even a 0% reply rate is real data once someone's actually been
    # contacted — not a fabricated placeholder.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    result = engagement_intelligence(conn)
    assert result["has_data"] is True
    assert result["reply_rate_by_persona"]["recruiter"] == 0.0


def test_engagement_intelligence_has_data_true_from_a_click_alone(conn):
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0)
    result = engagement_intelligence(conn)
    assert result["has_data"] is True


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


def test_todays_runs_excludes_zero_activity_completed_runs(conn):
    # A drain that found nothing to do (sent=0, failed=0, nothing left
    # queued) is a passive no-op — it shouldn't clutter the runs list.
    append_event(conn, type="run_started", timestamp=_ts(0))
    append_event(conn, type="run_completed", meta={"sent": 0, "failed": 0, "remaining_queued": 0},
                 timestamp=_ts(0) + timedelta(minutes=1))
    result = todays_runs(conn, today=date(2026, 1, 15))
    assert result["runs"] == []


def test_todays_runs_keeps_run_with_any_real_activity(conn):
    append_event(conn, type="run_started", timestamp=_ts(0))
    append_event(conn, type="run_completed", meta={"sent": 0, "failed": 0, "remaining_queued": 3},
                 timestamp=_ts(0) + timedelta(minutes=1))
    result = todays_runs(conn, today=date(2026, 1, 15))
    assert len(result["runs"]) == 1  # still_queued=3 is real information, not a no-op


def test_todays_runs_keeps_run_failed_even_though_counts_are_zero_like(conn):
    # run_failed entries never got real sent/failed/queued counts (they're
    # None, not 0) — but even so, a failure must never be filtered as a
    # "zero-activity" no-op.
    append_event(conn, type="run_failed", meta={"error": "no api key", "retry_count": 3}, timestamp=_ts(0))
    result = todays_runs(conn, today=date(2026, 1, 15))
    assert len(result["runs"]) == 1
    assert result["runs"][0]["run_failed"] is True


def test_todays_runs_caps_to_last_8_with_earlier_count(conn):
    for i in range(10):
        started_at = _ts(0) + timedelta(minutes=2 * i)
        append_event(conn, type="run_started", timestamp=started_at)
        append_event(conn, type="run_completed", meta={"sent": 1, "failed": 0, "remaining_queued": 0},
                     timestamp=started_at + timedelta(minutes=1))
    result = todays_runs(conn, today=date(2026, 1, 15))
    assert len(result["runs"]) == 8
    assert result["earlier_count"] == 2


def test_todays_runs_earlier_count_zero_when_under_the_cap(conn):
    append_event(conn, type="run_started", timestamp=_ts(0))
    append_event(conn, type="run_completed", meta={"sent": 1, "failed": 0, "remaining_queued": 0},
                 timestamp=_ts(0, 1))
    result = todays_runs(conn, today=date(2026, 1, 15))
    assert result["earlier_count"] == 0


# --- warm_but_silent / bounces / companies_contacted / next_drain ---------

def test_warm_but_silent_lists_clicked_but_not_replied(conn):
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0)
    result = warm_but_silent(conn)
    assert len(result) == 1
    assert result[0]["recipient"] == "a@x.com"
    assert result[0]["campaign"] == "c"
    assert result[0]["stages_clicked"] == [0]


def test_warm_but_silent_excludes_recipients_who_replied(conn):
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    assert warm_but_silent(conn) == []


def test_warm_but_silent_excludes_a_recipient_who_replied_after_a_later_ooo_cycle(conn):
    # Once replied at all, never "silent" again — even if a later OOO resend
    # reopened their sequence and they clicked a follow-up link too.
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c")
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c", stage=1, gmass_campaign_id="1")
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=1)
    assert warm_but_silent(conn) == []


def test_warm_but_silent_dedupes_multiple_stage_clicks_for_one_recipient(conn):
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0)
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=1)
    result = warm_but_silent(conn)
    assert len(result) == 1
    assert result[0]["stages_clicked"] == [0, 1]


def test_warm_but_silent_never_includes_a_literal_none_stage(conn):
    # A click's stage is None only if the recipient wasn't yet in the
    # recipients cache at sync time (shouldn't happen in practice) — must
    # never render a literal "None" in the stages-clicked list either way.
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=None)
    result = warm_but_silent(conn)
    assert len(result) == 1
    assert result[0]["stages_clicked"] == []


def test_warm_but_silent_empty_when_no_clicks(conn):
    assert warm_but_silent(conn) == []


def test_bounces_lists_bounced_recipients(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c")
    result = bounces(conn)
    assert len(result) == 1
    assert result[0]["recipient"] == "a@x.com"


def test_bounces_empty_when_none(conn):
    assert bounces(conn) == []


def test_bounces_excludes_non_bounced_recipients(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0)
    assert bounces(conn) == []


def test_bounces_distinguishes_bounce_from_block_category(conn):
    # The widget must not blend the two into an indistinguishable list —
    # each row reports which GMass category it actually was.
    append_event(conn, type="queued", recipient="bounced@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="bounce", recipient="bounced@x.com", campaign="c",
                 meta={"bounce_reason": "mailbox full", "bounce_time": "t1", "category": "bounce"})

    append_event(conn, type="queued", recipient="blocked@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="bounce", recipient="blocked@x.com", campaign="c",
                 meta={"bounce_reason": "spam policy", "bounce_time": "t2", "category": "block"})

    result = {r["recipient"]: r["category"] for r in bounces(conn)}
    assert result == {"bounced@x.com": "bounce", "blocked@x.com": "block"}


def test_bounces_defaults_category_to_bounce_for_pre_existing_events_without_it(conn):
    # Backward compatibility: a bounce event recorded before `category`
    # existed (e.g. the real, already-recorded bounce this investigation
    # started from) has no such meta key at all — must default to
    # "bounce", never crash, never guess "block".
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c",
                 meta={"bounce_reason": "mailbox full", "bounce_time": "t1"})  # no "category" key
    result = bounces(conn)
    assert result[0]["category"] == "bounce"


def test_companies_contacted_counts_distinct_non_consumer_domains(conn):
    append_event(conn, type="queued", recipient="a@acme.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="a@acme.com", campaign="c", stage=0, timestamp=_ts(0))
    append_event(conn, type="queued", recipient="b@acme.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="b@acme.com", campaign="c", stage=0, timestamp=_ts(0))
    append_event(conn, type="queued", recipient="c@other.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="c@other.com", campaign="c", stage=0, timestamp=_ts(0))

    result = companies_contacted(conn, consumer_domains=set(), today=date(2026, 1, 15))
    assert result["all_time_count"] == 2  # acme.com + other.com, not 3 people
    assert ("acme.com", 2) in result["top_companies"]


def test_companies_contacted_excludes_consumer_domains(conn):
    append_event(conn, type="queued", recipient="a@gmail.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="a@gmail.com", campaign="c", stage=0, timestamp=_ts(0))
    result = companies_contacted(conn, consumer_domains={"gmail.com"}, today=date(2026, 1, 15))
    assert result["all_time_count"] == 0


def test_companies_contacted_excludes_merely_staged_never_sent(conn):
    # A "queued" event alone (never actually sent) must not count as
    # "contacted" — that would overstate real outreach.
    append_event(conn, type="queued", recipient="a@acme.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    result = companies_contacted(conn, consumer_domains=set(), today=date(2026, 1, 15))
    assert result["all_time_count"] == 0


def test_companies_contacted_this_week_vs_all_time(conn):
    append_event(conn, type="queued", recipient="old@acme.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="old@acme.com", campaign="c", stage=0, timestamp=_ts(-30))
    append_event(conn, type="queued", recipient="new@other.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="new@other.com", campaign="c", stage=0, timestamp=_ts(0))

    result = companies_contacted(conn, consumer_domains=set(), today=date(2026, 1, 15))
    assert result["all_time_count"] == 2
    assert result["this_week_count"] == 1


def test_companies_contacted_empty_when_nothing_sent(conn):
    result = companies_contacted(conn, consumer_domains=set(), today=date(2026, 1, 15))
    assert result == {"all_time_count": 0, "this_week_count": 0, "top_companies": []}


def test_next_drain_reports_window_and_queue_depth(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    result = next_drain(conn, make_global_config())
    assert result["fire_window_start"] == "09:00"
    assert result["fire_window_end"] == "09:15"
    assert result["queue_depth"] == 1


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


# --- Reach-outs: all-campaigns, filterable, read-only recipient table ------

def _stage_and_send(conn, *, recipient, campaign, persona, company="", role="", req_id="",
                     timestamp=None, send=True):
    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": persona, "company": company, "role": role, "req_id": req_id},
                 timestamp=timestamp)
    if send:
        append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0,
                     gmass_campaign_id="1", timestamp=timestamp)


# --- _clicked_recipients ----------------------------------------------------

def test_clicked_recipients_includes_anyone_with_a_click_event(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0)
    assert _clicked_recipients(conn) == {"a@x.com"}


def test_clicked_recipients_empty_when_no_clicks(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    assert _clicked_recipients(conn) == set()


def test_clicked_recipients_agrees_with_warm_but_silent_on_who_clicked(conn):
    # Cross-consistency pin (see _clicked_recipients()'s own docstring):
    # warm_but_silent() is a STRICT SUBSET of "has clicked" (clicked minus
    # already-replied), not the same set, so the real invariant isn't
    # equality — it's that every recipient warm_but_silent() reports as
    # having clicked genuinely IS in _clicked_recipients()'s set too, i.e.
    # the two can never disagree about whether a click happened, only about
    # whether a later reply excludes someone from the "still silent" view.
    _stage_and_send(conn, recipient="silent@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="click", recipient="silent@x.com", campaign="c", stage=0)

    _stage_and_send(conn, recipient="clicked_then_replied@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="click", recipient="clicked_then_replied@x.com", campaign="c", stage=0)
    append_event(conn, type="reply", recipient="clicked_then_replied@x.com", campaign="c")

    clicked = _clicked_recipients(conn)
    warm = {w["recipient"] for w in warm_but_silent(conn)}

    assert clicked == {"silent@x.com", "clicked_then_replied@x.com"}
    assert warm == {"silent@x.com"}  # excluded for having replied, not for a click disagreement
    assert warm <= clicked  # every "warm but silent" recipient is also a "clicked" recipient


# --- reply_tags --------------------------------------------------------------

def test_reply_tags_untagged_for_unresolved_reply(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    assert reply_tags(conn) == {"a@x.com": "untagged"}


def test_reply_tags_real_and_not_interested_from_reply_reviewed(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    append_event(conn, type="reply_reviewed", recipient="a@x.com", campaign="c", meta={"tag": "real"})

    _stage_and_send(conn, recipient="b@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="b@x.com", campaign="c")
    append_event(conn, type="reply_reviewed", recipient="b@x.com", campaign="c", meta={"tag": "not_interested"})

    assert reply_tags(conn) == {"a@x.com": "real", "b@x.com": "not_interested"}


def test_reply_tags_ooo_from_ooo_tagged(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c")
    assert reply_tags(conn) == {"a@x.com": "ooo"}


def test_reply_tags_absent_for_recipient_who_never_replied(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    assert reply_tags(conn) == {}


def test_reply_tags_agrees_with_needs_triage_on_who_is_still_open(conn):
    # Cross-consistency pin (see reply_tags()'s own docstring): a recipient
    # in needs_triage()'s result must map to 'untagged' here, and a
    # recipient reply_tags() reports as anything OTHER than 'untagged' must
    # NOT appear in needs_triage() — the two must never silently disagree
    # about which replies are still open. Covers every resolution path
    # (plain open reply, reviewed-and-closed, OOO-tagged, and REOPENED after
    # a prior review) since those are exactly where the two independently
    # implemented resolution rules — needs_triage()'s SQL self-join with
    # NOT EXISTS vs. reply_tags()'s Python "latest wins" walk — could
    # silently diverge; a single plain-open-vs-resolved case wouldn't catch
    # an ordering bug in either one.
    _stage_and_send(conn, recipient="open@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="open@x.com", campaign="c")

    _stage_and_send(conn, recipient="resolved@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="resolved@x.com", campaign="c")
    append_event(conn, type="reply_reviewed", recipient="resolved@x.com", campaign="c", meta={"tag": "real"})

    _stage_and_send(conn, recipient="ooo@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="ooo@x.com", campaign="c")
    append_event(conn, type="ooo_tagged", recipient="ooo@x.com", campaign="c")

    _stage_and_send(conn, recipient="reopened@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="reopened@x.com", campaign="c")
    append_event(conn, type="reply_reviewed", recipient="reopened@x.com", campaign="c", meta={"tag": "real"})
    append_event(conn, type="reply", recipient="reopened@x.com", campaign="c")

    triage_recipients = {r["recipient"] for r in needs_triage(conn)}
    tags = reply_tags(conn)

    assert triage_recipients == {"open@x.com", "reopened@x.com"}
    for recipient in triage_recipients:
        assert tags[recipient] == "untagged"
    for recipient in ("resolved@x.com", "ooo@x.com"):
        assert tags[recipient] != "untagged"
        assert recipient not in triage_recipients
    assert tags["ooo@x.com"] == "ooo"
    assert tags["resolved@x.com"] == "real"


# --- _recipient_drop_meta -----------------------------------------------------

def test_recipient_drop_meta_reads_latest_queued_event(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter",
                     company="Acme", role="Backend Engineer", req_id="REQ-1")
    assert _recipient_drop_meta(conn)["a@x.com"] == {
        "company": "Acme", "role": "Backend Engineer", "req_id": "REQ-1",
    }


def test_recipient_drop_meta_blank_for_a_queued_event_without_these_keys(conn):
    # Backward compatibility: a queued event written before this capture
    # existed simply has no company/role/req_id keys in its meta at all.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    assert _recipient_drop_meta(conn)["a@x.com"] == {"company": "", "role": "", "req_id": ""}


def test_recipient_drop_meta_uses_the_most_recent_queued_event(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c1", persona="recruiter", company="Old Co")
    _stage_and_send(conn, recipient="a@x.com", campaign="c2", persona="founder", company="New Co")
    assert _recipient_drop_meta(conn)["a@x.com"]["company"] == "New Co"


# --- reachouts_rows ------------------------------------------------------------

def test_reachouts_rows_spans_all_three_campaigns_with_no_filter(conn):
    _stage_and_send(conn, recipient="jane@acme.com", campaign="coldpost-founder", persona="founder",
                     company="Acme", role="Founding Engineer")
    _stage_and_send(conn, recipient="bob@widgets.com", campaign="coldpost-recruiter", persona="recruiter",
                     company="Widgets Inc", role="Backend Engineer", req_id="REQ-123")
    _stage_and_send(conn, recipient="carol@other.com", campaign="linkpost-hiringmanager",
                     persona="hiring_manager", send=False)

    rows = {r["recipient"]: r for r in reachouts_rows(conn)}

    assert set(rows) == {"jane@acme.com", "bob@widgets.com", "carol@other.com"}
    assert rows["jane@acme.com"]["campaign"] == "coldpost-founder"
    assert rows["bob@widgets.com"]["campaign"] == "coldpost-recruiter"
    assert rows["carol@other.com"]["campaign"] == "linkpost-hiringmanager"


def test_reachouts_rows_status_queued_vs_active(conn):
    _stage_and_send(conn, recipient="sent@x.com", campaign="c", persona="recruiter", send=True)
    _stage_and_send(conn, recipient="queued@x.com", campaign="c", persona="recruiter", send=False)

    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["sent@x.com"]["status"] == "active"
    assert rows["queued@x.com"]["status"] == "queued"


def test_reachouts_rows_engagement_replied_beats_clicked(conn):
    _stage_and_send(conn, recipient="clicked@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="click", recipient="clicked@x.com", campaign="c", stage=0)

    _stage_and_send(conn, recipient="replied@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="click", recipient="replied@x.com", campaign="c", stage=0)
    append_event(conn, type="reply", recipient="replied@x.com", campaign="c")

    _stage_and_send(conn, recipient="neither@x.com", campaign="c", persona="recruiter")

    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["clicked@x.com"]["engagement"] == "clicked"
    assert rows["replied@x.com"]["engagement"] == "replied"
    assert rows["neither@x.com"]["engagement"] == "none"


def test_reachouts_rows_domain_company_and_req_id_present(conn):
    _stage_and_send(conn, recipient="jane@acme.com", campaign="c", persona="recruiter",
                     company="Acme", req_id="REQ-1")
    _stage_and_send(conn, recipient="bob@x.com", campaign="c", persona="recruiter")

    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["jane@acme.com"]["domain"] == "acme.com"
    assert rows["jane@acme.com"]["company"] == "Acme"
    assert rows["jane@acme.com"]["req_id_present"] is True
    assert rows["bob@x.com"]["company"] == ""
    assert rows["bob@x.com"]["req_id_present"] is False


def test_reachouts_rows_date_falls_back_to_last_event_at_when_never_sent(conn):
    ts = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    _stage_and_send(conn, recipient="queued@x.com", campaign="c", persona="recruiter",
                     send=False, timestamp=ts)
    row = reachouts_rows(conn)[0]
    assert row["date"] is not None
    assert row["date_local"] == ts.astimezone().date().isoformat()


def test_reachouts_rows_reply_tag_none_when_never_replied(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    assert reachouts_rows(conn)[0]["reply_tag"] is None


# --- filter_reachouts (pure function — no DB needed) --------------------------

def _row(**overrides):
    base = {
        "recipient": "jane@acme.com", "campaign": "coldpost-recruiter", "persona": "recruiter",
        "status": "active", "engagement": "none", "reply_tag": None, "domain": "acme.com",
        "company": "Acme", "req_id_present": False, "date": "2026-06-15T00:00:00+00:00",
        "date_local": "2026-06-15",
    }
    base.update(overrides)
    return base


def test_filter_reachouts_no_filters_returns_everything():
    rows = [_row(recipient="a@x.com"), _row(recipient="b@x.com")]
    assert filter_reachouts(rows, {}) == rows


def test_filter_reachouts_by_campaign():
    rows = [_row(recipient="a@x.com", campaign="coldpost-founder"), _row(recipient="b@x.com", campaign="coldpost-recruiter")]
    result = filter_reachouts(rows, {"campaign": "coldpost-founder"})
    assert [r["recipient"] for r in result] == ["a@x.com"]


def test_filter_reachouts_by_persona():
    rows = [_row(recipient="a@x.com", persona="founder"), _row(recipient="b@x.com", persona="recruiter")]
    result = filter_reachouts(rows, {"persona": "founder"})
    assert [r["recipient"] for r in result] == ["a@x.com"]


def test_filter_reachouts_by_status():
    rows = [_row(recipient="a@x.com", status="bounced"), _row(recipient="b@x.com", status="active")]
    result = filter_reachouts(rows, {"status": "bounced"})
    assert [r["recipient"] for r in result] == ["a@x.com"]


def test_filter_reachouts_by_engagement():
    rows = [_row(recipient="a@x.com", engagement="clicked"), _row(recipient="b@x.com", engagement="none")]
    result = filter_reachouts(rows, {"engagement": "clicked"})
    assert [r["recipient"] for r in result] == ["a@x.com"]


def test_filter_reachouts_by_reply_tag():
    rows = [_row(recipient="a@x.com", reply_tag="real"), _row(recipient="b@x.com", reply_tag="untagged"),
            _row(recipient="c@x.com", reply_tag=None)]
    result = filter_reachouts(rows, {"reply_tag": "untagged"})
    assert [r["recipient"] for r in result] == ["b@x.com"]


def test_filter_reachouts_by_domain():
    rows = [_row(recipient="a@acme.com", domain="acme.com"), _row(recipient="b@widgets.com", domain="widgets.com")]
    result = filter_reachouts(rows, {"domain": "widgets.com"})
    assert [r["recipient"] for r in result] == ["b@widgets.com"]


def test_filter_reachouts_by_req_id_present():
    rows = [_row(recipient="a@x.com", req_id_present=True), _row(recipient="b@x.com", req_id_present=False)]
    assert [r["recipient"] for r in filter_reachouts(rows, {"req_id_present": True})] == ["a@x.com"]
    assert [r["recipient"] for r in filter_reachouts(rows, {"req_id_present": False})] == ["b@x.com"]


def test_filter_reachouts_by_date_range():
    rows = [_row(recipient="early@x.com", date_local="2026-06-01"),
            _row(recipient="mid@x.com", date_local="2026-06-15"),
            _row(recipient="late@x.com", date_local="2026-06-30")]
    result = filter_reachouts(rows, {"date_start": "2026-06-10", "date_end": "2026-06-20"})
    assert [r["recipient"] for r in result] == ["mid@x.com"]


def test_filter_reachouts_by_search_matches_recipient_or_company():
    rows = [_row(recipient="jane@acme.com", company="Acme"), _row(recipient="bob@widgets.com", company="Widgets Inc")]
    assert [r["recipient"] for r in filter_reachouts(rows, {"search": "acme"})] == ["jane@acme.com"]
    assert [r["recipient"] for r in filter_reachouts(rows, {"search": "widgets"})] == ["bob@widgets.com"]
    assert [r["recipient"] for r in filter_reachouts(rows, {"search": "bob"})] == ["bob@widgets.com"]


def test_filter_reachouts_combines_with_and_not_or():
    rows = [
        _row(recipient="a@x.com", campaign="coldpost-founder", status="bounced"),
        _row(recipient="b@x.com", campaign="coldpost-founder", status="active"),
        _row(recipient="c@x.com", campaign="coldpost-recruiter", status="bounced"),
    ]
    result = filter_reachouts(rows, {"campaign": "coldpost-founder", "status": "bounced"})
    assert [r["recipient"] for r in result] == ["a@x.com"]


def test_filter_reachouts_no_match_returns_empty_list():
    rows = [_row(recipient="a@x.com", campaign="coldpost-founder")]
    assert filter_reachouts(rows, {"campaign": "coldpost-recruiter"}) == []


# --- create_app: /reachouts route --------------------------------------------

def test_create_app_reachouts_empty_state_when_no_recipients(app):
    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        client = app.test_client()
        resp = client.get("/reachouts")
    assert resp.status_code == 200
    assert b"No reach-outs yet" in resp.data


def test_create_app_reachouts_renders_rows_across_campaigns(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    _stage_and_send(conn, recipient="jane@acme.com", campaign="coldpost-founder", persona="founder",
                     company="Acme")
    _stage_and_send(conn, recipient="bob@widgets.com", campaign="coldpost-recruiter", persona="recruiter")
    conn.close()

    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        client = app.test_client()
        resp = client.get("/reachouts")

    assert resp.status_code == 200
    assert b"jane@acme.com" in resp.data
    assert b"bob@widgets.com" in resp.data
    assert b"2 of 2 reach-outs shown" in resp.data


def test_create_app_reachouts_never_calls_gmass(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    _stage_and_send(conn, recipient="jane@acme.com", campaign="c", persona="recruiter")
    conn.close()

    with patch("slap.dashboard.gmass.get_reports") as mock_get_reports:
        client = app.test_client()
        resp = client.get("/reachouts")

    assert resp.status_code == 200
    mock_get_reports.assert_not_called()
