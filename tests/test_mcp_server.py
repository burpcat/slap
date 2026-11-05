"""The extension flow end to end at the slap level: capture (with campaign,
résumé, contact) -> Claude writes the drop -> applying stages the reach-out,
exactly once. MCP tool functions are called directly (MCPServer.tool()
returns the undecorated function)."""
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import slap_mcp
from slap import leads, outreach, runner, smtp, tracking
from slap.config import load_global_config
from slap.latex import recipient_workdir
from slap.queue import load_manifest

CONFIG = """
sender: { from_email: everythingforgenius@gmail.com, from_name: Test Owner }
signature: "Test Owner"
personas: { hiring_manager: { stages: [1, 2] } }
schedule:
  fire_window_start: "00:00"
  fire_window_end: "00:01"
  send_delay_min: 0
  send_delay_max: 0
  daily_cap: 500
  drain_retries: 0
  active_days: [mon, tue, wed, thu, fri, sat, sun]
tracking: { consumer_domains_file: consumer_domains.txt }
docs_dir: docs
send_tags: { resume-a: a.pdf, resume-b: b.pdf }
references_dir: refs
"""

CAMPAIGN = """
persona: hiring_manager
cadence: [1, 2]
latex: { enabled: false, attachment_name: Resume.pdf }
name_field: name
fields:
  - { key: email,       label: Email }
  - { key: role_catted, label: Role }
  - { key: company,     label: Company }
  - { key: name,        label: Name }
  - { key: why_line,    label: Why, hint: one sentence from the JD }
  - { key: signal,      label: Signal, optional: true }
"""

JOB = "https://jobs.lever.co/acme/123"
CONTACT = "hm+slaptest1@gmail.com"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(smtp.PASSWORD_ENV, "app-password")
    (tmp_path / "config.yaml").write_text(CONFIG)
    (tmp_path / "consumer_domains.txt").write_text("gmail.com\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.pdf").write_bytes(b"%PDF-1.4 resume a")
    (tmp_path / "docs" / "b.pdf").write_bytes(b"%PDF-1.4 resume b")
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs" / "star.json").write_text(json.dumps({"experiences": [
        {"id": "s1", "title": "Shipped X", "tags": ["llm"], "core": "passes through"}]}))
    camp = tmp_path / "campaigns" / "test-hm"
    camp.mkdir(parents=True)
    (camp / "campaign.yaml").write_text(CAMPAIGN)
    (camp / "initial.txt").write_text("Subject: Your {{role_catted}} opening at {{company}}\n\n"
                                      "Hi {{name}},\n{{why_line}}\n{{signal}}\nThanks\n")
    (camp / "stage1.txt").write_text("Bump {{name}}\n")
    (camp / "stage2.txt").write_text("Last {{name}}\n")
    return tmp_path


def _capture(**details):
    conn = tracking.connect()
    lead_id, _ = leads.capture(conn, url=JOB, jd="Build agents", company="Acme", role="ML Engineer",
                               **{"campaign": "test-hm", "resume": "resume-a", "contact_email": CONTACT,
                                  "contact_name": "Sam", **details})
    return conn, lead_id


def _apply(conn, resume="resume-a"):
    """What POST /api/leads/applied does."""
    pdf = "docs/a.pdf" if resume == "resume-a" else "docs/b.pdf"
    lead_id = leads.mark_applied(conn, url=JOB, resume=resume, resume_sha256=outreach._sha256(pdf))
    return outreach.stage(conn, load_global_config(), lead_id)


def _queued(conn):
    return conn.execute("SELECT recipient, meta FROM events WHERE type='queued'").fetchall()


DROP = {"why_line": "I've shipped LLM agents to production."}


def test_application_is_the_current_one_and_only_one(repo):
    with pytest.raises(ToolError, match="capture a posting"):
        slap_mcp.slap_application()
    _, lead_id = _capture()
    leads.capture(tracking.connect(), url="https://jobs.lever.co/other/9", jd="Other JD", company="Other")
    app = slap_mcp.slap_application()  # the most recently touched one
    assert app["company"] == "Other" and app["campaign"] is None and "no campaign" in app["drop"]
    app = slap_mcp.slap_application(lead_id)
    assert app["company"] == "Acme" and app["status"].startswith("not applied")
    assert [f["key"] for f in app["drop_fields"]] == ["role_catted", "company", "why_line", "signal"]
    assert app["stars"] == [{"id": "s1", "title": "Shipped X", "tags": ["llm"]}]
    assert app["email_preview"]["missing"] == ["why_line"]
    assert "jobs.lever.co/other" not in json.dumps(app)  # nothing about the other application


def test_drop_before_applied_then_applying_stages_once(repo):
    conn, lead_id = _capture()
    out = slap_mcp.slap_save_drop(lead_id, DROP)
    assert out["outreach"] == {"status": "not_applied"}
    assert out["email"]["subject"] == "Your ml engineer opening at Acme"
    assert "Hi Sam," in out["email"]["body"] and out["email"]["missing"] == []
    assert _queued(conn) == []

    assert _apply(conn) == {"status": "staged", "to": CONTACT}
    (row,) = _queued(conn)
    assert row["recipient"] == CONTACT and json.loads(row["meta"])["lead_id"] == lead_id
    manifest = load_manifest(recipient_workdir("test-hm", CONTACT))
    assert manifest["subject"] == out["email"]["subject"] and manifest["stage_bodies"] == ["Bump Sam", "Last Sam"]

    assert _apply(conn)["status"] == "already_staged"
    with pytest.raises(ToolError, match="already staged"):
        slap_mcp.slap_save_drop(lead_id, DROP)
    assert len(_queued(conn)) == 1


def test_applied_first_then_saving_the_drop_stages(repo):
    conn, lead_id = _capture()
    assert _apply(conn) == {"status": "incomplete", "missing": ["why_line"]}
    assert slap_mcp.slap_save_drop(lead_id, DROP)["outreach"] == {"status": "staged", "to": CONTACT}


def test_no_contact_waits_and_details_unblock_it(repo):
    conn, lead_id = _capture(contact_email=None)
    slap_mcp.slap_save_drop(lead_id, DROP)
    assert _apply(conn) == {"status": "incomplete", "missing": ["contact email"]}
    leads.set_details(conn, lead_id, contact_email=CONTACT)
    assert outreach.stage(conn, load_global_config(), lead_id)["status"] == "staged"


def test_already_contacted_needs_confirm(repo):
    conn, lead_id = _capture()
    tracking.append_event(conn, type="queued", recipient=CONTACT, campaign="old", stage=0, meta={"persona": "x"})
    slap_mcp.slap_save_drop(lead_id, DROP)
    out = _apply(conn)
    assert out["status"] == "needs_confirm" and out["warnings"][0].startswith("Already contacted")
    assert slap_mcp.slap_save_drop(lead_id, DROP, acknowledge_warnings=True)["outreach"]["status"] == "staged"


def test_drop_rejects_unknown_fields_and_contact_keys(repo):
    _, lead_id = _capture()
    with pytest.raises(ToolError, match="not drop fields"):
        slap_mcp.slap_save_drop(lead_id, {"email": "x@y.com"})
    with pytest.raises(ToolError, match="not drop fields"):
        slap_mcp.slap_save_drop(lead_id, {"why_lin": "typo"})


def test_resume_edited_after_applying_is_refused(repo):
    conn, lead_id = _capture()
    slap_mcp.slap_save_drop(lead_id, DROP)
    leads.mark_applied(conn, url=JOB, resume="resume-a", resume_sha256=outreach._sha256("docs/a.pdf"))
    (repo / "docs" / "a.pdf").write_bytes(b"%PDF-1.4 edited")
    with pytest.raises(outreach.OutreachError, match="changed since you applied"):
        outreach.stage(conn, load_global_config(), lead_id)
    assert _queued(conn) == []


def test_applied_resume_is_the_one_attached(repo):
    conn, lead_id = _capture()
    slap_mcp.slap_save_drop(lead_id, DROP)
    assert _apply(conn, resume="resume-b")["status"] == "staged"
    manifest = load_manifest(recipient_workdir("test-hm", CONTACT))
    assert manifest["attachment_source"].endswith("docs/b.pdf")


def test_questions_roundtrip(repo):
    conn, lead_id = _capture()
    leads.record_questions(conn, url=JOB, questions=[{"label": "Why Acme?", "kind": "textarea"}])
    assert slap_mcp.slap_application()["open_questions"][0]["label"] == "Why Acme?"
    slap_mcp.slap_answer_questions(lead_id, {"Why Acme?": "Agents."})
    assert slap_mcp.slap_application()["open_questions"] == []
    with pytest.raises(ToolError, match="not among"):
        slap_mcp.slap_answer_questions(lead_id, {"Typo?": "x"})


def test_staged_reach_out_sends_on_the_next_drain(repo):
    conn, lead_id = _capture()
    slap_mcp.slap_save_drop(lead_id, DROP)
    _apply(conn)
    sent = []

    def send_message_fn(smtp_config, *, recipient, attachment=None, **kw):
        sent.append(recipient)
        return {"message_id": "<1@slap>", "raw": {}}

    runner.drain(conn, load_global_config(), smtp.SmtpConfig(host="h", port=1, user="u", password="p"),
                 sleep_fn=lambda s: None, send_message_fn=send_message_fn, log_fn=lambda s: None)
    assert sent == [CONTACT]
    assert slap_mcp.slap_application(lead_id)["status"] == f"reach-out staged to {CONTACT}"


def test_stars_pass_through_and_reject_unknown(repo):
    assert slap_mcp.slap_stars(["s1"])[0]["core"] == "passes through"
    with pytest.raises(ToolError, match="unknown STAR"):
        slap_mcp.slap_stars(["nope"])
