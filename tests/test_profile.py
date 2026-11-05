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
