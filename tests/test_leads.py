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


LD = ('<html><script type="application/ld+json">{"@graph":[{"@type":"Org"},{"@type":"JobPosting",'
      '"title":" ML Engineer ","hiringOrganization":{"name":"Acme"},'
      '"description":"&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;<ul><li>LLMs</li></ul>"}]}</script></html>')


def test_parse_job_posting_handles_graph_entities_and_junk():
    assert leads.parse_job_posting(LD) == {"role": "ML Engineer", "company": "Acme", "jd": "Build & ship\n- LLMs"}
    assert leads.parse_job_posting("<script type='application/ld+json'>{not json</script>") is None
    assert leads.parse_job_posting("<html>no data</html>") is None


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status = text, status

    def raise_for_status(self):
        if self.status >= 400:
            import requests
            raise requests.HTTPError(f"{self.status} error")


def test_add_from_urls_per_link_results(conn):
    import requests

    def fetch(url):
        if url.endswith("/2"):
            raise requests.HTTPError("404 error")
        return {"https://jobs.lever.co/acme/1/apply?lever-source=x": LD,
                "https://acme.wd1.myworkdayjobs.com/x": "<html>spa</html>",
                "https://x.com/odd": LD.replace('"description":"', '"description":["list"],"x":"')}[url]

    urls = ["https://jobs.lever.co/acme/1/apply?lever-source=x", "https://jobs.lever.co/acme/2",
            "https://acme.wd1.myworkdayjobs.com/x", "https://x.com/odd", "not a url"]
    out = leads.add_from_urls(conn, urls, fetch=fetch)
    assert out[0]["company"] == "Acme" and out[0]["created"] is True
    assert "404" in out[1]["error"]
    assert "Send to slap" in out[2]["error"]
    assert "Send to slap" in out[3]["error"]  # non-string description: skipped, batch continues
    assert "http(s)" in out[4]["error"]
    # a pasted /apply link lands on the same lead the extension uses for the posting
    assert [l["url"] for l in leads.pending(conn)] == ["https://jobs.lever.co/acme/1"]
    assert leads.add_from_urls(conn, urls[:1], fetch=fetch)[0]["created"] is False
    with pytest.raises(leads.LeadError, match="at most"):
        leads.add_from_urls(conn, ["https://x.com/j"] * (leads.BATCH_MAX + 1))


def test_normalize_folds_step_pages_like_the_extension():
    assert leads.normalize_url("https://x.com/jobs/1/success") == "https://x.com/jobs/1"
    assert leads.normalize_url("https://example.com/apply/eng-123") == "https://example.com/apply/eng-123"
    assert leads.normalize_url("https://acme.wd1.myworkdayjobs.com/c/job/B/ML_R1/apply/applyManually") == \
        "https://acme.wd1.myworkdayjobs.com/c/job/B/ML_R1"


class _Stream:
    def __init__(self, body=b"", status=200, location=None, ctype="text/html", encoding="ISO-8859-1"):
        self.body, self.status_code, self.encoding = body, status, encoding
        self.headers = {"Content-Type": ctype, **({"Location": location} if location else {})}
        self.is_redirect = location is not None

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def close(self):
        pass


def test_fetch_page_refuses_private_hosts_including_via_redirect():
    with pytest.raises(leads.LeadError, match="non-public"):
        leads.fetch_page("http://127.0.0.1:5050/api/profile", get=lambda *a, **k: _Stream())
    hops = {"https://jobs.example/1": _Stream(location="http://localhost/secret")}
    checked = []

    def check(host):
        checked.append(host)
        if host == "localhost":
            raise leads.LeadError("refusing to fetch 'localhost': it resolves to non-public address 127.0.0.1")

    with pytest.raises(leads.LeadError, match="non-public"):
        leads.fetch_page("https://jobs.example/1", get=lambda u, **k: hops[u], check_host=check)
    assert checked == ["jobs.example", "localhost"]


def test_fetch_page_caps_size_and_decodes_utf8_without_charset():
    ok = lambda h: None
    big = _Stream(body=b"x" * (leads.FETCH_MAX_BYTES + 1))
    with pytest.raises(leads.LeadError, match="larger than"):
        leads.fetch_page("https://a.example/", get=lambda *a, **k: big, check_host=ok)
    page = _Stream(body="Zürich — café".encode("utf-8"))  # no charset header; requests would guess latin-1
    assert leads.fetch_page("https://a.example/", get=lambda *a, **k: page, check_host=ok) == "Zürich — café"
