"""Deterministic, storage-free per-campaign display colors.

Every campaign gets a stable identity color derived purely from its NAME —
no color is ever stored anywhere (not in campaign.yaml, not in the DB, not in
a sidecar file). This is the iron "derived, not stored" discipline applied to
a display value: the color is a pure function of the campaign name, recomputed
on demand, so it is identical across every process/run/machine and "cannot be
changed" falls out for free (the user's requirement) — the only way to change a
campaign's color is to rename the campaign, which every other part of the app
already treats as a different campaign entirely.

Why hash the NAME (not the campaign's sorted position): positional assignment
(index in `discover_campaigns()`'s sorted list) would silently reshuffle every
later campaign's color the moment one is added or removed mid-list. Hashing the
name pins each campaign's color independent of what other campaigns exist.

Why NOT Python's builtin `hash()`: str hashing is salted per-process
(PYTHONHASHSEED) unless explicitly disabled, so `hash("coldpost-yc")` differs
between runs — which would violate "same campaign, same color, always." We use
`hashlib.sha256` (stable across processes and Python versions) → an index into
a fixed categorical palette.

Colors are generated in OKLCH (perceptually uniform lightness) and converted to
sRGB hex here, once per (campaign, mode), following the dataviz skill's method:
a fixed set of categorical hue anchors, and MODE-SPECIFIC lightness/chroma bands
so the same hue reads as a legible, contrast-safe chip on both the light
(`--surface-1 #fcfcfb`) and dark (`--surface-1 #1a1a19`) dashboard surfaces —
light marks sit darker/more-saturated, dark marks sit lighter, per the skill's
documented per-mode bands. The React side consumes only the returned hex via a
CSS custom property; it never recomputes or hardcodes a color.
"""
from __future__ import annotations

import hashlib
import math

# Categorical hue anchors (degrees on the OKLCH hue wheel), chosen spread around
# the wheel and away from each other so adjacent campaigns in a roster read as
# clearly distinct. Twelve slots: enough variety for a realistic campaign count
# without letting worst-case adjacent hues collapse. A campaign roster is an
# open-ended set (unlike a <=8-series chart legend), so beyond these slots we
# simply wrap — two campaigns MAY share a hue by chance, which is acceptable for
# a decorative identity chip (it is never the sole carrier of meaning; the
# campaign name is always shown alongside it).
_HUE_ANCHORS = (
    262,  # indigo
    28,   # red-orange
    152,  # green
    322,  # magenta
    68,   # yellow-green / olive
    218,  # blue
    12,   # red
    128,  # emerald
    292,  # violet
    48,   # amber
    188,  # teal
    342,  # pink
)

# Mode-specific OKLCH lightness (L) and chroma (C). Within the dataviz skill's
# documented per-mode bands (light L 0.43-0.77, dark L 0.48-0.67), picked mid-band
# so any hue stays saturated-but-legible against the mode's surface.
_LIGHT_L, _LIGHT_C = 0.52, 0.135
_DARK_L, _DARK_C = 0.66, 0.115

# Neutral fallback (hue-less) — used when a caller asks for the color of a
# reserved pseudo-campaign like "__custom__", which should not get a random
# identity hue. Grey that works on both surfaces.
_NEUTRAL = {"light": "#8a8a86", "dark": "#9a9a95"}
_RESERVED = {"__custom__"}


def _stable_index(name: str, modulus: int) -> int:
    """A per-process-stable index in [0, modulus) derived from `name`.

    Uses sha256 (NOT builtin hash(), which is salted per process) so the same
    name always maps to the same slot on every machine and every run.
    """
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % modulus


def campaign_hue(name: str) -> int:
    """The stable OKLCH hue (degrees) assigned to `name`."""
    return _HUE_ANCHORS[_stable_index(name, len(_HUE_ANCHORS))]


def _oklch_to_hex(L: float, C: float, H_deg: float) -> str:
    """Convert one OKLCH color to an sRGB hex string, clamping into gamut.

    OKLCH -> OKLab -> linear sRGB -> gamma-encoded sRGB, per the OKLab spec
    (Björn Ottosson). Channels are clamped to [0,1] after the linear-sRGB step
    so any out-of-gamut (L,C,H) still yields a valid, closest-in-channel hex
    rather than raising — the anchors/bands above are chosen to stay in gamut,
    so clamping is a safety net, not the normal path.
    """
    h = math.radians(H_deg)
    a = C * math.cos(h)
    b = C * math.sin(h)

    # OKLab -> LMS' -> LMS
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l = l_ ** 3
    m = m_ ** 3
    s = s_ ** 3

    # LMS -> linear sRGB
    r_lin = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g_lin = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b_lin = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s

    def _encode(c: float) -> int:
        c = min(1.0, max(0.0, c))
        srgb = 1.055 * (c ** (1 / 2.4)) - 0.055 if c > 0.0031308 else 12.92 * c
        return round(min(1.0, max(0.0, srgb)) * 255)

    return "#{:02x}{:02x}{:02x}".format(_encode(r_lin), _encode(g_lin), _encode(b_lin))


def campaign_color(name: str, mode: str) -> str:
    """The sRGB hex identity color for `name` in the given `mode`.

    `mode` is "light" or "dark". Reserved pseudo-campaigns (e.g. the custom-send
    label "__custom__") get a neutral grey rather than a random identity hue.
    """
    if mode not in ("light", "dark"):
        raise ValueError(f"mode must be 'light' or 'dark', got {mode!r}")
    if name in _RESERVED:
        return _NEUTRAL[mode]
    hue = campaign_hue(name)
    if mode == "light":
        return _oklch_to_hex(_LIGHT_L, _LIGHT_C, hue)
    return _oklch_to_hex(_DARK_L, _DARK_C, hue)


def campaign_colors(name: str) -> dict:
    """Both-mode colors for `name`: {"light": "#rrggbb", "dark": "#rrggbb"}.

    Returned to the frontend so a campaign chip/row-tint can be themed from one
    payload without a second request when the user toggles light/dark.
    """
    return {"light": campaign_color(name, "light"), "dark": campaign_color(name, "dark")}
