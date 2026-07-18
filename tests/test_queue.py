"""Queue staging tests (Build Order step 9), per SLAP_BUILD_PROMPT.md §13 B:
send stages without firing. Also covers OOO re-queue tagging (step 10, §7).
"""
import json
from datetime import date

import pytest

from slap.queue import (
    QueueError, _pending_ooo_resume_date, due_for_ooo_resend, due_recipients, load_manifest,
    resend_bounced, stage_recipient, tag_ooo,
)
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
        attachment_path=attachment, attachment_name="Jane_Resume.pdf", latex_enabled=True,
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
        attachment_path=attachment, attachment_name="Jane_Resume.pdf", latex_enabled=True,
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


def test_stage_recipient_latex_enabled_records_no_attachment_source(tmp_path):
    # latex_enabled=True -> None signals "read from workdir/attachment_name"
    # at drain time, since the compiled PDF genuinely differs per recipient.
    conn = connect(tmp_path / "test.db")
    attachment = make_attachment(tmp_path)
    workdir = stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="Jane_Resume.pdf", latex_enabled=True,
        workdir_root=tmp_path / "workdir",
    )
    manifest = load_manifest(workdir)
    assert manifest["attachment_source"] is None
    assert (workdir / "Jane_Resume.pdf").exists()


def test_stage_recipient_static_does_not_copy_attachment_into_workdir(tmp_path):
    # A static (latex-disabled) campaign's attachment is identical for every
    # recipient — copying it into each recipient's workdir would be false
    # per-recipient state. It must stay un-copied; the manifest instead
    # records the shared source path directly.
    conn = connect(tmp_path / "test.db")
    shared_resume = make_attachment(tmp_path, name="campaign_resume.pdf")
    workdir = stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=shared_resume, attachment_name="Jane_Resume.pdf", latex_enabled=False,
        workdir_root=tmp_path / "workdir",
    )
    assert not (workdir / "Jane_Resume.pdf").exists()  # never copied
    manifest = load_manifest(workdir)
    assert manifest["attachment_source"] == str(shared_resume.resolve())


def test_stage_recipient_static_multiple_recipients_share_one_source_no_duplication(tmp_path):
    conn = connect(tmp_path / "test.db")
    shared_resume = make_attachment(tmp_path, name="campaign_resume.pdf")
    workdir_root = tmp_path / "workdir"
    for recipient in ["a@acme.com", "b@acme.com", "c@acme.com"]:
        workdir = stage_recipient(
            conn, campaign="c", recipient=recipient, persona="recruiter", cadence=[2, 3, 5],
            subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
            attachment_path=shared_resume, attachment_name="Resume.pdf", latex_enabled=False,
            workdir_root=workdir_root,
        )
        assert not (workdir / "Resume.pdf").exists()
        assert load_manifest(workdir)["attachment_source"] == str(shared_resume.resolve())
    # Only the one original file exists anywhere under workdir/ — no per-recipient copies.
    assert list(workdir_root.rglob("*.pdf")) == []


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
        attachment_path=already_staged, attachment_name="Jane_Resume.pdf", latex_enabled=True,
        workdir_root=workdir_root,
    )
    assert already_staged.read_bytes() == b"%PDF-already-there"


# --- resend_bounced: bounce remediation (post-launch feature) --------------

def _write_valid_pdf(path):
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as f:
        writer.write(f)


def _stage_and_bounce(conn, tmp_path, *, recipient, campaign="c", persona="recruiter",
                      company="", role="", req_id="", latex_enabled=True, workdir_root=None,
                      reason="550 no such user"):
    workdir_root = workdir_root or (tmp_path / "workdir")
    attachment = make_attachment(tmp_path, name=f"orig-{recipient.replace('@', '-')}.pdf")
    stage_recipient(
        conn, campaign=campaign, recipient=recipient, persona=persona, cadence=[2, 3, 5],
        subject="Hi {{company}}", body="Body text", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="Resume.pdf", latex_enabled=latex_enabled,
        company=company, role=role, req_id=req_id, workdir_root=workdir_root,
    )
    append_event(conn, type="sent", recipient=recipient, campaign=campaign, stage=0, gmass_campaign_id="1")
    append_event(conn, type="bounce", recipient=recipient, campaign=campaign,
                 meta={"bounce_reason": reason, "bounce_time": "t1", "category": "bounce"})
    return workdir_root


def test_resend_bounced_stages_new_recipient_with_recovered_content(tmp_path):
    conn = connect(tmp_path / "test.db")
    workdir_root = _stage_and_bounce(conn, tmp_path, recipient="jane@acme.com", company="Acme", role="SWE")

    workdir = resend_bounced(conn, original_recipient="jane@acme.com",
                              corrected_email="jane.doe@acme.com", workdir_root=workdir_root)

    manifest = load_manifest(workdir)
    assert manifest["subject"] == "Hi {{company}}"
    assert manifest["body"] == "Body text"
    assert manifest["cadence"] == [2, 3, 5]
    assert manifest["persona"] == "recruiter"

    events = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE recipient = ? AND type = 'queued'", ("jane.doe@acme.com",)
    )]
    assert len(events) == 1
    meta = json.loads(events[0]["meta"])
    assert meta == {"persona": "recruiter", "company": "Acme", "role": "SWE", "req_id": "",
                     "cadence": [2, 3, 5], "corrected_from": "jane@acme.com"}

    row = conn.execute("SELECT * FROM recipients WHERE recipient = ?", ("jane.doe@acme.com",)).fetchone()
    assert row["status"] == "active"  # always restarts fresh, never inherits the bounced one's state

    # The original bounced row is untouched -- a new recipient, not a mutation.
    original = conn.execute("SELECT * FROM recipients WHERE recipient = ?", ("jane@acme.com",)).fetchone()
    assert original["status"] == "bounced"


def test_resend_bounced_raises_for_unknown_recipient(tmp_path):
    conn = connect(tmp_path / "test.db")
    with pytest.raises(QueueError, match="not a known recipient"):
        resend_bounced(conn, original_recipient="ghost@x.com", corrected_email="new@x.com",
                        workdir_root=tmp_path / "workdir")


def test_resend_bounced_raises_when_recipient_is_not_bounced(tmp_path):
    conn = connect(tmp_path / "test.db")
    workdir_root = tmp_path / "workdir"
    attachment = make_attachment(tmp_path)
    stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="Resume.pdf", latex_enabled=True,
        workdir_root=workdir_root,
    )
    with pytest.raises(QueueError, match="not bounced"):
        resend_bounced(conn, original_recipient="jane@acme.com", corrected_email="jane2@acme.com",
                        workdir_root=workdir_root)


def test_resend_bounced_static_campaign_reuses_shared_attachment_source(tmp_path):
    conn = connect(tmp_path / "test.db")
    shared_pdf = tmp_path / "shared_resume.pdf"
    shared_pdf.write_bytes(b"%PDF-shared")
    workdir_root = tmp_path / "workdir"
    stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=shared_pdf, attachment_name="Resume.pdf", latex_enabled=False,
        workdir_root=workdir_root,
    )
    append_event(conn, type="sent", recipient="jane@acme.com", campaign="c", stage=0, gmass_campaign_id="1")
    append_event(conn, type="bounce", recipient="jane@acme.com", campaign="c",
                 meta={"bounce_reason": "mailbox full", "bounce_time": "t1", "category": "bounce"})

    workdir = resend_bounced(conn, original_recipient="jane@acme.com",
                              corrected_email="jane2@acme.com", workdir_root=workdir_root)
    manifest = load_manifest(workdir)
    assert manifest["attachment_source"] == str(shared_pdf.resolve())
    assert not (workdir / "Resume.pdf").exists()  # never duplicated per-recipient


def test_resend_bounced_raises_when_pdf_missing_and_archive_match_not_yet_chosen(tmp_path):
    # A match existing is NOT enough to auto-reuse it, even when there's
    # only one -- same "never auto-pick, always a real confirmed choice"
    # rule the original résumé-reuse feature (_offer_resume_reuse) already
    # follows. Without an explicit archive_choice, this must fail loud
    # rather than silently guessing.
    conn = connect(tmp_path / "test.db")
    workdir_root = _stage_and_bounce(conn, tmp_path, recipient="jane@acme.com", company="Acme", role="SWE")
    original_workdir = workdir_root / "c" / "jane@acme.com"
    (original_workdir / "Resume.pdf").unlink()  # simulate slap.cleanup having reclaimed it

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archived_pdf = tmp_path / "archived.pdf"
    _write_valid_pdf(archived_pdf)
    (archive_dir / "acme-swe-2026-01-01.pdf").symlink_to(archived_pdf)

    with pytest.raises(QueueError, match="none chosen"):
        resend_bounced(conn, original_recipient="jane@acme.com", corrected_email="jane2@acme.com",
                        archive_dir=archive_dir, workdir_root=workdir_root)


def test_resend_bounced_reuses_an_explicitly_chosen_archive_entry(tmp_path):
    conn = connect(tmp_path / "test.db")
    workdir_root = _stage_and_bounce(conn, tmp_path, recipient="jane@acme.com", company="Acme", role="SWE")
    original_workdir = workdir_root / "c" / "jane@acme.com"
    (original_workdir / "Resume.pdf").unlink()  # simulate slap.cleanup having reclaimed it

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archived_pdf = tmp_path / "archived.pdf"
    _write_valid_pdf(archived_pdf)
    entry = archive_dir / "acme-swe-2026-01-01.pdf"
    entry.symlink_to(archived_pdf)

    workdir = resend_bounced(conn, original_recipient="jane@acme.com", corrected_email="jane2@acme.com",
                              archive_dir=archive_dir, archive_choice=entry, workdir_root=workdir_root)
    assert (workdir / "Resume.pdf").exists()
    assert (workdir / "Resume.pdf").read_bytes() == archived_pdf.read_bytes()


def test_resend_bounced_raises_queue_error_when_chosen_archive_entry_is_broken(tmp_path):
    conn = connect(tmp_path / "test.db")
    workdir_root = _stage_and_bounce(conn, tmp_path, recipient="jane@acme.com", company="Acme", role="SWE")
    original_workdir = workdir_root / "c" / "jane@acme.com"
    (original_workdir / "Resume.pdf").unlink()

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    gone = tmp_path / "gone.pdf"
    gone.write_bytes(b"%PDF-fake")
    entry = archive_dir / "acme-swe-2026-01-01.pdf"
    entry.symlink_to(gone)
    gone.unlink()  # dangling target

    with pytest.raises(QueueError, match="could not reuse"):
        resend_bounced(conn, original_recipient="jane@acme.com", corrected_email="jane2@acme.com",
                        archive_dir=archive_dir, archive_choice=entry, workdir_root=workdir_root)


def test_resend_bounced_raises_when_pdf_missing_and_no_archive_match(tmp_path):
    conn = connect(tmp_path / "test.db")
    workdir_root = _stage_and_bounce(conn, tmp_path, recipient="jane@acme.com", company="Acme", role="SWE")
    original_workdir = workdir_root / "c" / "jane@acme.com"
    (original_workdir / "Resume.pdf").unlink()

    with pytest.raises(QueueError, match="re-paste the LaTeX source"):
        resend_bounced(conn, original_recipient="jane@acme.com", corrected_email="jane2@acme.com",
                        workdir_root=workdir_root)


def test_due_recipients_empty_when_nothing_queued(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert due_recipients(conn) == []


def test_due_recipients_includes_queued_excludes_already_sent(tmp_path):
    conn = connect(tmp_path / "test.db")
    attachment = make_attachment(tmp_path)
    stage_recipient(
        conn, campaign="c", recipient="queued-only@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="r.pdf", latex_enabled=True,
        workdir_root=tmp_path / "workdir",
    )
    stage_recipient(
        conn, campaign="c", recipient="already-sent@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="r.pdf", latex_enabled=True,
        workdir_root=tmp_path / "workdir",
    )
    append_event(conn, type="sent", recipient="already-sent@acme.com", campaign="c",
                 stage=0, gmass_campaign_id="1")

    due = due_recipients(conn)
    assert [r["recipient"] for r in due] == ["queued-only@acme.com"]


def test_due_recipients_includes_recipient_re_staged_after_a_prior_campaigns_send(tmp_path):
    # Real BLOCKER: a recipient already sent to in an EARLIER, different
    # campaign (dedup hard-warn fired, owner confirmed proceed-anyway per
    # §6's warn-don't-block) has a permanent, first-write-wins
    # recipients.first_sent_at from that prior send. Re-staging them for a
    # NEW campaign must still make them due — checking first_sent_at IS NULL
    # (the old, buggy query) would silently and permanently exclude them
    # from every future drain the instant they'd ever been sent anything.
    conn = connect(tmp_path / "test.db")
    attachment = make_attachment(tmp_path)
    recipient = "already-contacted@acme.com"

    append_event(conn, type="queued", recipient=recipient, campaign="old-campaign", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient=recipient, campaign="old-campaign", stage=0,
                 gmass_campaign_id="111")
    assert due_recipients(conn) == []  # fully resolved, nothing due yet

    stage_recipient(
        conn, campaign="new-campaign", recipient=recipient, persona="founder", cadence=[2, 5, 7],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="r.pdf", latex_enabled=True,
        workdir_root=tmp_path / "workdir",
    )

    due = due_recipients(conn)
    assert [r["recipient"] for r in due] == [recipient]
    assert due[0]["campaign"] == "new-campaign"


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
    tag_ooo(conn, "a@x.com", date.today())
    events = [dict(r) for r in conn.execute("SELECT * FROM events WHERE type = 'ooo_tagged'")]
    assert len(events) == 1
    assert events[0]["campaign"] == "c1"


def test_tag_ooo_sets_status_ooo_requeued(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date.today())
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "ooo_requeued"


def test_due_for_ooo_resend_empty_when_nothing_tagged(tmp_path):
    conn = connect(tmp_path / "test.db")
    assert due_for_ooo_resend(conn) == []


def test_due_for_ooo_resend_includes_tagged_recipient(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date.today())
    assert [r["recipient"] for r in due_for_ooo_resend(conn)] == ["a@x.com"]


def test_due_for_ooo_resend_excludes_recipient_after_successful_requeue(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date.today())
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c1", stage=1,
                 gmass_campaign_id="1")
    assert due_for_ooo_resend(conn) == []


def test_due_for_ooo_resend_still_includes_recipient_after_failed_resend(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date.today())
    append_event(conn, type="send_failed", recipient="a@x.com", campaign="c1",
                 meta={"error": "boom"})
    assert [r["recipient"] for r in due_for_ooo_resend(conn)] == ["a@x.com"]


# --- manual OOO-pause date gating (post-launch feature) ---------------------
# Core requirement: tag_ooo is callable for ANY recipient at ANY time (no
# reply/engagement precondition), and due_for_ooo_resend must hold off until
# the owner-chosen resume_date arrives -- unlike the original reply-detected
# recovery, which resent on the very next drain with no wait at all.

def test_tag_ooo_writes_resume_date_in_meta(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 1))
    row = conn.execute("SELECT meta FROM events WHERE type = 'ooo_tagged'").fetchone()
    assert json.loads(row["meta"]) == {"resume_date": "2026-08-01"}


def test_tag_ooo_callable_with_no_prior_reply_or_engagement_at_all(tmp_path):
    # The whole point of the manual-OOO-pause feature: markable even when
    # SLAP never saw a reply (the OOO notice arrived somewhere else
    # entirely). No 'reply' event anywhere in this recipient's history.
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="a@x.com", campaign="c1", stage=0, gmass_campaign_id="1")
    tag_ooo(conn, "a@x.com", date(2026, 8, 1))  # no reply, no click, nothing -- still works
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "ooo_requeued"


def test_due_for_ooo_resend_excludes_recipient_before_resume_date(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 10))
    assert due_for_ooo_resend(conn, today=date(2026, 8, 9)) == []


def test_due_for_ooo_resend_includes_recipient_exactly_on_resume_date(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 10))
    assert [r["recipient"] for r in due_for_ooo_resend(conn, today=date(2026, 8, 10))] == ["a@x.com"]


def test_due_for_ooo_resend_includes_recipient_after_resume_date(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 10))
    assert [r["recipient"] for r in due_for_ooo_resend(conn, today=date(2026, 8, 20))] == ["a@x.com"]


def test_due_for_ooo_resend_treats_missing_resume_date_as_immediately_due(tmp_path):
    # An ooo_tagged event written before this feature existed (no meta at
    # all) must behave exactly like the ORIGINAL reply-detected recovery --
    # due on the very next drain, never silently stuck waiting forever.
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="ooo_tagged", recipient="a@x.com", campaign="c1")  # no meta
    assert [r["recipient"] for r in due_for_ooo_resend(conn, today=date(2020, 1, 1))] == ["a@x.com"]


def test_pending_ooo_resume_date_none_for_normal_recipient_with_no_ooo_history(tmp_path):
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    append_event(conn, type="sent", recipient="a@x.com", campaign="c1", stage=0, gmass_campaign_id="1")
    assert _pending_ooo_resume_date(conn, "a@x.com", "c1") is None


def test_pending_ooo_resume_date_reads_next_resume_date_from_requeued_meta(tmp_path):
    # Simulates the auto-continuation state _send_ooo_resend produces when
    # more stages remain (see slap.runner._send_ooo_resend) -- a `requeued`
    # event carrying next_resume_date, NOT a fresh ooo_tagged.
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 1))
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c1", stage=1,
                 gmass_campaign_id="1", meta={"next_resume_date": "2026-08-04"})
    assert _pending_ooo_resume_date(conn, "a@x.com", "c1") == date(2026, 8, 4)


def test_pending_ooo_resume_date_none_when_requeued_has_no_next_resume_date(tmp_path):
    # Final-stage resend -- sequence exhausted, nothing left pending.
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 1))
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c1", stage=1, gmass_campaign_id="1")
    assert _pending_ooo_resume_date(conn, "a@x.com", "c1") is None


def test_fresh_manual_ooo_retag_overrides_a_pending_auto_continuation(tmp_path):
    # A FRESH owner-driven re-tag is always the latest event and must
    # override whatever next_resume_date an in-progress auto-continuation
    # had already scheduled -- "any recipient, any time" includes overriding
    # SLAP's own auto-computed schedule.
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 1))
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c1", stage=1,
                 gmass_campaign_id="1", meta={"next_resume_date": "2026-08-04"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 2))  # owner overrides with an earlier date
    assert _pending_ooo_resume_date(conn, "a@x.com", "c1") == date(2026, 8, 2)


def test_due_for_ooo_resend_includes_active_status_continuation_recipient_once_due(tmp_path):
    # After ONE stage of a multi-stage OOO pause fires, status flips back to
    # 'active' (requeued's existing, unchanged cache handler) even though
    # another stage is still pending -- due_for_ooo_resend must still find
    # them via the pending next_resume_date, not via status alone.
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 1))
    append_event(conn, type="requeued", recipient="a@x.com", campaign="c1", stage=1,
                 gmass_campaign_id="1", meta={"next_resume_date": "2026-08-04"})
    row = conn.execute("SELECT status FROM recipients WHERE recipient = ?", ("a@x.com",)).fetchone()
    assert row["status"] == "active"  # sanity: confirms this is testing the 'active' branch
    assert due_for_ooo_resend(conn, today=date(2026, 8, 3)) == []
    assert [r["recipient"] for r in due_for_ooo_resend(conn, today=date(2026, 8, 4))] == ["a@x.com"]


def test_pending_ooo_resume_date_is_campaign_scoped_not_recipient_wide(tmp_path):
    # iron-audit BLOCKER fix: a recipient's dangling OOO continuation for an
    # OLD campaign must never resume against a NEW campaign they've since
    # been re-staged into (a supported flow -- the dedup hard-warn only
    # warns, never blocks re-contacting the same person). Without
    # campaign-scoping, this would silently cross-contaminate: the new
    # campaign's stage body sent threaded into the old campaign's Gmail
    # conversation, alongside a normal initial send in the same drain.
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="old-campaign", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 1))
    append_event(conn, type="requeued", recipient="a@x.com", campaign="old-campaign", stage=1,
                 gmass_campaign_id="1", meta={"next_resume_date": "2026-08-04"})
    # Re-staged into a brand new campaign before the continuation resolves.
    append_event(conn, type="queued", recipient="a@x.com", campaign="new-campaign", stage=0,
                 meta={"persona": "founder"})

    # The dangling old-campaign continuation must be invisible now -- it can
    # never resume against a campaign it was never paused for.
    assert _pending_ooo_resume_date(conn, "a@x.com", "new-campaign") is None
    assert due_for_ooo_resend(conn, today=date(2026, 8, 4)) == []
    # The OLD campaign's own pending date is still there if queried directly
    # (nothing corrupted) -- it's simply orphaned now that recipients.campaign
    # has moved on, exactly like the recipients cache's own one-row-per-
    # recipient grain already treats a re-staged recipient elsewhere.
    assert _pending_ooo_resume_date(conn, "a@x.com", "old-campaign") == date(2026, 8, 4)


def test_due_for_ooo_resend_never_overlaps_due_recipients_after_a_restage(tmp_path):
    # The actual double-send scenario the campaign-scoping fix closes: a
    # re-staged recipient must appear in due_recipients() (fresh initial send
    # due) but NEVER ALSO in due_for_ooo_resend() in the same drain.
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="old-campaign", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 1))
    append_event(conn, type="requeued", recipient="a@x.com", campaign="old-campaign", stage=1,
                 gmass_campaign_id="1", meta={"next_resume_date": "2026-08-04"})
    append_event(conn, type="queued", recipient="a@x.com", campaign="new-campaign", stage=0,
                 meta={"persona": "founder"})

    assert [r["recipient"] for r in due_recipients(conn)] == ["a@x.com"]
    assert due_for_ooo_resend(conn, today=date(2026, 8, 4)) == []


def test_transient_send_failed_does_not_close_a_pending_ooo_resend(tmp_path):
    # Mirrors due_recipients()'s own convention: a transient failed attempt
    # stays due for retry on the next drain -- only the specific
    # "ooo_cadence_exhausted" reason is terminal (see the next test).
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 1))
    append_event(conn, type="send_failed", recipient="a@x.com", campaign="c1",
                 meta={"stage": "create_draft_ooo", "error": "boom"})
    assert _pending_ooo_resume_date(conn, "a@x.com", "c1") == date(2026, 8, 1)


def test_ooo_cadence_exhausted_send_failed_permanently_closes_the_pending_resend(tmp_path):
    # iron-audit SHOULD-FIX: without this discriminator, a recipient marked
    # OOO with nothing left to resend (e.g. a single-stage persona, or
    # already at their final stage -- newly reachable via the unconditional
    # Mark OOO action) would generate an identical send_failed on every
    # single future drain, forever.
    conn = connect(tmp_path / "test.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"})
    tag_ooo(conn, "a@x.com", date(2026, 8, 1))
    append_event(conn, type="send_failed", recipient="a@x.com", campaign="c1",
                 meta={"stage": "ooo_cadence_exhausted", "error": "no next stage"})
    assert _pending_ooo_resume_date(conn, "a@x.com", "c1") is None
    assert due_for_ooo_resend(conn, today=date(2099, 1, 1)) == []


# --- résumé archive integration (post-launch feature) -----------------------

WHEN = date(2026, 7, 8)


def test_stage_recipient_archives_latex_attachment_at_its_real_workdir_path(tmp_path):
    conn = connect(tmp_path / "test.db")
    attachment = make_attachment(tmp_path)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    workdir_root = tmp_path / "workdir"

    stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="Jane_Resume.pdf", latex_enabled=True,
        company="Acme", role="SWE", archive_dir=archive_dir, when=WHEN,
        workdir_root=workdir_root,
    )

    link = archive_dir / "acme-swe-2026-07-08.pdf"
    assert list(archive_dir.iterdir()) == [link]
    assert link.resolve() == (workdir_root / "c" / "jane@acme.com" / "Jane_Resume.pdf").resolve()


def test_stage_recipient_archives_static_attachment_at_its_shared_path(tmp_path):
    conn = connect(tmp_path / "test.db")
    shared_resume = make_attachment(tmp_path, name="campaign_resume.pdf")
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=shared_resume, attachment_name="Resume.pdf", latex_enabled=False,
        company="Acme", role="SWE", archive_dir=archive_dir, when=WHEN,
        workdir_root=tmp_path / "workdir",
    )

    link = archive_dir / "acme-swe-2026-07-08.pdf"
    assert link.resolve() == shared_resume.resolve()


def test_stage_recipient_rerun_same_recipient_does_not_duplicate_archive_symlink(tmp_path):
    conn = connect(tmp_path / "test.db")
    attachment = make_attachment(tmp_path)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    workdir_root = tmp_path / "workdir"
    kwargs = dict(
        conn=conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="Jane_Resume.pdf", latex_enabled=True,
        company="Acme", role="SWE", archive_dir=archive_dir, when=WHEN, workdir_root=workdir_root,
    )

    stage_recipient(**kwargs)
    stage_recipient(**kwargs)  # e.g. a re-stage of the same recipient

    assert len(list(archive_dir.iterdir())) == 1


def test_stage_recipient_two_recipients_colliding_on_name_get_dash_2(tmp_path):
    conn = connect(tmp_path / "test.db")
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    workdir_root = tmp_path / "workdir"

    for recipient in ("jane@acme.com", "john@acme.com"):
        recipient_dir = tmp_path / recipient.split("@")[0]
        recipient_dir.mkdir()
        attachment = make_attachment(recipient_dir, name="r.pdf")
        stage_recipient(
            conn, campaign="c", recipient=recipient, persona="recruiter", cadence=[2, 3, 5],
            subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
            attachment_path=attachment, attachment_name="Resume.pdf", latex_enabled=True,
            company="Acme", role="SWE", archive_dir=archive_dir, when=WHEN,
            workdir_root=workdir_root,
        )

    names = sorted(p.name for p in archive_dir.iterdir())
    assert names == ["acme-swe-2026-07-08-2.pdf", "acme-swe-2026-07-08.pdf"]


def test_stage_recipient_archive_dir_unset_still_succeeds_no_symlink(tmp_path):
    conn = connect(tmp_path / "test.db")
    attachment = make_attachment(tmp_path)

    workdir = stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="Jane_Resume.pdf", latex_enabled=True,
        workdir_root=tmp_path / "workdir",
    )

    assert (workdir / "Jane_Resume.pdf").exists()
    events = [dict(r) for r in conn.execute("SELECT * FROM events")]
    assert len(events) == 1 and events[0]["type"] == "queued"


def test_stage_recipient_archive_dir_missing_still_succeeds_with_warning(tmp_path, capsys):
    conn = connect(tmp_path / "test.db")
    attachment = make_attachment(tmp_path)
    missing_archive_dir = tmp_path / "does-not-exist"

    workdir = stage_recipient(
        conn, campaign="c", recipient="jane@acme.com", persona="recruiter", cadence=[2, 3, 5],
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name="Jane_Resume.pdf", latex_enabled=True,
        company="Acme", role="SWE", archive_dir=missing_archive_dir, when=WHEN,
        workdir_root=tmp_path / "workdir",
    )

    assert (workdir / "Jane_Resume.pdf").exists()  # the send-relevant staging still succeeded
    events = [dict(r) for r in conn.execute("SELECT * FROM events")]
    assert len(events) == 1 and events[0]["type"] == "queued"
    assert "skipping archive symlink" in capsys.readouterr().out
