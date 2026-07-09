#!/usr/bin/env python3
"""GMass API-truth probes (Phase 0).

Single CLI, one subcommand per probe. Every probe records its raw request +
response into probes/findings/ so findings are auditable, not just summarized.

SAFETY (non-negotiable, not a config knob): any outbound recipient that is not
a plus-tagged self-test address for THIS install's own config.yaml sender.from_email
raises BEFORE any network call. See _guard()/_owner_email_parts(). Config-driven
(not a hardcoded literal) so the guard always follows whoever owns the install —
a clone with a different owner's config.yaml is automatically guarded to THEIR
inbox, never to one hardcoded person's. This makes emailing a real lead with test
data impossible.

Usage:
    python probes/run.py <auth|attach|casing|stop|thread|reports|verify|swagger|client|resend|clicktest|unsubscribe|all>
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_URL = "https://api.gmass.co/api"
FINDINGS_DIR = Path(__file__).parent / "findings"

# Campaign ids produced during a run, so the reports probe can query real per-campaign data.
_RUN_STATE = {"campaign_ids": []}
_OWNER_EMAIL_PARTS = {}


def _owner_email_parts() -> tuple:
    """Loads config.yaml's sender.from_email lazily (once per process) and
    splits it into (local, domain) — the guard and every probe's self-test
    recipient are derived from this, never hardcoded. Fail loud if config.yaml
    is missing/invalid: the safety guard must never silently fall back to
    some default address."""
    if "parts" not in _OWNER_EMAIL_PARTS:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from slap.config import ConfigError, load_global_config
        try:
            gc = load_global_config()
        except ConfigError as e:
            sys.exit(f"FAIL: could not load config.yaml to build the owner safety guard — {e}\n"
                     f"Run `python slap.py init` first.")
        local, sep, domain = gc.from_email.partition("@")
        if not sep or not local or not domain:
            sys.exit(f"FAIL: config.yaml sender.from_email={gc.from_email!r} is not a valid "
                     f"email address — cannot build the safety guard.")
        _OWNER_EMAIL_PARTS["parts"] = (local, domain)
    return _OWNER_EMAIL_PARTS["parts"]


def _allowed_re():
    local, domain = _owner_email_parts()
    return re.compile(rf"^{re.escape(local)}\+testmass\d+@{re.escape(domain)}$")


def owner_test_address(n) -> str:
    """The Nth self-test address for whoever owns THIS install's config.yaml
    (e.g. local+testmass3@domain) — never a hardcoded literal."""
    local, domain = _owner_email_parts()
    return f"{local}+testmass{n}@{domain}"


def _guard(recipient: str) -> str:
    """Raise before any network call if recipient is not an owner self-test address."""
    if not _allowed_re().match(recipient or ""):
        local, domain = _owner_email_parts()
        raise RuntimeError(
            f"SAFETY GUARD: refusing to target {recipient!r}. Probes may only send to "
            f"{local}+testmass{{N}}@{domain}. No network call was made."
        )
    return recipient


def _guard_body(body: dict) -> dict:
    """Re-guard the ACTUAL outbound recipient fields on a request body, immediately
    before the network call. This is the real safety boundary: an early _guard() on a
    function argument can be stale if the body is mutated afterward (e.g. via an
    `extra` dict merged in later). Checks emailAddresses/to/cc/bcc/listAddress — every field this
    API accepts that could carry a recipient. Called right before every requests.post
    that sends to a recipient."""
    for field in ("emailAddresses", "to", "cc", "bcc", "listAddress"):
        value = body.get(field)
        if value is None:
            continue
        for addr in (value if isinstance(value, list) else [value]):
            _guard(addr)
    return body


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _api_key() -> str:
    load_dotenv()
    key = os.environ.get("GMASS_API_KEY", "").strip()
    if not key:
        sys.exit("FAIL: GMASS_API_KEY not set (put it in .env or the environment).")
    return key


def _record(probe: str, payload: dict) -> None:
    """Persist a raw capture and echo a short summary."""
    FINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = FINDINGS_DIR / f"{probe}_{_now()}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"  -> recorded {path}")


def _summarize(resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:2000]
    return {"status": resp.status_code, "body": body}


# --- probes -----------------------------------------------------------------

def probe_auth(key: str) -> None:
    """#1 Auth transport: query param vs X-apikey header vs both, on GET /api/sheets."""
    print("[auth] GET /api/sheets three ways")
    attempts = {
        "query_param": lambda: requests.get(f"{BASE_URL}/sheets", params={"apikey": key}),
        "header": lambda: requests.get(f"{BASE_URL}/sheets", headers={"X-apikey": key}),
        "both": lambda: requests.get(
            f"{BASE_URL}/sheets", params={"apikey": key}, headers={"X-apikey": key}
        ),
    }
    results = {}
    for name, call in attempts.items():
        r = call()
        results[name] = _summarize(r)
        print(f"  {name}: HTTP {r.status_code}")
    _record("auth", {"probe": "auth", "results": results})


def probe_swagger(key: str) -> None:
    """Fetch + save the authoritative OpenAPI spec (closes iron-audit BLOCKER: CONTROL_SHEET
    cited swagger claims with no saved artifact). No recipient involved — public docs
    endpoint, no guard needed. Records the full `definitions` section (small; `paths` is
    omitted for size but definitions is what every schema claim in CONTROL_SHEET rests on)
    plus explicit computed answers to the specific claims under audit: does `autoFollowup`/
    `autoFollowupBatch` (the only read-model surface for stage config) have an `action`
    field anywhere, and does the `campaign` read model (what GET /api/campaigns/{id}
    returns) expose `campaignSettings` (where `stageOneAction` actually lives as a
    write-only input field)."""
    print("[swagger] GET https://api.gmass.co/swagger/docs/v1")
    r = requests.get("https://api.gmass.co/swagger/docs/v1", timeout=30)
    print(f"  status: {r.status_code}")
    body = r.json() if r.status_code == 200 else r.text[:2000]
    definitions = body.get("definitions", {}) if isinstance(body, dict) else {}

    def _props(name: str) -> list:
        return list(definitions.get(name, {}).get("properties", {}).keys())

    auto_followup_props = _props("autoFollowup")
    auto_followup_batch_props = _props("autoFollowupBatch")
    campaign_props = _props("campaign")
    campaign_settings_props = _props("campaignSettings")
    action_unreachable_from_any_read_model = (
        "action" not in auto_followup_props
        and "action" not in auto_followup_batch_props
        and "campaignSettings" not in campaign_props
    )
    findings = {
        "autoFollowup_properties": auto_followup_props,
        "autoFollowup_has_action": "action" in auto_followup_props,
        "autoFollowupBatch_properties": auto_followup_batch_props,
        "autoFollowupBatch_has_action": "action" in auto_followup_batch_props,
        "campaign_read_model_properties": campaign_props,
        "campaign_read_model_exposes_campaignSettings": "campaignSettings" in campaign_props,
        "campaignSettings_has_stageOneAction": "stageOneAction" in campaign_settings_props,
        "conclusion": (
            "action lives ONLY in the write-only campaignSettings/extraStage input models; "
            "the campaign read model (GET /api/campaigns/{id}) has no campaignSettings field "
            "and autoFollowup/autoFollowupBatch (its only stage-related read models) have no "
            "action field either -- so no live read call can ever echo back which action "
            "value was configured for a stage."
            if action_unreachable_from_any_read_model else
            "UNEXPECTED: an action-bearing field IS reachable from a read model -- "
            "re-check the claim in CONTROL_SHEET.md."
        ),
    }
    for key_name, val in findings.items():
        if key_name.endswith("_properties"):
            continue
        print(f"  {key_name}: {val}")
    _record("swagger", {
        "probe": "swagger", "status": r.status_code,
        "top_level_keys": list(body.keys()) if isinstance(body, dict) else None,
        "findings": findings,
        "definitions": definitions,
    })


def _create_draft(key: str, recipient: str, *, subject, message,
                  attachment: tuple | None = None, extra: dict | None = None) -> dict:
    """POST /api/campaigndrafts as a JSON `campaignDraft` (verified via probe + OpenAPI
    spec: the endpoint is a .NET JSON API — multipart is rejected 415). attachment=
    (filename, bytes) is encoded as a `campaignDraftAttachment`:
    {fileName, contentType, base64Content}. Returns {request, response, draft_id}."""
    _guard(recipient)
    body = {
        "emailAddresses": recipient,
        "subject": subject,
        "message": message,
        "messageType": "text",
    }
    if extra:
        body.update(extra)
    req_note = "no attachment"
    if attachment:
        fname, fbytes = attachment
        body["attachments"] = [{
            "fileName": fname,
            "contentType": "application/pdf",
            "base64Content": base64.b64encode(fbytes).decode(),
        }]
        req_note = f"JSON attachments[0] fileName={fname}, {len(fbytes)} bytes base64"
    _guard_body(body)
    r = requests.post(f"{BASE_URL}/campaigndrafts", headers={"X-apikey": key}, json=body)
    summary = _summarize(r)
    draft_id = None
    if isinstance(summary["body"], dict):
        draft_id = summary["body"].get("campaignDraftId") or summary["body"].get("id")
    logged = {k: (v[:80] if isinstance(v, str) else v) for k, v in body.items() if k != "attachments"}
    return {
        "request": {"data": logged, "attachment": req_note},
        "response": summary,
        "draft_id": draft_id,
    }


def _campaign_id(summary: dict):
    body = summary.get("body")
    if isinstance(body, dict):
        return body.get("campaignId") or body.get("id")
    return None


def _post_campaign(key: str, draft_id, fields: dict, shape: str):
    """Send/keep a campaign. shape='path' -> POST /campaigns/{id}; shape='body' -> POST
    /campaigns with campaignDraftId in the body (the two forms the docs disagree on).
    No recipient parameter: the recipient is fixed by the already-guarded draft_id, so
    this can never reach an unguarded address."""
    if shape == "path":
        return requests.post(f"{BASE_URL}/campaigns/{draft_id}",
                            headers={"X-apikey": key}, json=dict(fields))
    body = dict(fields)
    body["campaignDraftId"] = draft_id
    return requests.post(f"{BASE_URL}/campaigns", headers={"X-apikey": key}, json=body)


def probe_attach(key: str) -> None:
    """#3 Attachment mechanism on /api/campaigndrafts. RESOLVED: JSON body, attachments as
    an array of campaignDraftAttachment {fileName, contentType, base64Content}. This probe
    records the working positive control, the multipart negative control (415), and a
    size sweep so the accepted ceiling is visible."""
    print("[attach] POST /api/campaigndrafts — JSON attachment (correct), multipart (neg), size sweep")
    recipient = owner_test_address(1)
    tiny_pdf = _tiny_pdf()

    # Positive control: the verified JSON attachment mechanism.
    correct = _create_draft(
        key, recipient, subject="slap probe attach (json)",
        message="probe attach json", attachment=("resume.pdf", tiny_pdf),
    )
    print(f"  json-attachment: HTTP {correct['response']['status']}, draft_id={correct['draft_id']}")

    # Negative control: multipart/form-data — documents that the API rejects it (415).
    neg_data = {"emailAddresses": recipient, "subject": "slap probe attach (multipart neg)",
                "message": "neg"}
    _guard_body(neg_data)
    rmp = requests.post(
        f"{BASE_URL}/campaigndrafts", headers={"X-apikey": key},
        data=neg_data,
        files={"attachments": ("resume.pdf", tiny_pdf, "application/pdf")},
    )
    multipart_neg = _summarize(rmp)
    print(f"  multipart-negative: HTTP {multipart_neg['status']} (expect 415)")

    # Size sweep (#3): representative sizes via the correct JSON path; record status per size.
    size_results = {}
    for mb in (1, 5, 10, 20, 25):
        pdf = _padded_pdf(mb * 1024 * 1024)
        res = _create_draft(
            key, recipient, subject=f"slap probe attach {mb}MB",
            message=f"probe attach {mb}MB", attachment=(f"probe_{mb}mb.pdf", pdf),
        )
        size_results[f"{mb}MB"] = {"status": res["response"]["status"],
                                   "draft_id": res["draft_id"],
                                   "body": res["response"]["body"]}
        print(f"  size {mb}MB: HTTP {res['response']['status']}")
    _record("attach", {"probe": "attach", "json_attachment": correct,
                      "multipart_negative": multipart_neg, "size_limit": size_results})


def probe_casing(key: str) -> None:
    """#4 Exact casing + endpoint shape: two FRESH drafts, given an EQUAL settle delay,
    then path-form vs body-form. (Fixes a confound in the original run: body-form used a
    freshly-created draft with zero delay while path-form reused an older, already-
    settled draft — producing a 400 that was actually GMass's generic "draft hasn't been
    saved into your Gmail account yet, try waiting a few more seconds" transient error,
    not a shape/schema rejection. Equalizing settle time isolates the real cause.)"""
    print("[casing] two fresh, equally-settled drafts -> path-form vs body-form")
    recipient = owner_test_address(1)
    d_path = _create_draft(key, recipient, subject="slap probe casing (path)",
                           message="probe casing path-form body")
    d_body = _create_draft(key, recipient, subject="slap probe casing (body)",
                           message="probe casing body-form body")
    print(f"  draft[path]: HTTP {d_path['response']['status']}, draft_id={d_path['draft_id']}")
    print(f"  draft[body]: HTTP {d_body['response']['status']}, draft_id={d_body['draft_id']}")

    settle_seconds = 15
    print(f"  waiting {settle_seconds}s settle delay (equal for both drafts)...")
    time.sleep(settle_seconds)

    # createDrafts:true is ASSUMED to suppress the actual send so casing can be
    # inspected send-free (worst case it sends only to the guarded +testmass1).
    send_fields = {
        "openTracking": False,
        "clickTracking": True,
        "createDrafts": True,  # keep as draft; casing probe must not fire a real send
        "stageOneDays": 2,
        "stageOneCampaignText": "casing stage one",
        "stageOneAction": "r",
    }
    result = {"probe": "casing", "settle_seconds": settle_seconds,
              "draft_path": d_path, "draft_body": d_body}
    # Probe BOTH endpoint shapes the docs disagree on:
    #   path form  -> POST /api/campaigns/{id}            (blog / create-send-campaign)
    #   body form  -> POST /api/campaigns  {campaignDraftId: id}  (brief §3)
    for shape, d in (("path", d_path), ("body", d_body)):
        if not d["draft_id"]:
            continue
        r = _post_campaign(key, d["draft_id"], send_fields, shape)
        result[f"campaigns_call_{shape}"] = {
            "shape": shape, "sent_fields": send_fields,
            "campaignDraftId_in_body": shape == "body",
            "response": _summarize(r)}
        print(f"  campaigns[{shape}] (createDrafts=true, settled {settle_seconds}s): HTTP {r.status_code}")
    _record("casing", result)


def probe_stop(key: str) -> None:
    """#2 Stop-on-reply param: real self-send with stageOneAction='r'. Records accepted settings."""
    print("[stop] real self-send with stageOneAction='r' (If No Reply)")
    recipient = owner_test_address(2)
    draft = _create_draft(key, recipient, subject="slap probe stop-on-reply",
                          message="probe stop-on-reply body")
    print(f"  draft: HTTP {draft['response']['status']}, draft_id={draft['draft_id']}")
    result = {"probe": "stop", "draft": draft}
    if draft["draft_id"]:
        send_fields = {
            "openTracking": False,
            "clickTracking": True,
            "createDrafts": False,  # real send to self
            "stageOneDays": 2,
            "stageOneCampaignText": "stop probe stage one",
            "stageOneAction": "r",
        }
        r = _post_campaign(key, draft["draft_id"], send_fields, "path")
        summary = _summarize(r)
        result["campaigns_call"] = {"sent_fields": send_fields, "response": summary}
        cid = _campaign_id(summary)
        result["campaign_id"] = cid
        if cid:
            _RUN_STATE["campaign_ids"].append(cid)
        print(f"  send: HTTP {r.status_code}, campaign_id={cid}")
    _record("stop", result)


def probe_thread(key: str) -> None:
    """#5 sendAsReply threading: initial send, then a send-as-reply into that campaign."""
    print("[thread] initial send, then sendAsReply into it")
    recipient = owner_test_address(3)
    d1 = _create_draft(key, recipient, subject="slap probe thread initial",
                       message="thread initial body")
    campaign_id = None
    if d1["draft_id"]:
        r1 = _post_campaign(key, d1["draft_id"],
                           {"createDrafts": False, "openTracking": False, "clickTracking": True},
                           "path")
        s1 = _summarize(r1)
        campaign_id = _campaign_id(s1)
        if campaign_id:
            _RUN_STATE["campaign_ids"].append(campaign_id)
        print(f"  initial send: HTTP {r1.status_code}, campaign_id={campaign_id}")
    result = {"probe": "thread", "initial": d1, "initial_campaign_id": campaign_id}

    if campaign_id:
        time.sleep(5)
        d2 = _create_draft(key, recipient, subject="slap probe thread reply",
                          message="thread reply body")
        if d2["draft_id"]:
            reply_fields = {
                "createDrafts": False, "openTracking": False, "clickTracking": True,
                "sendAsReply": True, "campaignIdToReplyTo": campaign_id,
            }
            r2 = _post_campaign(key, d2["draft_id"], reply_fields, "path")
            s2 = _summarize(r2)
            result["reply"] = {"draft": d2, "sent_fields": reply_fields, "response": s2}
            rcid = _campaign_id(s2)
            if rcid:
                _RUN_STATE["campaign_ids"].append(rcid)
            print(f"  reply send: HTTP {r2.status_code}")
    _record("thread", result)


def probe_reports(key: str) -> None:
    """#6 Reports shapes: replies, clicks, bounces JSON structure.

    Report shapes are per-campaign, so query using a campaign id produced earlier in the
    run when available (SHOULD-FIX from audit). Also record the no-id and query-param
    variants so the real path shape is discoverable even run in isolation."""
    cid = _RUN_STATE["campaign_ids"][0] if _RUN_STATE["campaign_ids"] else None
    print(f"[reports] GET reports/{{campaignId}}/{{replies|clicks|bounces}} (campaign_id={cid})")
    hdr = {"X-apikey": key}
    results = {}
    for name in ("replies", "clicks", "bounces"):
        results[name] = {}
        # Sample endpoint: returns example shape without needing a live campaign.
        rs = requests.get(f"{BASE_URL}/sample/{name}", headers=hdr)
        results[name]["sample"] = _summarize(rs)
        print(f"  {name}[sample]: HTTP {rs.status_code}")
        if cid:
            rc = requests.get(f"{BASE_URL}/reports/{cid}/{name}", headers=hdr)
            results[name]["campaign"] = _summarize(rc)
            print(f"  {name}[campaign {cid}]: HTTP {rc.status_code}")
    _record("reports", {"probe": "reports", "campaign_id": cid, "results": results,
                        "envelope": "ApiListResponse[T]: {metadata:{links,totalRecords,offset,limit,count}, data:[...]}"})


def probe_verify(key: str) -> None:
    """Positive-verification probe (closes iron-audit B1/B2/S3/S2): a REAL send with a
    real attachment, then reads the campaign back and polls the recipients report until
    it has real data — closing the gap where a bare HTTP 200 was treated as proof a
    field registered, and where every prior report capture had an empty `data` array.

    - B2: GET /api/campaigns/{id} after the send, recording `autoFollowups`, the only
      read-model surface the live OpenAPI spec (https://api.gmass.co/swagger/docs/v1)
      exposes for stage config. The `autoFollowup`/`autoFollowupBatch` schemas have NO
      `action` field at all — there is no read endpoint that echoes back stageOneAction.
      That is a real API limitation, recorded as a residual tracked follow-up below, not
      silently assumed away.
    - B1: poll GET /api/reports/{id}/recipients (populates immediately on a real send,
      unlike replies/clicks/bounces which need real recipient action) until non-empty,
      to capture one genuine live item shape instead of an empty array.
    - S3: real send (createDrafts:false) with a real PDF attachment. Whether the PDF is
      actually present in the delivered Gmail message can't be checked via the API —
      that remains an explicit manual owner check (see CONTROL_SHEET.md)."""
    print("[verify] real send + attachment, then GET campaign back + poll recipients report")
    recipient = owner_test_address(5)
    tiny_pdf = _tiny_pdf()
    draft = _create_draft(
        key, recipient, subject="slap probe verify (real send + attachment)",
        message="probe verify body", attachment=("resume.pdf", tiny_pdf),
    )
    print(f"  draft: HTTP {draft['response']['status']}, draft_id={draft['draft_id']}")
    result = {"probe": "verify", "recipient": recipient, "draft": draft}
    if not draft["draft_id"]:
        _record("verify", result)
        return

    send_fields = {
        "openTracking": False,
        "clickTracking": True,
        "createDrafts": False,  # REAL send, to the guarded test address only
        "stageOneDays": 2,
        "stageOneCampaignText": "verify probe stage one",
        "stageOneAction": "r",
    }
    r = _post_campaign(key, draft["draft_id"], send_fields, "path")
    summary = _summarize(r)
    campaign_id = _campaign_id(summary)
    result["send"] = {"sent_fields": send_fields, "response": summary, "campaign_id": campaign_id}
    print(f"  send: HTTP {r.status_code}, campaign_id={campaign_id}")
    if campaign_id:
        _RUN_STATE["campaign_ids"].append(campaign_id)
    if not campaign_id:
        _record("verify", result)
        return

    hdr = {"X-apikey": key}

    # B2: read the campaign back. Read-only, addressed by id, no recipient in this call.
    rc = requests.get(f"{BASE_URL}/campaigns/{campaign_id}", headers=hdr)
    result["campaign_readback"] = _summarize(rc)
    print(f"  GET campaign back: HTTP {rc.status_code}")

    # B1: poll recipients report with backoff until non-empty (or attempts exhausted).
    # Observed live: campaign.status stays "scheduled" and the recipients report is
    # empty for several seconds after a real send; it settles to "sent" with a populated
    # recipients report around ~45s worst case (see verify_20260702T205332Z.json). Poll
    # window sized with margin above that observed settle time.
    recipients_polls = []
    last_report = None
    for attempt in range(8):
        time.sleep(15)
        rr = requests.get(f"{BASE_URL}/reports/{campaign_id}/recipients", headers=hdr)
        last_report = _summarize(rr)
        data = last_report["body"].get("data") if isinstance(last_report["body"], dict) else None
        record_count = len(data) if isinstance(data, list) else None
        recipients_polls.append({"attempt": attempt + 1, "status": rr.status_code,
                                 "record_count": record_count})
        print(f"  recipients poll {attempt + 1}: HTTP {rr.status_code}, records={record_count}")
        if record_count:
            break
    result["recipients_polls"] = recipients_polls
    result["recipients_report"] = last_report
    _record("verify", result)


def probe_clicktest(key: str) -> None:
    """Investigates a real BLOCKER bug report: a delivered email's links stay raw
    (never rewritten to a GMass click-tracking redirect) despite clickTracking=true.
    Root-cause hypothesis: _create_draft()/slap.gmass.create_draft() hardcode
    messageType="text" -- NOT a valid campaignDraft enum value (the live swagger
    spec documents only "html"/"plain"). Click tracking works by rewriting <a href>
    targets, which only exist in an HTML message; a plain-text message has no anchor
    tag to rewrite, so clickTracking=true has nothing to act on.

    Sends TWO real test emails, each with a real clickable link, clickTracking=true/
    openTracking=false, to compare messageType="plain" vs messageType="html". The
    live API has no endpoint that echoes back the delivered message content (the
    `campaign` read model has no message/body/links field at all -- checked against
    the saved swagger spec), so whether either link was actually rewritten can only
    be confirmed by opening the two real received emails -- a manual step, recorded
    as such rather than silently assumed."""
    print("[clicktest] two real sends (messageType=plain vs html), each with a real link, clickTracking=true")
    result = {"probe": "clicktest", "sends": {}}
    link = "https://www.linkedin.com/in/slap-probe-test"

    cases = [
        ("plain", owner_test_address(8), f"Plain-text click-tracking test.\n\nLink: {link}\n"),
        ("html", owner_test_address(9),
         f'<p>HTML click-tracking test.</p><p><a href="{link}">Click here</a></p>'),
    ]
    for message_type, recipient, message in cases:
        draft = _create_draft(
            key, recipient, subject=f"slap probe clicktest ({message_type})",
            message=message, extra={"messageType": message_type},
        )
        print(f"  [{message_type}] draft: HTTP {draft['response']['status']}, draft_id={draft['draft_id']}")
        entry = {"recipient": recipient, "message_type": message_type, "link": link, "draft": draft}
        if draft["draft_id"]:
            send_fields = {
                "openTracking": False, "clickTracking": True, "createDrafts": False,
                "stageOneDays": 2, "stageOneCampaignText": "clicktest stage one", "stageOneAction": "r",
            }
            r = _post_campaign(key, draft["draft_id"], send_fields, "path")
            summary = _summarize(r)
            campaign_id = _campaign_id(summary)
            print(f"  [{message_type}] send: HTTP {r.status_code}, campaign_id={campaign_id}")
            entry["send"] = {"sent_fields": send_fields, "response": summary, "campaign_id": campaign_id}
            if campaign_id:
                _RUN_STATE["campaign_ids"].append(campaign_id)
        result["sends"][message_type] = entry

    print("  MANUAL STEP REQUIRED: open both delivered test emails in Gmail and check whether "
          "the link's actual href/hover target (right-click -> copy link, or hover and read the "
          "status bar) is rewritten to a gmass.co domain for EACH message type above.")
    _record("clicktest", result)


def probe_resend(key: str) -> None:
    """Resolves iron-audit SHOULD-FIX #2 (step 9): if a runner crashes AFTER
    send_campaign succeeds server-side but BEFORE the local 'sent' event
    commits, a retry (which correctly reuses the existing draft_id per §3
    idempotency) will call POST /api/campaigns/{draftId} a SECOND time on a
    draft that's already been sent. Is that safe (rejected/no-op) or does it
    actually double-send? Real send, to the guarded test address only —
    genuinely calls the same endpoint twice on purpose to observe this."""
    print("[resend] real send, then call POST /api/campaigns/{draftId} a second time on the SAME draft")
    recipient = owner_test_address(7)
    draft = _create_draft(key, recipient, subject="slap probe resend (idempotency check)",
                          message="probe resend body")
    print(f"  draft: HTTP {draft['response']['status']}, draft_id={draft['draft_id']}")
    result = {"probe": "resend", "recipient": recipient, "draft": draft}
    if not draft["draft_id"]:
        _record("resend", result)
        return

    send_fields = {
        "openTracking": False, "clickTracking": True, "createDrafts": False,
        "stageOneDays": 2, "stageOneCampaignText": "resend probe stage one", "stageOneAction": "r",
    }
    r1 = _post_campaign(key, draft["draft_id"], send_fields, "path")
    s1 = _summarize(r1)
    cid1 = _campaign_id(s1)
    print(f"  first send: HTTP {r1.status_code}, campaign_id={cid1}")
    result["first_send"] = {"sent_fields": send_fields, "response": s1, "campaign_id": cid1}
    if cid1:
        _RUN_STATE["campaign_ids"].append(cid1)

    # The actual question: call send_campaign AGAIN on the exact same draft_id.
    r2 = _post_campaign(key, draft["draft_id"], send_fields, "path")
    s2 = _summarize(r2)
    cid2 = _campaign_id(s2)
    print(f"  second send (same draft_id): HTTP {r2.status_code}, campaign_id={cid2}")
    result["second_send"] = {"sent_fields": send_fields, "response": s2, "campaign_id": cid2}
    if cid2:
        _RUN_STATE["campaign_ids"].append(cid2)

    result["same_campaign_id"] = (cid1 == cid2) if (cid1 and cid2) else None
    _record("resend", result)


def probe_client(key: str) -> None:
    """Live verification of the PRODUCTION slap.gmass client (Build Order step 6) —
    not this script's own duplicate HTTP calls. Proves create_draft/send_campaign/
    get_reports/build_campaign_settings work end-to-end against the real API exactly
    as the mocked unit tests (tests/test_gmass.py) assume they do. Real send, to the
    guarded test address only — never a real lead."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from slap import gmass  # local import: only this probe needs the production package

    print("[client] live-verify slap.gmass.* against the real API (self-send only)")
    recipient = _guard(owner_test_address(6))
    tiny_pdf = _tiny_pdf()

    draft = gmass.create_draft(
        key, recipient=recipient, subject="slap probe client (production gmass.py)",
        message="probe client body", attachment=("resume.pdf", tiny_pdf, "application/pdf"),
    )
    print(f"  create_draft: draft_id={draft['draft_id']}")
    result = {"probe": "client", "recipient": recipient, "draft": draft}

    cadence = [2, 3, 5]
    stage_bodies = ["stage one body", "stage two body", "stage three body"]
    settings = gmass.build_campaign_settings(cadence, stage_bodies, create_drafts=False)
    result["campaign_settings"] = settings

    sent = gmass.send_campaign(key, draft["draft_id"], campaign_settings=settings)
    print(f"  send_campaign: campaign_id={sent['campaign_id']}")
    result["send"] = sent

    recipients_polls = []
    report_items = []
    for attempt in range(8):
        time.sleep(15)
        report_items = gmass.get_reports(key, sent["campaign_id"], "recipients")
        recipients_polls.append({"attempt": attempt + 1, "record_count": len(report_items)})
        print(f"  get_reports(recipients) poll {attempt + 1}: records={len(report_items)}")
        if report_items:
            break
    result["recipients_polls"] = recipients_polls
    result["recipients_report_items"] = report_items
    _record("client", result)


def probe_unsubscribe(key: str) -> None:
    """Verifies the manual-OOO-pause feature's core safety mechanism before any
    production code relies on it: does POST /api/unsubscribes/{campaignId}
    actually scope to just that one campaign (its OWN summary's claim --
    "Suppresses an email address for just a particular email campaign",
    operationId Unsubscribes_AddUnsubscribeForCampaign) or does it apply
    account-wide (GET /api/reports/{campaignId}/unsubscribes's OWN description's
    claim -- "Unsubscribed addresses apply account-wide and are eliminated from
    future campaigns")? GMass's own docs directly contradict each other on this
    -- checked empirically here rather than trusted from either description.

    Also checks the question the whole reschedule design actually depends on:
    does unsubscribing a recipient block a SUBSEQUENT manual send to them (a
    fresh create_draft + send_campaign call -- exactly what the OOO-pause
    reschedule does on the recipient's return date), or does GMass's
    unsubscribe suppression only affect its OWN native automated follow-up
    firing, leaving an explicit single-recipient API-driven send unaffected?
    If suppression silently swallows the manual resend too, the reschedule
    design as specified cannot work.

    Real sends, guarded test address only. Three campaigns to the SAME
    guarded address: A (gets unsubscribed after sending), B (an independent
    control, left untouched, to check for cross-campaign leakage), C (created
    AFTER A is unsubscribed, to check whether a manual resend still actually
    goes out). NOTE: whether GMass's native stage-2/3 timer for campaign A
    actually stays silent is inherently timing-dependent (days out, per that
    campaign's own cadence) and is NOT something this probe can prove in one
    run -- see CONTROL_SHEET.md's tracked follow-ups for the same class of gap
    already open on stop-on-reply."""
    print("[unsubscribe] does POST /api/unsubscribes/{campaignId} stay scoped to one "
          "campaign, and does it block a later manual resend to the same address?")
    recipient = owner_test_address(10)
    result = {"probe": "unsubscribe", "recipient": recipient}
    hdr = {"X-apikey": key}

    def _send(label):
        draft = _create_draft(key, recipient, subject=f"slap probe unsubscribe ({label})",
                              message=f"unsubscribe probe body ({label})")
        entry = {"draft": draft, "campaign_id": None}
        print(f"  [{label}] draft: HTTP {draft['response']['status']}, draft_id={draft['draft_id']}")
        if draft["draft_id"]:
            send_fields = {
                "openTracking": False, "clickTracking": True, "createDrafts": False,
                "stageOneDays": 2, "stageOneCampaignText": f"unsubscribe probe {label} stage one",
                "stageOneAction": "r",
            }
            r = _post_campaign(key, draft["draft_id"], send_fields, "path")
            summary = _summarize(r)
            campaign_id = _campaign_id(summary)
            print(f"  [{label}] send: HTTP {r.status_code}, campaign_id={campaign_id}")
            entry["send"] = {"sent_fields": send_fields, "response": summary, "campaign_id": campaign_id}
            entry["campaign_id"] = campaign_id
            if campaign_id:
                _RUN_STATE["campaign_ids"].append(campaign_id)
        return entry

    entry_a = _send("A-to-be-unsubscribed")
    result["campaign_a"] = entry_a
    campaign_a_id = entry_a["campaign_id"]

    time.sleep(3)
    entry_b = _send("B-control-untouched")
    result["campaign_b"] = entry_b
    campaign_b_id = entry_b["campaign_id"]

    if not campaign_a_id:
        _record("unsubscribe", result)
        return

    time.sleep(3)
    unsub_resp = requests.post(f"{BASE_URL}/unsubscribes/{campaign_a_id}", headers=hdr,
                               json={"emailAddress": recipient})
    result["unsubscribe_call"] = _summarize(unsub_resp)
    print(f"  unsubscribe recipient for campaign {campaign_a_id}: HTTP {unsub_resp.status_code}")

    time.sleep(3)

    # Q1: cross-campaign scope.
    scope_checks = {}
    for label, cid in (("campaign_a", campaign_a_id), ("campaign_b", campaign_b_id), ("account_wide", 0)):
        if cid is None:
            continue
        rr = requests.get(f"{BASE_URL}/reports/{cid}/unsubscribes", headers=hdr)
        summary = _summarize(rr)
        data = summary["body"].get("data") if isinstance(summary["body"], dict) else None
        found = isinstance(data, list) and any(
            (item.get("emailAddress") or "").lower() == recipient.lower() for item in data
        )
        scope_checks[label] = {"campaign_id": cid, "response": summary, "recipient_present": found}
        print(f"  unsubscribes report [{label}] (campaign_id={cid}): HTTP {rr.status_code}, "
              f"recipient present={found}")
    result["scope_checks"] = scope_checks

    # Campaign A's own first-party read model: GET /api/campaigns/{id} exposes
    # statistics.unsubscribes -- a campaign-scoped counter independent of the
    # reports/unsubscribes endpoint used above. If the per-campaign POST had
    # actually registered anything against campaign A specifically, THIS
    # counter is where it would show up.
    rc = requests.get(f"{BASE_URL}/campaigns/{campaign_a_id}", headers=hdr)
    rc_summary = _summarize(rc)
    unsubscribes_stat = None
    if isinstance(rc_summary["body"], dict):
        unsubscribes_stat = rc_summary["body"].get("statistics", {}).get("unsubscribes")
    result["campaign_a_readback"] = rc_summary
    result["campaign_a_statistics_unsubscribes"] = unsubscribes_stat
    print(f"  GET campaign {campaign_a_id} back: HTTP {rc.status_code}, "
          f"statistics.unsubscribes={unsubscribes_stat}")

    # Control: does the ACCOUNT-WIDE POST /api/unsubscribes (no campaignId)
    # actually work at all, on this same account/plan? Uses a SEPARATE
    # address so it can't be confused with campaign_a/b/c's own recipient,
    # and is reversed (DELETE) at the end so no stray state is left on the
    # real account.
    account_wide_email = owner_test_address(11)
    aw_resp = requests.post(f"{BASE_URL}/unsubscribes", headers=hdr, json={"emailAddress": account_wide_email})
    result["account_wide_unsubscribe_call"] = _summarize(aw_resp)
    print(f"  account-wide POST /api/unsubscribes for {account_wide_email}: HTTP {aw_resp.status_code}")
    time.sleep(3)
    aw_report = requests.get(f"{BASE_URL}/reports/0/unsubscribes", headers=hdr)
    aw_report_summary = _summarize(aw_report)
    aw_data = aw_report_summary["body"].get("data") if isinstance(aw_report_summary["body"], dict) else None
    aw_found = isinstance(aw_data, list) and any(
        (item.get("emailAddress") or "").lower() == account_wide_email.lower() for item in aw_data
    )
    result["account_wide_control"] = {
        "email": account_wide_email, "report_after_post": aw_report_summary, "recipient_present": aw_found,
    }
    print(f"  account-wide report after account-wide POST: HTTP {aw_report.status_code}, present={aw_found}")
    # Reverse it -- this is a real, permanent-ish suppression on the real
    # account; don't leave stray state behind from this probe run.
    del_resp = requests.delete(f"{BASE_URL}/unsubscribes", headers=hdr, params={"emailAddress": account_wide_email})
    result["account_wide_control_cleanup_delete"] = _summarize(del_resp)
    print(f"  cleanup DELETE /api/unsubscribes for {account_wide_email}: HTTP {del_resp.status_code}")

    result["conclusion"] = (
        "POST /api/unsubscribes/{campaignId} (per-campaign) does NOT appear to register "
        "anything -- its own response echoes a zero-value default timestamp, campaign A's "
        "own statistics.unsubscribes counter never increments, and the address never shows "
        "up in ANY unsubscribes report (that campaign's, a sibling's, or the account-wide "
        "one). POST /api/unsubscribes (account-wide, no campaignId) DOES work -- real "
        "timestamp, shows up immediately in the account-wide report. A manual resend to the "
        "address after either unsubscribe attempt still goes out normally (see campaign_c) "
        "-- suppression, whichever kind, does not block an explicit API-driven send."
        if unsubscribes_stat == 0 and not any(c["recipient_present"] for c in scope_checks.values()) and aw_found
        else "UNEXPECTED -- re-check the individual fields above before trusting the summary in "
             "slap.gmass.unsubscribe_recipient's docstring."
    )

    # Q2 (the one the whole feature depends on): does a brand-new manual send
    # to the now-unsubscribed address -- exactly what the reschedule does on
    # the return date -- still actually go out?
    time.sleep(3)
    entry_c = _send("C-after-unsubscribe")
    result["campaign_c"] = entry_c
    campaign_c_id = entry_c["campaign_id"]

    if campaign_c_id:
        recipients_polls = []
        last_report = None
        for attempt in range(8):
            time.sleep(15)
            rr = requests.get(f"{BASE_URL}/reports/{campaign_c_id}/recipients", headers=hdr)
            last_report = _summarize(rr)
            data = last_report["body"].get("data") if isinstance(last_report["body"], dict) else None
            record_count = len(data) if isinstance(data, list) else None
            recipients_polls.append({"attempt": attempt + 1, "status": rr.status_code,
                                     "record_count": record_count})
            print(f"  post-unsubscribe manual send poll {attempt + 1}: HTTP {rr.status_code}, "
                  f"records={record_count}")
            if record_count:
                break
        result["campaign_c_recipients_polls"] = recipients_polls
        result["campaign_c_recipients_report"] = last_report
        result["manual_send_after_unsubscribe_appears_to_have_gone_out"] = bool(
            last_report and isinstance(last_report["body"], dict) and last_report["body"].get("data")
        )

    print("  MANUAL STEP STILL REQUIRED: confirm in Gmail that campaign C's email actually "
          "arrived (the recipients report populating is a strong signal GMass processed the "
          "send, not proof of inbox delivery -- same residual gap as every other real send "
          "this probe suite has ever made).")
    _record("unsubscribe", result)


def _tiny_pdf() -> bytes:
    """Smallest valid one-page PDF."""
    return (b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
            b"trailer<</Root 1 0 R>>\n%%EOF")


def _padded_pdf(total_bytes: int) -> bytes:
    """A valid PDF padded to roughly total_bytes via a trailing comment, for size probing."""
    base = _tiny_pdf()
    pad = max(0, total_bytes - len(base) - 2)
    return base + b"\n%" + (b"A" * pad)


PROBES = {
    "auth": probe_auth, "attach": probe_attach, "casing": probe_casing,
    "stop": probe_stop, "thread": probe_thread, "reports": probe_reports,
    "verify": probe_verify, "swagger": probe_swagger, "client": probe_client,
    "resend": probe_resend, "clicktest": probe_clicktest, "unsubscribe": probe_unsubscribe,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="GMass Phase-0 probes (self-send only).")
    parser.add_argument("probe", choices=list(PROBES) + ["all", "guardtest"])
    args = parser.parse_args()

    if args.probe == "guardtest":
        # Dry assertion: guard rejects a non-testmass address, no network.
        try:
            _guard("someone@realcompany.com")
        except RuntimeError as e:
            print(f"OK guard rejected real address: {e}")
            _guard(owner_test_address(1))
            print("OK guard accepted +testmass address")
            return
        sys.exit("FAIL: guard did not reject a real address")

    key = _api_key()
    targets = list(PROBES) if args.probe == "all" else [args.probe]
    for name in targets:
        print(f"\n=== probe: {name} ===")
        PROBES[name](key)


if __name__ == "__main__":
    main()
