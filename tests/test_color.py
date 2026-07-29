"""Per-campaign color tests: deterministic, stable, storage-free, theme-safe."""
import os
import re
import subprocess
import sys

import pytest

from slap.color import campaign_color, campaign_colors, campaign_hue

HEX = re.compile(r"^#[0-9a-f]{6}$")


def test_color_is_valid_hex_in_both_modes():
    for mode in ("light", "dark"):
        assert HEX.match(campaign_color("coldpost-yc", mode))


def test_color_is_deterministic_within_process():
    assert campaign_color("linkpost-recruiter", "light") == campaign_color("linkpost-recruiter", "light")


def test_light_and_dark_differ():
    # Same hue, different lightness band per mode — a chip must adapt to its
    # surface, not render identically on both.
    c = campaign_colors("coldpost-founder")
    assert c["light"] != c["dark"]


def test_different_campaigns_generally_get_different_hues():
    names = [
        "coldpost-founder", "coldpost-recruiter", "coldpost-yc",
        "linkpost-hiringmanager", "linkpost-recruiter", "linkpost-vibe-startups",
    ]
    hues = {campaign_hue(n) for n in names}
    # Not a guarantee of uniqueness (open-ended set wraps the anchor list), but
    # a realistic small roster should spread across several anchors.
    assert len(hues) >= 4


def test_reserved_custom_campaign_gets_neutral_not_a_random_hue():
    # The custom-send pseudo-campaign must not be assigned an identity hue.
    assert campaign_color("__custom__", "light") == "#8a8a86"
    assert campaign_color("__custom__", "dark") == "#9a9a95"


def test_invalid_mode_fails_loud():
    with pytest.raises(ValueError):
        campaign_color("x", "sepia")


def test_color_is_stable_across_processes():
    # The whole point of sha256 over builtin hash(): the SAME campaign resolves
    # to the SAME color in a brand-new interpreter (no PYTHONHASHSEED drift).
    code = (
        "from slap.color import campaign_color;"
        "print(campaign_color('coldpost-yc','light'), campaign_color('coldpost-yc','dark'))"
    )
    out1 = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    out2 = subprocess.check_output(
        [sys.executable, "-c", code], text=True,
        env={**os.environ, "PYTHONHASHSEED": "1"},
    ).strip()
    # And matches the in-process value.
    live = f"{campaign_color('coldpost-yc','light')} {campaign_color('coldpost-yc','dark')}"
    assert out1 == out2 == live
