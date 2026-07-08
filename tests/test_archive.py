"""Résumé archive tests (post-launch feature, slap/archive.py): symlinks
(never copies) into RESUME_ARCHIVE_DIR, named <company>-<role>-<date>.pdf,
idempotent re-runs, collision handling, and warn-don't-block behavior for a
missing/unwritable archive dir.
"""
from datetime import date

import pytest

from slap.archive import (
    ArchiveError, ENV_VAR, archive_dir_from_env, archive_resume, copy_reused_resume,
    find_broken_symlinks, find_matches_for_company, resolve_live_targets,
)

WHEN = date(2026, 7, 8)


def make_pdf(tmp_path, name="resume.pdf"):
    p = tmp_path / name
    p.write_bytes(b"%PDF-fake")
    return p


def make_valid_pdf(tmp_path, name="resume.pdf"):
    """A genuinely pypdf-parseable one-page PDF — copy_reused_resume() really
    parses its target (unlike most of this module's other tests, which only
    need something symlink-able and don't care whether it's a real PDF)."""
    from pypdf import PdfWriter
    p = tmp_path / name
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(p, "wb") as f:
        writer.write(f)
    return p


# --- archive_dir_from_env ----------------------------------------------

def test_archive_dir_from_env_unset_returns_none(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert archive_dir_from_env() is None


def test_archive_dir_from_env_blank_returns_none(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "   ")
    assert archive_dir_from_env() is None


def test_archive_dir_from_env_set_returns_path(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_VAR, str(tmp_path))
    assert archive_dir_from_env() == tmp_path


# --- archive_resume ------------------------------------------------------

def test_archive_resume_off_when_archive_dir_none(tmp_path):
    pdf = make_pdf(tmp_path)
    assert archive_resume(pdf, None, company="Acme", role="SWE", when=WHEN) is None


def test_archive_resume_creates_correctly_named_symlink(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    pdf = make_pdf(tmp_path)

    result = archive_resume(pdf, archive_dir, company="Acme Corp", role="Staff Engineer", when=WHEN)

    expected = archive_dir / "acme-corp-staff-engineer-2026-07-08.pdf"
    assert result == expected
    assert expected.is_symlink()
    assert expected.resolve() == pdf.resolve()


def test_archive_resume_slugifies_punctuation_out_of_company_and_role(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    pdf = make_pdf(tmp_path)

    result = archive_resume(pdf, archive_dir, company="Acme, Inc.!", role="SWE (Backend)", when=WHEN)

    assert result.name == "acme-inc-swe-backend-2026-07-08.pdf"


def test_archive_resume_rerun_for_same_recipient_is_idempotent_no_duplicate(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    pdf = make_pdf(tmp_path)

    first = archive_resume(pdf, archive_dir, company="Acme", role="SWE", when=WHEN)
    second = archive_resume(pdf, archive_dir, company="Acme", role="SWE", when=WHEN)

    assert first == second
    assert list(archive_dir.iterdir()) == [first]


def test_archive_resume_collision_with_different_file_appends_dash_2(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    pdf_a = make_pdf(tmp_path, "a.pdf")
    pdf_b = make_pdf(tmp_path, "b.pdf")

    first = archive_resume(pdf_a, archive_dir, company="Acme", role="SWE", when=WHEN)
    second = archive_resume(pdf_b, archive_dir, company="Acme", role="SWE", when=WHEN)

    assert first.name == "acme-swe-2026-07-08.pdf"
    assert second.name == "acme-swe-2026-07-08-2.pdf"
    assert first.resolve() == pdf_a.resolve()
    assert second.resolve() == pdf_b.resolve()


def test_archive_resume_missing_dir_warns_and_returns_none(tmp_path, capsys):
    pdf = make_pdf(tmp_path)
    missing = tmp_path / "does-not-exist"

    result = archive_resume(pdf, missing, company="Acme", role="SWE", when=WHEN)

    assert result is None
    assert "skipping archive symlink" in capsys.readouterr().out


def test_archive_resume_non_directory_warns_and_returns_none(tmp_path, capsys):
    pdf = make_pdf(tmp_path)
    not_a_dir = tmp_path / "im_a_file"
    not_a_dir.write_text("oops")

    result = archive_resume(pdf, not_a_dir, company="Acme", role="SWE", when=WHEN)

    assert result is None
    assert capsys.readouterr().out  # a warning was printed


def test_archive_resume_warns_when_role_is_empty_but_still_archives(tmp_path, capsys):
    # Regression for a campaign whose fields don't line up with 'role_catted'
    # (e.g. slap/init.py's scaffolded example campaign uses 'role' instead) —
    # must warn instead of silently producing an uninformative filename.
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    pdf = make_pdf(tmp_path)

    result = archive_resume(pdf, archive_dir, company="Acme", role="", when=WHEN)

    assert result == archive_dir / "acme--2026-07-08.pdf"
    assert "company and/or role is empty" in capsys.readouterr().out


def test_archive_resume_no_warning_when_company_and_role_both_present(tmp_path, capsys):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    pdf = make_pdf(tmp_path)

    archive_resume(pdf, archive_dir, company="Acme", role="SWE", when=WHEN)

    assert "empty" not in capsys.readouterr().out


# --- find_broken_symlinks -------------------------------------------------

def test_find_broken_symlinks_none_when_archive_dir_unset():
    assert find_broken_symlinks(None) == []


def test_find_broken_symlinks_detects_dangling_target(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    target = make_pdf(tmp_path)
    link = archive_dir / "a.pdf"
    link.symlink_to(target)
    target.unlink()  # cleanup-style deletion, leaving the symlink dangling

    assert find_broken_symlinks(archive_dir) == [link]


def test_find_broken_symlinks_empty_when_all_targets_exist(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    target = make_pdf(tmp_path)
    (archive_dir / "a.pdf").symlink_to(target)

    assert find_broken_symlinks(archive_dir) == []


# --- resolve_live_targets --------------------------------------------------

def test_resolve_live_targets_empty_when_archive_dir_unset():
    assert resolve_live_targets(None) == set()


def test_resolve_live_targets_excludes_dangling_symlinks(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    live_target = make_pdf(tmp_path, "live.pdf")
    dead_target = make_pdf(tmp_path, "dead.pdf")
    (archive_dir / "live.pdf").symlink_to(live_target)
    dangling_link = archive_dir / "dead.pdf"
    dangling_link.symlink_to(dead_target)
    dead_target.unlink()

    assert resolve_live_targets(archive_dir) == {live_target.resolve()}


# --- find_matches_for_company (résumé-reuse lookup) ------------------------

def test_find_matches_for_company_none_when_archive_dir_unset():
    assert find_matches_for_company(None, "Acme") == []


def test_find_matches_for_company_none_when_archive_dir_missing(tmp_path):
    assert find_matches_for_company(tmp_path / "does-not-exist", "Acme") == []


def test_find_matches_for_company_none_when_company_slugifies_to_empty(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "acme-swe-2026-07-08.pdf").symlink_to(make_pdf(tmp_path))
    assert find_matches_for_company(archive_dir, "!!!") == []


def test_find_matches_for_company_returns_matching_entries_sorted(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "acme-swe-2026-07-08.pdf").symlink_to(make_pdf(tmp_path, "a.pdf"))
    (archive_dir / "acme-recruiter-2026-06-01.pdf").symlink_to(make_pdf(tmp_path, "b.pdf"))
    (archive_dir / "other-co-swe-2026-07-01.pdf").symlink_to(make_pdf(tmp_path, "c.pdf"))

    matches = find_matches_for_company(archive_dir, "Acme")

    assert [m.name for m in matches] == ["acme-recruiter-2026-06-01.pdf", "acme-swe-2026-07-08.pdf"]


def test_find_matches_for_company_respects_slug_prefix_boundary(tmp_path):
    # "Acme" must not match "AcmeWidgets" — both slugify without a shared
    # "-" boundary unless the full "<slug>-" prefix is checked.
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "acmewidgets-swe-2026-07-08.pdf").symlink_to(make_pdf(tmp_path))

    assert find_matches_for_company(archive_dir, "Acme") == []


def test_find_matches_for_company_ignores_non_symlink_files(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "acme-swe-2026-07-08.pdf").write_bytes(b"%PDF-not-a-symlink")

    assert find_matches_for_company(archive_dir, "Acme") == []


# --- copy_reused_resume ------------------------------------------------------

def test_copy_reused_resume_copies_target_into_workdir_renamed(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    target = make_valid_pdf(tmp_path, "original.pdf")
    entry = archive_dir / "acme-swe-2026-06-01.pdf"
    entry.symlink_to(target)
    workdir = tmp_path / "workdir" / "coldpost-recruiter" / "jane@acme.com"

    dest = copy_reused_resume(entry, workdir, "AvinashArutla.pdf")

    assert dest == workdir / "AvinashArutla.pdf"
    assert dest.exists()
    assert not dest.is_symlink()  # a real copy, not a symlink
    assert dest.read_bytes() == target.read_bytes()
    assert target.exists()  # original untouched


def test_copy_reused_resume_creates_workdir_if_missing(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    entry = archive_dir / "acme-swe-2026-06-01.pdf"
    entry.symlink_to(make_valid_pdf(tmp_path))
    workdir = tmp_path / "workdir" / "c" / "jane@acme.com"
    assert not workdir.exists()

    copy_reused_resume(entry, workdir, "Resume.pdf")

    assert workdir.is_dir()


def test_copy_reused_resume_raises_when_target_missing(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    target = make_valid_pdf(tmp_path, "gone.pdf")
    entry = archive_dir / "acme-swe-2026-06-01.pdf"
    entry.symlink_to(target)
    target.unlink()

    with pytest.raises(ArchiveError, match="missing file"):
        copy_reused_resume(entry, tmp_path / "workdir", "Resume.pdf")


def test_copy_reused_resume_raises_when_target_empty(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    entry = archive_dir / "acme-swe-2026-06-01.pdf"
    entry.symlink_to(empty)

    with pytest.raises(ArchiveError, match="empty"):
        copy_reused_resume(entry, tmp_path / "workdir", "Resume.pdf")


def test_copy_reused_resume_raises_when_target_not_a_valid_pdf(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    target = make_pdf(tmp_path, "fake.pdf")  # b"%PDF-fake" -- not really parseable
    entry = archive_dir / "acme-swe-2026-06-01.pdf"
    entry.symlink_to(target)

    with pytest.raises(ArchiveError, match="readable PDF"):
        copy_reused_resume(entry, tmp_path / "workdir", "Resume.pdf")


def test_copy_reused_resume_does_not_write_anything_on_failure(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    entry = archive_dir / "acme-swe-2026-06-01.pdf"
    entry.symlink_to(tmp_path / "never-created.pdf")
    workdir = tmp_path / "workdir"

    with pytest.raises(ArchiveError):
        copy_reused_resume(entry, workdir, "Resume.pdf")

    assert not workdir.exists()
