"""Root conftest so `slap` (a flat top-level package, not pip-installed) is
importable from tests regardless of whether pytest is invoked as `pytest` or
`python -m pytest`."""

import pytest


@pytest.fixture(autouse=True)
def _no_resume_archive_leak(monkeypatch):
    """Force RESUME_ARCHIVE_DIR blank for EVERY test (archiving off).

    slap.py's load_dotenv() walks UP from slap.py's location; when the suite
    runs from a worktree nested under the real repo, that walk finds the real
    developer .env and picks up a real RESUME_ARCHIVE_DIR — so any test that
    stages a recipient (in-process, or in a subprocess that inherits this
    env) would silently write symlinks into the owner's REAL archive folder,
    pointing at pytest tmp dirs that vanish on teardown = dangling symlinks
    (see CONTROL_SHEET.md). A blank value here neutralises that at the root:
    archiving is off in-process, and because load_dotenv(override=False) skips
    a key ALREADY present in the environment, a blank value also blocks the
    real .env from leaking into any child process that inherits os.environ.

    Tests that genuinely exercise the archive set their own RESUME_ARCHIVE_DIR
    (via monkeypatch.setenv or an explicit subprocess env) AFTER this autouse
    fixture runs, so they override this blank with no conflict."""
    monkeypatch.setenv("RESUME_ARCHIVE_DIR", "")
