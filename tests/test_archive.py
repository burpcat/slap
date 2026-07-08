"""Résumé archive tests (post-launch feature, slap/archive.py): symlinks
(never copies) into RESUME_ARCHIVE_DIR, named <company>-<role>-<date>.pdf,
idempotent re-runs, collision handling, and warn-don't-block behavior for a
missing/unwritable archive dir.
"""
from datetime import date

from slap.archive import (
    ENV_VAR, archive_dir_from_env, archive_resume, find_broken_symlinks, resolve_live_targets,
)

WHEN = date(2026, 7, 8)


def make_pdf(tmp_path, name="resume.pdf"):
    p = tmp_path / name
    p.write_bytes(b"%PDF-fake")
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
