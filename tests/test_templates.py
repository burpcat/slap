"""Drop parser + template fill tests (Build Order step 4), per
SLAP_BUILD_PROMPT.md §13 B: first-colon split; one-space strip; optional
empty -> line dropped; the 'Req ID: 6900' colon-in-value case.
"""
from slap.config import CampaignField
from slap.templates import fill_template, find_malformed_placeholders, parse_drop

FIELDS = [
    CampaignField(key="email", label="Email"),
    CampaignField(key="company", label="Company"),
    CampaignField(key="req_id", label="Req ID", optional=True),
    CampaignField(key="meeting_time", label="Meeting Time"),
]


# --- parse_drop --------------------------------------------------------

def test_parse_drop_basic():
    drop = "Email : jane@acme.com\nCompany : Acme Corp\n"
    values = parse_drop(drop, FIELDS)
    assert values["email"] == "jane@acme.com"
    assert values["company"] == "Acme Corp"


def test_parse_drop_matches_label_case_insensitively():
    # Real bug: a drop typed with the label's casing off (e.g. "email:"
    # against a field declared `label: Email`) must still match — a pasted
    # drop shouldn't silently lose a field just because a human typed it in
    # a different case than campaign.yaml happens to declare.
    drop = "email: jane@acme.com\ncompany: Acme Corp\n"
    values = parse_drop(drop, FIELDS)
    assert values["email"] == "jane@acme.com"
    assert values["company"] == "Acme Corp"


def test_parse_drop_matches_field_key_when_drop_uses_key_not_label():
    # Real BLOCKER: a drop typed with snake_case field KEYS on the left
    # (matching {{placeholder}} names, not campaign.yaml's human labels)
    # must still parse. Single-word fields (email/company) "accidentally"
    # worked before this fix because key and label happened to coincide;
    # every multi-word-labeled field (role_catted -> "Role", recruiter_name
    # -> "Recruiter name", experience_1 -> "Experience 1", byebye ->
    # "Signoff") silently defaulted to empty. Mirrors a real-world recruiter
    # campaign.yaml's field definitions exactly.
    recruiter_fields = [
        CampaignField(key="email", label="Email"),
        CampaignField(key="role_catted", label="Role"),
        CampaignField(key="company", label="Company"),
        CampaignField(key="req_id", label="Req ID"),
        CampaignField(key="recruiter_name", label="Recruiter name"),
        CampaignField(key="experience_1", label="Experience 1"),
        CampaignField(key="experience_2", label="Experience 2"),
        CampaignField(key="byebye", label="Signoff"),
    ]
    drop = (
        "email : jane+testmass1@example.com\n"
        "role_catted : data scientist\n"
        "company : TestCo\n"
        "req_id :  Req ID: 6900\n"
        "recruiter_name : Test Recruiter\n"
        "experience_1 : built an eval...\n"
        "experience_2 : cut onboarding...\n"
        "byebye : Thanks a ton\n"
    )
    values = parse_drop(drop, recruiter_fields)
    assert values["email"] == "jane+testmass1@example.com"
    assert values["role_catted"] == "data scientist"
    assert values["company"] == "TestCo"
    assert values["req_id"] == " Req ID: 6900"
    assert values["recruiter_name"] == "Test Recruiter"
    assert values["experience_1"] == "built an eval..."
    assert values["experience_2"] == "cut onboarding..."
    assert values["byebye"] == "Thanks a ton"


def test_parse_drop_still_matches_label_when_drop_uses_label_not_key():
    # The other convention must keep working too — matching key first must
    # not break matching by human label for anyone who types it that way.
    recruiter_fields = [
        CampaignField(key="role_catted", label="Role"),
        CampaignField(key="recruiter_name", label="Recruiter name"),
    ]
    drop = "Role : Data Scientist\nRecruiter name : Test Recruiter\n"
    values = parse_drop(drop, recruiter_fields)
    assert values["role_catted"] == "Data Scientist"
    assert values["recruiter_name"] == "Test Recruiter"


def test_parse_drop_req_id_colon_in_value_example_from_brief():
    # The brief's own example: "Req ID: 6900" (no space before the colon).
    drop = "Req ID: 6900\n"
    values = parse_drop(drop, FIELDS)
    assert values["req_id"] == "6900"


def test_parse_drop_first_colon_only_preserves_embedded_colons_in_value():
    # partition() must split on the FIRST colon only, so a value containing
    # its own colon (e.g. a time) is preserved whole, not truncated.
    drop = "Meeting Time : 10:30 AM\n"
    values = parse_drop(drop, FIELDS)
    assert values["meeting_time"] == "10:30 AM"


def test_parse_drop_strips_exactly_one_separator_space():
    # Exactly one leading space after the colon is stripped; any further
    # leading whitespace in the value is preserved as-is.
    drop = "Email :   jane@acme.com\n"
    values = parse_drop(drop, FIELDS)
    assert values["email"] == "  jane@acme.com"


def test_parse_drop_no_separator_space_leaves_value_untouched():
    drop = "Email:jane@acme.com\n"
    values = parse_drop(drop, FIELDS)
    assert values["email"] == "jane@acme.com"


def test_parse_drop_ignores_colonless_lines():
    drop = "just a stray line with no colon\nEmail : jane@acme.com\n"
    values = parse_drop(drop, FIELDS)
    assert values["email"] == "jane@acme.com"


def test_parse_drop_ignores_unknown_labels():
    drop = "Nickname : Janey\nEmail : jane@acme.com\n"
    values = parse_drop(drop, FIELDS)
    assert "Nickname" not in values
    assert values["email"] == "jane@acme.com"


def test_parse_drop_missing_label_defaults_to_empty():
    drop = "Email : jane@acme.com\n"
    values = parse_drop(drop, FIELDS)
    assert values["company"] == ""
    assert values["req_id"] == ""


# --- fill_template -------------------------------------------------------

def test_fill_template_substitutes_known_placeholders():
    text = "Hi {{company}} team, this is about {{email}}."
    values = {"company": "Acme", "email": "jane@acme.com", "req_id": ""}
    assert fill_template(text, values, FIELDS) == "Hi Acme team, this is about jane@acme.com."


def test_fill_template_drops_line_with_empty_optional_field():
    text = "Line one\nReq ID: {{req_id}}\nLine three"
    values = {"email": "", "company": "", "req_id": "", "meeting_time": ""}
    assert fill_template(text, values, FIELDS) == "Line one\nLine three"


def test_fill_template_keeps_line_when_optional_field_has_value():
    text = "Line one\nReq ID: {{req_id}}\nLine three"
    values = {"email": "", "company": "", "req_id": "6900", "meeting_time": ""}
    assert fill_template(text, values, FIELDS) == "Line one\nReq ID: 6900\nLine three"


def test_fill_template_drops_line_even_if_other_placeholder_on_line_has_value():
    text = "{{company}} — Req ID: {{req_id}}"
    values = {"email": "", "company": "Acme", "req_id": "", "meeting_time": ""}
    assert fill_template(text, values, FIELDS) == ""


def test_fill_template_does_not_drop_line_for_empty_non_optional_field():
    # Line-drop is scoped to optional fields only; a non-optional empty field
    # is just substituted as an empty string, the line stays.
    text = "Company: {{company}}"
    values = {"email": "", "company": "", "req_id": "", "meeting_time": ""}
    assert fill_template(text, values, FIELDS) == "Company: "


def test_find_malformed_placeholders_detects_stray_space():
    assert find_malformed_placeholders("Hi {{company }}!") == {"company "}


def test_find_malformed_placeholders_detects_hyphenated_key():
    assert find_malformed_placeholders("{{role-catted}}") == {"role-catted"}


def test_find_malformed_placeholders_ignores_well_formed():
    assert find_malformed_placeholders("Hi {{company}}, re {{req_id}}") == set()


def test_fill_template_end_to_end_with_parse_drop():
    drop = "Email : jane@acme.com\nCompany : Acme Corp\n"
    text = "Subject line unrelated\nHi {{company}} team,\nReq ID: {{req_id}}\nBest,\n{{email}}"
    values = parse_drop(drop, FIELDS)
    filled = fill_template(text, values, FIELDS)
    assert filled == "Subject line unrelated\nHi Acme Corp team,\nBest,\njane@acme.com"
