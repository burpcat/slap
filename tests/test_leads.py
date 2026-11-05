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


def test_normalize_folds_step_pages_like_the_extension():
    assert leads.normalize_url("https://x.com/jobs/1/success") == "https://x.com/jobs/1"
    assert leads.normalize_url("https://example.com/apply/eng-123") == "https://example.com/apply/eng-123"
    assert leads.normalize_url("https://acme.wd1.myworkdayjobs.com/c/job/B/ML_R1/apply/applyManually") == \
        "https://acme.wd1.myworkdayjobs.com/c/job/B/ML_R1"


def test_capture_is_idempotent_per_url(conn):
    lead_id, created = leads.capture(conn, url=URL, jd="Build agents", company="Acme", role="ml eng")
    assert created
    assert leads.capture(conn, url=URL, jd="Build agents") == (lead_id, False)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='lead'").fetchone()[0] == 1
    with pytest.raises(leads.LeadError, match="jd is empty"):
        leads.capture(conn, url="https://x.com/j/2", jd="  ")


def test_details_latest_wins_and_capture_carries_them(conn):
    lead_id, _ = leads.capture(conn, url=URL, jd="JD", company="Acme", role="ml eng",
                               campaign="hm", resume="a", contact_email="sam@acme.com", contact_name="Sam")
    leads.set_details(conn, lead_id, resume="b", contact_name=" Samantha ")
    lead = leads.get(conn, lead_id)
    assert (lead["campaign"], lead["resume"], lead["contact_email"], lead["contact_name"]) == \
        ("hm", "b", "sam@acme.com", "Samantha")
    assert lead["stage"] == "captured"
    with pytest.raises(leads.LeadError, match="doesn't look like an email"):
        leads.set_details(conn, lead_id, contact_email="not-an-email")
    with pytest.raises(leads.LeadError, match="unknown lead details"):
        leads.set_details(conn, lead_id, resme="typo")
    # a re-capture records new details but never a second capture
    leads.capture(conn, url=URL, jd="JD", contact_email="hm@acme.com")
    assert leads.get(conn, lead_id)["contact_email"] == "hm@acme.com"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE json_extract(meta,'$.stage')='captured'").fetchone()[0] == 1


def test_drop_and_applied_fold_and_apply_first(conn):
    lead_id, _ = leads.capture(conn, url=URL, jd="JD")
    leads.save_drop(conn, lead_id, {"why_line": "v1"})
    leads.save_drop(conn, lead_id, {"why_line": "v2"})
    assert leads.get(conn, lead_id)["drop"] == {"why_line": "v2"}
    leads.mark_applied(conn, url=URL, resume="a", resume_sha256="h1")
    leads.mark_applied(conn, url=URL, resume="a", resume_sha256="h1")  # same résumé: no new event
    assert conn.execute("SELECT COUNT(*) FROM events WHERE json_extract(meta,'$.stage')='applied'").fetchone()[0] == 1
    assert leads.get(conn, lead_id)["stage"] == "applied"
    other = leads.mark_applied(conn, url="https://x.com/jobs/9", resume="b")  # never captured
    assert leads.get(conn, other)["applied_resume"] == "b" and leads.get(conn, other)["jd"] == ""


def test_current_is_the_most_recently_touched_lead(conn):
    assert leads.current(conn) is None
    a, _ = leads.capture(conn, url="https://x.com/jobs/1", jd="A")
    b, _ = leads.capture(conn, url="https://x.com/jobs/2", jd="B")
    assert leads.current(conn) == b
    leads.record_questions(conn, url="https://x.com/jobs/1/apply", questions=[{"label": "Why?"}])
    assert leads.current(conn) == a  # the apply page folds into posting 1


def test_staged_lead_is_frozen(conn):
    lead_id, _ = leads.capture(conn, url=URL, jd="JD")
    append_event(conn, type="queued", recipient="hm@x.com", campaign="c", stage=0, meta={"lead_id": lead_id})
    assert leads.get(conn, lead_id)["staged_to"] == ["hm@x.com"]
    with pytest.raises(leads.LeadError, match="already staged"):
        leads.save_drop(conn, lead_id, {"x": "y"})
    with pytest.raises(leads.LeadError, match="already staged"):
        leads.set_details(conn, lead_id, contact_name="z")


def test_questions_answers_fold(conn):
    lead_id, _ = leads.capture(conn, url=URL, jd="JD")
    leads.mark_applied(conn, url=URL, resume="a")
    leads.record_questions(conn, url=URL, questions=[
        {"label": "Why Acme?", "kind": "textarea"},
        {"label": "Years with Python?", "kind": "select", "options": ["0-2", "3-5", "6+"]}])
    leads.answer_questions(conn, lead_id, {"Why Acme?": "Your agent platform."})
    lead = leads.get(conn, lead_id)
    assert [q["label"] for q in lead["questions"]] == ["Why Acme?", "Years with Python?"]
    assert lead["answers"] == {"Why Acme?": "Your agent platform."}
    assert lead["stage"] == "applied"  # questions/answers never move the lifecycle stage


def test_answer_questions_refuses_unknown_label_and_empty(conn):
    lead_id = leads.record_questions(conn, url=URL, questions=[{"label": "Why us?"}])
    with pytest.raises(leads.LeadError, match="not among"):
        leads.answer_questions(conn, lead_id, {"Why u?": "x"})
    with pytest.raises(leads.LeadError, match="non-empty"):
        leads.answer_questions(conn, lead_id, {"Why us?": " "})
    with pytest.raises(leads.LeadError, match="label"):
        leads.record_questions(conn, url=URL, questions=[{"kind": "text"}])


def test_contacts_at_company_matches_queued_company_name(conn):
    append_event(conn, type="queued", recipient="jane@acme.com", campaign="c", stage=0,
                 meta={"persona": "recruiter", "company": "Acme "})
    assert [c.recipient for c in contacts_at_company(conn, "acme")] == ["jane@acme.com"]
    assert contacts_at_company(conn, "Other") == [] and contacts_at_company(conn, "") == []
