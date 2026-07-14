"""Config loader tests (Build Order step 3), per SLAP_BUILD_PROMPT.md §13 B:
valid load; missing stage file for a defined cadence fails loud; initial.txt
without Subject/blank-line fails loud; auto-discovery finds folders.
"""
import pytest

from slap.config import ConfigError, discover_campaigns, load_campaign, load_global_config

VALID_CONFIG_YAML = """
sender:
  from_email: everythingforgenius@gmail.com
  from_name: Test Owner

signature: "Test Owner\\nlinkedin.com/in/testowner"

gmass:
  api_key_env: GMASS_API_KEY

personas:
  hiring_manager: { stages: [2, 4, 6] }
  recruiter:      { stages: [2, 3, 5] }
  founder:        { stages: [2, 5, 7] }

schedule:
  fire_window_start: "09:00"
  fire_window_end:   "09:15"
  send_delay_min: 10
  send_delay_max: 15
  daily_cap: 500
  drain_retries: 3
  active_days: [mon, tue, wed, thu, fri]

tracking:
  consumer_domains_file: consumer_domains.txt
"""

VALID_CAMPAIGN_YAML = """
persona: recruiter
latex:
  enabled: true
  attachment_name: "Firstname_Lastname_Resume.pdf"
fields:
  - { key: email,        label: Email }
  - { key: role_catted,  label: Role }
  - { key: company,      label: Company }
  - { key: req_id,       label: Req ID, optional: true }
  - { key: byebye,       label: Signoff }
"""

VALID_INITIAL_TXT = "Subject: Quick note about the {{role_catted}} role at {{company}}\n\nHi {{company}} team,\n{{byebye}}\n"


def write_global_config(tmp_path, text=VALID_CONFIG_YAML):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def write_campaign(tmp_path, name="coldpost", campaign_yaml=VALID_CAMPAIGN_YAML,
                    initial_txt=VALID_INITIAL_TXT, stage_count=3):
    campaigns_dir = tmp_path / "campaigns"
    campaign_dir = campaigns_dir / name
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.yaml").write_text(campaign_yaml)
    (campaign_dir / "initial.txt").write_text(initial_txt)
    for i in range(1, stage_count + 1):
        (campaign_dir / f"stage{i}.txt").write_text(f"Following up — stage {i}.\n")
    return campaigns_dir, campaign_dir


# --- load_global_config -------------------------------------------------

def test_load_global_config_valid(tmp_path):
    path = write_global_config(tmp_path)
    cfg = load_global_config(path)
    assert cfg.from_email == "everythingforgenius@gmail.com"
    assert cfg.from_name == "Test Owner"
    assert cfg.api_key_env == "GMASS_API_KEY"
    assert cfg.personas == {
        "hiring_manager": [2, 4, 6],
        "recruiter": [2, 3, 5],
        "founder": [2, 5, 7],
    }
    assert cfg.schedule.daily_cap == 500
    assert cfg.consumer_domains_file == "consumer_domains.txt"
    assert cfg.signature == "Test Owner\nlinkedin.com/in/testowner"


def test_load_global_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_global_config(tmp_path / "config.yaml")


# --- signature ------------------------------------------------------------

def test_load_global_config_missing_signature_key_fails_loud(tmp_path):
    bad = VALID_CONFIG_YAML.replace('signature: "Test Owner\\nlinkedin.com/in/testowner"\n', "")
    path = write_global_config(tmp_path, bad)
    with pytest.raises(ConfigError, match="signature"):
        load_global_config(path)


def test_load_global_config_empty_signature_is_allowed(tmp_path):
    # Explicit, deliberate choice to send with no signature — the key must
    # still be PRESENT, but an empty string is not a missing-config error.
    text = VALID_CONFIG_YAML.replace('signature: "Test Owner\\nlinkedin.com/in/testowner"', 'signature: ""')
    path = write_global_config(tmp_path, text)
    cfg = load_global_config(path)
    assert cfg.signature == ""


def test_load_global_config_non_string_signature_fails_loud(tmp_path):
    text = VALID_CONFIG_YAML.replace('signature: "Test Owner\\nlinkedin.com/in/testowner"', "signature: [1, 2]")
    path = write_global_config(tmp_path, text)
    with pytest.raises(ConfigError, match="must be a string"):
        load_global_config(path)


def test_load_global_config_missing_key_fails_loud(tmp_path):
    bad = VALID_CONFIG_YAML.replace("api_key_env: GMASS_API_KEY", "")
    path = write_global_config(tmp_path, bad)
    with pytest.raises(ConfigError, match="gmass.api_key_env"):
        load_global_config(path)


# --- redis (post-launch feature: dashboard GMass-data cache) ---------------
# Genuinely optional, unlike every other key above: no _require(), and
# VALID_CONFIG_YAML itself has no `redis:` block at all — every other test
# in this file already proves loading still succeeds without one.

def test_load_global_config_redis_url_defaults_when_block_absent(tmp_path):
    path = write_global_config(tmp_path)  # VALID_CONFIG_YAML has no redis: block
    cfg = load_global_config(path)
    assert cfg.redis_url == "redis://localhost:6379/0"


def test_load_global_config_redis_url_custom_value(tmp_path):
    text = VALID_CONFIG_YAML + "\nredis:\n  url: redis://example.internal:6380/2\n"
    path = write_global_config(tmp_path, text)
    cfg = load_global_config(path)
    assert cfg.redis_url == "redis://example.internal:6380/2"


def test_load_global_config_redis_block_not_a_mapping_fails_loud(tmp_path):
    text = VALID_CONFIG_YAML + "\nredis: \"oops\"\n"
    path = write_global_config(tmp_path, text)
    with pytest.raises(ConfigError, match="redis"):
        load_global_config(path)


def test_load_global_config_redis_url_non_string_fails_loud(tmp_path):
    text = VALID_CONFIG_YAML + "\nredis:\n  url: 6380\n"
    path = write_global_config(tmp_path, text)
    with pytest.raises(ConfigError, match="redis.url must be a string"):
        load_global_config(path)


def test_load_global_config_invalid_yaml_fails_loud(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("sender: [this is not\n  a valid: mapping")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_global_config(path)


# --- schedule.active_days -------------------------------------------------

def test_load_global_config_active_days_parsed_and_lowercased(tmp_path):
    text = VALID_CONFIG_YAML.replace("active_days: [mon, tue, wed, thu, fri]",
                                      "active_days: [MON, Tue, wed]")
    path = write_global_config(tmp_path, text)
    cfg = load_global_config(path)
    assert cfg.schedule.active_days == ["mon", "tue", "wed"]


def test_load_global_config_missing_active_days_fails_loud(tmp_path):
    bad = VALID_CONFIG_YAML.replace("active_days: [mon, tue, wed, thu, fri]\n", "")
    path = write_global_config(tmp_path, bad)
    with pytest.raises(ConfigError, match="active_days"):
        load_global_config(path)


def test_load_global_config_empty_active_days_fails_loud(tmp_path):
    bad = VALID_CONFIG_YAML.replace("active_days: [mon, tue, wed, thu, fri]", "active_days: []")
    path = write_global_config(tmp_path, bad)
    with pytest.raises(ConfigError, match="non-empty"):
        load_global_config(path)


def test_load_global_config_invalid_day_name_fails_loud(tmp_path):
    bad = VALID_CONFIG_YAML.replace("active_days: [mon, tue, wed, thu, fri]", "active_days: [mon, funday]")
    path = write_global_config(tmp_path, bad)
    with pytest.raises(ConfigError, match="not a valid day"):
        load_global_config(path)


def test_load_global_config_duplicate_day_fails_loud(tmp_path):
    bad = VALID_CONFIG_YAML.replace("active_days: [mon, tue, wed, thu, fri]", "active_days: [mon, mon]")
    path = write_global_config(tmp_path, bad)
    with pytest.raises(ConfigError, match="duplicate"):
        load_global_config(path)


# --- gmass.allowed_days / gmass.skip_holidays (independent of schedule.active_days) ---

def test_load_global_config_gmass_allowed_days_and_skip_holidays_default_when_absent(tmp_path):
    path = write_global_config(tmp_path)  # VALID_CONFIG_YAML's gmass: block has neither key
    cfg = load_global_config(path)
    assert cfg.gmass_allowed_days is None
    assert cfg.gmass_skip_holidays is None


def test_load_global_config_gmass_allowed_days_parsed_and_lowercased(tmp_path):
    text = VALID_CONFIG_YAML.replace("gmass:\n  api_key_env: GMASS_API_KEY",
                                      "gmass:\n  api_key_env: GMASS_API_KEY\n  allowed_days: [MON, Tue, wed]")
    path = write_global_config(tmp_path, text)
    cfg = load_global_config(path)
    assert cfg.gmass_allowed_days == ["mon", "tue", "wed"]
    # Independent of schedule.active_days — no cross-contamination either way.
    assert cfg.schedule.active_days == ["mon", "tue", "wed", "thu", "fri"]


def test_load_global_config_gmass_allowed_days_empty_list_fails_loud(tmp_path):
    text = VALID_CONFIG_YAML.replace("gmass:\n  api_key_env: GMASS_API_KEY",
                                      "gmass:\n  api_key_env: GMASS_API_KEY\n  allowed_days: []")
    path = write_global_config(tmp_path, text)
    with pytest.raises(ConfigError, match="non-empty"):
        load_global_config(path)


def test_load_global_config_gmass_allowed_days_invalid_day_name_fails_loud(tmp_path):
    text = VALID_CONFIG_YAML.replace("gmass:\n  api_key_env: GMASS_API_KEY",
                                      "gmass:\n  api_key_env: GMASS_API_KEY\n  allowed_days: [mon, funday]")
    path = write_global_config(tmp_path, text)
    with pytest.raises(ConfigError, match="not a valid day"):
        load_global_config(path)


def test_load_global_config_gmass_allowed_days_duplicate_fails_loud(tmp_path):
    text = VALID_CONFIG_YAML.replace("gmass:\n  api_key_env: GMASS_API_KEY",
                                      "gmass:\n  api_key_env: GMASS_API_KEY\n  allowed_days: [mon, mon]")
    path = write_global_config(tmp_path, text)
    with pytest.raises(ConfigError, match="duplicate"):
        load_global_config(path)


def test_load_global_config_gmass_skip_holidays_true(tmp_path):
    text = VALID_CONFIG_YAML.replace("gmass:\n  api_key_env: GMASS_API_KEY",
                                      "gmass:\n  api_key_env: GMASS_API_KEY\n  skip_holidays: true")
    path = write_global_config(tmp_path, text)
    cfg = load_global_config(path)
    assert cfg.gmass_skip_holidays is True


def test_load_global_config_gmass_skip_holidays_explicit_false_is_distinct_from_absent(tmp_path):
    # Tri-state, not a plain bool: GMass support confirms their server-side
    # default when skipHolidays is OMITTED is actually True — an owner who
    # wants holidays NOT skipped must be able to send `false` explicitly,
    # which must survive as False, not collapse to the same None the
    # absent-key case produces.
    text = VALID_CONFIG_YAML.replace("gmass:\n  api_key_env: GMASS_API_KEY",
                                      "gmass:\n  api_key_env: GMASS_API_KEY\n  skip_holidays: false")
    path = write_global_config(tmp_path, text)
    cfg = load_global_config(path)
    assert cfg.gmass_skip_holidays is False


def test_load_global_config_gmass_skip_holidays_non_bool_fails_loud(tmp_path):
    text = VALID_CONFIG_YAML.replace("gmass:\n  api_key_env: GMASS_API_KEY",
                                      "gmass:\n  api_key_env: GMASS_API_KEY\n  skip_holidays: yesplease")
    path = write_global_config(tmp_path, text)
    with pytest.raises(ConfigError, match="skip_holidays must be a boolean"):
        load_global_config(path)


# --- discover_campaigns ---------------------------------------------------

def test_discover_campaigns_finds_folders_with_campaign_yaml(tmp_path):
    campaigns_dir, _ = write_campaign(tmp_path, name="foo")
    write_campaign(tmp_path, name="baz")
    (campaigns_dir / "bar").mkdir()  # no campaign.yaml — must be excluded
    (campaigns_dir / "bar" / "notes.txt").write_text("not a campaign")

    assert discover_campaigns(campaigns_dir) == ["baz", "foo"]


def test_discover_campaigns_missing_dir_returns_empty(tmp_path):
    assert discover_campaigns(tmp_path / "campaigns") == []


# --- load_campaign ---------------------------------------------------------

def test_load_campaign_valid(tmp_path):
    global_config = load_global_config(write_global_config(tmp_path))
    campaigns_dir, _ = write_campaign(tmp_path)
    campaign = load_campaign("coldpost", global_config, campaigns_dir)
    assert campaign.persona == "recruiter"
    assert campaign.cadence == [2, 3, 5]
    assert campaign.latex_enabled is True
    assert campaign.attachment_name == "Firstname_Lastname_Resume.pdf"
    assert [f.key for f in campaign.fields] == ["email", "role_catted", "company", "req_id", "byebye"]
    req_id_field = next(f for f in campaign.fields if f.key == "req_id")
    assert req_id_field.optional is True
    assert campaign.subject_template == "Quick note about the {{role_catted}} role at {{company}}"
    assert campaign.body_template == "Hi {{company}} team,\n{{byebye}}"
    assert campaign.stage_bodies == [
        "Following up — stage 1.\n",
        "Following up — stage 2.\n",
        "Following up — stage 3.\n",
    ]


def test_load_campaign_missing_stage_file_fails_loud(tmp_path):
    global_config = load_global_config(write_global_config(tmp_path))
    # recruiter cadence has 3 stages; only provide 2.
    campaigns_dir, _ = write_campaign(tmp_path, stage_count=2)
    with pytest.raises(ConfigError, match="stage3.txt"):
        load_campaign("coldpost", global_config, campaigns_dir)


def test_load_campaign_extra_stage_file_fails_loud(tmp_path):
    global_config = load_global_config(write_global_config(tmp_path))
    # recruiter cadence has 3 stages; provide 4.
    campaigns_dir, _ = write_campaign(tmp_path, stage_count=4)
    with pytest.raises(ConfigError, match="stage4.txt"):
        load_campaign("coldpost", global_config, campaigns_dir)


def test_load_campaign_initial_txt_missing_subject_fails_loud(tmp_path):
    global_config = load_global_config(write_global_config(tmp_path))
    campaigns_dir, _ = write_campaign(tmp_path, initial_txt="Hi there,\n\nno subject line\n")
    with pytest.raises(ConfigError, match="Subject:"):
        load_campaign("coldpost", global_config, campaigns_dir)


def test_load_campaign_initial_txt_missing_blank_line_fails_loud(tmp_path):
    global_config = load_global_config(write_global_config(tmp_path))
    bad_initial = "Subject: Quick note\nHi there, no blank line separator\n"
    campaigns_dir, _ = write_campaign(tmp_path, initial_txt=bad_initial)
    with pytest.raises(ConfigError, match="blank"):
        load_campaign("coldpost", global_config, campaigns_dir)


def test_load_campaign_unknown_persona_fails_loud(tmp_path):
    global_config = load_global_config(write_global_config(tmp_path))
    bad_campaign_yaml = VALID_CAMPAIGN_YAML.replace("persona: recruiter", "persona: nonexistent")
    campaigns_dir, _ = write_campaign(tmp_path, campaign_yaml=bad_campaign_yaml)
    with pytest.raises(ConfigError, match="not defined"):
        load_campaign("coldpost", global_config, campaigns_dir)


def test_load_campaign_unknown_placeholder_fails_loud(tmp_path):
    global_config = load_global_config(write_global_config(tmp_path))
    bad_initial = "Subject: Hi\n\nWelcome, {{nonexistent_field}}!\n"
    campaigns_dir, _ = write_campaign(tmp_path, initial_txt=bad_initial)
    with pytest.raises(ConfigError, match="nonexistent_field"):
        load_campaign("coldpost", global_config, campaigns_dir)


def test_load_campaign_allows_signature_placeholder_with_no_matching_field(tmp_path):
    # {{signature}} is a config-sourced constant (CONFIG_SOURCED_KEYS), not a
    # drop-parsed field — campaign.yaml never declares a "signature" field,
    # and load_campaign() must not treat that as an unknown-placeholder error.
    global_config = load_global_config(write_global_config(tmp_path))
    initial_with_signature = "Subject: Hi\n\nWelcome, {{byebye}},\n{{signature}}\n"
    campaigns_dir, _ = write_campaign(tmp_path, initial_txt=initial_with_signature)
    campaign = load_campaign("coldpost", global_config, campaigns_dir)
    assert "{{signature}}" in campaign.body_template


def test_load_campaign_malformed_placeholder_fails_loud(tmp_path):
    # {{company }} (stray internal space) isn't \w+, so the well-formed
    # placeholder regex would silently miss it — must be caught separately.
    global_config = load_global_config(write_global_config(tmp_path))
    bad_initial = "Subject: Hi\n\nWelcome, {{company }}!\n"
    campaigns_dir, _ = write_campaign(tmp_path, initial_txt=bad_initial)
    with pytest.raises(ConfigError, match="malformed placeholder"):
        load_campaign("coldpost", global_config, campaigns_dir)


def test_load_campaign_requires_an_email_field(tmp_path):
    # send (step 9) needs to know which drop field is the recipient address.
    global_config = load_global_config(write_global_config(tmp_path))
    no_email_yaml = VALID_CAMPAIGN_YAML.replace(
        "  - { key: email,        label: Email }\n", ""
    )
    campaigns_dir, _ = write_campaign(tmp_path, campaign_yaml=no_email_yaml)
    with pytest.raises(ConfigError, match="email"):
        load_campaign("coldpost", global_config, campaigns_dir)


def test_load_campaign_latex_disabled_requires_attachment_file(tmp_path):
    global_config = load_global_config(write_global_config(tmp_path))
    no_attachment_yaml = VALID_CAMPAIGN_YAML.replace(
        "latex:\n  enabled: true", "latex:\n  enabled: false"
    )
    campaigns_dir, _ = write_campaign(tmp_path, campaign_yaml=no_attachment_yaml)
    with pytest.raises(ConfigError, match="attachment_file"):
        load_campaign("coldpost", global_config, campaigns_dir)
