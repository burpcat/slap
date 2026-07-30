"""Tests for the JSON API layer (slap/api.py), registered onto the same
Flask app create_app() (slap/dashboard.py) already builds for the Jinja
routes. Mirrors tests/test_dashboard.py's fixtures (FakeRedis,
make_global_config, _ImmediateThread, the `app` fixture) rather than
importing them cross-module, so this file stays self-contained and doesn't
couple to test_dashboard.py's own internals.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from werkzeug.serving import make_server

import redis as redis_lib

from slap.config import GlobalConfig, ScheduleConfig
from slap import gmass_cache
from slap.dashboard import create_app
from slap.queue import stage_recipient
from slap.tracking import append_event, connect


class FakeRedis:
    """Same minimal in-memory Redis stand-in as test_dashboard.py's."""
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


class _ImmediateThread:
    """threading.Thread stand-in that runs synchronously on .start() — see
    test_dashboard.py's identical fixture for the full reasoning."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def make_global_config(*, daily_cap=500, weekly_target=None):
    return GlobalConfig(
        from_email="owner@gmail.com", from_name="Owner", api_key_env="GMASS_API_KEY",
        personas={"recruiter": [2, 3, 5], "founder": [2, 5, 7], "hiring_manager": [2, 4, 6]},
        schedule=ScheduleConfig(fire_window_start="09:00", fire_window_end="09:15",
                                 send_delay_min=10, send_delay_max=15,
                                 daily_cap=daily_cap, drain_retries=3,
                                 active_days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                                 weekly_target=weekly_target),
        consumer_domains_file="consumer_domains.txt", path="config.yaml",
    )


def seed_sent_recipient(conn, recipient="jane@acme.com", campaign="c", persona="recruiter",
                         company="", campaign_id="555"):
    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": persona, "company": company})
    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0,
                 gmass_campaign_id=campaign_id)


def stage_and_bounce(conn, tmp_path, *, recipient, campaign="c", company=""):
    # Mirrors test_dashboard.py's _stage_and_bounce helper: resend_bounced()
    # (slap.queue) needs a REAL staged manifest/attachment to recover from
    # (see that function's own docstring), not just a bare `queued` event —
    # a bounce test built on seed_sent_recipient() alone 400s at the
    # "recover the original staged send" step, not at the assertion this
    # test actually wants to make.
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


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # today_strip()/discover_campaigns() read cwd
    # Runs get_gmass_dependent_data's background refresh synchronously, same
    # as test_dashboard.py's `app` fixture, so every assertion below sees a
    # deterministic, already-refreshed cache rather than racing a daemon
    # thread.
    monkeypatch.setattr("slap.dashboard.threading.Thread", _ImmediateThread)
    db_path = tmp_path / "test.db"
    connect(db_path).close()
    flask_app = create_app(db_path, make_global_config(), consumer_domains=set(), api_key="fake-key",
                            redis_client=FakeRedis())
    flask_app.db_path = db_path
    return flask_app


# --- GET endpoints: 200 + expected top-level keys ---------------------------

def test_api_home_returns_expected_keys(app):
    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        resp = app.test_client().get("/api/home")
    assert resp.status_code == 200
    body = resp.get_json()
    for key in ("sync_result", "replies", "cache_status", "today", "week", "runs", "next_drain",
                "follow_up_reminders", "pipeline", "companies"):
        assert key in body, key
    # `week` carries date objects (this_week()'s range_start/range_end) --
    # proves the custom JSON provider serializes them as plain ISO strings,
    # not Flask's default HTTP-date format.
    assert body["week"]["range_start"] == (date.today() - timedelta(days=6)).isoformat()


def test_api_pipeline_returns_expected_keys(app):
    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        resp = app.test_client().get("/api/pipeline")
    assert resp.status_code == 200
    body = resp.get_json()
    for key in ("today", "active_leads", "follow_up_reminders", "pipeline", "companies", "bounces",
                "stopped_outreach"):
        assert key in body, key


def test_api_engagement_returns_expected_keys(app):
    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        resp = app.test_client().get("/api/engagement")
    assert resp.status_code == 200
    body = resp.get_json()
    for key in ("sync_result", "cache_status", "engagement", "warm_but_silent",
                "warm_but_silent_hidden_count", "show_hidden", "engagement-analytics"):
        assert key in body, key
    assert body["show_hidden"] is False
    analytics = body["engagement-analytics"]
    for key in ("trend", "bounce_data", "reply_rate_by_persona", "time_to_first_reply", "weekly_goal"):
        assert key in analytics, key


def test_api_engagement_show_hidden_query_param(app):
    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        resp = app.test_client().get("/api/engagement?show_hidden=1")
    assert resp.status_code == 200
    assert resp.get_json()["show_hidden"] is True


def test_api_campaigns_default_summary(app, tmp_path):
    campaigns_dir = tmp_path / "campaigns" / "coldpost"
    campaigns_dir.mkdir(parents=True)
    (campaigns_dir / "campaign.yaml").write_text("persona: recruiter\n")
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="coldpost")
    conn.close()

    resp = app.test_client().get("/api/campaigns")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "campaigns" in body
    names = [c["campaign"] for c in body["campaigns"]]
    assert "coldpost" in names


def test_api_campaigns_filtered_by_query_param(app, tmp_path):
    campaigns_dir = tmp_path / "campaigns" / "coldpost"
    campaigns_dir.mkdir(parents=True)
    (campaigns_dir / "campaign.yaml").write_text("persona: recruiter\n")
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="coldpost")
    conn.close()

    resp = app.test_client().get("/api/campaigns?campaign=coldpost")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["campaign"] == "coldpost"
    assert body["recipient_count"] == 1


def test_api_campaigns_unknown_campaign_404(app):
    resp = app.test_client().get("/api/campaigns?campaign=nope")
    assert resp.status_code == 404


def test_api_campaigns_includes_per_campaign_color(app, tmp_path):
    campaigns_dir = tmp_path / "campaigns" / "coldpost"
    campaigns_dir.mkdir(parents=True)
    (campaigns_dir / "campaign.yaml").write_text("persona: recruiter\n")
    resp = app.test_client().get("/api/campaigns?campaign=coldpost")
    body = resp.get_json()
    # Both-mode colors ride along so a chip can theme without a second request.
    assert set(body["color"]) == {"light", "dark"}
    assert body["color"]["light"].startswith("#") and body["color"]["dark"].startswith("#")


def test_api_reachouts_includes_campaign_color_map(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="coldpost")
    conn.close()
    resp = app.test_client().get("/api/reachouts")
    body = resp.get_json()
    assert "campaign_colors" in body
    assert set(body["campaign_colors"]["coldpost"]) == {"light", "dark"}


def test_api_commands_derived_from_argparse(app):
    # The Commands tab is derived live from build_parser() (slap.py), so real
    # subcommands appear without hand-maintaining a list. A Python-internals
    # change to argparse would break this — catching it here, not silently.
    resp = app.test_client().get("/api/commands")
    assert resp.status_code == 200
    commands = resp.get_json()["commands"]
    names = {c["name"] for c in commands}
    # A representative slice of the real CLI surface must be present.
    assert {"list", "send", "dashboard", "doctor", "runner"} <= names
    send = next(c for c in commands if c["name"] == "send")
    assert send["help"]  # help text carried through
    assert any(a["name"] == "campaign" for a in send["args"])  # positional arg surfaced
    # Usage must be plain text — no leaked ANSI color escapes (argparse 3.13+
    # colorizes format_usage()).
    assert "\x1b" not in send["usage"]
    assert "[1;3" not in send["usage"]
    # Every command carries at least one concrete example invocation.
    assert all(c["examples"] for c in commands)
    assert any("slap.py send" in ex for ex in send["examples"])


def test_api_reachouts_returns_rows_and_total_count(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="c")
    conn.close()

    resp = app.test_client().get("/api/reachouts")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total_count"] == 1
    assert body["rows"][0]["recipient"] == "a@x.com"


def test_api_logs_returns_expected_keys(app):
    resp = app.test_client().get("/api/logs")
    assert resp.status_code == 200
    body = resp.get_json()
    for key in ("events", "total_count", "limit", "truncated", "event_types", "logs"):
        assert key in body, key


def test_api_template_failures_returns_expected_keys(app):
    resp = app.test_client().get("/api/template-failures")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"failures": [], "total_count": 0}


def test_api_sync_status_returns_expected_keys(app):
    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        resp = app.test_client().get("/api/sync-status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body.keys()) == {"sync_result", "cache_status"}


def test_api_nav_returns_expected_keys(app):
    resp = app.test_client().get("/api/nav")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body.keys()) == {"template_failures_count", "runner_staleness_warning"}


# --- POST endpoints ----------------------------------------------------

def test_api_reply_tag_invalid_resume_date_returns_400(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    conn.close()

    resp = app.test_client().post("/api/reply/a@x.com/tag", json={"tag": "ooo", "resume_date": "not-a-date"})
    assert resp.status_code == 400
    assert "invalid resume_date" in resp.get_json()["error"]


def test_api_reply_tag_success_returns_ok_true(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    conn.close()

    # tag='real' takes the pure-bookkeeping path (no GMass call), same as
    # tag_reply()'s own docstring describes.
    resp = app.test_client().post("/api/reply/a@x.com/tag", json={"tag": "real"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    conn2 = connect(tmp_path / "test.db")
    row = conn2.execute(
        "SELECT meta FROM events WHERE recipient = ? AND type = 'reply_reviewed'", ("a@x.com",)
    ).fetchone()
    assert json.loads(row["meta"])["tag"] == "real"


def test_api_reply_tag_ooo_missing_resume_date_returns_400_with_valueerror_message(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    conn.close()

    resp = app.test_client().post("/api/reply/a@x.com/tag", json={"tag": "ooo"})
    assert resp.status_code == 400
    assert "resume_date" in resp.get_json()["error"]


def test_api_reply_tag_unsubscribe_failure_returns_502(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0, meta={"persona": "recruiter"})
    append_event(conn, type="reply", recipient="a@x.com", campaign="c")
    conn.close()

    with patch("slap.dashboard.gmass.unsubscribe_recipient", side_effect=RuntimeError("boom")):
        resp = app.test_client().post(
            "/api/reply/a@x.com/tag", json={"tag": "ooo", "resume_date": "2026-08-01"}
        )
    assert resp.status_code == 502
    assert "nothing was recorded" in resp.get_json()["error"]

    conn2 = connect(tmp_path / "test.db")
    row = conn2.execute("SELECT * FROM events WHERE recipient = ? AND type = 'ooo_tagged'", ("a@x.com",)).fetchone()
    assert row is None  # nothing recorded locally, matching tag_reply()'s guarantee


def test_api_stop_outreach_success(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="c")
    conn.close()

    with patch("slap.dashboard.gmass.unsubscribe_recipient", return_value={}):
        resp = app.test_client().post("/api/reachouts/a@x.com/stop")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_api_stop_outreach_failure_returns_502(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="c")
    conn.close()

    with patch("slap.dashboard.gmass.unsubscribe_recipient", side_effect=RuntimeError("boom")):
        resp = app.test_client().post("/api/reachouts/a@x.com/stop")
    assert resp.status_code == 502
    assert "nothing was recorded" in resp.get_json()["error"]


def test_api_resend_missing_corrected_email_returns_400(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="c")
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c",
                 meta={"bounce_reason": "550 no such user", "bounce_time": "t1", "category": "bounce"})
    conn.close()

    resp = app.test_client().post("/api/reachouts/a@x.com/resend", json={})
    assert resp.status_code == 400
    assert "corrected_email is required" in resp.get_json()["error"]


def test_api_resend_success_returns_ok_and_null_warning(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    stage_and_bounce(conn, tmp_path, recipient="a@acme.com", company="Acme")
    conn.close()

    resp = app.test_client().post(
        "/api/reachouts/a@acme.com/resend", json={"corrected_email": "a@othercorp.com"}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["warning"] is None

    conn2 = connect(tmp_path / "test.db")
    row = conn2.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@othercorp.com",)).fetchone()
    assert row is not None
    assert row["status"] == "active"


def test_api_resend_dedup_hit_returns_warning(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    stage_and_bounce(conn, tmp_path, recipient="a@acme.com", company="Acme")
    seed_sent_recipient(conn, recipient="a2@acme.com", campaign="c")
    conn.close()

    resp = app.test_client().post(
        "/api/reachouts/a@acme.com/resend", json={"corrected_email": "a2@acme.com"}
    )
    assert resp.status_code == 200
    assert "already contacted" in resp.get_json()["warning"]


def test_api_resend_not_bounced_returns_400(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="c")
    conn.close()

    resp = app.test_client().post("/api/reachouts/a@x.com/resend", json={"corrected_email": "b@x.com"})
    assert resp.status_code == 400


def test_api_hide_and_unhide_warm_but_silent(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="c")
    append_event(conn, type="click", recipient="a@x.com", campaign="c", stage=0,
                 meta={"url": "http://x", "click_time": "t1"})
    conn.close()

    client = app.test_client()
    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        # First load: no cache yet -- renders an honest empty state while a
        # (synchronous, in this fixture) background refresh populates it.
        # Second load: reads that now-fresh cache. Same two-request pattern
        # test_dashboard.py's own cache tests use (see e.g.
        # test_create_app_index_bounces_widget_shows_real_reason_text).
        client.get("/api/engagement")

    resp = client.post("/api/warm-but-silent/a@x.com/hide")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        hidden = client.get("/api/engagement").get_json()
    assert hidden["warm_but_silent_hidden_count"] == 1
    assert hidden["warm_but_silent"] == []

    resp = client.post("/api/warm-but-silent/a@x.com/unhide")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        unhidden = client.get("/api/engagement?show_hidden=1").get_json()
    assert unhidden["warm_but_silent_hidden_count"] == 0


def test_api_gmass_refresh_ok(app):
    resp = app.test_client().post("/api/gmass/refresh")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_api_gmass_refresh_redis_unavailable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("slap.dashboard.threading.Thread", _ImmediateThread)
    db_path = tmp_path / "test.db"
    connect(db_path).close()
    down_app = create_app(db_path, make_global_config(), consumer_domains=set(), api_key="fake-key",
                           redis_client=FakeRedisDown())

    resp = down_app.test_client().post("/api/gmass/refresh")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": False, "reason": "redis_unavailable"}


# --- threaded-server regression (same class of bug as test_dashboard.py's) --

@pytest.mark.slow
def test_api_survives_real_concurrent_request_threads(tmp_path, monkeypatch):
    # Analogous to test_dashboard.py's
    # test_dashboard_survives_real_concurrent_request_threads: the Flask test
    # client runs every request synchronously on the calling thread, so it
    # can never catch a sqlite3 connection being opened on one thread and
    # reused on another (check_same_thread=True by default). register_api()
    # reuses create_app()'s own per-request get_conn() closure, so this is
    # really re-proving the SAME fix applies to the new /api/* routes, not a
    # new code path -- a real threaded WSGI server is the only way to
    # reproduce the original bug at all.
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "test.db"
    connect(db_path).close()
    app = create_app(db_path, make_global_config(), consumer_domains=set(), api_key="fake-key",
                      redis_client=FakeRedis())

    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        server = make_server("127.0.0.1", 0, app, threaded=True)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            statuses = []
            for _ in range(5):
                try:
                    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/home")
                    statuses.append(resp.status)
                except urllib.error.HTTPError as e:
                    statuses.append(e.code)
            assert statuses == [200, 200, 200, 200, 200]
        finally:
            server.shutdown()
            thread.join()

        deadline = time.monotonic() + 5
        cached = None
        while time.monotonic() < deadline:
            cached = gmass_cache.read_cache(app.redis_client)
            if cached is not None:
                break
            time.sleep(0.05)
        assert cached is not None, "no background refresh completed within 5s — possible swallowed cross-thread error"


# --- interaction endpoints: LinkedIn-replied + followed-up ------------------

def test_api_linkedin_replied_gates_outreach_and_reflects_in_reachouts(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="c")
    conn.close()
    client = app.test_client()

    # Marking LinkedIn-replied now HALTS GMass outreach (a real unsubscribe),
    # so the endpoint fires gmass.unsubscribe_recipient and returns the new
    # gated status.
    with patch("slap.dashboard.gmass.unsubscribe_recipient", return_value={}) as unsub:
        resp = client.post("/api/reachouts/a@x.com/linkedin-replied", json={"replied": True})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "status": "linkedin-gate"}
    assert unsub.call_count == 1

    row = next(r for r in client.get("/api/reachouts").get_json()["rows"] if r["recipient"] == "a@x.com")
    assert row["linkedin_replied"] is True   # still lights the reply cell / Campaigns card
    assert row["linkedin_gated"] is True
    assert row["status"] == "linkedin-gate"
    assert row["chip"]["label"] == "LinkedIn"


def test_api_linkedin_replied_failure_returns_502(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="c")
    conn.close()

    with patch("slap.dashboard.gmass.unsubscribe_recipient", side_effect=RuntimeError("boom")):
        resp = app.test_client().post("/api/reachouts/a@x.com/linkedin-replied", json={"replied": True})
    assert resp.status_code == 502
    assert "nothing was recorded" in resp.get_json()["error"]


def test_api_linkedin_replied_ungate_not_supported_400(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="c")
    conn.close()
    # One-way, like Stop: an explicit un-gate fails loud and never hits GMass.
    with patch("slap.dashboard.gmass.unsubscribe_recipient") as unsub:
        resp = app.test_client().post("/api/reachouts/a@x.com/linkedin-replied", json={"replied": False})
    assert resp.status_code == 400
    assert unsub.call_count == 0


def test_api_linkedin_replied_unknown_recipient_404(app):
    # Unknown recipient fails loud before any GMass call (guard is first).
    resp = app.test_client().post("/api/reachouts/ghost@x.com/linkedin-replied", json={"replied": True})
    assert resp.status_code == 404


def test_api_followed_up_ok(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="c")
    conn.close()
    resp = app.test_client().post("/api/reachouts/a@x.com/followed-up", json={})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_api_followed_up_unknown_recipient_404(app):
    resp = app.test_client().post("/api/reachouts/ghost@x.com/followed-up", json={})
    assert resp.status_code == 404


# --- Remind endpoints -------------------------------------------------------

def _seed_real_lead_api(conn, recipient="a@acme.com", campaign="c"):
    append_event(conn, type="queued", recipient=recipient, campaign=campaign, stage=0,
                 meta={"persona": "recruiter", "cadence": []})
    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0, gmass_campaign_id="555")
    append_event(conn, type="reply", recipient=recipient, campaign=campaign)
    append_event(conn, type="reply_reviewed", recipient=recipient, campaign=campaign, meta={"tag": "real"})


def test_api_remind_ineligible_returns_400(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    seed_sent_recipient(conn, recipient="a@x.com", campaign="c")  # sent, not eligible
    conn.close()
    resp = app.test_client().post("/api/reachouts/a@x.com/remind", json={"body": "hi"})
    assert resp.status_code == 400


def test_api_remind_body_queues_for_real_lead(app, tmp_path):
    from slap.queue import due_for_remind
    conn = connect(tmp_path / "test.db")
    _seed_real_lead_api(conn, recipient="a@acme.com")
    conn.close()
    resp = app.test_client().post("/api/reachouts/a@acme.com/remind", json={"body": "circle back"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True

    conn = connect(tmp_path / "test.db")
    assert len(due_for_remind(conn)) == 1


def test_api_remind_save_title_creates_reusable_template(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed_real_lead_api(conn, recipient="a@acme.com")
    conn.close()
    client = app.test_client()
    resp = client.post("/api/reachouts/a@acme.com/remind", json={"body": "hi", "save_title": "My Nudge"})
    assert resp.status_code == 200
    assert resp.get_json()["followup"] == "my-nudge"
    listed = client.get("/api/followups").get_json()["followups"]
    assert any(f["slug"] == "my-nudge" for f in listed)


def test_api_remind_missing_body_and_slug_returns_400(app, tmp_path):
    conn = connect(tmp_path / "test.db")
    _seed_real_lead_api(conn, recipient="a@acme.com")
    conn.close()
    resp = app.test_client().post("/api/reachouts/a@acme.com/remind", json={})
    assert resp.status_code == 400


# --- company word-cloud roster ----------------------------------------------

def test_api_home_companies_include_all_companies_roster(app):
    with patch("slap.dashboard.gmass.get_reports", return_value=[]):
        companies = app.test_client().get("/api/home").get_json()["companies"]
    # Full roster is present and is a superset-shaped (domain, count) list — same
    # shape as top_companies, just not truncated to 5.
    assert "all_companies" in companies
    assert isinstance(companies["all_companies"], list)
