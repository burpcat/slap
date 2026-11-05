"""slap_mcp contract: preview renders exactly what send stages, once, and
dedup warnings refuse a send until acknowledged. Tool functions are called
directly (MCPServer.tool() returns the undecorated function)."""
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import slap_mcp
from slap import runner, smtp, tracking
from slap.latex import recipient_workdir
from slap.queue import load_manifest

CONFIG = """
sender: { from_email: everythingforgenius@gmail.com, from_name: Test Owner }
signature: "Test Owner"
personas: { recruiter: { stages: [2, 5] } }
schedule:
  fire_window_start: "09:00"
  fire_window_end: "09:15"
  send_delay_min: 0
  send_delay_max: 0
  daily_cap: 500
  drain_retries: 0
  active_days: [mon, tue, wed, thu, fri, sat, sun]
tracking: { consumer_domains_file: consumer_domains.txt }
docs_dir: docs
send_tags: { ai: ai.pdf }
references_dir: refs
"""

CAMPAIGN = """
persona: recruiter
cadence: [2, 5]
latex: { enabled: false, attachment_name: Resume.pdf }
name_field: name
fields:
  - { key: email,   label: Email }
  - { key: company, label: Company, hint: as written in the JD }
  - { key: name,    label: Name }
  - { key: signal,  label: Signal, optional: true }
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(smtp.PASSWORD_ENV, "app-password")
    monkeypatch.setattr(slap_mcp, "_previews", {})
    (tmp_path / "config.yaml").write_text(CONFIG)
    (tmp_path / "consumer_domains.txt").write_text("gmail.com\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ai.pdf").write_bytes(b"%PDF-1.4 real resume")
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs" / "star.json").write_text(json.dumps({"experiences": [
        {"id": "s1", "title": "Shipped X", "tags": ["llm"], "core": "anything else passes through"},
    ]}))
    camp = tmp_path / "campaigns" / "recruiter"
    camp.mkdir(parents=True)
    (camp / "campaign.yaml").write_text(CAMPAIGN)
    (camp / "initial.txt").write_text("Subject: Hi from {{company}}\n\nHi {{name}},\n{{signal}}\nThanks\n")
    (camp / "stage1.txt").write_text("Bump {{name}}\n")
    (camp / "stage2.txt").write_text("Last bump {{name}}\n")
    return tmp_path


FIELDS = {"email": "jane@acme.com", "company": "Acme", "name": "Jane"}


def _queued(recipient):
    rows = tracking.connect().execute(
        "SELECT * FROM events WHERE type='queued' AND recipient=?", (recipient,)).fetchall()
    return rows


def test_context_lists_campaign_hints_stars_and_stale_card(repo):
    ctx = slap_mcp.slap_context()
    (camp,) = ctx["campaigns"]
    assert camp["name"] == "recruiter"
    assert {"key": "company", "optional": False, "hint": "as written in the JD"} in camp["fields"]
    assert ctx["stars"] == [{"id": "s1", "title": "Shipped X", "tags": ["llm"]}]
    assert ctx["resumes"] == [{"tag": "ai", "file": "ai.pdf", "card": None, "stale": True}]


def test_saved_card_goes_stale_when_pdf_changes(repo):
    slap_mcp.slap_save_derived("card:ai", "resume:ai", "AI eng card")
    assert slap_mcp.slap_context()["resumes"][0] == {"tag": "ai", "file": "ai.pdf", "card": "AI eng card",
                                                     "stale": False}
    (repo / "docs" / "ai.pdf").write_bytes(b"%PDF-1.4 edited")
    assert slap_mcp.slap_context()["resumes"][0]["stale"] is True


def test_stars_passes_entries_through_and_rejects_unknown(repo):
    assert slap_mcp.slap_stars(["s1"])[0]["core"] == "anything else passes through"
    with pytest.raises(ToolError, match="unknown STAR"):
        slap_mcp.slap_stars(["nope"])


def test_star_entry_missing_required_key_fails_loud(repo):
    (repo / "refs" / "star.json").write_text(json.dumps({"experiences": [{"id": "s1"}]}))
    with pytest.raises(ToolError, match=r"experiences\[0\] is missing \['title', 'tags'\]"):
        slap_mcp.slap_stars(["s1"])


def test_preview_then_send_stages_exactly_the_preview_once(repo):
    p = slap_mcp.slap_preview("recruiter", "ai", FIELDS)
    assert p["subject"] == "Hi from Acme"
    assert "Hi Jane," in p["body"] and "{{" not in p["body"]  # empty optional signal line dropped
    assert p["warnings"] == [] and p["empty_required"] == []

    assert slap_mcp.slap_send(p["preview_id"]) == {"staged": "jane@acme.com"}
    assert len(_queued("jane@acme.com")) == 1
    manifest = load_manifest(recipient_workdir("recruiter", "jane@acme.com"))
    assert (manifest["subject"], manifest["body"]) == (p["subject"], p["body"])
    assert manifest["stage_bodies"] == ["Bump Jane", "Last bump Jane"]

    with pytest.raises(ToolError, match="unknown preview_id"):
        slap_mcp.slap_send(p["preview_id"])
    assert len(_queued("jane@acme.com")) == 1


def test_followups_override_truncates_cadence(repo):
    p = slap_mcp.slap_preview("recruiter", "ai", FIELDS, followups=1)
    assert [f["day"] for f in p["followups"]] == [2]
    with pytest.raises(ToolError, match="followups must be 0-2"):
        slap_mcp.slap_preview("recruiter", "ai", FIELDS, followups=3)


def test_hard_warn_refuses_send_until_acknowledged(repo):
    slap_mcp.slap_send(slap_mcp.slap_preview("recruiter", "ai", FIELDS)["preview_id"])
    p = slap_mcp.slap_preview("recruiter", "ai", FIELDS)
    assert p["warnings"][0].startswith("HARD:")
    with pytest.raises(ToolError, match="acknowledge_warnings"):
        slap_mcp.slap_send(p["preview_id"])
    slap_mcp.slap_send(p["preview_id"], acknowledge_warnings=True)
    assert len(_queued("jane@acme.com")) == 2


def test_unknown_field_key_and_bad_resume_fail_loud(repo):
    with pytest.raises(ToolError, match="unknown field keys"):
        slap_mcp.slap_preview("recruiter", "ai", {**FIELDS, "nmae": "typo"})
    with pytest.raises(ToolError, match="doesn't offer résumé"):
        slap_mcp.slap_preview("recruiter", "nope", FIELDS)


def test_send_now_drains_staged_only(repo, monkeypatch):
    calls = {}

    def fake_drain(conn, global_config, smtp_config, **kw):
        calls.update(kw, password=smtp_config.password)
        return runner.DrainResult(ran=True, sent=1)

    monkeypatch.setattr(runner, "drain", fake_drain)
    out = slap_mcp.slap_send(slap_mcp.slap_preview("recruiter", "ai", FIELDS)["preview_id"], now=True)
    assert out["drain"]["sent"] == 1
    assert calls["staged_only"] is True and calls["password"] == "app-password"
