import pytest

from slap import leads
from slap.domains import contacts_at_company
from slap.tracking import append_event, connect

URL = "https://boards.greenhouse.io/acme/jobs/123?gh_jid=123&utm_source=x#apply"


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


def test_normalize_keeps_job_id_param_drops_tracking_and_fragment():
    assert leads.normalize_url(URL) == "https://boards.greenhouse.io/acme/jobs/123?gh_jid=123"
    assert leads.lead_id_for(URL) == leads.lead_id_for("https://BOARDS.greenhouse.io/acme/jobs/123/?gh_jid=123")
    with pytest.raises(leads.LeadError):
        leads.normalize_url("javascript:alert(1)")


def test_capture_is_idempotent_per_url(conn):
    lead_id, created = leads.capture(conn, url=URL, jd="Build agents", company="Acme", role="ml eng")
    assert created
    assert leads.capture(conn, url=URL, jd="Build agents") == (lead_id, False)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='lead'").fetchone()[0] == 1
    with pytest.raises(leads.LeadError, match="jd is empty"):
        leads.capture(conn, url="https://x.com/j/2", jd="  ")


def test_lifecycle_fold_latest_match_wins(conn):
    lead_id, _ = leads.capture(conn, url=URL, jd="JD", company="Acme", role="ml eng")
    leads.match(conn, lead_id, resume="ds", campaign="recruiter")
    leads.match(conn, lead_id, resume="ai-eng", campaign="hiring-manager")
    leads.mark_applied(conn, url=URL, resume="ai-eng")
    lead = leads.by_url(conn, URL)
    assert (lead["stage"], lead["resume"], lead["campaign"], lead["applied_resume"]) == \
        ("applied", "ai-eng", "hiring-manager", "ai-eng")
    assert lead["jd"] == "JD" and lead["applied_at"]
    with pytest.raises(leads.LeadError, match="unknown lead"):
        leads.match(conn, "nope", resume="x", campaign="y")


def test_apply_first_creates_lead_without_jd(conn):
    lead_id = leads.mark_applied(conn, url="https://x.com/jobs/9", resume="ds")
    lead = leads.get(conn, lead_id)
    assert lead["stage"] == "applied" and lead["applied_resume"] == "ds" and lead["url"] == "https://x.com/jobs/9"
    assert leads.pending(conn) == []  # no JD captured -> nothing for Claude to draft from


def test_pending_excludes_drafted_leads(conn):
    a, _ = leads.capture(conn, url="https://x.com/jobs/1", jd="A")
    b, _ = leads.capture(conn, url="https://x.com/jobs/2", jd="B")
    append_event(conn, type="queued", recipient="hm@x.com", campaign="c", stage=0, meta={"lead_id": a})
    assert [l["lead_id"] for l in leads.pending(conn)] == [b]
    assert leads.get(conn, a)["drafted_for"] == ["hm@x.com"]


def test_contacts_at_company_matches_queued_company_name(conn):
    append_event(conn, type="queued", recipient="jane@acme.com", campaign="c", stage=0,
                 meta={"persona": "recruiter", "company": "Acme "})
    assert [c.recipient for c in contacts_at_company(conn, "acme")] == ["jane@acme.com"]
    assert contacts_at_company(conn, "Other") == [] and contacts_at_company(conn, "") == []


def test_questions_answers_lifecycle(conn):
    lead_id, _ = leads.capture(conn, url=URL, jd="JD", company="Acme")
    leads.mark_applied(conn, url=URL, resume="ai")
    leads.record_questions(conn, url=URL, questions=[
        {"label": "Why Acme?", "kind": "textarea"},
        {"label": "Years with Python?", "kind": "select", "options": ["0-2", "3-5", "6+"]}])
    (pending,) = leads.pending_questions(conn)
    assert [q["label"] for q in pending["questions"]] == ["Why Acme?", "Years with Python?"]
    assert pending["jd"] == "JD"

    leads.answer_questions(conn, lead_id, {"Why Acme?": "Your agent platform."})
    assert [q["label"] for q in leads.pending_questions(conn)[0]["questions"]] == ["Years with Python?"]
    leads.answer_questions(conn, lead_id, {"Years with Python?": "3-5"})
    assert leads.pending_questions(conn) == []
    lead = leads.get(conn, lead_id)
    assert lead["stage"] == "applied"  # questions/answers never move the lifecycle stage
    assert lead["answers"] == {"Why Acme?": "Your agent platform.", "Years with Python?": "3-5"}


def test_answer_questions_refuses_unknown_label_and_empty(conn):
    lead_id = leads.record_questions(conn, url=URL, questions=[{"label": "Why us?"}])
    with pytest.raises(leads.LeadError, match="not among"):
        leads.answer_questions(conn, lead_id, {"Why u?": "x"})
    with pytest.raises(leads.LeadError, match="non-empty"):
        leads.answer_questions(conn, lead_id, {"Why us?": " "})
    with pytest.raises(leads.LeadError, match="label"):
        leads.record_questions(conn, url=URL, questions=[{"kind": "text"}])
