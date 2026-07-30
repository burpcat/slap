"""Dashboard tests (Build Order step 11), per SLAP_BUILD_PROMPT.md §8:
on-open poll writes new events; panels render from tracking data;
reply-tag -> OOO -> re-queue fires a send-as-reply.
"""
import dataclasses
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from werkzeug.serving import make_server

from slap.config import GlobalConfig, ScheduleConfig
from slap import gmass_cache, ui_state
from slap.queue import stage_recipient
from slap.dashboard import (
    _add_working_days, _click_details, _clicked_recipients, _recipient_drop_meta, active_leads,
    actionable_replies,
    bounce_breakdown, bounces, companies_contacted, compute_gmass_dependent_data, create_app,
    engagement_intelligence, event_display, filter_reachouts, follow_up_aging, follow_up_reminders,
    gate_linkedin, get_gmass_dependent_data, linkedin_replied_at, needs_triage, next_drain,
    pipeline, reachouts_rows,
    read_log_tail,
    recent_events, reply_tags, sent_reply_trend, stop_outreach, stopped_outreach_roster,
    sync_reports, tag_reply, template_failures, this_week, today_strip, todays_runs,
    visible_warm_but_silent, warm_but_silent, weekly_goal_progress,
)
from slap.reload import ReloadFailure, write_failures
from slap.tracking import append_event, connect

import redis as redis_lib


class FakeRedis:
    """Minimal in-memory stand-in for redis.Redis, covering exactly the
    operations slap.gmass_cache uses (get/set with nx+ex/delete/ping) —
    mirrors this project's existing "mock the external service at the
    boundary" convention (see test_gmass.py's mocked requests.post/get)
    rather than requiring a real Redis server for tests."""
    def __init__(self):
        self._store = {}

    def ping(self):
        return True

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    def delete(self, key):
        self._store.pop(key, None)


class FakeRedisDown:
    """Simulates Redis being completely unreachable — every operation
    raises, exactly like redis-py does on a real connection failure."""
    def _raise(self, *a, **k):
        raise redis_lib.exceptions.ConnectionError("simulated: redis unreachable")

    def ping(self):
        self._raise()

    def get(self, key):
        self._raise()

    def set(self, key, value, *, ex=None, nx=False):
        self._raise()

    def delete(self, key):
        self._raise()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path):
    return connect(db_path)


class _ImmediateThread:
    """threading.Thread stand-in that runs its target synchronously on
    .start(), so tests can assert on a spawned background GMass refresh's
    effects deterministically instead of racing a real daemon thread (and
    so a patched gmass.get_reports doesn't get called for real once the
    `with patch(...)` scope that covered the triggering request has already
    exited)."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def sync_background_thread(monkeypatch):
    monkeypatch.setattr("slap.dashboard.threading.Thread", _ImmediateThread)


def make_global_config(*, daily_cap=500, drain_retries=3, weekly_target=None):
    return GlobalConfig(
        from_email="owner@gmail.com", from_name="Owner", api_key_env="GMASS_API_KEY",
        personas={"recruiter": [2, 3, 5], "founder": [2, 5, 7], "hiring_manager": [2, 4, 6]},
        schedule=ScheduleConfig(fire_window_start="09:00", fire_window_end="09:15",
                                 send_delay_min=10, send_delay_max=15,
                                 daily_cap=daily_cap, drain_retries=drain_retries,
                                 active_days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                                 weekly_target=weekly_target),
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


# --- Redis-backed cache for the GMass-dependent widgets (post-launch) ------
# Only engagement_intelligence/warm_but_silent/bounces/actionable_replies
# depend on GMass report data — confirmed by reading slap/dashboard.py, not
# assumed from names (see compute_gmass_dependent_data's own docstring).
# today_strip/this_week/next_drain/todays_runs/pipeline/companies_contacted
# are untouched by any of this — already covered by their own pre-existing
# tests above/below, which still pass unmodified.

def test_compute_gmass_dependent_data_matches_individual_widget_functions(conn):
    seed_sent_recipient(conn)
    append_event(conn, type="reply", recipient="jane@acme.com", campaign="c")
    result = compute_gmass_dependent_data(conn, "fake-key", consumer_domains=set())

    assert result["engagement"] == engagement_intelligence(conn)
    assert result["warm_but_silent"] == warm_but_silent(conn)
    assert result["bounces"] == bounces(conn)
    live_replies = actionable_replies(conn, consumer_domains=set())
    assert result["replies"] == [
        {**r, "dedup_context": dataclasses.asdict(r["dedup_context"])} for r in live_replies
    ]


def test_compute_gmass_dependent_data_runs_the_real_sync_and_writes_events(conn):
    # "The hourly job populates Redis and writes the same events the old
    # on-open trigger would have" — proves this isn't a rewrite of
    # sync_reports(), just a different trigger for the exact same function.
    seed_sent_recipient(conn)
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        mock_get.side_effect = lambda api_key, cid, report_type: (
            [{"replyId": "r1", "replyTime": "2026-01-01T00:00:00"}] if report_type == "replies" else []
        )
        compute_gmass_dependent_data(conn, "fake-key", consumer_domains=set())
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'reply'")]
    assert len(events) == 1
    assert json.loads(events[0]["meta"])["reply_id"] == "r1"


def test_compute_gmass_dependent_data_is_json_serializable(conn):
    seed_sent_recipient(conn)
    append_event(conn, type="reply", recipient="jane@acme.com", campaign="c")
    result = compute_gmass_dependent_data(conn, "fake-key", consumer_domains=set())
    json.dumps(result)  # must not raise


def test_engagement_reply_by_stage_survives_a_real_json_round_trip(conn):
    # iron-audit BLOCKER regression test: engagement_intelligence()'s
    # reply_by_stage/click_by_stage dicts used to be keyed by Python int,
    # which JSON silently stringifies on any real round-trip — a fresh
    # cache hit (the DOMINANT case, served ~59 minutes of every hour)
    # rendered these panels as all-zero, every time, with no error at all.
    # This does the SAME round-trip write_cache()/read_cache() do (not
    # just json.dumps in isolation) and checks the actual values survive.
    append_event(conn, type="queued", recipient="a@acme.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="a@acme.com", campaign="c", stage=0, gmass_campaign_id="1")
    append_event(conn, type="reply", recipient="a@acme.com", campaign="c", stage=0)
    append_event(conn, type="click", recipient="a@acme.com", campaign="c", stage=0)

    result = compute_gmass_dependent_data(conn, "fake-key", consumer_domains=set())
    client = FakeRedis()
    gmass_cache.write_cache(client, result)
    round_tripped = gmass_cache.read_cache(client)

    # The exact lookup dashboard.html now performs: an int loop variable,
    # looked up via the |string filter.
    for stage in (0, 1, 2, 3):
        assert (round_tripped["engagement"]["reply_by_stage"].get(str(stage), 0)
                == result["engagement"]["reply_by_stage"].get(str(stage), 0))
    assert round_tripped["engagement"]["reply_by_stage"]["0"] == 1
    assert round_tripped["engagement"]["click_by_stage"]["0"] == 1


def test_flushing_cache_and_recomputing_produces_identical_results(conn):
    # "Redis is a cache, never a new source of truth" — flushing it and
    # letting the next refresh repopulate it must produce identical
    # results, since SQLite's events table is the only real source.
    seed_sent_recipient(conn)
    append_event(conn, type="reply", recipient="jane@acme.com", campaign="c")
    first = compute_gmass_dependent_data(conn, "fake-key", consumer_domains=set())
    second = compute_gmass_dependent_data(conn, "fake-key", consumer_domains=set())
    # cached_at/synced_at legitimately differ (each call stamps its own
    # "now") — every other field must be byte-identical.
    first.pop("cached_at"), second.pop("cached_at")
    first["sync_result"].pop("synced_at"), second["sync_result"].pop("synced_at")
    assert first == second


# --- get_gmass_dependent_data: cache orchestration --------------------------

def _fresh_cache_entry(**overrides):
    entry = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "sync_result": {"synced_at": "2026-01-01T00:00:00+00:00", "new_replies": 0,
                        "new_clicks": 0, "new_bounces": 0, "errors": []},
        "engagement": {"reply_rate_by_persona": {}, "reply_by_stage": {}, "click_by_stage": {},
                       "time_to_first_reply": {"same_day": 0, "1_2_days": 0, "3_7_days": 0, "8_plus_days": 0},
                       "has_data": False},
        "warm_but_silent": [], "bounces": [], "replies": [],
    }
    entry.update(overrides)
    return entry


def test_get_gmass_dependent_data_fresh_cache_makes_zero_gmass_calls(conn, db_path):
    client = FakeRedis()
    gmass_cache.write_cache(client, _fresh_cache_entry(engagement={"has_data": True, "reply_rate_by_persona": {},
                                                                    "reply_by_stage": {}, "click_by_stage": {},
                                                                    "time_to_first_reply": {}}))
    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        result = get_gmass_dependent_data("fake-key", set(), client, db_path)
    mock_get.assert_not_called()
    assert result["cache_status"] == "fresh"
    assert result["engagement"]["has_data"] is True


def test_get_gmass_dependent_data_stale_cache_spawns_background_refresh(conn, db_path, sync_background_thread):
    seed_sent_recipient(conn)
    client = FakeRedis()
    stale = _fresh_cache_entry()
    stale["cached_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    gmass_cache.write_cache(client, stale)

    with patch("slap.dashboard.gmass.get_reports", return_value=[]) as mock_get:
        result = get_gmass_dependent_data("fake-key", set(), client, db_path)
    mock_get.assert_called()  # the SAME refresh path the hourly job uses, run on a (synchronous, in this test) thread
    assert result["cache_status"] == "stale_refreshing"
    assert result["bounces"] == []  # returned the STALE snapshot immediately — never waits on the refresh
    assert gmass_cache.is_fresh(gmass_cache.read_cache(client))  # but the background refresh already wrote fresh data


def test_get_gmass_dependent_data_missing_cache_spawns_background_refresh(conn, db_path, sync_background_thread):
    seed_sent_recipient(conn)
    client = FakeRedis()
    with patch("slap.dashboard.gmass.get_reports", return_value=[]) as mock_get:
        result = get_gmass_dependent_data("fake-key", set(), client, db_path)
    mock_get.assert_called()
    assert result["cache_status"] == "stale_refreshing"
    assert gmass_cache.is_fresh(gmass_cache.read_cache(client))


def test_get_gmass_dependent_data_redis_unavailable_never_auto_refreshes(conn, db_path):
    # Priority-0 fix: Redis unreachable means acquire_lock() is impossible
    # too, so there's no safe way to coordinate an auto-triggered refresh —
    # a version that called the live poll directly here let concurrent
    # requests during a Redis outage fire multiple unlocked, simultaneous
    # GMass sweeps. Must render the honest empty state and do nothing else.
    seed_sent_recipient(conn)
    with patch("slap.dashboard.gmass.get_reports", return_value=[]) as mock_get:
        result = get_gmass_dependent_data("fake-key", set(), FakeRedisDown(), db_path)
    mock_get.assert_not_called()
    assert result["cache_status"] == "redis_unavailable"
    assert result["engagement"]["has_data"] is False
    assert result["bounces"] == []


def test_get_gmass_dependent_data_lock_held_uses_stale_cached_data_without_a_second_refresh(
    conn, db_path, sync_background_thread,
):
    client = FakeRedis()
    stale = _fresh_cache_entry(bounces=[{"recipient": "stale@x.com"}])
    stale["cached_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    gmass_cache.write_cache(client, stale)
    gmass_cache.acquire_lock(client)  # simulates the hourly job already mid-refresh

    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        result = get_gmass_dependent_data("fake-key", set(), client, db_path)
    mock_get.assert_not_called()  # never a second, concurrent refresh
    assert result["cache_status"] == "stale_refreshing"
    assert result["bounces"] == [{"recipient": "stale@x.com"}]  # served the last known data


def test_get_gmass_dependent_data_lock_held_with_no_cache_yet_renders_honest_empty_state(
    conn, db_path, sync_background_thread,
):
    client = FakeRedis()
    gmass_cache.acquire_lock(client)  # e.g. the very first sync ever, racing a dashboard open

    with patch("slap.dashboard.gmass.get_reports") as mock_get:
        result = get_gmass_dependent_data("fake-key", set(), client, db_path)
    mock_get.assert_not_called()
    assert result["cache_status"] == "stale_refreshing"
    assert result["engagement"]["has_data"] is False
    assert result["warm_but_silent"] == []
    assert result["bounces"] == []
    assert result["replies"] == []


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
    # String keys, not int — iron-audit BLOCKER fix: this dict is cached via
    # JSON (Redis), and JSON object keys are always strings. Keying it by
    # int here (and looking it up by int in the template) meant a fresh
    # cache hit silently rendered these panels as all-zero, every time.
    assert result["reply_by_stage"] == {"0": 1, "1": 1}
    assert result["click_by_stage"] == {"0": 1}


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


# --- Analytics page: sent_reply_trend / bounce_breakdown / weekly_goal_progress

def test_sent_reply_trend_buckets_by_local_day(conn):
    today = date(2026, 1, 15)
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, timestamp=_ts(-2))
    append_event(conn, type="requeued", recipient="b@x.com", campaign="c", stage=1, timestamp=_ts(-1))
    append_event(conn, type="reply", recipient="a@x.com", campaign="c", timestamp=_ts(0))

    series = sent_reply_trend(conn, days=3, today=today)

    assert [row["date"] for row in series] == ["2026-01-13", "2026-01-14", "2026-01-15"]
    assert series[0] == {"date": "2026-01-13", "new": 1, "follow_up": 0, "replies": 0}
    assert series[1] == {"date": "2026-01-14", "new": 0, "follow_up": 1, "replies": 0}
    assert series[2] == {"date": "2026-01-15", "new": 0, "follow_up": 0, "replies": 1}


def test_bounce_breakdown_groups_by_category_and_reason(conn):
    today = date(2026, 1, 15)
    for i in range(3):
        append_event(conn, type="bounce", recipient=f"full{i}@x.com", campaign="c",
                     meta={"category": "bounce", "bounce_reason": "Mailbox full"}, timestamp=_ts(-1))
    for i in range(2):
        append_event(conn, type="bounce", recipient=f"invalid{i}@x.com", campaign="c",
                     meta={"category": "bounce", "bounce_reason": "Invalid address"}, timestamp=_ts(-2))
    append_event(conn, type="bounce", recipient="blocked@x.com", campaign="c",
                 meta={"category": "block", "bounce_reason": "Blocked by policy"}, timestamp=_ts(-3))

    result = bounce_breakdown(conn, weeks=4, today=today)

    assert result["top_reasons"] == [
        {"reason": "Mailbox full", "count": 3},
        {"reason": "Invalid address", "count": 2},
        {"reason": "Blocked by policy", "count": 1},
    ]
    assert sum(w["bounce"] for w in result["by_category_over_time"]) == 5
    assert sum(w["block"] for w in result["by_category_over_time"]) == 1


def test_bounce_breakdown_folds_extra_reasons_into_other(conn):
    counts = [7, 6, 5, 4, 3, 2, 1]
    for reason_idx, count in enumerate(counts):
        for i in range(count):
            append_event(conn, type="bounce", recipient=f"r{reason_idx}-{i}@x.com", campaign="c",
                         meta={"category": "bounce", "bounce_reason": f"reason{reason_idx}"},
                         timestamp=_ts(-1))

    result = bounce_breakdown(conn, weeks=4, today=date(2026, 1, 15))

    assert result["top_reasons"][:5] == [
        {"reason": "reason0", "count": 7}, {"reason": "reason1", "count": 6},
        {"reason": "reason2", "count": 5}, {"reason": "reason3", "count": 4},
        {"reason": "reason4", "count": 3},
    ]
    assert result["top_reasons"][5] == {"reason": "other", "count": 3}  # reason5(2) + reason6(1)


def test_weekly_goal_progress_none_when_not_configured():
    assert weekly_goal_progress(None, None) is None


def test_weekly_goal_progress_reports_pct_when_configured(conn):
    today = date(2026, 1, 15)
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, timestamp=_ts(0))
    append_event(conn, type="sent", recipient="b@x.com", campaign="c", stage=0, timestamp=_ts(-1))

    result = weekly_goal_progress(conn, 10, today=today)

    assert result == {"target": 10, "actual": 2, "pct": 20}


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


def _fake_unsubscribe(api_key, email):
    return {"emailAddress": email, "unsubscribeTime": "2026-01-01T00:00:00", "sender": None}


def test_tag_reply_ooo_calls_the_real_requeue_mechanism(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "ooo", resume_date=date.today(), api_key="fake-key",
              unsubscribe_fn=_fake_unsubscribe)
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "ooo_requeued"
    assert needs_triage(conn) == []  # resolved


def test_tag_reply_ooo_requires_resume_date(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    with pytest.raises(ValueError, match="resume_date"):
        tag_reply(conn, "a@x.com", "ooo", api_key="fake-key", unsubscribe_fn=_fake_unsubscribe)


def test_tag_reply_ooo_available_with_no_prior_reply_or_engagement_at_all(conn):
    # The core requirement: markable OOO even when SLAP never detected
    # anything at all (no reply, no click) — the real-world trigger is an
    # OOO notice arriving somewhere SLAP/GMass never saw.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, gmass_campaign_id="1")
    tag_reply(conn, "a@x.com", "ooo", resume_date=date(2026, 8, 1), api_key="fake-key",
              unsubscribe_fn=_fake_unsubscribe)
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "ooo_requeued"


def test_tag_reply_ooo_calls_unsubscribe_before_recording_anything_locally(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})

    def failing_unsubscribe(api_key, email):
        raise RuntimeError("simulated GMass failure")

    with pytest.raises(RuntimeError, match="simulated GMass failure"):
        tag_reply(conn, "a@x.com", "ooo", resume_date=date(2026, 8, 1), api_key="fake-key",
                  unsubscribe_fn=failing_unsubscribe)
    # Nothing was recorded locally — a local-only pause with no working
    # GMass-side suppression would be worse than not marking OOO at all
    # (false confidence the double-send risk was handled).
    assert conn.execute("SELECT * FROM events WHERE type = 'ooo_tagged'").fetchone() is None
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "active"  # unchanged from the queued event


def test_tag_reply_ooo_calls_unsubscribe_with_the_recipient_and_api_key(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    captured = {}

    def capturing_unsubscribe(api_key, email):
        captured["api_key"] = api_key
        captured["email"] = email
        return _fake_unsubscribe(api_key, email)

    tag_reply(conn, "a@x.com", "ooo", resume_date=date(2026, 8, 1), api_key="the-real-key",
              unsubscribe_fn=capturing_unsubscribe)
    assert captured == {"api_key": "the-real-key", "email": "a@x.com"}


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
    tag_reply(conn, "a@x.com", "not_interested", api_key="fake-key", unsubscribe_fn=_fake_unsubscribe)
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'reply_reviewed'")]
    assert len(events) == 1
    assert needs_triage(conn) == []


def test_tag_reply_not_interested_calls_unsubscribe_same_as_ooo(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    captured = {}

    def capturing_unsubscribe(api_key, email):
        captured["api_key"] = api_key
        captured["email"] = email
        return _fake_unsubscribe(api_key, email)

    tag_reply(conn, "a@x.com", "not_interested", api_key="the-real-key", unsubscribe_fn=capturing_unsubscribe)
    assert captured == {"api_key": "the-real-key", "email": "a@x.com"}


def test_tag_reply_not_interested_calls_unsubscribe_before_recording_anything_locally(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")

    def failing_unsubscribe(api_key, email):
        raise RuntimeError("simulated GMass failure")

    with pytest.raises(RuntimeError, match="simulated GMass failure"):
        tag_reply(conn, "a@x.com", "not_interested", api_key="fake-key", unsubscribe_fn=failing_unsubscribe)
    assert conn.execute("SELECT * FROM events WHERE type = 'reply_reviewed'").fetchone() is None


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


def test_pipeline_followups_scheduled_prefers_recipients_own_cadence(conn):
    # Per-recipient follow-up override (post-launch): recruiter's persona
    # default is [2,3,5], but this recipient's own staged cadence is [2, 10]
    # -- the schedule must reflect what was ACTUALLY staged, not the config
    # default, which would put the next follow-up on an entirely different day.
    first_sent = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter", "cadence": [2, 10]}, timestamp=first_sent)
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=0, timestamp=first_sent)
    append_event(conn, type="sent", recipient="a@x.com", campaign="c", stage=1,
                 timestamp=first_sent + timedelta(days=2))

    # persona default [2,3,5] would schedule the next stage for day 6 (Mar 6);
    # this recipient's own [2, 10] cadence actually schedules it for day 13.
    result_day6 = pipeline(conn, make_global_config(), today=date(2026, 3, 6))
    assert result_day6["followups_scheduled"]["today"] == []

    result_day13 = pipeline(conn, make_global_config(), today=date(2026, 3, 13))
    assert [e["recipient"] for e in result_day13["followups_scheduled"]["today"]] == ["a@x.com"]


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


def test_warm_but_silent_includes_click_url_detail(conn):
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0,
                 meta={"url": "https://acme.com/careers", "click_time": "2026-07-03T00:00:00"})
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=1,
                 meta={"url": "https://linkedin.com/in/x", "click_time": "2026-07-10T00:00:00"})
    result = warm_but_silent(conn)
    assert len(result) == 1
    assert result[0]["clicks"] == [
        {"url": "https://acme.com/careers", "stage": 0, "click_time": "2026-07-03T00:00:00"},
        {"url": "https://linkedin.com/in/x", "stage": 1, "click_time": "2026-07-10T00:00:00"},
    ]


def test_warm_but_silent_clicks_empty_when_no_url_meta(conn):
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0)
    result = warm_but_silent(conn)
    assert result[0]["clicks"] == []


def test_click_details_dedupes_by_url_keeping_earliest_time(conn):
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0,
                 meta={"url": "https://acme.com", "click_time": "2026-07-05T00:00:00"})
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=1,
                 meta={"url": "https://acme.com", "click_time": "2026-07-01T00:00:00"})
    result = _click_details(conn)
    assert result["a@x.com"] == [{"url": "https://acme.com", "stage": 1, "click_time": "2026-07-01T00:00:00"}]


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


def test_bounces_surfaces_the_actual_reason_text_for_a_bounce(conn):
    # Previously: the widget only ever showed a generic "Bounced" label,
    # even though the real reason text was already captured in
    # meta.bounce_reason at sync time -- never read back out for display.
    append_event(conn, type="queued", recipient="bounced@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="bounce", recipient="bounced@x.com", campaign="c",
                 meta={"bounce_reason": "mailbox full", "bounce_time": "t1", "category": "bounce"})
    result = bounces(conn)
    assert result[0]["reason"] == "mailbox full"


def test_bounces_surfaces_the_actual_reason_text_for_a_block(conn):
    append_event(conn, type="queued", recipient="blocked@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="bounce", recipient="blocked@x.com", campaign="c",
                 meta={"bounce_reason": "rejected due to security policies", "bounce_time": "t2",
                       "category": "block"})
    result = bounces(conn)
    assert result[0]["reason"] == "rejected due to security policies"


def test_bounces_invalid_address_shows_real_reason_not_a_generic_label(conn):
    # A real, GMass-shaped hard-bounce diagnostic for a non-existent
    # recipient -- must show up verbatim, not collapse to a bare "bounced"
    # with no detail on WHY.
    real_reason = (
        "Final-Recipient: rfc822; akshat@truefoundry.com\n"
        "Action: failed\nStatus: 5.1.1\n"
        "Diagnostic-Code: smtp; 550-5.1.1 The email account that you tried to reach "
        "does not exist."
    )
    append_event(conn, type="queued", recipient="akshat@truefoundry.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="bounce", recipient="akshat@truefoundry.com", campaign="c",
                 meta={"bounce_reason": real_reason, "bounce_time": "t3", "category": "bounce"})
    result = bounces(conn)
    assert result[0]["reason"] == real_reason
    assert result[0]["reason"] != "bounced"
    assert "does not exist" in result[0]["reason"]


def test_bounces_reason_blank_not_none_when_gmass_gave_no_reason_text(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c",
                 meta={"bounce_reason": None, "bounce_time": "t1", "category": "bounce"})
    result = bounces(conn)
    assert result[0]["reason"] == ""


# --- stopped_outreach_roster (Deliverability page, post-launch) ------------

def test_stopped_outreach_roster_empty_when_none(conn):
    assert stopped_outreach_roster(conn) == []


def test_stopped_outreach_roster_lists_a_stopped_recipient(conn):
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter", "company": "Acme"})
    stop_outreach(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    result = stopped_outreach_roster(conn)
    assert len(result) == 1
    assert result[0]["recipient"] == "a@x.com"
    assert result[0]["campaign"] == "c"
    assert result[0]["company"] == "Acme"
    assert result[0]["scope"] == "recipient"
    assert result[0]["stopped_at"]


def test_stopped_outreach_roster_survives_a_later_bounce_overwriting_status(conn):
    # Same iron-audit-caught bug class as active_leads()'s analogous
    # regression test: a later bounce event (sync_reports() re-polling
    # regardless of any local stop) flips recipients.status away from
    # 'stopped' to 'bounced' -- the roster must still list the recipient,
    # since it's sourced from the append-only 'stopped' event itself
    # (_stopped_recipients()), not the mutable status column.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    stop_outreach(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c",
                 meta={"bounce_reason": "mailbox full", "bounce_time": "t1", "category": "bounce"})

    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "bounced"  # confirms the status column really did flip

    result = stopped_outreach_roster(conn)
    assert len(result) == 1
    assert result[0]["recipient"] == "a@x.com"


def test_stopped_outreach_roster_sorted_most_recently_stopped_first(conn):
    append_event(conn, type="queued", recipient="old@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="stopped", recipient="old@x.com", campaign="c", meta={"scope": "recipient"},
                 timestamp=_ts(0))
    append_event(conn, type="queued", recipient="new@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="stopped", recipient="new@x.com", campaign="c", meta={"scope": "recipient"},
                 timestamp=_ts(1))
    result = stopped_outreach_roster(conn)
    assert [r["recipient"] for r in result] == ["new@x.com", "old@x.com"]


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
    assert result == {"all_time_count": 0, "this_week_count": 0, "top_companies": [], "all_companies": []}


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
    # get_gmass_dependent_data's background GMass refresh runs synchronously
    # in tests (see _ImmediateThread) — every other assertion in this file
    # expects a request's effect on GMass-dependent data to be visible
    # immediately afterward, not racing a real daemon thread. The one test
    # that needs a REAL thread (concurrent-request-threads regression,
    # below) builds its own app directly rather than using this fixture.
    monkeypatch.setattr("slap.dashboard.threading.Thread", _ImmediateThread)
    db_path = tmp_path / "test.db"
    connect(db_path).close()
    return create_app(db_path, make_global_config(), consumer_domains=set(), api_key="fake-key",
                       redis_client=FakeRedis())


# --- multi-page redesign: base.html/nav + each new page's happy path -------

def test_visible_warm_but_silent_excludes_hidden_recipient(tmp_path):
    conn = connect(tmp_path / "test.db")
    ui_state.hide(conn, "a@x.com", "warm_but_silent")
    rows = [{"recipient": "a@x.com"}, {"recipient": "b@x.com"}]
    assert [r["recipient"] for r in visible_warm_but_silent(conn, rows)] == ["b@x.com"]


def test_visible_warm_but_silent_auto_resurfaces_on_newer_click(tmp_path):
    # _warm_but_silent_hidden_recipients() reads click timing LIVE from
    # `conn` (the click event's own `timestamp` column), not from the `rows`
    # argument's own `clicks` field -- deliberately: `rows` may be up to an
    # hour stale (served from the Redis cache), but a fresh click that
    # landed AFTER the hide action must resurface the row immediately, not
    # wait for the next cache refresh.
    conn = connect(tmp_path / "test.db")
    ui_state.hide(conn, "a@x.com", "warm_but_silent", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0,
                 meta={"url": "https://x.com", "click_time": "2026-07-01T00:00:00+00:00"},
                 timestamp=datetime(2026, 7, 1, tzinfo=timezone.utc))
    rows = [{"recipient": "a@x.com"}]
    assert [r["recipient"] for r in visible_warm_but_silent(conn, rows)] == ["a@x.com"]


def test_visible_warm_but_silent_stays_hidden_without_a_newer_click(tmp_path):
    conn = connect(tmp_path / "test.db")
    ui_state.hide(conn, "a@x.com", "warm_but_silent", when=datetime(2026, 7, 1, tzinfo=timezone.utc))
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0,
                 meta={"url": "https://x.com", "click_time": "2026-01-01T00:00:00+00:00"},
                 timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))
    rows = [{"recipient": "a@x.com"}]
    assert visible_warm_but_silent(conn, rows) == []


def test_visible_warm_but_silent_resurfaces_on_a_repeat_click_of_the_same_url(tmp_path):
    # The bug the auditor caught: _click_details() dedupes by url, keeping
    # only the EARLIEST click_time per url -- using that for the resurface
    # check would silently discard a RE-click of a url the recipient already
    # clicked before hiding, and the row would never resurface. The fix
    # reads events.timestamp directly (no url-dedup) so a repeat click still
    # counts.
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0,
                 meta={"url": "https://x.com", "click_time": "2026-01-01T00:00:00+00:00"},
                 timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))
    ui_state.hide(conn, "a@x.com", "warm_but_silent", when=datetime(2026, 3, 1, tzinfo=timezone.utc))
    # A second click on the SAME url, after the hide.
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0,
                 meta={"url": "https://x.com", "click_time": "2026-01-01T00:00:00+00:00"},
                 timestamp=datetime(2026, 7, 1, tzinfo=timezone.utc))
    rows = [{"recipient": "a@x.com"}]
    assert [r["recipient"] for r in visible_warm_but_silent(conn, rows)] == ["a@x.com"]


# --- Reach-outs: all-campaigns, filterable, read-only recipient table ------

def _stage_and_send(conn, *, recipient, campaign, persona, company="", role="", req_id="", name="",
                     timestamp=None, send=True):
    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": persona, "company": company, "role": role, "req_id": req_id, "name": name},
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


# --- tag_reply: unreal -------------------------------------------------------

def test_tag_reply_unreal_writes_reply_reviewed_and_never_calls_unsubscribe(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "real")

    def boom(api_key, email):
        raise AssertionError("unreal must never call GMass")

    tag_reply(conn, "a@x.com", "unreal", unsubscribe_fn=boom)
    assert reply_tags(conn) == {"a@x.com": "unreal"}


def test_tag_reply_unreal_supersedes_real(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "real")
    assert reply_tags(conn) == {"a@x.com": "real"}
    tag_reply(conn, "a@x.com", "unreal")
    assert reply_tags(conn) == {"a@x.com": "unreal"}


# --- active_leads / follow_up_reminders --------------------------------------

def test_active_leads_lists_real_tagged_recipients(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter", company="Acme")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "real")

    leads = active_leads(conn)
    assert len(leads) == 1
    assert leads[0]["recipient"] == "a@x.com"
    assert leads[0]["company"] == "Acme"
    assert leads[0]["campaign"] == "c"
    assert leads[0]["persona"] == "recruiter"
    assert leads[0]["real_tagged_at"]


def test_active_leads_excludes_untagged_and_not_interested(conn):
    _stage_and_send(conn, recipient="untagged@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="untagged@x.com", campaign="c")

    _stage_and_send(conn, recipient="not_interested@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="not_interested@x.com", campaign="c")
    tag_reply(conn, "not_interested@x.com", "not_interested", api_key="k", unsubscribe_fn=_fake_unsubscribe)

    assert active_leads(conn) == []


def test_active_leads_excludes_unreal(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "real")
    assert len(active_leads(conn)) == 1
    tag_reply(conn, "a@x.com", "unreal")
    assert active_leads(conn) == []


def test_active_leads_excludes_stopped_recipient_even_though_still_tagged_real(conn):
    # Stop outreach is a SEPARATE axis from Real/Unreal (append-only — Stop
    # outreach never rewrites the reply tag), so a stopped recipient must
    # fall out of Active Leads via the append-only 'stopped' event.
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "real")
    assert len(active_leads(conn)) == 1

    stop_outreach(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    assert active_leads(conn) == []
    assert reply_tags(conn)["a@x.com"] == "real"  # the tag itself is untouched


def test_active_leads_stays_excluded_after_a_later_bounce_overwrites_status(conn):
    # An iron-audit SHOULD-FIX: sync_reports() re-polls every known campaign
    # regardless of any local stop, so a real GMass bounce CAN arrive after
    # stop_outreach() and overwrite recipients.status away from 'stopped'.
    # active_leads() must still exclude this recipient — it checks the
    # append-only 'stopped' event's own existence, not the mutable status
    # column, so it can never be silently re-admitted this way.
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "real")
    stop_outreach(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    assert active_leads(conn) == []

    append_event(conn, type="bounce", recipient="a@x.com", campaign="c",
                 meta={"bounce_reason": "mailbox full", "bounce_time": "t1", "category": "bounce"})
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "bounced"  # confirms the status column really did flip
    assert active_leads(conn) == []  # active_leads() is unaffected either way


def test_active_leads_sorted_most_recent_first(conn):
    _stage_and_send(conn, recipient="old@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="old@x.com", campaign="c", timestamp=_ts(1))
    append_event(conn, type="reply_reviewed", recipient="old@x.com", campaign="c",
                 meta={"tag": "real"}, timestamp=_ts(1))

    _stage_and_send(conn, recipient="new@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="new@x.com", campaign="c", timestamp=_ts(2))
    append_event(conn, type="reply_reviewed", recipient="new@x.com", campaign="c",
                 meta={"tag": "real"}, timestamp=_ts(2))

    leads = active_leads(conn)
    assert [l["recipient"] for l in leads] == ["new@x.com", "old@x.com"]


def test_follow_up_reminders_reuses_active_leads_and_adds_days_since(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter", company="Acme")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c", timestamp=_ts(0))
    append_event(conn, type="reply_reviewed", recipient="a@x.com", campaign="c",
                 meta={"tag": "real"}, timestamp=_ts(0))

    reminders = follow_up_reminders(conn, today=_ts(0).date() + timedelta(days=4))
    assert len(reminders) == 1
    assert reminders[0]["recipient"] == "a@x.com"
    assert reminders[0]["company"] == "Acme"
    assert reminders[0]["days_since"] == 4
    # Next-nudge date is the real-tag anchor (no interaction yet) + the fixed
    # nudge gap in WORKING days — computed live, not stored.
    from slap.dashboard import FOLLOW_UP_NUDGE_DAYS
    expected = _add_working_days(_ts(0).date(), FOLLOW_UP_NUDGE_DAYS).isoformat()
    assert reminders[0]["next_follow_up_date"] == expected


def test_follow_up_reminders_sorted_most_overdue_first(conn):
    _stage_and_send(conn, recipient="recent@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="recent@x.com", campaign="c", timestamp=_ts(9))
    append_event(conn, type="reply_reviewed", recipient="recent@x.com", campaign="c",
                 meta={"tag": "real"}, timestamp=_ts(9))

    _stage_and_send(conn, recipient="overdue@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="overdue@x.com", campaign="c", timestamp=_ts(0))
    append_event(conn, type="reply_reviewed", recipient="overdue@x.com", campaign="c",
                 meta={"tag": "real"}, timestamp=_ts(0))

    reminders = follow_up_reminders(conn, today=_ts(20).date())
    assert [r["recipient"] for r in reminders] == ["overdue@x.com", "recent@x.com"]


def test_follow_up_reminders_excludes_stopped(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "real")
    stop_outreach(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    assert follow_up_reminders(conn) == []


def test_follow_up_reminders_empty_when_no_active_leads(conn):
    assert follow_up_reminders(conn) == []


# --- _add_working_days -------------------------------------------------------

def test_add_working_days_skips_weekends():
    # 2026-01-15 is a Thursday. +3 working days -> Fri 16, (skip Sat/Sun), Mon 19, Tue 20.
    assert _add_working_days(date(2026, 1, 15), 3) == date(2026, 1, 20)
    # A Friday start: +1 working day lands on the following Monday, not Saturday.
    assert _add_working_days(date(2026, 1, 16), 1) == date(2026, 1, 19)
    # Zero is a no-op.
    assert _add_working_days(date(2026, 1, 15), 0) == date(2026, 1, 15)


# --- follow_up_aging ---------------------------------------------------------

def test_follow_up_aging_includes_real_lead(conn):
    _stage_and_send(conn, recipient="real@x.com", campaign="c", persona="recruiter", company="Acme")
    append_event(conn, type="reply", recipient="real@x.com", campaign="c", timestamp=_ts(0))
    append_event(conn, type="reply_reviewed", recipient="real@x.com", campaign="c",
                 meta={"tag": "real"}, timestamp=_ts(0))

    aging = follow_up_aging(conn, today=_ts(0).date() + timedelta(days=4))
    assert len(aging) == 1
    assert aging[0]["recipient"] == "real@x.com"
    assert aging[0]["company"] == "Acme"
    assert aging[0]["days_since"] == 4
    assert aging[0]["linkedin"] is False
    assert aging[0]["next_follow_up_date"] == _add_working_days(_ts(0).date(), 3).isoformat()


def test_follow_up_aging_includes_linkedin_only_lead(conn):
    # A LinkedIn-replied lead who was NEVER tagged Real still shows up here
    # (the whole point of this widget vs. the real-only follow_up_reminders).
    _stage_and_send(conn, recipient="li@x.com", campaign="c", persona="recruiter", company="LinkedCo")
    append_event(conn, type="interaction", recipient="li@x.com", campaign="c",
                 meta={"channel": "linkedin_reply", "state": True}, timestamp=_ts(0))

    assert active_leads(conn) == []  # not real-tagged
    aging = follow_up_aging(conn, today=_ts(0).date() + timedelta(days=2))
    assert [a["recipient"] for a in aging] == ["li@x.com"]
    assert aging[0]["linkedin"] is True
    assert aging[0]["days_since"] == 2


def test_follow_up_aging_excludes_stopped(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    tag_reply(conn, "a@x.com", "real")
    stop_outreach(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    assert follow_up_aging(conn) == []


def test_follow_up_aging_followed_up_resets_days_since(conn):
    _stage_and_send(conn, recipient="li@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="interaction", recipient="li@x.com", campaign="c",
                 meta={"channel": "linkedin_reply", "state": True}, timestamp=_ts(0))
    today = _ts(10).date()
    assert follow_up_aging(conn, today=today)[0]["days_since"] == 10

    # A "followed up" interaction moves the anchor forward -> days_since resets.
    append_event(conn, type="interaction", recipient="li@x.com", campaign="c",
                 meta={"channel": "followed_up"}, timestamp=_ts(10))
    assert follow_up_aging(conn, today=today)[0]["days_since"] == 0


def test_follow_up_aging_empty_when_nothing(conn):
    assert follow_up_aging(conn) == []


# --- linkedin_replied_at -----------------------------------------------------

def test_linkedin_replied_at_surfaces_marked_timestamp(conn):
    _stage_and_send(conn, recipient="li@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="interaction", recipient="li@x.com", campaign="c",
                 meta={"channel": "linkedin_reply", "state": True}, timestamp=_ts(3))

    at = linkedin_replied_at(conn)
    assert at["li@x.com"] == _ts(3).isoformat()

    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["li@x.com"]["linkedin_replied_at"] == _ts(3).isoformat()


def test_linkedin_replied_at_absent_when_toggled_off(conn):
    _stage_and_send(conn, recipient="li@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="interaction", recipient="li@x.com", campaign="c",
                 meta={"channel": "linkedin_reply", "state": True}, timestamp=_ts(0))
    append_event(conn, type="interaction", recipient="li@x.com", campaign="c",
                 meta={"channel": "linkedin_reply", "state": False}, timestamp=_ts(1))

    assert "li@x.com" not in linkedin_replied_at(conn)
    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["li@x.com"]["linkedin_replied_at"] is None


# --- stop_outreach -------------------------------------------------------------

def test_stop_outreach_writes_stopped_event_and_sets_status(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    stop_outreach(conn, "a@x.com", api_key="fake-key", unsubscribe_fn=_fake_unsubscribe)
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "stopped"
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'stopped'")]
    assert len(events) == 1
    assert json.loads(events[0]["meta"]) == {"scope": "recipient"}


def test_stop_outreach_calls_unsubscribe_before_recording_anything_locally(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")

    def failing_unsubscribe(api_key, email):
        raise RuntimeError("simulated GMass failure")

    with pytest.raises(RuntimeError, match="simulated GMass failure"):
        stop_outreach(conn, "a@x.com", api_key="fake-key", unsubscribe_fn=failing_unsubscribe)
    assert conn.execute("SELECT * FROM events WHERE type = 'stopped'").fetchone() is None
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "active"


def test_stop_outreach_calls_unsubscribe_with_recipient_and_api_key(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    captured = {}

    def capturing_unsubscribe(api_key, email):
        captured["api_key"] = api_key
        captured["email"] = email
        return _fake_unsubscribe(api_key, email)

    stop_outreach(conn, "a@x.com", api_key="the-real-key", unsubscribe_fn=capturing_unsubscribe)
    assert captured == {"api_key": "the-real-key", "email": "a@x.com"}


def test_stop_outreach_removes_recipient_from_due_recipients(conn):
    from slap.queue import due_recipients
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter", send=False)
    assert len(due_recipients(conn)) == 1
    stop_outreach(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    assert due_recipients(conn) == []


def test_stop_outreach_does_not_affect_pipeline_for_other_recipients(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    _stage_and_send(conn, recipient="b@x.com", campaign="c", persona="recruiter")
    stop_outreach(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    result = pipeline(conn, make_global_config())
    all_active = [r for stage_list in result["mid_sequence_by_stage"].values() for r in stage_list]
    assert "a@x.com" not in all_active
    assert "b@x.com" in all_active


# --- _status_chip: stopped -----------------------------------------------------

def test_reachouts_rows_chip_stopped(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    stop_outreach(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    rows = reachouts_rows(conn)
    row = next(r for r in rows if r["recipient"] == "a@x.com")
    assert row["chip"] == {"color": "critical", "label": "Stopped"}


def test_reachouts_rows_chip_stopped_takes_priority_over_replied(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    stop_outreach(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    rows = reachouts_rows(conn)
    row = next(r for r in rows if r["recipient"] == "a@x.com")
    assert row["chip"]["label"] == "Stopped"
    assert row["engagement"] == "replied"  # history preserved, just not what the chip leads with


def test_reachouts_rows_chip_stays_stopped_after_a_later_bounce_overwrites_status(conn):
    # An iron-audit SHOULD-FIX: a LATER bounce event (sync_reports() re-
    # polling regardless of any local stop) flips recipients.status away
    # from 'stopped' to 'bounced' — the chip must still say "Stopped", and
    # the row's own `stopped` field (which reachouts.html gates the "Stop
    # outreach" button on) must still be True.
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    stop_outreach(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c",
                 meta={"bounce_reason": "mailbox full", "bounce_time": "t1", "category": "bounce"})

    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "bounced"  # confirms the status column really did flip

    rows = reachouts_rows(conn)
    reachout = next(r for r in rows if r["recipient"] == "a@x.com")
    assert reachout["stopped"] is True
    assert reachout["chip"] == {"color": "critical", "label": "Stopped"}


# --- gate_linkedin: LinkedIn reply-gate (halts GMass, status linkedin-gate) ----

def test_gate_linkedin_writes_events_and_sets_status(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    gate_linkedin(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "linkedin-gate"
    # Both events written: the interaction (keeps linkedin_replied True) and the gate.
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type = 'linkedin_gate'").fetchone()[0] == 1
    inter = conn.execute(
        "SELECT meta FROM events WHERE type = 'interaction' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert json.loads(inter["meta"]) == {"channel": "linkedin_reply", "state": True}


def test_gate_linkedin_calls_unsubscribe_before_recording_anything(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")

    def failing(api_key, email):
        raise RuntimeError("simulated GMass failure")

    with pytest.raises(RuntimeError, match="simulated GMass failure"):
        gate_linkedin(conn, "a@x.com", api_key="k", unsubscribe_fn=failing)
    assert conn.execute("SELECT * FROM events WHERE type = 'linkedin_gate'").fetchone() is None
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "active"  # unchanged — nothing recorded


def test_gate_linkedin_is_idempotent(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    calls = []

    def counting(api_key, email):
        calls.append(email)
        return _fake_unsubscribe(api_key, email)

    gate_linkedin(conn, "a@x.com", api_key="k", unsubscribe_fn=counting)
    gate_linkedin(conn, "a@x.com", api_key="k", unsubscribe_fn=counting)  # already gated → no-op
    assert calls == ["a@x.com"]  # GMass hit exactly once
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type = 'linkedin_gate'").fetchone()[0] == 1


def test_gate_linkedin_unknown_recipient_fails_loud(conn):
    with pytest.raises(ValueError, match="unknown recipient"):
        gate_linkedin(conn, "ghost@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)


def test_gate_linkedin_removes_recipient_from_due_recipients(conn):
    from slap.queue import due_recipients
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter", send=False)
    assert len(due_recipients(conn)) == 1
    gate_linkedin(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    assert due_recipients(conn) == []


def test_reachouts_rows_chip_linkedin_gate(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    gate_linkedin(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    row = next(r for r in reachouts_rows(conn) if r["recipient"] == "a@x.com")
    assert row["chip"] == {"color": "good", "label": "LinkedIn"}
    assert row["linkedin_gated"] is True
    assert row["linkedin_replied"] is True
    assert row["status"] == "linkedin-gate"


def test_reachouts_rows_chip_stays_linkedin_gate_after_a_later_bounce(conn):
    # Same durability as stopped: a later bounce flips recipients.status, but the
    # gate chip reads the append-only linkedin_gate event, so it holds.
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    gate_linkedin(conn, "a@x.com", api_key="k", unsubscribe_fn=_fake_unsubscribe)
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c",
                 meta={"bounce_reason": "mailbox full", "bounce_time": "t1", "category": "bounce"})
    assert conn.execute("SELECT status FROM recipients WHERE recipient = ?",
                        ("a@x.com",)).fetchone()["status"] == "bounced"
    row = next(r for r in reachouts_rows(conn) if r["recipient"] == "a@x.com")
    assert row["chip"]["label"] == "LinkedIn"
    assert row["linkedin_gated"] is True


# --- _recipient_drop_meta -----------------------------------------------------

def test_recipient_drop_meta_reads_latest_queued_event(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter",
                     company="Acme", role="Backend Engineer", req_id="REQ-1", name="Cassie")
    assert _recipient_drop_meta(conn)["a@x.com"] == {
        "company": "Acme", "role": "Backend Engineer", "req_id": "REQ-1", "name": "Cassie",
    }


def test_recipient_drop_meta_blank_for_a_queued_event_without_these_keys(conn):
    # Backward compatibility: a queued event written before this capture
    # existed simply has no company/role/req_id/name keys in its meta at all.
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    assert _recipient_drop_meta(conn)["a@x.com"] == {"company": "", "role": "", "req_id": "", "name": ""}


def test_recipient_drop_meta_uses_the_most_recent_queued_event(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c1", persona="recruiter", company="Old Co", name="Old Name")
    _stage_and_send(conn, recipient="a@x.com", campaign="c2", persona="founder", company="New Co", name="New Name")
    assert _recipient_drop_meta(conn)["a@x.com"]["company"] == "New Co"
    assert _recipient_drop_meta(conn)["a@x.com"]["name"] == "New Name"


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


# --- reachouts_rows: status chip (color-coded badge, item 1) ---------------

def test_reachouts_rows_chip_bounced_with_reason(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c",
                 meta={"bounce_reason": "550 no such user", "bounce_time": "t1", "category": "bounce"})
    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["a@x.com"]["chip"] == {"color": "critical", "label": "Bounced — 550 no such user"}
    assert rows["a@x.com"]["bounce_category"] == "bounce"
    assert rows["a@x.com"]["bounce_reason"] == "550 no such user"


def test_reachouts_rows_chip_blocked_without_reason(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c",
                 meta={"bounce_reason": None, "bounce_time": "t1", "category": "block"})
    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["a@x.com"]["chip"] == {"color": "critical", "label": "Blocked"}


def test_reachouts_rows_chip_bounced_takes_priority_over_prior_reply(conn):
    # Structural overlap check (item 1): status and engagement are derived
    # independently, so a recipient who replied and was LATER (re)sent to and
    # bounced is both 'bounced' (status) and 'replied' (engagement) at once.
    # The chip's color must still show bounced (red), not replied (green).
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c",
                 meta={"bounce_reason": "mailbox full", "bounce_time": "t1", "category": "bounce"})
    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    row = rows["a@x.com"]
    assert row["status"] == "bounced"
    assert row["engagement"] == "replied"  # history preserved, just not color-coded
    assert row["chip"]["color"] == "critical"


def test_reachouts_rows_chip_ooo_with_resume_date(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c",
                 meta={"resume_date": "2026-08-01"})
    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["a@x.com"]["chip"] == {"color": None, "label": "OOO — resumes 2026-08-01"}


def test_reachouts_rows_chip_not_interested(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    append_event(conn, type="reply_reviewed", recipient="a@x.com", campaign="c", meta={"tag": "not_interested"})
    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["a@x.com"]["chip"] == {"color": None, "label": "Not interested"}


def test_reachouts_rows_chip_replied(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["a@x.com"]["chip"] == {"color": "good", "label": "Replied"}


def test_reachouts_rows_chip_clicked_multiple_distinct_links(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0,
                 meta={"url": "https://acme.com/careers", "click_time": "t1"})
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=1,
                 meta={"url": "https://linkedin.com/in/x", "click_time": "t2"})
    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["a@x.com"]["chip"] == {"color": "serious", "label": "Clicked (2)"}
    assert [c["url"] for c in rows["a@x.com"]["clicks"]] == ["https://acme.com/careers", "https://linkedin.com/in/x"]


def test_reachouts_rows_chip_clicked_no_url_meta_still_labeled_clicked(conn):
    _stage_and_send(conn, recipient="a@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0)
    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["a@x.com"]["chip"] == {"color": "serious", "label": "Clicked"}
    assert rows["a@x.com"]["clicks"] == []


def test_reachouts_rows_chip_active_done_queued(conn):
    _stage_and_send(conn, recipient="active@x.com", campaign="c", persona="recruiter")
    _stage_and_send(conn, recipient="queued@x.com", campaign="c", persona="recruiter", send=False)
    append_event(conn, type="queued", recipient="done@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="done@x.com", campaign="c", stage=0, gmass_campaign_id="1",
                 meta={"is_final_stage": True})
    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["active@x.com"]["chip"] == {"color": None, "label": "Active"}
    assert rows["queued@x.com"]["chip"] == {"color": None, "label": "Queued"}
    assert rows["done@x.com"]["chip"] == {"color": None, "label": "Done"}


def test_reachouts_rows_domain_company_and_req_id_present(conn):
    _stage_and_send(conn, recipient="jane@acme.com", campaign="c", persona="recruiter",
                     company="Acme", req_id="REQ-1", name="Jane Doe")
    _stage_and_send(conn, recipient="bob@x.com", campaign="c", persona="recruiter")

    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["jane@acme.com"]["domain"] == "acme.com"
    assert rows["jane@acme.com"]["company"] == "Acme"
    assert rows["jane@acme.com"]["name"] == "Jane Doe"
    assert rows["jane@acme.com"]["req_id_present"] is True
    assert rows["bob@x.com"]["company"] == ""
    assert rows["bob@x.com"]["name"] == ""
    assert rows["bob@x.com"]["req_id_present"] is False


def test_reachouts_rows_surfaces_corrected_from(conn, tmp_path):
    from slap.queue import resend_bounced

    workdir_root = tmp_path / "workdir"
    attachment = tmp_path / "attach.pdf"
    attachment.write_bytes(b"%PDF-fake")
    stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"], attachment_path=attachment,
        attachment_name="Resume.pdf", latex_enabled=True, company="Acme", workdir_root=workdir_root,
    )
    append_event(conn, type="sent", recipient="jane@acme.com", campaign="c", stage=0, gmass_campaign_id="1")
    append_event(conn, type="bounce", recipient="jane@acme.com", campaign="c",
                 meta={"bounce_reason": "550 no such user", "bounce_time": "t1", "category": "bounce"})

    resend_bounced(conn, original_recipient="jane@acme.com", corrected_email="jane.doe@acme.com",
                    workdir_root=workdir_root)

    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["jane.doe@acme.com"]["corrected_from"] == "jane@acme.com"
    assert rows["jane@acme.com"]["corrected_from"] is None


def test_reachouts_rows_surfaces_already_corrected_to(conn, tmp_path):
    # The bounced row's own detail must show it was already resent -- an
    # iron-audit SHOULD-FIX so the owner isn't offered "Resend" again with
    # no memory a correction already happened.
    from slap.queue import resend_bounced

    workdir_root = tmp_path / "workdir"
    attachment = tmp_path / "attach.pdf"
    attachment.write_bytes(b"%PDF-fake")
    stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"], attachment_path=attachment,
        attachment_name="Resume.pdf", latex_enabled=True, company="Acme", workdir_root=workdir_root,
    )
    append_event(conn, type="sent", recipient="jane@acme.com", campaign="c", stage=0, gmass_campaign_id="1")
    append_event(conn, type="bounce", recipient="jane@acme.com", campaign="c",
                 meta={"bounce_reason": "550 no such user", "bounce_time": "t1", "category": "bounce"})

    resend_bounced(conn, original_recipient="jane@acme.com", corrected_email="jane.doe@acme.com",
                    workdir_root=workdir_root)

    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    assert rows["jane@acme.com"]["already_corrected_to"] == [{"recipient": "jane.doe@acme.com", "status": "active"}]
    assert rows["jane.doe@acme.com"]["already_corrected_to"] == []


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


# --- reachouts_rows: OOO status/resume-date (investigation + fix) ------------
#
# Investigation finding: status and reply_tag ALREADY correctly reflect an
# unconditional Mark-OOO with zero prior reply — both _apply_event_to_cache's
# ooo_tagged handler and reply_tags()'s resolution rule key off the
# ooo_tagged event itself, never a prior reply. The real, confirmed gap was
# that the actual resume date was never surfaced on the row at all.
# Reuses the module's existing _fake_unsubscribe() helper (defined above,
# near tag_reply's other tests) rather than a second, redundant fake.

def test_reachouts_rows_ooo_with_zero_prior_reply_shows_status_and_resume_date(conn):
    # The exact case the task calls out as possibly falling through a gap:
    # marking OOO with no prior reply/engagement whatsoever.
    _stage_and_send(conn, recipient="cold@company.com", campaign="c", persona="recruiter")
    tag_reply(conn, "cold@company.com", "ooo", resume_date=date(2026, 8, 15),
              api_key="fake", unsubscribe_fn=_fake_unsubscribe)

    row = reachouts_rows(conn)[0]
    assert row["status"] == "ooo_requeued"
    assert row["reply_tag"] == "ooo"
    assert row["ooo_resume_date"] == "2026-08-15"


def test_reachouts_rows_ooo_resume_date_none_for_a_normal_recipient(conn):
    _stage_and_send(conn, recipient="normal@x.com", campaign="c", persona="recruiter")
    assert reachouts_rows(conn)[0]["ooo_resume_date"] is None


def test_reachouts_rows_bounce_reason_shown_for_a_bounced_recipient(conn):
    # Previously: this page showed a bare "bounced" status with no detail
    # on why -- same gap the Bounces & Blocks widget had.
    _stage_and_send(conn, recipient="dead@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="bounce", recipient="dead@x.com", campaign="c",
                 meta={"bounce_reason": "mailbox full", "bounce_time": "t1", "category": "bounce"})
    row = reachouts_rows(conn)[0]
    assert row["status"] == "bounced"
    assert row["bounce_reason"] == "mailbox full"


def test_reachouts_rows_bounce_reason_none_for_a_normal_recipient(conn):
    _stage_and_send(conn, recipient="normal@x.com", campaign="c", persona="recruiter")
    assert reachouts_rows(conn)[0]["bounce_reason"] is None


def test_reachouts_rows_ooo_resume_date_none_for_legacy_ooo_tagged_without_a_resume_date(conn):
    # A pre-resume_date-feature ooo_tagged event (no resume_date in its meta)
    # has no genuine specific date to show — _pending_ooo_resume_date treats
    # it as date.min ("immediately due"), which must not be rendered as a
    # fabricated date.
    _stage_and_send(conn, recipient="legacy@x.com", campaign="c", persona="recruiter")
    append_event(conn, type="ooo_tagged", recipient="legacy@x.com", campaign="c", meta={})

    row = reachouts_rows(conn)[0]
    assert row["status"] == "ooo_requeued"
    assert row["ooo_resume_date"] is None


def test_reachouts_rows_status_and_resume_date_revert_after_resume_fires(conn):
    # Task recommendation 3: once the resume date passes and the sequence
    # continues normally, the row should reflect the recipient's current
    # normal status, not a stale OOO badge -- this is already the existing
    # behavior (requeued's _apply_event_to_cache handler flips status back),
    # pinned here as a regression test.
    _stage_and_send(conn, recipient="cold@company.com", campaign="c", persona="recruiter")
    tag_reply(conn, "cold@company.com", "ooo", resume_date=date(2026, 7, 1),
              api_key="fake", unsubscribe_fn=_fake_unsubscribe)
    assert reachouts_rows(conn)[0]["status"] == "ooo_requeued"

    # Simulate the runner's OOO resend actually firing (cadence exhausted --
    # a single-stage persona has no next_resume_date).
    append_event(conn, type="requeued", recipient="cold@company.com", campaign="c", stage=1,
                 gmass_campaign_id="2", gmass_draft_id="d2", meta=None)

    row = reachouts_rows(conn)[0]
    assert row["status"] == "active"
    assert row["ooo_resume_date"] is None
    # reply_tag is a known, pre-existing exception: reply_tags() only
    # updates on a later reply/ooo_tagged/reply_reviewed event, and
    # `requeued` isn't one of those, so it stays 'ooo' -- exactly why status,
    # not reply_tag, is the dimension wired up as the "currently OOO" filter.
    assert row["reply_tag"] == "ooo"


def test_reachouts_rows_ooo_resume_date_none_mid_multi_stage_continuation(conn):
    # A pending next_resume_date for a LATER stage (status already back to
    # 'active' between resends) is just the persona's normal inter-stage gap
    # -- not "still OOO" -- so it must not render an OOO resume date either.
    _stage_and_send(conn, recipient="cold@company.com", campaign="c", persona="recruiter")
    tag_reply(conn, "cold@company.com", "ooo", resume_date=date(2026, 7, 1),
              api_key="fake", unsubscribe_fn=_fake_unsubscribe)
    append_event(conn, type="requeued", recipient="cold@company.com", campaign="c", stage=1,
                 gmass_campaign_id="2", gmass_draft_id="d2",
                 meta={"next_resume_date": "2026-07-10"})

    row = reachouts_rows(conn)[0]
    assert row["status"] == "active"
    assert row["ooo_resume_date"] is None


def test_reachouts_rows_ooo_via_widget_and_via_reachouts_render_identically(conn):
    # Parity requirement: both OOO entry points -- the original reply-tag
    # widget (dashboard.html, gated on a detected reply) and the Reach-outs
    # row's unconditional "Mark OOO" action -- hit the same route and call
    # the same tag_reply()/_tag_ooo() function (see tag_reply's own
    # docstring), so for recipients in the same underlying state, tagging
    # OOO must produce identical rows regardless of which entry point is
    # conceptually behind the call. Both recipients here have a prior reply
    # (the widget's own gating precondition), isolating "does the entry
    # point matter" from "does having replied matter" (already covered by
    # the zero-prior-reply test above).
    ts = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    _stage_and_send(conn, recipient="widget@x.com", campaign="c", persona="recruiter", timestamp=ts)
    append_event(conn, type="reply", recipient="widget@x.com", campaign="c", timestamp=ts)
    tag_reply(conn, "widget@x.com", "ooo", resume_date=date(2026, 8, 15),
              api_key="fake", unsubscribe_fn=_fake_unsubscribe)

    _stage_and_send(conn, recipient="direct@x.com", campaign="c", persona="recruiter", timestamp=ts)
    append_event(conn, type="reply", recipient="direct@x.com", campaign="c", timestamp=ts)
    tag_reply(conn, "direct@x.com", "ooo", resume_date=date(2026, 8, 15),
              api_key="fake", unsubscribe_fn=_fake_unsubscribe)

    rows = {r["recipient"]: r for r in reachouts_rows(conn)}
    widget_row = {k: v for k, v in rows["widget@x.com"].items() if k != "recipient"}
    direct_row = {k: v for k, v in rows["direct@x.com"].items() if k != "recipient"}
    assert widget_row == direct_row


# --- filter_reachouts (pure function — no DB needed) --------------------------

def _row(**overrides):
    base = {
        "recipient": "jane@acme.com", "campaign": "coldpost-recruiter", "persona": "recruiter",
        "status": "active", "engagement": "none", "reply_tag": None, "domain": "acme.com",
        "company": "Acme", "req_id_present": False, "date": "2026-06-15T00:00:00+00:00",
        "date_local": "2026-06-15", "ooo_resume_date": None,
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


def test_filter_reachouts_by_status_filters_down_to_currently_ooo():
    # "Make it filterable" requirement: status is the dimension wired up for
    # "who's currently OOO" (see reachouts_rows()'s docstring for why status,
    # not reply_tag) -- reachouts.html's Status filter already dynamically
    # lists every live status value with no template change needed, so this
    # pins that filter_reachouts() itself already supports it correctly.
    rows = [
        _row(recipient="ooo@x.com", status="ooo_requeued", ooo_resume_date="2026-08-15"),
        _row(recipient="active@x.com", status="active"),
    ]
    result = filter_reachouts(rows, {"status": "ooo_requeued"})
    assert [r["recipient"] for r in result] == ["ooo@x.com"]


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

def _stage_and_bounce(conn, tmp_path, *, recipient, campaign="c", company=""):
    attachment = tmp_path / f"{recipient.replace('@', '-')}.pdf"
    attachment.write_bytes(b"%PDF-fake")
    stage_recipient(
        conn, campaign=campaign, recipient=recipient, persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="Resume.pdf", latex_enabled=True, company=company,
    )
    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0, gmass_campaign_id="1")
    append_event(conn, type="bounce", recipient=recipient, campaign=campaign,
                 meta={"bounce_reason": "550 no such user", "bounce_time": "t1", "category": "bounce"})


# --- Template Failures tab (slap.reload) ------------------------------------

def _make_failure(recipient="jane@acme.com", campaign="coldpost", reason="template now references "
                   "field(s) this recipient has no stored value for: special_note",
                   missing_fields=None):
    return ReloadFailure(
        recipient=recipient, campaign=campaign, reason=reason,
        missing_fields=missing_fields if missing_fields is not None else ["special_note"],
        attempted_at="2026-07-10T12:00:00+00:00",
    )


def test_template_failures_helper_reads_written_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert template_failures() == []
    write_failures([_make_failure()])
    result = template_failures()
    assert len(result) == 1
    assert result[0]["recipient"] == "jane@acme.com"


# --- Logs page: recent_events / event_display / read_log_tail / /logs -----

def test_recent_events_orders_newest_first_and_respects_limit(conn):
    append_event(conn, type="run_started")
    append_event(conn, type="queued", recipient="jane@acme.com", campaign="c")
    append_event(conn, type="sent", recipient="jane@acme.com", campaign="c", stage=0,
                 meta={"is_final_stage": False})

    all_events = recent_events(conn)
    assert [e["type"] for e in all_events] == ["sent", "queued", "run_started"]  # newest first
    assert all_events[0]["meta"] == {"is_final_stage": False}  # meta JSON-decoded, not a raw string

    limited = recent_events(conn, limit=2)
    assert [e["type"] for e in limited] == ["sent", "queued"]


def test_recent_events_meta_defaults_to_empty_dict_when_absent(conn):
    append_event(conn, type="run_started")
    [ev] = recent_events(conn)
    assert ev["meta"] == {}


def test_event_display_sent_and_final_stage():
    assert event_display({"type": "sent", "meta": {"is_final_stage": False}}) == {
        "label": "Sent", "chip": "chip-good", "detail": "",
    }
    assert event_display({"type": "sent", "meta": {"is_final_stage": True}})["detail"] == "final stage"


def test_event_display_send_failed_shows_stage_and_error():
    d = event_display({"type": "send_failed", "meta": {"stage": "create_draft", "error": "boom"}})
    assert d["chip"] == "chip-critical"
    assert d["detail"] == "create_draft: boom"


def test_event_display_run_completed_chip_reflects_failures():
    good = event_display({"type": "run_completed", "meta": {"sent": 3, "failed": 0, "remaining_queued": 0}})
    assert good["chip"] == "chip-good"
    assert good["detail"] == "3 sent, 0 failed, 0 remaining"

    serious = event_display({"type": "run_completed", "meta": {"sent": 1, "failed": 2, "remaining_queued": 0}})
    assert serious["chip"] == "chip-serious"


def test_event_display_bounce_vs_block_category():
    bounce = event_display({"type": "bounce", "meta": {"category": "bounce", "bounce_reason": "mailbox full"}})
    assert bounce["label"] == "Bounced"
    assert bounce["detail"] == "mailbox full"

    block = event_display({"type": "bounce", "meta": {"category": "block", "bounce_reason": "spam"}})
    assert block["label"] == "Blocked"


def test_event_display_unknown_type_never_crashes():
    d = event_display({"type": "some_future_type", "meta": {"whatever": 1}})
    assert d["label"] == "some_future_type"
    assert d["chip"] == "chip-neutral"


def test_read_log_tail_returns_last_n_lines_newest_first(tmp_path):
    path = tmp_path / "runner.log"
    path.write_text("\n".join(f"line {i}" for i in range(1, 6)) + "\n")

    assert read_log_tail(path, n_lines=3) == ["line 5", "line 4", "line 3"]


def test_read_log_tail_missing_file_returns_empty_list(tmp_path):
    assert read_log_tail(tmp_path / "does-not-exist.log") == []


