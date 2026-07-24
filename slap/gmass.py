"""GMass API client (Build Order step 6).

Wired to the Phase-0-verified API contract only — every shape/endpoint here
was proven against the live API before being hardcoded (see CONTROL_SHEET.md
and probes/findings/*.json), not guessed from GMass's blog/docs. Deviations
from the brief's §3 examples that Phase-0 caught:

- Step 2 is `POST /api/campaigns/{campaignDraftId}` (id in the URL path).
  Putting `campaignDraftId` in the body returns 400.
- Attachments are base64 JSON (`{fileName, contentType, base64Content}`),
  not multipart (multipart returns 415).
- Send fields (`stageOneDays`, `openTracking`, ...) are flat top-level JSON
  fields on the POST /api/campaigns/{id} body — NOT nested under a
  `campaignSettings` wrapper key. ("campaignSettings" is just the name of
  the swagger model documenting this field list, not a JSON envelope.)
- Stage fields use GMass's English-ordinal names — `stageOneDays`,
  `stageTwoDays`, `stageThreeDays` — not `stage1Days`/`stage2Days`.
- Stop-on-reply is a per-stage `stageNAction: "r"`, not a single global flag.
- **`messageType` must be `"html"` for click tracking to do anything**
  (verified live, post-launch — see CONTROL_SHEET.md's "Post-launch BLOCKER:
  click tracking never fires" section). The `campaignDraft` swagger model
  only documents `"html"`/`"plain"` (not `"text"`, which this client used to
  send). More importantly: `clickTracking: true` works by rewriting an
  `<a href>` target to a GMass tracking redirect while keeping the visible
  text/URL looking normal — a plain-text message has no anchor tag to
  rewrite, so click tracking is structurally impossible on it regardless of
  the `clickTracking` flag. `create_draft()` therefore auto-converts the
  plain-text `message` it's given into minimal HTML (escaped, line breaks
  preserved, bare URLs auto-linkified into real `<a href>` tags) and sends
  `messageType: "html"` — campaign `.txt` template authoring stays 100%
  plain text; only the wire format changes.
- **`unsubscribe_recipient()` uses the ACCOUNT-WIDE `POST /api/unsubscribes`,
  not the per-campaign `POST /api/unsubscribes/{campaignId}` its own name
  suggests.** Live-tested first: the per-campaign variant never actually
  registers anything (see that function's own docstring + CONTROL_SHEET.md's
  "manual OOO pause" section for the full evidence). This is a real,
  accepted tradeoff — see the function's docstring.

Idempotency (§3): create_draft() and send_campaign() are deliberately two
separate calls, mirroring the two real API calls. The caller MUST persist
the returned draft_id (e.g. via slap.tracking.append_event) the instant
create_draft() returns, BEFORE calling send_campaign() — so a crash/failure
between the two calls leaves a retryable draft, never an orphan or a
double-send. This module has no tracking-store dependency and does not
enforce that ordering itself; wiring the two together is the runner's job
(step 9), which has both this client and the tracking store available.
"""
from __future__ import annotations

import base64
import html
import re
from urllib.parse import urlparse

import requests

BASE_URL = "https://api.gmass.co/api"

# Every real HTTP call this module makes uses this timeout — an iron-audit
# BLOCKER fix found while building the dashboard's Redis-backed cache
# (slap/gmass_cache.py): NOT ONE call here ever had a timeout before this,
# meaning a single hung/slow GMass response could block a caller
# indefinitely. That's exactly what caused a real, owner-hit incident (the
# dashboard "just kept buffering" — see CONTROL_SHEET.md) and directly
# undermines the new cache's own safety margin (a refresh's lock TTL is
# only a meaningful upper bound if each individual call it makes is itself
# bounded). 20s is generous for a real GMass response but still finite.
DEFAULT_TIMEOUT = 20

REPORT_TYPES = {"replies", "clicks", "bounces", "opens", "recipients", "unsubscribes", "blocks"}

# GMass's per-stage field names use English ordinal words, not digits.
_ORDINALS = ["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight"]

# campaignSettings.allowedDays's live-verified accepted format — full day
# names, case-insensitive, comma-separated. NOT what the live swagger spec's
# own field description claims ("comma separated string where 1=Saturday,
# 2=Sunday, 3=Monday, and so on"): every integer 0-20 (plain and zero-padded)
# was rejected live with HTTP 400 "Invalid day of the week: {n}"; every full
# day name (any case) was accepted with HTTP 200. Abbreviations ("Mon",
# "Su", single letters) were also rejected — only the full name works.
# Confirmed via a live sweep (probes/findings/full_sweep_allowed_days_result.json),
# not trusted from the docs.
_GMASS_DAY_NAME = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}


def _allowed_days_value(days: list) -> str:
    return ",".join(_GMASS_DAY_NAME[d] for d in days)

_URL_RE = re.compile(r'https?://[^\s<>"]+')


class GMassError(Exception):
    """Raised when the GMass API returns an unexpected/error response."""


def _plain_text_to_html(text: str) -> str:
    """Minimal plain-text -> HTML conversion so click tracking has a real
    <a href> to rewrite (see the module docstring). Escapes HTML-special
    characters, auto-linkifies bare URLs into anchor tags, and preserves
    line breaks — campaign .txt template authoring never needs to change.

    Each link's VISIBLE TEXT is the URL's domain, not the full URL —
    verified live (three real guarded sends, see CONTROL_SHEET.md): GMass
    only rewrites an <a href> for click tracking when its visible text
    DIFFERS from the href. An anchor whose text is identical to its own
    href (a "naked" URL rendered as a link — what a naive linkify would
    produce) is left untouched, silently defeating clickTracking=true.
    Domain-only text is enough to trigger rewriting, requires no
    per-site label mapping, and still shows the recipient roughly where a
    link goes rather than an opaque "click here"."""
    escaped = html.escape(text)

    def _linkify(match: re.Match) -> str:
        # match is against the ALREADY-escaped text (e.g. a literal '&' in
        # the URL is '&amp;' at this point) — unescape back to the raw URL,
        # then re-escape it correctly as an HTML attribute value.
        url = html.unescape(match.group(0))
        label = urlparse(url).netloc or url
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'

    linkified = _URL_RE.sub(_linkify, escaped)
    return linkified.replace("\n", "<br>\n")


def _headers(api_key: str) -> dict:
    return {"X-apikey": api_key}


def _parse(resp: requests.Response, context: str) -> dict:
    if resp.status_code >= 400:
        raise GMassError(f"GMass {context} returned HTTP {resp.status_code}: {resp.text[:500]}")
    try:
        return resp.json()
    except ValueError as e:
        raise GMassError(f"GMass {context} returned non-JSON response: {resp.text[:500]}") from e


def create_draft(api_key: str, *, recipient: str, subject: str, message: str,
                  attachment: tuple = None) -> dict:
    """POST /api/campaigndrafts — creates the Gmail draft, carries the
    attachment. `attachment`, if given, is (filename, bytes, content_type).
    `message` is plain text (matches every caller/template in this app) —
    converted to minimal HTML here (see _plain_text_to_html) so click
    tracking has a real <a href> to rewrite; sent as messageType="html".
    Returns {"draft_id": ..., "raw": <full response body>}."""
    body = {
        "emailAddresses": recipient,
        "subject": subject,
        "message": _plain_text_to_html(message),
        "messageType": "html",
    }
    if attachment:
        fname, fbytes, ctype = attachment
        body["attachments"] = [{
            "fileName": fname,
            "contentType": ctype,
            "base64Content": base64.b64encode(fbytes).decode(),
        }]
    resp = requests.post(f"{BASE_URL}/campaigndrafts", headers=_headers(api_key), json=body,
                          timeout=DEFAULT_TIMEOUT)
    data = _parse(resp, "campaigndrafts")
    draft_id = data.get("campaignDraftId") or data.get("id")
    if not draft_id:
        raise GMassError(f"campaigndrafts response had no draft id: {data!r}")
    return {"draft_id": draft_id, "raw": data}


def send_campaign(api_key: str, draft_id, *, campaign_settings: dict) -> dict:
    """POST /api/campaigns/{draft_id} — sends it and sets the follow-up
    sequence. `draft_id` goes in the URL path (verified: a body-field
    `{campaignDraftId: ...}` form returns 400). Returns
    {"campaign_id": ..., "raw": <full response body>}."""
    resp = requests.post(f"{BASE_URL}/campaigns/{draft_id}", headers=_headers(api_key),
                          json=campaign_settings, timeout=DEFAULT_TIMEOUT)
    data = _parse(resp, "campaigns")
    campaign_id = data.get("campaignId") or data.get("id")
    if not campaign_id:
        raise GMassError(f"campaigns response had no campaign id: {data!r}")
    return {"campaign_id": campaign_id, "raw": data}


def unsubscribe_recipient(api_key: str, email: str) -> dict:
    """POST /api/unsubscribes — GMass's ACCOUNT-WIDE unsubscribe list. Called
    by the manual OOO pause (slap.dashboard.tag_reply) the moment the owner
    marks a recipient OOO, including from an email SLAP never saw (so there
    was never a detected `reply` event for the per-stage
    `stageNAction: "r"` stop-on-reply to react to) — this is the mechanism
    SLAP relies on to stop GMass's own native follow-up timer for that
    recipient.

    **What is actually verified, and what is NOT — read carefully before
    trusting this more than the evidence supports.** VERIFIED live
    (`probes/run.py unsubscribe`, findings recorded in
    `probes/findings/unsubscribe_*.json` — see CONTROL_SHEET.md for the full
    writeup): the call registers a real, queryable record (real timestamp,
    shows up immediately in `GET /api/reports/0/unsubscribes`), and a LATER
    manual send to the same address (exactly what the OOO-pause reschedule
    does) still goes out normally afterward. **NOT verified — genuinely
    unproven, not just untested**: whether this registered record actually
    causes GMass to skip firing its own native follow-up stages for this
    recipient. That can only be observed days out, after a real stage's
    scheduled fire date passes, and hasn't been. This is inferred (not
    confirmed) from it being GMass's own real, compliance-driven "stop
    sending this address" list — the same class of residual gap as the
    original stop-on-reply proof (see CONTROL_SHEET.md's tracked
    follow-ups), not a settled fact. Design accordingly: local pause-window
    enforcement (slap.queue.due_for_ooo_resend) is what actually controls
    SLAP's OWN sends and is fully verified; this call is SLAP's only lever
    against GMass's native timer and is the one part of the whole feature
    still resting on an unconfirmed assumption.

    ACCOUNT-WIDE, NOT PER-CAMPAIGN — a deliberate choice, not an oversight.
    GMass's docs also describe a per-campaign variant
    (`POST /api/unsubscribes/{campaignId}`, "suppresses an email address for
    just a particular email campaign"), which is what this feature's design
    originally called for (narrower blast radius). Live-tested it first and
    it does NOT appear to register anything AT ALL: its own response echoes
    a zero-value default timestamp (`0001-01-01T00:00:00`, not a real one),
    the campaign's own `GET /api/campaigns/{id}` read-back shows
    `statistics.unsubscribes` staying at 0, and the address never appears in
    ANY unsubscribes report — that campaign's own, a sibling campaign's, or
    the account-wide one (`campaignId=0`) — even after ~90s of polling. The
    account-wide endpoint used here, by contrast, is the one confirmed to
    register at all (see above).

    Real, accepted tradeoff (owner-confirmed): marking one recipient OOO in
    one campaign now also silences their native GMass follow-ups in every
    OTHER campaign they're ever contacted in — broader than originally
    scoped, but the only mechanism GMass's API actually registers anything
    for. No reversal (`DELETE /api/unsubscribes`) is ever called by this
    app — once marked OOO, SLAP's own scheduling permanently owns the rest
    of that recipient's sequence; there's no path back to GMass's native
    timer.

    Returns the parsed `unsubscribe` object ({emailAddress, unsubscribeTime,
    sender})."""
    resp = requests.post(f"{BASE_URL}/unsubscribes", headers=_headers(api_key),
                          json={"emailAddress": email}, timeout=DEFAULT_TIMEOUT)
    return _parse(resp, "unsubscribes")


def get_reports(api_key: str, campaign_id, report_type: str) -> list:
    """GET /api/reports/{campaignId}/{report_type}. Returns the
    ApiListResponse[T] envelope's `data` list (items are camelCase — see
    CONTROL_SHEET.md for the per-type item schema; do not build against
    /api/sample/* — that legacy path returns a different, inconsistent
    schema)."""
    if report_type not in REPORT_TYPES:
        raise GMassError(f"unknown report type {report_type!r} — must be one of {sorted(REPORT_TYPES)}")
    resp = requests.get(f"{BASE_URL}/reports/{campaign_id}/{report_type}", headers=_headers(api_key),
                        timeout=DEFAULT_TIMEOUT)
    data = _parse(resp, f"reports/{report_type}")
    return data.get("data", [])


def build_campaign_settings(cadence: list, stage_bodies: list, *, open_tracking: bool = False,
                             click_tracking: bool = True, create_drafts: bool = False,
                             stop_action: str = "r", allowed_days: list = None,
                             skip_holidays: bool = None) -> dict:
    """Build the flat send_campaign() body for an initial send: one
    stageNDays/stageNCampaignText/stageNAction triple per persona cadence
    stage, using GMass's English-ordinal field names. Does not cover the OOO
    send-as-reply shape (sendAsReply/campaignIdToReplyTo) — that's a
    distinct, simpler payload built by step 10's re-queue path.

    Each `stageNCampaignText` is run through `_plain_text_to_html()`, same as
    `create_draft()`'s `message` — GMass's follow-up stages are fired from
    within this same campaign object, so a stage body with a bare URL is just
    as un-anchored (and just as invisible to `clickTracking`) as the initial
    email was before that conversion was added there. Every real campaign's
    stageN.txt ends in `{{signature}}`, which carries real links, so this
    isn't a theoretical gap.

    This one call is the entire reason `slap.py template-reload` (post-
    launch, see `slap.reload`'s module docstring) can only ever touch a
    recipient with nothing sent at all: every stage's wording gets flattened
    into `stageNCampaignText` fields on THIS call, sent exactly once at
    initial send (`slap.runner._send_one`) — there is no second call anyone
    could make later to update stage 2/3's text after the fact. Confirmed
    against this function before `template-reload` was built, not assumed.

    `allowed_days`/`skip_holidays` are OPTIONAL and additive: `allowed_days`
    omitted (`None`, the default) sends no `allowedDays` field at all —
    byte-for-byte the pre-existing request body. `skipWeekends` is
    deliberately never sent by this function: `allowedDays` is a strict
    superset (omitting sat/sun already encodes "skip weekends," plus
    arbitrary other exclusions), and the API gives no documented precedence
    rule for what happens if the two disagree.

    `skip_holidays` is TRI-STATE, not a plain on/off: `None` (the default)
    omits `skipHolidays` entirely; `True`/`False` sends that literal value.
    This matters because GMass support has confirmed their server-side
    default when the field is OMITTED is actually `True` (holidays ARE
    skipped) — so an owner who explicitly wants holidays NOT skipped needs
    to be able to send `skipHolidays: false`, which a plain bool defaulting
    to `False` could never distinguish from "not configured at all."

    GMass support has also confirmed (2026-07-13, in response to this
    exact question) that `allowedDays` reschedules AUTO FOLLOW-UP STAGES,
    not just the initial send: a follow-up that would otherwise land on a
    disallowed day gets pushed to the next allowed day/time. This was the
    one open question this whole feature depended on — no longer a
    documented-but-unverified blog claim.

    Like every other field this function sets, `allowed_days`/
    `skip_holidays` are locked in permanently at THIS call — see this
    function's own docstring above about template-reload for why."""
    if len(cadence) != len(stage_bodies):
        raise GMassError(
            f"cadence has {len(cadence)} stage(s) but {len(stage_bodies)} stage bodies given"
        )
    if len(cadence) > len(_ORDINALS):
        raise GMassError(f"GMass supports at most {len(_ORDINALS)} follow-up stages")

    settings = {
        "openTracking": open_tracking,
        "clickTracking": click_tracking,
        "createDrafts": create_drafts,
    }
    for i, (days, body) in enumerate(zip(cadence, stage_bodies)):
        ordinal = _ORDINALS[i]
        settings[f"stage{ordinal}Days"] = days
        settings[f"stage{ordinal}CampaignText"] = _plain_text_to_html(body)
        settings[f"stage{ordinal}Action"] = stop_action
    if allowed_days:
        settings["allowedDays"] = _allowed_days_value(allowed_days)
    if skip_holidays is not None:
        settings["skipHolidays"] = skip_holidays
    return settings


def build_reply_settings(campaign_id_to_reply_to: int, *, open_tracking: bool = False,
                          click_tracking: bool = True, create_drafts: bool = False) -> dict:
    """Build the flat send_campaign() body for an OOO resend (§7, step 10):
    a single stage sent as a reply into the original conversation via
    `sendAsReply` + `campaignIdToReplyTo` — deterministic threading, never
    GMass's own "last conversation" auto-detection. `campaignIdToReplyTo` is
    an integer per the Phase-0-verified contract (see CONTROL_SHEET.md)."""
    return {
        "openTracking": open_tracking,
        "clickTracking": click_tracking,
        "createDrafts": create_drafts,
        "sendAsReply": True,
        "campaignIdToReplyTo": int(campaign_id_to_reply_to),
    }
