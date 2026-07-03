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

import requests

BASE_URL = "https://api.gmass.co/api"

REPORT_TYPES = {"replies", "clicks", "bounces", "opens", "recipients", "unsubscribes", "blocks"}

# GMass's per-stage field names use English ordinal words, not digits.
_ORDINALS = ["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight"]


class GMassError(Exception):
    """Raised when the GMass API returns an unexpected/error response."""


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
    Returns {"draft_id": ..., "raw": <full response body>}."""
    body = {
        "emailAddresses": recipient,
        "subject": subject,
        "message": message,
        "messageType": "text",
    }
    if attachment:
        fname, fbytes, ctype = attachment
        body["attachments"] = [{
            "fileName": fname,
            "contentType": ctype,
            "base64Content": base64.b64encode(fbytes).decode(),
        }]
    resp = requests.post(f"{BASE_URL}/campaigndrafts", headers=_headers(api_key), json=body)
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
                          json=campaign_settings)
    data = _parse(resp, "campaigns")
    campaign_id = data.get("campaignId") or data.get("id")
    if not campaign_id:
        raise GMassError(f"campaigns response had no campaign id: {data!r}")
    return {"campaign_id": campaign_id, "raw": data}


def get_reports(api_key: str, campaign_id, report_type: str) -> list:
    """GET /api/reports/{campaignId}/{report_type}. Returns the
    ApiListResponse[T] envelope's `data` list (items are camelCase — see
    CONTROL_SHEET.md for the per-type item schema; do not build against
    /api/sample/* — that legacy path returns a different, inconsistent
    schema)."""
    if report_type not in REPORT_TYPES:
        raise GMassError(f"unknown report type {report_type!r} — must be one of {sorted(REPORT_TYPES)}")
    resp = requests.get(f"{BASE_URL}/reports/{campaign_id}/{report_type}", headers=_headers(api_key))
    data = _parse(resp, f"reports/{report_type}")
    return data.get("data", [])


def build_campaign_settings(cadence: list, stage_bodies: list, *, open_tracking: bool = False,
                             click_tracking: bool = True, create_drafts: bool = False,
                             stop_action: str = "r") -> dict:
    """Build the flat send_campaign() body for an initial send: one
    stageNDays/stageNCampaignText/stageNAction triple per persona cadence
    stage, using GMass's English-ordinal field names. Does not cover the OOO
    send-as-reply shape (sendAsReply/campaignIdToReplyTo) — that's a
    distinct, simpler payload built by step 10's re-queue path."""
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
        settings[f"stage{ordinal}CampaignText"] = body
        settings[f"stage{ordinal}Action"] = stop_action
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
