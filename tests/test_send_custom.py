"""`slap.py send custom`: no-attachment send path + end-to-end staging."""
import os
import subprocess
import sys
from pathlib import Path

from slap.queue import stage_recipient
from slap.runner import _send_one
from slap.tracking import connect

SLAP_PY = Path(__file__).resolve().parent.parent / "slap.py"


# --- the net-new no-attachment send path (mode 4) ---------------------------

def test_no_attachment_send_omits_the_attachment(tmp_path):
    conn = connect(tmp_path / "t.db")
    workdir_root = tmp_path / "workdir"
    stage_recipient(
        conn, campaign="__custom__", recipient="a@x.com", persona="custom",
        cadence=[], subject="Hi", body="Body", stage_bodies=[],
        attachment_path=None, attachment_name=None, latex_enabled=False,
        workdir_root=workdir_root,
    )
    captured = {}

    def fake_create_draft(api_key, *, recipient, subject, message, attachment=None):
        captured["attachment"] = attachment
        return {"draft_id": "d1"}

    def fake_send_campaign(api_key, draft_id, *, campaign_settings):
        return {"campaign_id": "c1"}

    ok = _send_one(
        conn, "key", {"recipient": "a@x.com", "campaign": "__custom__"},
        workdir_root=workdir_root, create_draft_fn=fake_create_draft, send_campaign_fn=fake_send_campaign,
    )
    assert ok is True
    assert captured["attachment"] is None  # no attachment sent
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='sent'").fetchone()[0] == 1


def test_no_attachment_manifest_records_none(tmp_path):
    conn = connect(tmp_path / "t.db")
    workdir_root = tmp_path / "workdir"
    workdir = stage_recipient(
        conn, campaign="__custom__", recipient="a@x.com", persona="custom",
        cadence=[2], subject="Hi", body="Body", stage_bodies=["s1"],
        attachment_path=None, attachment_name=None, latex_enabled=False,
        workdir_root=workdir_root,
    )
    import json
    manifest = json.loads((workdir / "staged.json").read_text())
    assert manifest["attachment_name"] is None
    assert manifest["attachment_source"] is None
    assert manifest["cadence"] == [2]  # custom cadence stored per-recipient


# --- end-to-end: `send custom` via subprocess with a fake editor ------------

def _fake_editor(tmp_path) -> Path:
    """A tiny executable stand-in for $editor that writes canned content based
    on which scratch file it's opening — so `send custom`'s editor round-trips
    (write file, wait, re-read) can be driven non-interactively."""
    script = tmp_path / "fake_editor.sh"
    script.write_text(
        "#!/bin/bash\n"
        'f="$1"\n'
        'case "$f" in\n'
        '  *initial.txt) printf "Subject: Hello there\\n\\nHi, quick note." > "$f" ;;\n'
        '  *stage1.txt) printf "Just circling back." > "$f" ;;\n'
        "esac\n"
    )
    script.chmod(0o755)
    return script


def _write_config(tmp_path, editor_path):
    cfg = (Path(__file__).resolve().parent.parent / "config.yaml.example").read_text()
    cfg = cfg.replace("<Owner Name>", "Test Owner")
    cfg = cfg.replace('editor: "code --wait"', f'editor: "{editor_path}"')
    (tmp_path / "config.yaml").write_text(cfg)
    (tmp_path / "consumer_domains.txt").write_text(
        (Path(__file__).resolve().parent.parent / "consumer_domains.txt").read_text()
    )


def _run(*args, cwd, stdin):
    env = {**os.environ, "GMASS_API_KEY": "fake-key", "NO_COLOR": "1", "RESUME_ARCHIVE_DIR": ""}
    return subprocess.run(
        [sys.executable, str(SLAP_PY), *args],
        input=stdin, capture_output=True, text=True, cwd=cwd, env=env, timeout=20,
    )


def test_send_custom_no_attachment_stages(tmp_path):
    editor = _fake_editor(tmp_path)
    _write_config(tmp_path, editor)
    # recipient, no follow-up, attachment mode 4 (none), confirm stage
    stdin = "jane@acme.com\nn\n4\ny\n"
    result = _run("send", "custom", cwd=tmp_path, stdin=stdin)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Staged jane@acme.com (custom send)." in result.stdout

    conn = connect(tmp_path / "slap.db")
    rows = conn.execute(
        "SELECT campaign, meta FROM events WHERE type='queued' AND recipient='jane@acme.com'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["campaign"] == "__custom__"


def test_send_custom_with_one_followup_records_custom_cadence(tmp_path):
    editor = _fake_editor(tmp_path)
    _write_config(tmp_path, editor)
    import json
    # recipient, add follow-up=y, gap=3, (editor writes stage1), no more=n, attach mode 4, confirm
    stdin = "jane@acme.com\ny\n3\nn\n4\ny\n"
    result = _run("send", "custom", cwd=tmp_path, stdin=stdin)
    assert result.returncode == 0, result.stdout + result.stderr

    workdir = tmp_path / "workdir" / "__custom__" / "jane@acme.com"
    manifest = json.loads((workdir / "staged.json").read_text())
    assert manifest["cadence"] == [3]
    assert manifest["stage_bodies"] == ["Just circling back."]
    assert manifest["subject"] == "Hello there"
