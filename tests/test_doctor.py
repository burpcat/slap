"""Doctor preflight tests (Build Order step 12), per SLAP_BUILD_PROMPT.md §11:
GMASS_API_KEY present; config sender fields set; SQLite reachable;
consumer_domains.txt present (seeded if missing); attachment resolvable
(xelatex+code on PATH when latex is on, attachment_file exists when off).
"""
from unittest.mock import patch

import pytest

from slap.config import CampaignConfig, CampaignField, GlobalConfig, ScheduleConfig
from slap.doctor import (
    DEFAULT_CONSUMER_DOMAINS, check_api_key, check_attachment, check_consumer_domains,
    check_db, check_sender_fields, run_campaign_checks, run_global_checks,
)
from slap.tracking import connect


def make_global_config(tmp_path, *, from_email="owner@gmail.com", from_name="Owner",
                        api_key_env="GMASS_API_KEY", consumer_domains_file=None):
    return GlobalConfig(
        from_email=from_email, from_name=from_name, api_key_env=api_key_env,
        personas={"recruiter": [2, 3, 5]},
        schedule=ScheduleConfig(fire_window_start="09:00", fire_window_end="09:15",
                                 send_delay_min=10, send_delay_max=15,
                                 daily_cap=500, drain_retries=3,
                                 active_days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"]),
        consumer_domains_file=consumer_domains_file or str(tmp_path / "consumer_domains.txt"),
        path=tmp_path / "config.yaml",
    )


def make_campaign_config(tmp_path, *, latex_enabled=False, attachment_file="resume.pdf"):
    campaign_path = tmp_path / "campaigns" / "coldpost"
    campaign_path.mkdir(parents=True, exist_ok=True)
    return CampaignConfig(
        name="coldpost", path=campaign_path, persona="recruiter", cadence=[2, 3, 5],
        latex_enabled=latex_enabled, attachment_name="r.pdf",
        attachment_file=None if latex_enabled else attachment_file,
        fields=[CampaignField(key="email", label="Email")],
        subject_template="Hi", body_template="Body", stage_bodies=["s1", "s2", "s3"],
    )


# --- check_api_key -----------------------------------------------------

def test_check_api_key_passes_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("GMASS_API_KEY", "real-key")
    result = check_api_key(make_global_config(tmp_path))
    assert result.ok


def test_check_api_key_fails_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("GMASS_API_KEY", raising=False)
    result = check_api_key(make_global_config(tmp_path))
    assert not result.ok
    assert "not set" in result.detail


def test_check_api_key_fails_when_blank(tmp_path, monkeypatch):
    monkeypatch.setenv("GMASS_API_KEY", "   ")
    result = check_api_key(make_global_config(tmp_path))
    assert not result.ok


# --- check_sender_fields -------------------------------------------------

def test_check_sender_fields_passes_when_both_set(tmp_path):
    result = check_sender_fields(make_global_config(tmp_path))
    assert result.ok


def test_check_sender_fields_fails_when_email_blank(tmp_path):
    result = check_sender_fields(make_global_config(tmp_path, from_email=""))
    assert not result.ok


def test_check_sender_fields_fails_when_name_blank(tmp_path):
    result = check_sender_fields(make_global_config(tmp_path, from_name="   "))
    assert not result.ok


# --- check_db ------------------------------------------------------------

def test_check_db_passes_with_a_given_open_connection(tmp_path):
    conn = connect(tmp_path / "test.db")
    result = check_db(conn)
    assert result.ok


def test_check_db_fails_when_given_connection_is_closed(tmp_path):
    conn = connect(tmp_path / "test.db")
    conn.close()
    result = check_db(conn)
    assert not result.ok


def test_check_db_opens_its_own_connection_when_none_given(tmp_path, monkeypatch):
    # No conn provided -> falls back to tracking.connect()'s default
    # cwd-relative path, so isolate cwd to avoid touching the real repo.
    monkeypatch.chdir(tmp_path)
    result = check_db()
    assert result.ok
    assert (tmp_path / "slap.db").exists()


# --- check_consumer_domains -----------------------------------------------

def test_check_consumer_domains_passes_without_writing_when_already_present(tmp_path):
    gc = make_global_config(tmp_path)
    path = tmp_path / "consumer_domains.txt"
    path.write_text("custom.com\n")
    result = check_consumer_domains(gc)
    assert result.ok
    assert result.detail == ""
    assert path.read_text() == "custom.com\n"  # untouched, not overwritten


def test_check_consumer_domains_seeds_default_when_missing(tmp_path):
    gc = make_global_config(tmp_path)
    path = tmp_path / "consumer_domains.txt"
    assert not path.exists()
    result = check_consumer_domains(gc)
    assert result.ok
    assert "seeded" in result.detail
    assert path.exists()
    seeded = {line.strip() for line in path.read_text().splitlines() if line.strip()}
    assert seeded == set(DEFAULT_CONSUMER_DOMAINS)


# --- run_global_checks -----------------------------------------------------

def test_run_global_checks_returns_all_four_and_reuses_given_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("GMASS_API_KEY", "real-key")
    conn = connect(tmp_path / "test.db")
    gc = make_global_config(tmp_path)
    results = run_global_checks(gc, conn)
    assert {r.name for r in results} == {"GMASS_API_KEY", "sender fields", "SQLite DB", "consumer_domains.txt"}
    assert all(r.ok for r in results)


# --- check_attachment / run_campaign_checks --------------------------------

def test_check_attachment_latex_off_passes_when_file_exists(tmp_path):
    campaign = make_campaign_config(tmp_path, latex_enabled=False)
    (campaign.path / campaign.attachment_file).write_bytes(b"%PDF-fake")
    results = check_attachment(campaign)
    assert len(results) == 1
    assert results[0].ok


def test_check_attachment_latex_off_fails_when_file_missing(tmp_path):
    campaign = make_campaign_config(tmp_path, latex_enabled=False)
    results = check_attachment(campaign)
    assert len(results) == 1
    assert not results[0].ok
    assert "not found" in results[0].detail


def test_check_attachment_latex_on_checks_xelatex_and_code(tmp_path):
    campaign = make_campaign_config(tmp_path, latex_enabled=True)
    with patch("slap.doctor.shutil.which", return_value="/usr/bin/fake"):
        results = check_attachment(campaign)
    assert {r.name for r in results} == {"xelatex", "code"}
    assert all(r.ok for r in results)


def test_check_attachment_latex_on_fails_when_binary_missing(tmp_path):
    campaign = make_campaign_config(tmp_path, latex_enabled=True)

    def which(binary):
        return None if binary == "xelatex" else "/usr/bin/fake"

    with patch("slap.doctor.shutil.which", side_effect=which):
        results = check_attachment(campaign)
    by_name = {r.name: r for r in results}
    assert not by_name["xelatex"].ok
    assert "not found on PATH" in by_name["xelatex"].detail
    assert by_name["code"].ok


def test_run_campaign_checks_matches_check_attachment(tmp_path):
    campaign = make_campaign_config(tmp_path, latex_enabled=False)
    (campaign.path / campaign.attachment_file).write_bytes(b"%PDF-fake")
    assert run_campaign_checks(campaign) == check_attachment(campaign)
