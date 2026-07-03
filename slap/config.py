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

from slap.templates import extract_placeholder_keys, find_malformed_placeholders

CONFIG_PATH = Path("config.yaml")
CAMPAIGNS_DIR = Path("campaigns")


class ConfigError(Exception):
    """Raised on any fail-loud config/campaign validation failure."""


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


@dataclass
class ScheduleConfig:
    fire_window_start: str
    fire_window_end: str
    send_delay_min: int
    send_delay_max: int
    daily_cap: int
    drain_retries: int


@dataclass
class GlobalConfig:
    from_email: str
    from_name: str
    api_key_env: str
    personas: dict  # persona name -> cadence stage-day list, e.g. {"recruiter": [2, 3, 5]}
    schedule: ScheduleConfig
    consumer_domains_file: str
    path: Path


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
    schedule = ScheduleConfig(
        fire_window_start=schedule_values["fire_window_start"],
        fire_window_end=schedule_values["fire_window_end"],
        send_delay_min=schedule_values["send_delay_min"],
        send_delay_max=schedule_values["send_delay_max"],
        daily_cap=schedule_values["daily_cap"],
        drain_retries=schedule_values["drain_retries"],
    )

    consumer_domains_file = _require(raw, "tracking.consumer_domains_file", path)

    return GlobalConfig(
        from_email=from_email,
        from_name=from_name,
        api_key_env=api_key_env,
        personas=personas,
        schedule=schedule,
        consumer_domains_file=consumer_domains_file,
        path=path,
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

    subject_template, body_template = _validate_initial_txt(campaign_path / "initial.txt")
    _validate_stage_files(campaign_path, cadence)
    stage_bodies = _read_stage_bodies(campaign_path, cadence)
    _validate_placeholder_keys(
        yaml_path, {f.key for f in fields}, subject_template, body_template, *stage_bodies
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
    )


def _validate_initial_txt(path: Path) -> tuple:
    if not path.exists():
        raise ConfigError(f"{path} not found — every campaign needs an initial.txt")
    lines = path.read_text().splitlines()
    if not lines or not lines[0].startswith("Subject:"):
        raise ConfigError(f"{path}: first line must be 'Subject: ...'")
    if len(lines) < 2 or lines[1] != "":
        raise ConfigError(f"{path}: second line must be blank (Subject line + blank-line separator)")
    subject = lines[0].removeprefix("Subject:").lstrip(" ")
    body = "\n".join(lines[2:])
    return subject, body


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
            f"— check campaign.yaml 'fields' vs {{{{...}}}} placeholders"
        )
