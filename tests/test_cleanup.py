"""Stale-PDF cleanup tests (post-launch feature), per the owner's explicit
requirement: classification must be provably event-log-derived (never file
mtime, never a blind delete), dry-run by default, and never touch resume.tex.
"""
from datetime import datetime, timedelta, timezone

from slap.cleanup import (
    classify_recipient, delete_eligible, find_cleanup_candidates,
)
from slap.config import GlobalConfig, ScheduleConfig
from slap.queue import stage_recipient
from slap.tracking import append_event, connect

NOW = datetime(2026, 7, 3, tzinfo=timezone.utc)


def make_global_config(tmp_path, personas=None):
    return GlobalConfig(
        from_email="owner@gmail.com", from_name="Owner", api_key_env="GMASS_API_KEY",
        personas=personas or {"recruiter": [2, 3, 5]},
        schedule=ScheduleConfig(fire_window_start="09:00", fire_window_end="09:15",
                                 send_delay_min=10, send_delay_max=15, daily_cap=500, drain_retries=3),
        consumer_domains_file=str(tmp_path / "consumer_domains.txt"), path=tmp_path / "config.yaml",
    )


def days_ago(n):
    return NOW - timedelta(days=n)


def make_attachment(tmp_path, name="resume.pdf"):
    p = tmp_path / name
    p.write_bytes(b"%PDF-fake")
    return p


def stage(conn, tmp_path, *, campaign="c1", recipient="a@x.com", persona="recruiter",
          cadence=(2, 3, 5), attachment_name="Resume.pdf", workdir_root=None):
    workdir_root = workdir_root or (tmp_path / "workdir")
    attachment = make_attachment(tmp_path, name=f"src_{recipient}.pdf")
    return stage_recipient(
        conn, campaign=campaign, recipient=recipient, persona=persona, cadence=list(cadence),
        subject="Hi", body="Body", stage_bodies=["s1", "s2", "s3"],
        attachment_path=attachment, attachment_name=attachment_name, workdir_root=workdir_root,
    )


# --- classify_recipient: pure classification logic --------------------------

def test_classify_bounced_and_idle_is_eligible(tmp_path):
    conn = connect(tmp_path / "t.db")
    append_event(conn, type="queued", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"}, timestamp=days_ago(25))
    append_event(conn, type="sent", recipient="a@x.com", campaign="c1", stage=0,
                 meta={"is_final_stage": False}, timestamp=days_ago(24))
    append_event(conn, type="bounce", recipient="a@x.com", campaign="c1", timestamp=days_ago(20))

    v = classify_recipient(conn, "c1", "a@x.com", make_global_config(tmp_path), now=NOW)
    assert v.status == "eligible"
    assert "bounced" in v.reason


def test_classify_recently_sent_is_not_yet(tmp_path):
    conn = connect(tmp_path / "t.db")
    append_event(conn, type="queued", recipient="b@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"}, timestamp=days_ago(6))
    append_event(conn, type="sent", recipient="b@x.com", campaign="c1", stage=0,
                 meta={"is_final_stage": False}, timestamp=days_ago(5))

    v = classify_recipient(conn, "c1", "b@x.com", make_global_config(tmp_path), now=NOW)
    assert v.status == "not_yet"


def test_classify_active_but_cadence_window_elapsed_is_eligible(tmp_path):
    conn = connect(tmp_path / "t.db")
    # recruiter cadence [2,3,5] sums to 10 days; idle 16d clears both the
    # 15-day threshold AND the cadence window.
    append_event(conn, type="queued", recipient="c@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"}, timestamp=days_ago(17))
    append_event(conn, type="sent", recipient="c@x.com", campaign="c1", stage=0,
                 meta={"is_final_stage": False}, timestamp=days_ago(16))

    v = classify_recipient(conn, "c1", "c@x.com", make_global_config(tmp_path), now=NOW)
    assert v.status == "eligible"
    assert "cadence window elapsed" in v.reason


def test_classify_active_within_cadence_window_is_not_yet_even_if_idle_15_days(tmp_path):
    # A longer cadence (sum 40d) must NOT be short-circuited by the 15-day
    # idle threshold alone — this is the exact "not a blind file-age delete"
    # guarantee the owner asked for.
    conn = connect(tmp_path / "t.db")
    long_cadence_config = make_global_config(tmp_path, personas={"recruiter": [10, 15, 15]})
    append_event(conn, type="queued", recipient="d@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"}, timestamp=days_ago(21))
    append_event(conn, type="sent", recipient="d@x.com", campaign="c1", stage=0,
                 meta={"is_final_stage": False}, timestamp=days_ago(20))

    v = classify_recipient(conn, "c1", "d@x.com", long_cadence_config, now=NOW)
    assert v.status == "not_yet"
    assert "cadence window" in v.reason


def test_classify_replied_is_never_eligible(tmp_path):
    conn = connect(tmp_path / "t.db")
    append_event(conn, type="queued", recipient="e@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"}, timestamp=days_ago(30))
    append_event(conn, type="sent", recipient="e@x.com", campaign="c1", stage=0,
                 meta={"is_final_stage": False}, timestamp=days_ago(29))
    append_event(conn, type="reply", recipient="e@x.com", campaign="c1", timestamp=days_ago(28))

    v = classify_recipient(conn, "c1", "e@x.com", make_global_config(tmp_path), now=NOW)
    assert v.status == "not_yet"
    assert "replied" in v.reason


def test_classify_open_ooo_resend_is_not_yet(tmp_path):
    conn = connect(tmp_path / "t.db")
    append_event(conn, type="queued", recipient="f@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"}, timestamp=days_ago(30))
    append_event(conn, type="sent", recipient="f@x.com", campaign="c1", stage=0,
                 meta={"is_final_stage": False}, timestamp=days_ago(29))
    append_event(conn, type="ooo_tagged", recipient="f@x.com", campaign="c1", timestamp=days_ago(20))

    v = classify_recipient(conn, "c1", "f@x.com", make_global_config(tmp_path), now=NOW)
    assert v.status == "not_yet"
    assert "OOO" in v.reason


def test_classify_resolved_ooo_then_final_stage_is_eligible(tmp_path):
    conn = connect(tmp_path / "t.db")
    append_event(conn, type="queued", recipient="g@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"}, timestamp=days_ago(30))
    append_event(conn, type="sent", recipient="g@x.com", campaign="c1", stage=0,
                 meta={"is_final_stage": False}, timestamp=days_ago(29))
    append_event(conn, type="ooo_tagged", recipient="g@x.com", campaign="c1", timestamp=days_ago(28))
    append_event(conn, type="requeued", recipient="g@x.com", campaign="c1", stage=1,
                 meta={"is_final_stage": True}, timestamp=days_ago(27))

    v = classify_recipient(conn, "c1", "g@x.com", make_global_config(tmp_path), now=NOW)
    assert v.status == "eligible"


def test_classify_no_events_is_undetermined(tmp_path):
    conn = connect(tmp_path / "t.db")
    v = classify_recipient(conn, "c1", "ghost@x.com", make_global_config(tmp_path), now=NOW)
    assert v.status == "undetermined"
    assert "no events" in v.reason


def test_classify_unknown_persona_is_undetermined(tmp_path):
    conn = connect(tmp_path / "t.db")
    append_event(conn, type="queued", recipient="h@x.com", campaign="c1", stage=0,
                 meta={"persona": "ghost_persona"}, timestamp=days_ago(30))
    append_event(conn, type="sent", recipient="h@x.com", campaign="c1", stage=0,
                 meta={"is_final_stage": False}, timestamp=days_ago(29))

    v = classify_recipient(conn, "c1", "h@x.com", make_global_config(tmp_path), now=NOW)
    assert v.status == "undetermined"
    assert "persona" in v.reason


def test_classify_never_looks_at_file_mtime(tmp_path, monkeypatch):
    # Sanity check on the "not a blind file-age delete" guarantee: touching
    # os.stat must have zero effect on the verdict, since classify_recipient
    # never calls it at all.
    import os
    conn = connect(tmp_path / "t.db")
    append_event(conn, type="queued", recipient="i@x.com", campaign="c1", stage=0,
                 meta={"persona": "recruiter"}, timestamp=days_ago(25))
    append_event(conn, type="sent", recipient="i@x.com", campaign="c1", stage=0,
                 meta={"is_final_stage": False}, timestamp=days_ago(24))
    append_event(conn, type="bounce", recipient="i@x.com", campaign="c1", timestamp=days_ago(20))

    def fail_if_called(*a, **k):
        raise AssertionError("classify_recipient must never touch the filesystem")

    monkeypatch.setattr(os, "stat", fail_if_called)
    v = classify_recipient(conn, "c1", "i@x.com", make_global_config(tmp_path), now=NOW)
    assert v.status == "eligible"


# --- find_cleanup_candidates: workdir scan + report --------------------------

def test_find_cleanup_candidates_lists_eligible_and_skips_active(tmp_path):
    conn = connect(tmp_path / "t.db")
    workdir_root = tmp_path / "workdir"

    stage(conn, tmp_path, recipient="bounced@x.com", workdir_root=workdir_root)
    append_event(conn, type="bounce", recipient="bounced@x.com", campaign="c1", timestamp=days_ago(20))
    # Backdate the queued/sent events so idle threshold clears too.
    conn.execute("UPDATE events SET timestamp = ? WHERE recipient = 'bounced@x.com' AND type = 'queued'",
                 (days_ago(25).isoformat(),))
    conn.execute("UPDATE events SET timestamp = ? WHERE recipient = 'bounced@x.com' AND type = 'sent'",
                 (days_ago(24).isoformat(),))

    stage(conn, tmp_path, recipient="active@x.com", workdir_root=workdir_root)

    report = find_cleanup_candidates(conn, make_global_config(tmp_path), workdir_root=workdir_root, now=NOW)
    eligible_recipients = {c.recipient for c in report.eligible}
    assert eligible_recipients == {"bounced@x.com"}
    assert not report.undetermined


def test_find_cleanup_candidates_reports_undetermined_separately(tmp_path):
    conn = connect(tmp_path / "t.db")
    workdir_root = tmp_path / "workdir"
    stage(conn, tmp_path, recipient="unknown@x.com", persona="ghost_persona", workdir_root=workdir_root)
    append_event(conn, type="sent", recipient="unknown@x.com", campaign="c1", stage=0,
                 meta={"is_final_stage": False}, timestamp=days_ago(29))
    conn.execute("UPDATE events SET timestamp = ? WHERE recipient = 'unknown@x.com'", (days_ago(30).isoformat(),))

    report = find_cleanup_candidates(conn, make_global_config(tmp_path), workdir_root=workdir_root, now=NOW)
    assert not report.eligible
    assert len(report.undetermined) == 1
    assert report.undetermined[0].recipient == "unknown@x.com"


def test_find_cleanup_candidates_skips_recipients_with_no_pdf_on_disk(tmp_path):
    conn = connect(tmp_path / "t.db")
    workdir_root = tmp_path / "workdir"
    workdir = stage(conn, tmp_path, recipient="nopdf@x.com", workdir_root=workdir_root)
    append_event(conn, type="bounce", recipient="nopdf@x.com", campaign="c1", timestamp=days_ago(20))
    conn.execute("UPDATE events SET timestamp = ? WHERE recipient = 'nopdf@x.com' AND type = 'queued'",
                 (days_ago(25).isoformat(),))
    conn.execute("UPDATE events SET timestamp = ? WHERE recipient = 'nopdf@x.com' AND type = 'sent'",
                 (days_ago(24).isoformat(),))
    (workdir / "Resume.pdf").unlink()  # already cleaned up

    report = find_cleanup_candidates(conn, make_global_config(tmp_path), workdir_root=workdir_root, now=NOW)
    assert not report.eligible


# --- delete_eligible: the actual destructive step ---------------------------

def test_delete_eligible_removes_pdf_and_hash_but_keeps_tex(tmp_path):
    conn = connect(tmp_path / "t.db")
    workdir_root = tmp_path / "workdir"
    workdir = stage(conn, tmp_path, recipient="del@x.com", workdir_root=workdir_root)
    (workdir / "resume.tex").write_text("\\documentclass{article}")
    (workdir / "Resume.pdf.hash").write_text("deadbeef")
    append_event(conn, type="bounce", recipient="del@x.com", campaign="c1", timestamp=days_ago(20))
    conn.execute("UPDATE events SET timestamp = ? WHERE recipient = 'del@x.com' AND type = 'queued'",
                 (days_ago(25).isoformat(),))
    conn.execute("UPDATE events SET timestamp = ? WHERE recipient = 'del@x.com' AND type = 'sent'",
                 (days_ago(24).isoformat(),))

    report = find_cleanup_candidates(conn, make_global_config(tmp_path), workdir_root=workdir_root, now=NOW)
    assert len(report.eligible) == 1

    deleted = delete_eligible(report.eligible)
    assert len(deleted) == 1
    assert not (workdir / "Resume.pdf").exists()
    assert not (workdir / "Resume.pdf.hash").exists()
    assert (workdir / "resume.tex").exists()
    assert (workdir / "staged.json").exists()


def test_delete_eligible_tolerates_missing_hash_sidecar(tmp_path):
    # latex-off campaigns never write a .pdf.hash — deleting must not fail
    # loud over an expected absence.
    conn = connect(tmp_path / "t.db")
    workdir_root = tmp_path / "workdir"
    workdir = stage(conn, tmp_path, recipient="del2@x.com", workdir_root=workdir_root)
    append_event(conn, type="bounce", recipient="del2@x.com", campaign="c1", timestamp=days_ago(20))
    conn.execute("UPDATE events SET timestamp = ? WHERE recipient = 'del2@x.com' AND type = 'queued'",
                 (days_ago(25).isoformat(),))
    conn.execute("UPDATE events SET timestamp = ? WHERE recipient = 'del2@x.com' AND type = 'sent'",
                 (days_ago(24).isoformat(),))

    report = find_cleanup_candidates(conn, make_global_config(tmp_path), workdir_root=workdir_root, now=NOW)
    deleted = delete_eligible(report.eligible)
    assert len(deleted) == 1
    assert not (workdir / "Resume.pdf").exists()
