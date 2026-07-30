"""Saved follow-up ("Remind") template store: parse, discover, save, load."""
import pytest

from slap.followups import (
    FollowupError, discover_followups, load_followup, parse_followup_text,
    save_followup, slugify,
)


def test_parse_requires_title_first_line():
    with pytest.raises(FollowupError):
        parse_followup_text("no title here\n\nbody")


def test_parse_requires_blank_second_line():
    with pytest.raises(FollowupError):
        parse_followup_text("Title: X\nbody immediately")


def test_parse_returns_title_and_body():
    title, body = parse_followup_text("Title: Quick nudge\n\nHi {{name}},\ncircling back")
    assert title == "Quick nudge"
    assert body == "Hi {{name}},\ncircling back"


def test_empty_title_rejected():
    with pytest.raises(FollowupError):
        parse_followup_text("Title:   \n\nbody")


def test_slugify_is_filesystem_safe_and_deterministic():
    assert slugify("Quick nudge on the Acme role!") == "quick-nudge-on-the-acme-role"
    assert slugify("Quick nudge") == slugify("Quick nudge")


def test_slugify_rejects_empty():
    with pytest.raises(FollowupError):
        slugify("!!!")


def test_save_then_load_roundtrip(tmp_path):
    saved = save_followup("Circle back", "Hi there,\njust checking in", followups_dir=tmp_path)
    assert saved["slug"] == "circle-back"
    loaded = load_followup("circle-back", followups_dir=tmp_path)
    assert loaded["title"] == "Circle back"
    assert loaded["body"] == "Hi there,\njust checking in"


def test_save_never_silently_overwrites(tmp_path):
    save_followup("Circle back", "first", followups_dir=tmp_path)
    with pytest.raises(FollowupError):
        save_followup("Circle back", "second", followups_dir=tmp_path)
    # original body untouched
    assert load_followup("circle-back", followups_dir=tmp_path)["body"] == "first"


def test_load_missing_fails_loud(tmp_path):
    with pytest.raises(FollowupError):
        load_followup("nope", followups_dir=tmp_path)


def test_discover_empty_when_dir_absent(tmp_path):
    assert discover_followups(followups_dir=tmp_path / "does-not-exist") == []


def test_discover_lists_saved_followups_sorted(tmp_path):
    save_followup("Beta", "b", followups_dir=tmp_path)
    save_followup("Alpha", "a", followups_dir=tmp_path)
    found = discover_followups(followups_dir=tmp_path)
    assert [f["slug"] for f in found] == ["alpha", "beta"]
    assert found[0]["title"] == "Alpha"
