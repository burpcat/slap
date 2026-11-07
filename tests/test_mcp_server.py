"""The whole flow through the MCP tools, as Claude Code drives it: capture
(with the owner's campaign, résumé, contact) -> Claude writes the drop ->
the owner applies -> slap_applied stages the reach-out, exactly once. Tool
functions are called directly (MCPServer.tool() returns the undecorated
function)."""
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import slap_mcp
from slap import runner, smtp, tracking
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
    app = slap_mcp.slap_capture(**{"url": JOB, "jd": "Build agents", "company": "Acme", "role": "ML Engineer",
                                   "campaign": "test-hm", "resume": "resume-a", "contact_email": CONTACT,
                                   "contact_name": "Sam", **details})
    return tracking.connect(), app["lead_id"]


def _apply(lead_id, resume="resume-a", to=CONTACT, **kw):
    return slap_mcp.slap_applied(lead_id, resume, to, **kw)


def _queued(conn):
    return conn.execute("SELECT recipient, meta FROM events WHERE type='queued'").fetchall()


DROP = {"why_line": "I've shipped LLM agents to production."}


def test_application_is_the_current_one_and_only_one(repo):
    with pytest.raises(ToolError, match="capture a posting"):
        slap_mcp.slap_application()
    _, lead_id = _capture()
    other = slap_mcp.slap_capture("https://jobs.lever.co/other/9", jd="Other JD", company="Other")
    assert other["created"] and "jd" not in other  # Claude already has the JD it passed in
    app = slap_mcp.slap_application()  # the most recently touched one
    assert app["company"] == "Other" and app["campaign"] is None and "campaign_options" in app["drop"]
    assert app["campaign_options"] == [{"name": "test-hm", "persona": "hiring_manager",
                                        "resumes": ["resume-a", "resume-b"]}]
    app = slap_mcp.slap_application(lead_id)
    assert app["company"] == "Acme" and app["status"].startswith("not applied")
    assert [f["key"] for f in app["drop_fields"]] == ["role_catted", "company", "why_line", "signal"]
    assert app["stars"] == [{"id": "s1", "title": "Shipped X", "tags": ["llm"]}]
    assert app["email_preview"]["missing"] == ["why_line"]
    assert app["warnings"] == []
    assert "jobs.lever.co/other" not in json.dumps(app)  # nothing about the other application


def test_capture_validates_the_owners_picks_and_updates_without_the_jd(repo):
    with pytest.raises(ToolError, match="unknown campaign 'nope'.*test-hm"):
        _capture(campaign="nope")
    with pytest.raises(ToolError, match="jd is empty"):
        _capture(jd="")
    conn, lead_id = _capture(resume="", contact_email="", contact_name="")
    with pytest.raises(ToolError, match="doesn't offer résumé 'resume-z'"):
        slap_mcp.slap_capture(JOB, resume="resume-z")  # checked against the lead's campaign
    app = slap_mcp.slap_capture(JOB, contact_email=CONTACT, contact_name="Sam")  # no JD the second time
    assert not app["created"] and app["contact"] == {"email": CONTACT, "name": "Sam"}
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='lead'").fetchone()[0] == 2


def test_drop_before_applied_then_applying_stages_once(repo):
    conn, lead_id = _capture()
    out = slap_mcp.slap_save_drop(lead_id, DROP)
    assert out["status"].startswith("not applied")
    assert out["email"]["subject"] == "Your ml engineer opening at Acme"
    assert "Hi Sam," in out["email"]["body"] and out["email"]["missing"] == []
    assert _queued(conn) == []

    assert _apply(lead_id) == {"status": "staged", "to": CONTACT}
    (row,) = _queued(conn)
    assert row["recipient"] == CONTACT and json.loads(row["meta"])["lead_id"] == lead_id
    manifest = load_manifest(recipient_workdir("test-hm", CONTACT))
    assert manifest["subject"] == out["email"]["subject"] and manifest["stage_bodies"] == ["Bump Sam", "Last Sam"]

    assert _apply(lead_id)["status"] == "already_staged"
    with pytest.raises(ToolError, match="already staged"):
        slap_mcp.slap_save_drop(lead_id, DROP)
    assert len(_queued(conn)) == 1


def test_applied_first_then_the_drop_then_applied_again_stages(repo):
    conn, lead_id = _capture()
    assert _apply(lead_id) == {"status": "incomplete", "missing": ["why_line"]}
    out = slap_mcp.slap_save_drop(lead_id, DROP)  # saving never stages
    assert out["status"].startswith("applied with résumé 'resume-a'") and _queued(conn) == []
    assert _apply(lead_id) == {"status": "staged", "to": CONTACT}


def test_applied_must_name_the_contact_it_goes_to(repo):
    conn, lead_id = _capture()
    slap_mcp.slap_save_drop(lead_id, DROP)
    with pytest.raises(ToolError, match="goes to the application's contact"):
        _apply(lead_id, to="someone+slaptest@gmail.com")
    assert _queued(conn) == []
    assert _apply(lead_id, to=CONTACT.upper())["status"] == "staged"


def test_no_contact_waits_and_details_unblock_it(repo):
    _, lead_id = _capture(contact_email="")
    slap_mcp.slap_save_drop(lead_id, DROP)
    assert _apply(lead_id, to="") == {"status": "incomplete", "missing": ["contact email"]}
    slap_mcp.slap_capture(JOB, contact_email=CONTACT)
    assert _apply(lead_id)["status"] == "staged"


def test_already_contacted_needs_confirm(repo):
    conn, lead_id = _capture()
    tracking.append_event(conn, type="queued", recipient=CONTACT, campaign="old", stage=0,
                          meta={"persona": "x", "company": "Acme"})
    tracking.append_event(conn, type="sent", recipient=CONTACT, campaign="old", stage=0, message_id="<o@x>")
    assert slap_mcp.slap_application(lead_id)["warnings"][0].startswith("Already contacted " + CONTACT)
    slap_mcp.slap_save_drop(lead_id, DROP)
    out = _apply(lead_id)
    assert out["status"] == "needs_confirm" and out["warnings"][0].startswith("Already contacted")
    assert _apply(lead_id, acknowledge_warnings=True)["status"] == "staged"


def test_someone_else_at_the_company_is_a_warning(repo):
    conn, lead_id = _capture()
    tracking.append_event(conn, type="queued", recipient="jane+slaptest@gmail.com", campaign="old", stage=0,
                          meta={"persona": "x", "company": "acme "})
    (warning,) = slap_mcp.slap_application(lead_id)["warnings"]
    assert warning.startswith("Already contacted at Acme: jane+slaptest@gmail.com")


def test_drop_rejects_unknown_fields_and_contact_keys(repo):
    _, lead_id = _capture()
    with pytest.raises(ToolError, match="not drop fields"):
        slap_mcp.slap_save_drop(lead_id, {"email": "x@y.com"})
    with pytest.raises(ToolError, match="not drop fields"):
        slap_mcp.slap_save_drop(lead_id, {"why_lin": "typo"})


def test_applied_needs_a_resume_the_campaign_offers(repo):
    conn, lead_id = _capture()
    with pytest.raises(ToolError, match="unknown résumé tag 'resume-z'"):
        _apply(lead_id, resume="resume-z")
    narrow = repo / "campaigns" / "test-narrow"
    narrow.mkdir()
    for f in (repo / "campaigns" / "test-hm").iterdir():
        (narrow / f.name).write_text(f.read_text())
    (narrow / "campaign.yaml").write_text(CAMPAIGN + "resumes: [resume-a]\n")
    with pytest.raises(ToolError, match="doesn't offer résumé 'resume-b'"):
        slap_mcp.slap_capture(JOB, campaign="test-narrow", resume="resume-b")
    slap_mcp.slap_capture(JOB, campaign="test-narrow")  # keeps resume-a, which it offers
    with pytest.raises(ToolError, match="doesn't offer résumé 'resume-b'"):
        _apply(lead_id, resume="resume-b")
    assert slap_mcp.slap_application(lead_id)["status"].startswith("not applied")  # nothing recorded
    _apply(lead_id)
    with pytest.raises(ToolError, match="already applied with résumé 'resume-a'"):
        slap_mcp.slap_capture(JOB, resume="resume-b")


def test_resume_edited_after_applying_is_refused(repo):
    conn, lead_id = _capture()
    _apply(lead_id)  # incomplete: no drop yet, but the PDF's hash is pinned
    (repo / "docs" / "a.pdf").write_bytes(b"%PDF-1.4 edited")
    slap_mcp.slap_save_drop(lead_id, DROP)
    with pytest.raises(ToolError, match="changed since you applied"):
        _apply(lead_id)  # re-applying can't quietly re-pin the new bytes
    assert _queued(conn) == []
    (repo / "docs" / "a.pdf").write_bytes(b"%PDF-1.4 resume a")  # restored
    assert _apply(lead_id)["status"] == "staged"


def test_applied_resume_is_the_one_attached(repo):
    _, lead_id = _capture()
    slap_mcp.slap_save_drop(lead_id, DROP)
    assert _apply(lead_id, resume="resume-b")["status"] == "staged"
    manifest = load_manifest(recipient_workdir("test-hm", CONTACT))
    snapshot = manifest["attachment_source"]
    assert snapshot.endswith("applied-resume.pdf")
    assert open(snapshot, "rb").read() == b"%PDF-1.4 resume b"


def test_editing_docs_after_staging_doesnt_change_what_is_sent(repo):
    conn, lead_id = _capture()
    slap_mcp.slap_save_drop(lead_id, DROP)
    _apply(lead_id)
    (repo / "docs" / "a.pdf").write_bytes(b"%PDF-1.4 re-exported for another job")
    attached = []

    def send_message_fn(smtp_config, *, recipient, attachment=None, **kw):
        attached.append(attachment)
        return {"message_id": "<1@slap>", "raw": {}}

    runner.drain(conn, load_global_config(), smtp.SmtpConfig(host="h", port=1, user="u", password="p"),
                 sleep_fn=lambda s: None, send_message_fn=send_message_fn, log_fn=lambda s: None)
    ((name, data, mime),) = attached
    assert (name, data) == ("Resume.pdf", b"%PDF-1.4 resume a")  # the applied bytes, not the re-export


def test_contact_with_an_unsent_reach_out_is_busy(repo):
    conn, lead_id = _capture()
    tracking.append_event(conn, type="queued", recipient=CONTACT, campaign="other", stage=0, meta={"persona": "x"})
    slap_mcp.slap_save_drop(lead_id, DROP)
    out = _apply(lead_id)
    assert out["status"] == "busy" and "already has an unsent reach-out" in out["reason"]


def test_staged_application_cant_switch_resume(repo):
    _, lead_id = _capture()
    slap_mcp.slap_save_drop(lead_id, DROP)
    _apply(lead_id)
    with pytest.raises(ToolError, match="already staged"):
        _apply(lead_id, resume="resume-b")


def test_empty_lead_id_is_refused_for_writes(repo):
    _capture()
    with pytest.raises(ToolError, match="lead_id is required"):
        slap_mcp.slap_save_drop("", DROP)
    with pytest.raises(ToolError, match="lead_id is required"):
        slap_mcp.slap_applied("", "resume-a", CONTACT)


def test_profile_and_answer_bank(repo):
    with pytest.raises(ToolError, match="no profile yet"):
        slap_mcp.slap_profile()
    with pytest.raises(ToolError, match="required"):
        slap_mcp.slap_save_answer("notice period", " ")
    assert slap_mcp.slap_save_answer("notice period", "Two weeks.") == {"match": "notice\\ period",
                                                                        "answer": "Two weeks."}
    slap_mcp.slap_save_answer("notice period", "Three weeks.")  # same words: replaced
    assert slap_mcp.slap_profile() == {"fields": {}, "answers": [{"match": "notice\\ period",
                                                                  "answer": "Three weeks."}]}
    (repo / "config.yaml").write_text(CONFIG + "profile: {fields: {first_name: Test}, answers: "
                                               "[{match: notice period, answer: One week.}]}\n")
    profile = slap_mcp.slap_profile()
    assert profile["fields"] == {"first_name": "Test"}
    assert profile["answers"][0]["answer"] == "One week."  # config.yaml's own answers win


def test_staged_reach_out_sends_on_the_next_drain(repo):
    conn, lead_id = _capture()
    slap_mcp.slap_save_drop(lead_id, DROP)
    _apply(lead_id)
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
