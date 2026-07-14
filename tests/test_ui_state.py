"""UI-only dismissal state tests (post-launch feature) — Warm-but-silent
hide/unhide. Deliberately a separate table from events/recipients; see
slap/ui_state.py's own module docstring for why.
"""
import sqlite3
from datetime import datetime, timezone

from slap.ui_state import hidden_at, hide, list_hidden, unhide


def connect(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    return conn


def test_hidden_at_none_when_never_hidden(tmp_path):
    conn = connect(tmp_path)
    assert hidden_at(conn, "a@x.com", "warm_but_silent") is None


def test_hide_then_hidden_at_returns_timestamp(tmp_path):
    conn = connect(tmp_path)
    when = datetime(2026, 7, 1, tzinfo=timezone.utc)
    hide(conn, "a@x.com", "warm_but_silent", when=when)
    assert hidden_at(conn, "a@x.com", "warm_but_silent") == when.isoformat()


def test_hide_is_scoped_per_widget(tmp_path):
    conn = connect(tmp_path)
    hide(conn, "a@x.com", "warm_but_silent")
    assert hidden_at(conn, "a@x.com", "some_other_widget") is None


def test_re_hide_bumps_hidden_at(tmp_path):
    conn = connect(tmp_path)
    hide(conn, "a@x.com", "warm_but_silent", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    hide(conn, "a@x.com", "warm_but_silent", when=datetime(2026, 7, 1, tzinfo=timezone.utc))
    assert hidden_at(conn, "a@x.com", "warm_but_silent") == datetime(2026, 7, 1, tzinfo=timezone.utc).isoformat()


def test_unhide_removes_the_row(tmp_path):
    conn = connect(tmp_path)
    hide(conn, "a@x.com", "warm_but_silent")
    unhide(conn, "a@x.com", "warm_but_silent")
    assert hidden_at(conn, "a@x.com", "warm_but_silent") is None


def test_unhide_of_never_hidden_recipient_is_a_no_op(tmp_path):
    conn = connect(tmp_path)
    unhide(conn, "a@x.com", "warm_but_silent")  # must not raise
    assert hidden_at(conn, "a@x.com", "warm_but_silent") is None


def test_list_hidden_returns_only_that_widget_most_recent_first(tmp_path):
    conn = connect(tmp_path)
    hide(conn, "a@x.com", "warm_but_silent", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    hide(conn, "b@x.com", "warm_but_silent", when=datetime(2026, 7, 1, tzinfo=timezone.utc))
    hide(conn, "c@x.com", "other_widget")
    result = list_hidden(conn, "warm_but_silent")
    assert [r["recipient"] for r in result] == ["b@x.com", "a@x.com"]


def test_list_hidden_empty_when_nothing_hidden(tmp_path):
    conn = connect(tmp_path)
    assert list_hidden(conn, "warm_but_silent") == []
