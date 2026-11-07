import pytest

from slap.profile import ProfileError, parse_profile


def test_parse_profile_valid_and_absent():
    assert parse_profile({"fields": {"first_name": "Ada", "phone": 5550100},
                          "answers": [{"match": "notice|start", "answer": "Two weeks"}]}) == \
        {"fields": {"first_name": "Ada", "phone": "5550100"},
         "answers": [{"match": "notice|start", "answer": "Two weeks"}]}
    assert parse_profile(None) == {"fields": {}, "answers": []}


@pytest.mark.parametrize("raw,msg", [
    (["a"], "mapping"),
    ({"fields": {"a": [1]}}, "plain values"),
    ({"answers": [{"match": "x"}]}, r"answers\[0\]"),
    ({"answers": [{"match": "(", "answer": "y"}]}, "valid regex"),
])
def test_parse_profile_fails_loud(raw, msg):
    with pytest.raises(ProfileError, match=msg):
        parse_profile(raw)


def test_saved_answers_replace_by_phrase_and_escape_regex(tmp_path):
    from slap.profile import load_learned, save_learned
    path = tmp_path / "answers.yaml"
    assert load_learned(path) == []
    save_learned(path, "notice period", "Two weeks.")
    save_learned(path, "notice period", "One month.")          # same words: replaced
    save_learned(path, "C++ (years)", "5")                      # plain words, escaped
    learned = load_learned(path)
    assert [a["answer"] for a in learned] == ["One month.", "5"]
    import re
    assert re.search(learned[1]["match"], "How many C++ (years) of experience?", re.I)
    with pytest.raises(ProfileError, match="required"):
        save_learned(path, " ", "x")
    assert sorted(f.name for f in tmp_path.iterdir()) == ["answers.yaml"]  # no temp file left behind
    path.write_text("- match: [unclosed\n")
    with pytest.raises(ProfileError, match="not valid YAML"):  # a hand-edit typo fails loud, as a ProfileError
        load_learned(path)
