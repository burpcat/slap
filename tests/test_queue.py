"""Queue staging tests (Build Order step 9), per SLAP_BUILD_PROMPT.md §13 B:
send stages without firing. Also covers OOO re-queue tagging (step 10, §7).
"""
from slap.queue import due_for_ooo_resend, due_recipients, load_manifest, stage_recipient, tag_ooo
from slap.tracking import append_event, connect


def make_attachment(tmp_path, name="resume.pdf"):
    p = tmp_path / name
    p.write_bytes(b"%PDF-fake")
    return p


def test_stage_recipient_writes_queued_event_only_not_sent(tmp_path):
    conn = connect(tmp_path / "test.db")
    attachment = make_attachment(tmp_path)
    stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="Jane_Resume.pdf",
        workdir_root=tmp_path / "workdir",
    )
    events = [dict(r) for r in conn.execute("SELECT * FROM events")]
    assert len(events) == 1
    assert events[0]["type"] == "queued"

    row = conn.execute("SELECT * FROM recipients WHERE recipient = ?", ("jane@acme.com",)).fetchone()
    assert row["status"] == "active"
    assert row["first_sent_at"] is None  # staged, not sent


def test_stage_recipient_writes_manifest_and_copies_attachment(tmp_path):
    conn = connect(tmp_path / "test.db")
    attachment = make_attachment(tmp_path)
    workdir_root = tmp_path / "workdir"
    workdir = stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi {{company}}", body="Body text", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="Jane_Resume.pdf",
        workdir_root=workdir_root,
    )
    assert (workdir / "Jane_Resume.pdf").exists()
    manifest = load_manifest(workdir)
    assert manifest["subject"] == "Hi {{company}}"
    assert manifest["body"] == "Body text"
    assert manifest["stage_bodies"] == ["s1", "s2", "s3"]
    assert manifest["cadence"] == [2, 3, 5]
    assert manifest["persona"] == "recruiter"
    assert manifest["attachment_name"] == "Jane_Resume.pdf"


def test_stage_recipient_does_not_double_copy_already_staged_attachment(tmp_path):
    # Mirrors the LaTeX-loop case (step 8): the attachment is already sitting
    # in the recipient's own workdir under attachment_name.
    conn = connect(tmp_path / "test.db")
    workdir_root = tmp_path / "workdir"
    workdir_root_recipient = workdir_root / "c" / "jane@acme.com"
    workdir_root_recipient.mkdir(parents=True)
    already_staged = workdir_root_recipient / "Jane_Resume.pdf"
    already_staged.write_bytes(b"%PDF-already-there")

    stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=already_staged, attachment_name="Jane_Resume.pdf",
        workdir_root=workdir_root,
    )
    assert already_staged.read_bytes() == b"%PDF-already-there"


def test_due_recipients_empty_when_nothing_queued(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert due_recipients(conn) == []


def test_due_recipients_includes_queued_excludes_already_sent(tmp_path):
    conn = connect(tmp_path / "test.db")
    attachment = make_attachment(tmp_path)
    stage_recipient(
        conn, campaign="c", recipient="queued-only@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="r.pdf", workdir_root=tmp_path / "workdir",
    )
    stage_recipient(
        conn, campaign="c", recipient="already-sent@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="r.pdf", workdir_root=tmp_path / "workdir",
    )
    append_event(conn, type="sent", recipient="already-sent@acme.com", campaign="c",
                 stage=0, gmass_campaign_id="1")

    due = due_recipients(conn)
    assert [r["recipient"] for r in due] == ["queued-only@acme.com"]


def test_due_recipients_excludes_bounced_or_replied_before_ever_sending(tmp_path):
    # Edge case: shouldn't happen via the normal flow (reply/bounce implies a
    # prior send), but the query should still be robust to it — a recipient
    # whose status isn't 'active' is never "due".
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c")
    assert due_recipients(conn) == []


# --- OOO re-queue tagging (Build Order step 10) -----------------------------

def test_tag_ooo_writes_ooo_tagged_event_with_campaign_from_cache(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com")
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'ooo_tagged'")]
    assert len(events) == 1
    assert events[0]["campaign"] == "c1"


def test_tag_ooo_sets_status_ooo_requeued(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com")
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "ooo_requeued"


def test_due_for_ooo_resend_empty_when_nothing_tagged(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert due_for_ooo_resend(conn) == []


def test_due_for_ooo_resend_includes_tagged_recipient(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com")
    assert [r["recipient"] for r in due_for_ooo_resend(conn)] == ["a@x.com"]


def test_due_for_ooo_resend_excludes_recipient_after_successful_requeue(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com")
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c1", stage=1,
                 gmass_campaign_id="1")
    assert due_for_ooo_resend(conn) == []


def test_due_for_ooo_resend_still_includes_recipient_after_failed_resend(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com")
    append_event(conn, type="send_failed", recipient="a@x.com", campaign="c1",
                 meta={"error": "boom"})
    assert [r["recipient"] for r in due_for_ooo_resend(conn)] == ["a@x.com"]
