import pytest

from slap.profile import ProfileError, load_profile


def test_load_profile_valid(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("fields: {first_name: Ada, phone: 5550100}\nanswers:\n  - {match: 'notice|start', answer: Two weeks}\n")
    assert load_profile(p) == {"fields": {"first_name": "Ada", "phone": "5550100"},
                               "answers": [{"match": "notice|start", "answer": "Two weeks"}]}


@pytest.mark.parametrize("text,msg", [
    (None, "not found"),
    ("- a\n- b\n", "mapping"),
    ("fields: {a: [1]}\n", "plain values"),
    ("answers: [{match: x}]\n", r"answers\[0\]"),
    ("answers: [{match: '(', answer: y}]\n", "valid regex"),
])
def test_load_profile_fails_loud(tmp_path, text, msg):
    p = tmp_path / "profile.yaml"
    if text is not None:
        p.write_text(text)
    with pytest.raises(ProfileError, match=msg):
        load_profile(p)
