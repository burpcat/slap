"""Config loading, validation, and campaign auto-discovery (Build Order step 3).

See SLAP_BUILD_PROMPT.md §4 for the full schema and fail-loud rules. Validation
here covers the config's own internal correctness (schema, cross-references,
required files present with the right shape) — not external system state like
`attachment_file`/`xelatex` presence, which is `doctor`'s job (§11, step 12).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from slap.templates import CONFIG_SOURCED_KEYS, extract_placeholder_keys, find_malformed_placeholders

CONFIG_PATH = Path("config.yaml")
CAMPAIGNS_DIR = Path("campaigns")


class ConfigError(Exception):
    """Raised on any fail-loud config/campaign validation failure.

    Every OTHER caller in this app treats a `ConfigError` from `load_campaign()`
    as fatal for the whole command (e.g. `slap.py`'s `cmd_send`/`cmd_list`
    catch it once at the top level and exit/skip-with-a-message). `slap.reload.
    scan()` (post-launch) is the one exception: it catches `ConfigError` PER
    CAMPAIGN, caches the error, and reports it as a failure only for that
    campaign's own recipients — a batch operation spanning every campaign at
    once can't let one broken campaign.yaml abort reloading every other,
    unrelated campaign's recipients too."""


def _require(mapping: dict, dotted_key: str, ctx: object) -> object:
    cur = mapping
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ConfigError(f"{ctx}: missing required key '{dotted_key}'")
        cur = cur[part]
    return cur


def _load_yaml_mapping(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML — {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: must be a YAML mapping at the top level")
    return raw


VALID_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _validate_day_list(raw_list, ctx: str) -> list:
    """Shared validation rule for any config key that's a list of lowercase
    3-letter day abbreviations: non-empty, every entry in VALID_DAYS, no
    duplicates. Reused by schedule.active_days (required) and
    gmass.allowed_days (optional) so both enforce the exact same rule
    rather than two copies that could quietly drift apart."""
    if not isinstance(raw_list, list) or not raw_list:
        raise ConfigError(f"{ctx} must be a non-empty list")
    days = []
    for d in raw_list:
        norm = d.lower() if isinstance(d, str) else None
        if norm not in VALID_DAYS:
            raise ConfigError(f"{ctx}: {d!r} is not a valid day — use one of {VALID_DAYS}")
        days.append(norm)
    if len(set(days)) != len(days):
        raise ConfigError(f"{ctx} contains duplicate day(s): {raw_list}")
    return days


@dataclass
class ScheduleConfig:
    fire_window_start: str
    fire_window_end: str
    send_delay_min: int
    send_delay_max: int
    daily_cap: int
    drain_retries: int
    active_days: list  # e.g. ["mon", "tue", "wed", "thu", "fri"] — days the unattended runner may drain
    # Optional (unlike every other ScheduleConfig field, all of which are
    # _require()'d below) — the dashboard's weekly-goal-pacing widget on the
    # Analytics page. None means "not configured," so the widget is simply
    # omitted rather than shown with a fabricated target.
    weekly_target: int = None


@dataclass
class GlobalConfig:
    from_email: str
    from_name: str
    api_key_env: str
    personas: dict  # persona name -> cadence stage-day list, e.g. {"recruiter": [2, 3, 5]}
    schedule: ScheduleConfig
    consumer_domains_file: str
    path: Path
    # Defaulted (unlike every field above) so the many existing tests that
    # construct GlobalConfig directly for unrelated features (dashboard,
    # doctor, cleanup, runner, launchd) don't need to care about this one —
    # load_global_config() below is what actually enforces "must be present
    # in config.yaml," not this dataclass's own constructor.
    signature: str = ""
    # Also defaulted, but for a different reason than signature: this one is
    # genuinely OPTIONAL, not "required but allowed to be empty." The whole
    # Redis-cache feature is designed to gracefully degrade when Redis isn't
    # configured/running at all (see slap/gmass_cache.py), so there's no
    # fail-loud enforcement for this key anywhere — an owner who never adds
    # a `redis:` block to config.yaml just gets this sensible local default.
    redis_url: str = "redis://localhost:6379/0"
    # Deliberately a SEPARATE key from schedule.active_days, not a reuse of
    # it: active_days answers "when may SLAP's own local runner wake up" — a
    # low-stakes, reversible, purely local knob (worst case, a drain waits a
    # day). This answers "which days may GMass fire THIS recipient's
    # follow-ups" — locked in per-recipient at send time with no retroactive
    # fix possible (see slap.gmass.build_campaign_settings's docstring: no
    # code path anywhere ever re-POSTs an already-created campaign's
    # settings). Conflating the two would mean an edit to the low-stakes
    # local knob silently starts carrying irreversible, recipient-facing
    # consequences. None (the default) means "send no allowedDays field at
    # all" — fully unrestricted, byte-for-byte today's pre-existing
    # behavior for anyone who never adds this key.
    gmass_allowed_days: list = None
    # Independent of gmass_allowed_days, not derived from it: GMass's own
    # designated-holiday calendar can fall on an otherwise-allowed weekday.
    # TRI-STATE, not a plain bool: None (the default, key absent from
    # config.yaml) sends no skipHolidays field at all, inheriting whatever
    # GMass does server-side when it's omitted — confirmed directly by
    # GMass support that this default is actually TRUE (holidays ARE
    # skipped) even with no field sent, surprising enough that an owner who
    # wants holidays NOT skipped needs to be able to send `skipHolidays:
    # false` explicitly, not just "not send true" — a plain bool defaulting
    # to False could never express that distinction (both "unset" and
    # "explicitly false" would look identical). True/False (explicitly
    # configured) always sends that literal value.
    gmass_skip_holidays: bool = None


@dataclass
class CampaignField:
    key: str
    label: str
    optional: bool = False


@dataclass
class CampaignConfig:
    name: str
    path: Path
    persona: str
    cadence: list
    latex_enabled: bool
    attachment_name: str
    attachment_file: str | None
    fields: list
    subject_template: str
    body_template: str
    stage_bodies: list
    name_field: str | None = None


def load_global_config(path: Path = CONFIG_PATH) -> GlobalConfig:
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy config.yaml.example to {path} and fill in your "
            f"sender details. See SLAP_BUILD_PROMPT.md §4 for the schema."
        )
    raw = _load_yaml_mapping(path)

    from_email = _require(raw, "sender.from_email", path)
    from_name = _require(raw, "sender.from_name", path)
    api_key_env = _require(raw, "gmass.api_key_env", path)

    # _require raises only when the KEY is absent — a key present but set to
    # "" returns "" cleanly, exactly matching this feature's required
    # behavior: missing entirely -> fail loud; present-but-empty -> allowed,
    # renders with no signature, no warning. Every template's {{signature}}
    # relies on this always being at least a string (never None) once
    # loading succeeds.
    signature = _require(raw, "signature", path)
    if not isinstance(signature, str):
        raise ConfigError(f"{path}: 'signature' must be a string — got {signature!r}")

    personas_raw = _require(raw, "personas", path)
    if not isinstance(personas_raw, dict) or not personas_raw:
        raise ConfigError(f"{path}: 'personas' must be a non-empty mapping")
    personas = {}
    for name, body in personas_raw.items():
        if not isinstance(body, dict) or "stages" not in body:
            raise ConfigError(f"{path}: personas.{name} is missing required key 'stages'")
        stages = body["stages"]
        if not isinstance(stages, list) or not stages or not all(
            isinstance(s, int) and not isinstance(s, bool) for s in stages
        ):
            raise ConfigError(f"{path}: personas.{name}.stages must be a non-empty list of integers")
        personas[name] = stages

    sched_raw = _require(raw, "schedule", path)
    schedule_ctx = f"{path}: schedule"
    numeric_fields = ("send_delay_min", "send_delay_max", "daily_cap", "drain_retries")
    schedule_values = {}
    for key in ("fire_window_start", "fire_window_end", *numeric_fields):
        schedule_values[key] = _require(sched_raw, key, schedule_ctx)
    for key in numeric_fields:
        value = schedule_values[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"{schedule_ctx}.{key} must be an integer — got {value!r}")

    active_days_raw = _require(sched_raw, "active_days", schedule_ctx)
    active_days = _validate_day_list(active_days_raw, f"{schedule_ctx}.active_days")

    # Optional, unlike every field above — absent entirely -> None -> the
    # Analytics page's weekly-goal-pacing widget is simply not shown (see
    # dashboard.weekly_goal_progress()). Present-but-invalid still fails
    # loud, same as every other malformed schedule value here.
    weekly_target = sched_raw.get("weekly_target")
    if weekly_target is not None and (
        not isinstance(weekly_target, int) or isinstance(weekly_target, bool) or weekly_target <= 0
    ):
        raise ConfigError(f"{schedule_ctx}.weekly_target must be a positive integer — got {weekly_target!r}")

    schedule = ScheduleConfig(
        fire_window_start=schedule_values["fire_window_start"],
        fire_window_end=schedule_values["fire_window_end"],
        send_delay_min=schedule_values["send_delay_min"],
        send_delay_max=schedule_values["send_delay_max"],
        daily_cap=schedule_values["daily_cap"],
        drain_retries=schedule_values["drain_retries"],
        active_days=active_days,
        weekly_target=weekly_target,
    )

    consumer_domains_file = _require(raw, "tracking.consumer_domains_file", path)

    # Optional block, unlike everything else above — no _require(). Absent
    # `redis:` entirely, or absent `redis.url` within it, both just fall
    # back to the dataclass default (see GlobalConfig.redis_url's own
    # comment for why this one is genuinely optional, not required-but-
    # allowed-to-be-empty like `signature`).
    redis_raw = raw.get("redis") or {}
    if not isinstance(redis_raw, dict):
        raise ConfigError(f"{path}: 'redis' must be a mapping — got {redis_raw!r}")
    redis_url = redis_raw.get("url", "redis://localhost:6379/0")
    if not isinstance(redis_url, str):
        raise ConfigError(f"{path}: redis.url must be a string — got {redis_url!r}")

    # Both optional, unlike gmass.api_key_env above (already _require()'d,
    # so `gmass` is already guaranteed to be a dict by this point). Absent
    # entirely -> None/False -> build_campaign_settings() sends neither
    # field, byte-for-byte today's pre-existing behavior.
    gmass_raw = raw["gmass"]
    gmass_allowed_days_raw = gmass_raw.get("allowed_days")
    gmass_allowed_days = (
        _validate_day_list(gmass_allowed_days_raw, f"{path}: gmass.allowed_days")
        if gmass_allowed_days_raw is not None else None
    )
    gmass_skip_holidays = gmass_raw.get("skip_holidays")
    if gmass_skip_holidays is not None and not isinstance(gmass_skip_holidays, bool):
        raise ConfigError(f"{path}: gmass.skip_holidays must be a boolean — got {gmass_skip_holidays!r}")

    return GlobalConfig(
        from_email=from_email,
        from_name=from_name,
        api_key_env=api_key_env,
        personas=personas,
        schedule=schedule,
        consumer_domains_file=consumer_domains_file,
        path=path,
        signature=signature,
        redis_url=redis_url,
        gmass_allowed_days=gmass_allowed_days,
        gmass_skip_holidays=gmass_skip_holidays,
    )


def discover_campaigns(campaigns_dir: Path = CAMPAIGNS_DIR) -> list:
    """Any folder under campaigns_dir containing a campaign.yaml file is live.

    This is a presence check only (no central registry) — deeper schema and
    cross-reference validation happens in load_campaign() when a specific
    campaign is actually used.
    """
    if not campaigns_dir.exists():
        return []
    return sorted(p.parent.name for p in campaigns_dir.glob("*/campaign.yaml") if p.is_file())


def load_campaign(name: str, global_config: GlobalConfig, campaigns_dir: Path = CAMPAIGNS_DIR) -> CampaignConfig:
    campaign_path = campaigns_dir / name
    yaml_path = campaign_path / "campaign.yaml"
    if not yaml_path.exists():
        raise ConfigError(f"campaign '{name}': {yaml_path} not found")

    raw = _load_yaml_mapping(yaml_path)

    persona = _require(raw, "persona", yaml_path)
    if not isinstance(persona, str):
        raise ConfigError(f"{yaml_path}: 'persona' must be a string — got {persona!r}")
    if persona not in global_config.personas:
        raise ConfigError(
            f"{yaml_path}: persona '{persona}' is not defined in {global_config.path} "
            f"(known personas: {sorted(global_config.personas)})"
        )
    cadence = global_config.personas[persona]

    latex_enabled = _require(raw, "latex.enabled", yaml_path)
    if not isinstance(latex_enabled, bool):
        raise ConfigError(f"{yaml_path}: latex.enabled must be true or false")
    attachment_name = _require(raw, "latex.attachment_name", yaml_path)

    attachment_file = raw.get("attachment_file")
    if not latex_enabled and not attachment_file:
        raise ConfigError(
            f"{yaml_path}: latex.enabled is false, so 'attachment_file' is required"
        )

    fields_raw = _require(raw, "fields", yaml_path)
    if not isinstance(fields_raw, list) or not fields_raw:
        raise ConfigError(f"{yaml_path}: 'fields' must be a non-empty list")
    fields = []
    for f in fields_raw:
        if not isinstance(f, dict) or "key" not in f or "label" not in f:
            raise ConfigError(f"{yaml_path}: each field needs 'key' and 'label' — got {f!r}")
        fields.append(CampaignField(key=f["key"], label=f["label"], optional=bool(f.get("optional", False))))
    if not any(f.key == "email" for f in fields):
        raise ConfigError(f"{yaml_path}: 'fields' must include a field with key 'email' — send needs it")

    name_field = raw.get("name_field")
    if name_field is not None:
        if not isinstance(name_field, str):
            raise ConfigError(f"{yaml_path}: 'name_field' must be a string — got {name_field!r}")
        if not any(f.key == name_field for f in fields):
            raise ConfigError(
                f"{yaml_path}: name_field '{name_field}' is not one of the declared fields "
                f"({sorted(f.key for f in fields)})"
            )

    subject_template, body_template = _validate_initial_txt(campaign_path / "initial.txt")
    _validate_stage_files(campaign_path, cadence)
    stage_bodies = _read_stage_bodies(campaign_path, cadence)
    _validate_placeholder_keys(
        yaml_path, {f.key for f in fields} | CONFIG_SOURCED_KEYS, subject_template, body_template, *stage_bodies
    )

    return CampaignConfig(
        name=name,
        path=campaign_path,
        persona=persona,
        cadence=cadence,
        latex_enabled=latex_enabled,
        attachment_name=attachment_name,
        attachment_file=attachment_file,
        fields=fields,
        subject_template=subject_template,
        body_template=body_template,
        stage_bodies=stage_bodies,
        name_field=name_field,
    )


def parse_initial_txt_text(text: str, *, ctx: str = "initial.txt") -> tuple:
    """The Subject:-line + blank-line-separator shape shared by a real
    initial.txt file (_validate_initial_txt below) AND `slap.onboard`'s
    interactive wizard, which validates a live paste against this EXACT rule
    before anything is written to campaigns/<name>/ — one source of truth so
    the two can never quietly drift apart. `ctx` is whatever the caller wants
    named in a raised ConfigError (a real path for the file-based caller, a
    plain step label for the wizard, which has no file yet to name)."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("Subject:"):
        raise ConfigError(f"{ctx}: first line must be 'Subject: ...'")
    if len(lines) < 2 or lines[1] != "":
        raise ConfigError(f"{ctx}: second line must be blank (Subject line + blank-line separator)")
    subject = lines[0].removeprefix("Subject:").lstrip(" ")
    body = "\n".join(lines[2:])
    return subject, body


def _validate_initial_txt(path: Path) -> tuple:
    if not path.exists():
        raise ConfigError(f"{path} not found — every campaign needs an initial.txt")
    return parse_initial_txt_text(path.read_text(), ctx=str(path))


def _validate_stage_files(campaign_path: Path, cadence: list) -> None:
    expected = {f"stage{i}.txt" for i in range(1, len(cadence) + 1)}
    found = {p.name for p in campaign_path.glob("stage*.txt")}
    missing = expected - found
    if missing:
        raise ConfigError(
            f"{campaign_path}: persona cadence has {len(cadence)} stage(s) but is missing "
            f"{sorted(missing)}"
        )
    extra = found - expected
    if extra:
        raise ConfigError(
            f"{campaign_path}: found unexpected stage file(s) {sorted(extra)} — persona "
            f"cadence only defines {len(cadence)} stage(s)"
        )


def _read_stage_bodies(campaign_path: Path, cadence: list) -> list:
    """Called only after _validate_stage_files confirms the exact file set exists."""
    return [(campaign_path / f"stage{i}.txt").read_text() for i in range(1, len(cadence) + 1)]


def _validate_placeholder_keys(yaml_path: Path, known_keys: set, *texts: str) -> None:
    used = set()
    malformed = set()
    for text in texts:
        used |= extract_placeholder_keys(text)
        malformed |= find_malformed_placeholders(text)
    if malformed:
        raise ConfigError(
            f"{yaml_path}: malformed placeholder(s) {sorted(malformed)} — a {{{{...}}}} "
            f"whose inside isn't a plain word would survive unfilled into a sent email"
        )
    unknown = used - known_keys
    if unknown:
        raise ConfigError(
            f"{yaml_path}: template(s) reference undefined field key(s) {sorted(unknown)} "
            f"— check campaign.yaml 'fields' vs {{{{...}}}} placeholders (config-sourced "
            f"constants like {sorted(CONFIG_SOURCED_KEYS)} are always allowed and need no "
            f"matching field)"
        )
