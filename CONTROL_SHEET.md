# CONTROL_SHEET.md

Single reference of every knob, toggle, default, confirmation gate, file location, and
Phase-0 probe finding. Updated as each piece is built (§12).

---

## ⇩ Build implications for later phases (read before building the GMass client)

Phase-0 probes contradicted the brief's §3 assumptions in three ways. These are the
authoritative directives — the brief's §3 examples are superseded where they conflict:

1. **Send endpoint = path form.** Step 2 is `POST /api/campaigns/{campaignDraftId}` (id in
   the URL). Do NOT put `campaignDraftId` in the body — that returns 400.
   **Re-verified with a controlled experiment** (two fresh drafts, equal 15s settle delay
   for both — see the retest note below): the body-form 400 **strongly indicates** a genuine
   shape rejection, not a stale-draft timing artifact (residual confound noted below — the
   build directive is safe either way). *(Affects: GMass client, §14 step 6.)*
2. **Attachment = base64 in JSON, not multipart.** `POST /api/campaigndrafts` is a JSON API
   (multipart → 415). Send `attachments: [{fileName, contentType, base64Content}]`. The
   compiled LaTeX / static PDF must be base64-encoded into the JSON body. **Caveat:** the
   API never echoes attachment content back in any response, on a draft OR a real send —
   whether the PDF actually rides on the delivered Gmail message cannot be checked
   programmatically. This is a permanent manual owner check, not a probing gap.
   *(Affects: GMass client + LaTeX loop staging, §14 steps 6 & 8.)*
3. **Stop-on-reply = per-stage `stageOneAction:"r"`** (also `stageTwoAction`,
   `stageThreeAction`, …). There is NO single global stop param. Set `stageNAction:"r"` on
   every configured stage at send time. The API is **write-only** for this field — no read
   endpoint echoes back which action was configured. This is now proven directly from the
   live OpenAPI spec (fetched and saved, not paraphrased — `swagger_20260702T205206Z.json`):
   the `autoFollowup`/`autoFollowupBatch` read models have no `action` property, and the
   `campaign` read model returned by `GET /api/campaigns/{id}` doesn't expose
   `campaignSettings` (where `stageOneAction` actually lives) at all — so a 200 on send is
   the strongest signal obtainable before actual stage-firing behavior can be observed days
   later. *(Affects: GMass client + persona cadence wiring.)*

Other load-bearing observations:

- **Auth:** use the `X-apikey` **header** everywhere (query `?apikey=` also works). Header
  keeps the key out of URL/proxy logs. *(§14 step 6 client, and any request helper.)*
- **`campaignIdToReplyTo` is an integer.** Store GMass campaign ids as integers in the
  `events` log so the OOO re-queue (§7) can pass them back cleanly. *(§14 steps 5 & 10.)*
- **Send fields all live under `campaignSettings`** (camelCase) — full list in the facts
  table below (stageOne…stageEight + `additionalStages`, `sendAsReply`, `emailsPerDay`,
  `throttling`, `skipWeekends`, `fromName`, …). Map persona cadence → `stageNDays`.
- **Reports envelope is `ApiListResponse[T]`** = `{metadata{links,totalRecords,offset,limit,
  count}, data:[…]}`. The dashboard/tracking parser reads `.data`; pagination via
  `offset`/`limit` if `totalRecords` is large. Endpoints are per-campaign:
  `/api/reports/{campaignId}/{replies|clicks|bounces|opens|recipients|unsubscribes}`.
  **Item casing is camelCase** (`{emailAddress, ...}`) — confirmed both by a live, non-empty
  `recipients` item captured by the committed, reproducible `verify` probe
  (`verify_20260702T205332Z.json`, campaign 52126285) and by the authoritative swagger
  `reply`/`click`/`bounce`/`recipient` schemas (`swagger_20260702T205206Z.json`). The
  **`/api/sample/*` endpoints are a separate, inconsistent code path** —
  `/api/sample/bounces` returned legacy PascalCase (`{BounceMessage, EmailAddress,
  CampaignID, TimeStamp}`) and `/api/sample/{replies,clicks}` currently 500. Do not build
  the parser against `/api/sample/*` shapes; use the per-campaign endpoint + swagger schema
  only.
- **Campaign state settles asynchronously.** Right after a real send, `GET
  /api/campaigns/{id}` still reports `status:"scheduled"` and an empty `recipients` report;
  live timing varies run to run — one capture
  (`verify_20260702T205332Z.json`: attempts 1-2 at 15s/30s still empty, attempt 3 at 45s
  non-empty) settled at 45s. Any code that reads a campaign back after sending must poll with backoff
  (not a single immediate read) and budget real margin above the observed ~45s worst case
  seen so far. `autoFollowups` stays `[]` even after the campaign settles to `"sent"` — it
  does not populate until a follow-up stage actually comes due (days out per the configured
  `stageNDays`), so it cannot be used as an immediate send-time confirmation signal.
- **Attachment size:** no rejection up to 25 MB; résumés are <1 MB so this is a non-issue,
  but note base64 inflates the JSON body ~33%.
- **Authoritative schema source:** `https://api.gmass.co/swagger/docs/v1` (OpenAPI), fetched
  and saved live (`swagger_20260702T205206Z.json` — full `definitions` section, not just
  paraphrased). Re-pull it if any field is uncertain in a later phase — do not guess from
  the blog, and don't rely on a prior paraphrase without a saved artifact to back it.
- **Open behavioral unknowns (not yet proven, all timing-dependent — see Tracked
  follow-ups):** (a) that a stage does NOT fire after a real reply; (b) that send-as-reply
  visually threads in Gmail; (c) that `createDrafts:true` suppresses the send; (d) that the
  attached PDF is actually present on the delivered email.

---

## Phase 0 — API-truth probes

Probe CLI: `probes/run.py <auth|attach|casing|stop|thread|reports|verify|swagger|all|guardtest>`.
Raw captures land in `probes/findings/` (timestamped JSON). Run in the venv:
`.venv/bin/python probes/run.py <probe>`.

`verify` is a positive-verification probe (added after an iron-audit pass) that does a
REAL send with a real attachment, then GETs the campaign back and polls the recipients
report with backoff until it's populated — closing the gap where earlier probes treated a
bare HTTP 200 as proof a field registered. It is self-contained and reproducible: running
`python probes/run.py verify` end-to-end is what produced `verify_20260702T205332Z.json`,
the artifact every "live non-empty recipients item" claim below cites.

`swagger` fetches and saves the live OpenAPI spec (`swagger_20260702T205206Z.json`) and
computes, from the actual saved `definitions`, whether `action` is reachable from any read
model — closing a second iron-audit gap where the swagger conclusion was stated as fact
with no fetched/saved artifact behind it.

**Safety guard (baked in, not overridable):** `probes/run.py::_guard()` raises before any
network call unless the recipient matches `everythingforgenius+testmass{N}@gmail.com`.
Verified: `guardtest` rejects a real address and accepts a `+testmass` one. ✅ The guard is
re-checked on the **actual outbound request body** (`_guard_body()`) immediately before
every recipient-carrying `requests.post`/`.get` call, not just once on the function
argument — this closes a latent gap where a body mutated after the initial guard check
(e.g. via a merged `extra` dict) could in principle have carried an unguarded address
through unchecked. Checked fields: `emailAddresses`/`to`/`cc`/`bcc`/`listAddress` — every
recipient-bearing field in the `campaignDraft` swagger model. No call site currently
mutates the body via `extra` in a way that would trigger this, but the boundary is now
enforced at the point of the network call itself, not earlier.

### Authoritative source

The live OpenAPI spec was retrieved from **`https://api.gmass.co/swagger/docs/v1`** and
saved via `python probes/run.py swagger` → `swagger_20260702T205206Z.json` (full
`definitions` section, 39 models — `paths` omitted for size, `definitions` is what every
schema claim in this document rests on). It defines every model below and was cross-checked
against live API calls.

### Facts — VERIFIED against the live API (2026-07-02)

| # | Item | Finding | Live-verified |
|---|------|---------|---------------|
| — | Base URL | `https://api.gmass.co/api/` | ✅ |
| 1 | Auth transport | `apikey` query param **and** `X-apikey` header both return 200 on `GET /api/sheets`. **Standard chosen: `X-apikey` header** (keeps the key out of URL/logs). Must be spelled `apikey`/`X-apikey`. | ✅ all 3 → 200 |
| 2 | Stop-on-reply | **`stageOneAction: "r"`** (per stage; also `stageTwoAction`…`stageEightAction`). Values `r`/`o`/`c`/`s`/`a`. Accepted on live send (campaign 52126285, HTTP 200, real send not a draft — `verify_20260702T205332Z.json`). Read-back attempted: the live, saved swagger spec (`swagger_20260702T205206Z.json`) shows `autoFollowup`/`autoFollowupBatch` have **no `action` field at all**, and the `campaign` read model returned by `GET /api/campaigns/{id}` doesn't expose `campaignSettings` either — there is no read endpoint, direct or indirect, that echoes a configured stage action back. A 200 is genuinely the strongest signal obtainable before a stage is actually due to fire. | ✅ param accepted on a real send; ✅ swagger fetched live and confirms no read path exists; ⏳ behavioral firing-suppression proof is inherently timing-dependent (days out) — no stronger immediate proof exists (see Tracked follow-ups) |
| 3 | Attachment | **JSON body**, `attachments`: array of `campaignDraftAttachment` = `{fileName, contentType, base64Content}`. Multipart → **415**; simple base64 field → 400. Sizes **1/5/10/20/25 MB all accepted (200)**. Real send with real attachment completed (campaign 52126285, `verify_20260702T205332Z.json`). | ✅ mechanism accepted incl. on a real send; ⏳ delivered-attachment presence is unobservable via the API — permanent manual owner check |
| 4 | Send endpoint + casing | **`POST /api/campaigns/{campaignDraftId}`** (id in URL path). Body-field form (`{campaignDraftId: …}`) → **400**, **re-confirmed with a controlled experiment** eliminating the original draft-freshness confound: two fresh drafts, equal 15s settle delay for both, path→200/body→400 (`casing_20260702T203823Z.json`). All send fields live under `campaignSettings` (camelCase): `openTracking`, `clickTracking`, `createDrafts`, `stageOneDays`, `stageOneCampaignText`, `stageOneCampaignId`, `stageOneAction`, `stageOneTime`, `stageOneThread` … through `stageEight*`, plus `additionalStages` (array of `extraStage`), `sendAsReply`, `campaignIdToReplyTo`, `fromName`, `emailsPerDay`, `throttling`, `skipWeekends`, etc. — full field list cross-checked against `swagger_20260702T205206Z.json`'s `campaignSettings` definition. | ✅ path→200, body→400, tested under controlled equal-settle-time conditions (n=1 per shape; see residual confound note below) |
| 5 | Threading | `sendAsReply` (bool) + **`campaignIdToReplyTo` (integer)**. Live: initial send (campaign 52120430, 200) then send-as-reply into it (200). | ✅ API-accepted; Gmail visual thread check = owner |
| 6 | Reports | `GET /api/reports/{campaignId}/{replies\|clicks\|bounces\|opens\|recipients\|unsubscribes\|blocks}`. Envelope `ApiListResponse[T]` = `{metadata:{links,totalRecords,offset,limit,count}, data:[…]}`. Item schemas per the authoritative swagger definitions (`swagger_20260702T205206Z.json`) — recipient: `{emailAddress, gmailResponseText, sentTime, sender}`; reply: `{emailAddress, replyId, alreadyReplied, replyTime, sender}`; click: `{emailAddress, url, clickTime, sender}`; bounce: `{emailAddress, bounceReason, bounceTime, sender}` — all camelCase. **Live non-empty item captured** by the committed, reproducible `verify` probe: a real `recipients` report item matching the swagger shape exactly (`verify_20260702T205332Z.json`, campaign 52126285, item arrived on poll attempt 3 of 8, ~45s after send). `/api/sample/{replies\|clicks\|bounces}` are a **separate, inconsistent legacy path** — `/api/sample/bounces` returned PascalCase, `/api/sample/{replies,clicks}` currently 500 — do not build the parser against `/api/sample/*`. replies/clicks/bounces item shapes themselves remain schema-verified-but-`data:[]`-live (no real reply/click/bounce has occurred against a test address) — expected, not a gap: those need real recipient action to populate. | ✅ recipients item live-verified non-empty + matches swagger, reproducible from committed code; reply/click/bounce shapes swagger-verified, live data pending real recipient action (not obtainable safely under the test-only guard) |

### ⚠️ DEVIATION FROM BRIEF §3 — stop-on-reply (probe #2, most load-bearing)

The brief's §3 field list (`stageOneDays`/`stageOneCampaignText`) does **not** include the
stop-behavior parameter. The live docs show it as a **per-stage action** field:

- Param: **`stageOneAction`** (and `stageTwoAction`, `stageThreeAction`, …)
- Values: `r` = If No Reply · `o` = If No Open · `c` = If No Click · `s` = If No Reply or Click · `a` = Everyone
- **Stop-on-reply = `stageNAction: "r"`** on every stage.

This means the app must set `stageNAction: "r"` per stage (not a single global stop param).
Confirmed accepted on a **real send** (not just a draft): campaign 52126285,
`verify_20260702T205332Z.json`. **Downstream build must use `stageNAction`, not a single
stop flag.**

> Note: a bare HTTP 200 on send is not, by itself, strong proof a field was *recognized*
> (a .NET JSON API can silently drop unrecognized properties and still return 200). We
> checked for a stronger signal: `GET /api/campaigns/{id}` read-back and the authoritative
> swagger schema, fetched and saved live (not paraphrased) via `python probes/run.py
> swagger` → `swagger_20260702T205206Z.json` — the `autoFollowup`/`autoFollowupBatch` models
> have **no `action` property**, and the `campaign` read model itself has no
> `campaignSettings` field at all, so the API has no read endpoint, direct or indirect,
> capable of confirming which action value registered — even after the campaign settles to
> `status:"sent"` (confirmed live: `autoFollowups` stayed `[]` at both the send-time and the
> settled read-back of campaign 52126285). Behavioral proof that a stage does *not* fire
> after an actual reply is therefore genuinely timing-dependent (days out) and remains a
> tracked follow-up — this is a real, spec-confirmed API limitation, not an unexamined
> assumption.

### ⚠️ DEVIATION FROM BRIEF §3 — send endpoint shape (probe #4) — CONFIRMED (re-verified)

Brief §3 (lines 101–102) describes step 2 as `POST /api/campaigns` with `campaignDraftId`
as a **body field**. **Live result: that form returns HTTP 400.** The working form is
**`POST /api/campaigns/{campaignDraftId}`** with the id in the URL path (HTTP 200).

The first run of this probe was confounded: the path-form call reused an older, already-
settled draft while the body-form call used a freshly-created draft with zero delay, and
the 400 body was GMass's generic message *"the message in your Compose hasn't been saved
properly into your Gmail account yet. Try waiting a few more seconds..."* — text that reads
like a timing issue, not a shape rejection. **Re-tested with the confound reduced:** two
fresh drafts created back-to-back, both given an equal 15s settle delay, then path-form on
one and body-form on the other (`casing_20260702T203823Z.json`). Result: path→200 (real
campaign created, `campaignId: 52125973`), body→**400 with the identical generic message**,
despite equal settle time. This **strongly indicates** the 400 is a genuine endpoint-shape
rejection rather than a draft-settle artifact. **Residual limitation:** the experiment is
n=1 per shape with one shared wall-clock delay, not an independently-verified per-draft
settle state (e.g. no cross-test of path-form against the same draft that got the body-form
400) — so a per-draft settle explanation isn't fully excluded, just made unlikely. This
doesn't change the build directive: **downstream build MUST use the path form** regardless.
Client retry logic should still treat this exact 400 message pattern as retryable-transient
in general (GMass does appear to reuse it for real timing issues too, per the docs' own
wording) but must never fall back to the body-form shape.

### ⚠️ DEVIATION FROM BRIEF §3 — attachment encoding (probe #3) — RESOLVED

Brief §3 left the attachment encoding open ("multipart vs base64"). **Live result:**
`/api/campaigndrafts` is a JSON API — **multipart is rejected (415)**. Attachments must be
a JSON array of `campaignDraftAttachment` objects: **`{fileName, contentType,
base64Content}`**. So the LaTeX/static PDF is base64-encoded into the JSON body, not
uploaded as a file part. No size rejection observed up to 25 MB. A real send with a real
attachment was completed (campaign 52126285, `verify_20260702T205332Z.json`) — but no API
response, at draft-creation or after a real send, ever echoes attachment content back
(`attachments: []` in every draft response, including the real send). **Whether the PDF is
actually present on the delivered Gmail message cannot be verified programmatically — this
is a permanent manual owner check**, tracked below, not a probing gap to close later.

### Minor: `campaignIdToReplyTo` is an **integer** (spec), so store GMass campaign ids as
integers in the event log for the OOO re-queue path (§7).

### Live probe findings — RESOLVED (raw captures in `probes/findings/`)

- [x] `auth` — all 3 transports → 200; chose `X-apikey` header.
- [x] `attach` — JSON `{fileName,contentType,base64Content}`; multipart→415; 1–25 MB accepted.
- [x] `casing` — **re-run with a controlled equal-settle-time experiment**: path send form →
  200, body form → 400, on two equally-fresh drafts (`casing_20260702T203823Z.json`); full
  `campaignSettings` field list confirmed via swagger.
- [x] `stop` — `stageOneAction:"r"` accepted on a **real send** (campaign 52126285, 200,
  `verify_20260702T205332Z.json`); read-back confirmed via the live-fetched swagger spec
  (`swagger_20260702T205206Z.json`) that the API genuinely has no field to echo the action
  value back — real API limit, not a probing gap.
- [x] `thread` — `sendAsReply`+`campaignIdToReplyTo` (int) both send 200 (52120430 + reply).
- [x] `reports` — per-campaign paths + `ApiListResponse[T]` envelope confirmed; item schemas
  confirmed against the authoritative, live-fetched swagger definitions
  (`swagger_20260702T205206Z.json`); a **live non-empty `recipients` item** captured by the
  committed `verify` probe and matches the swagger shape exactly
  (`verify_20260702T205332Z.json`, campaign 52126285).
- [x] `verify` — real send + real attachment + campaign read-back + recipients-report poll,
  all in one reproducible probe (`probes/run.py verify`). Reproducibly captures a non-empty
  recipients item end-to-end (confirmed by re-running it: `verify_20260702T205332Z.json`).
  Closes the "was this actually verified or just accepted" gap for stop-on-reply,
  attachment, and reports.
- [x] `swagger` (new) — fetches and saves the live OpenAPI spec, then computes from the
  actual saved `definitions` whether `action` is reachable from any read model
  (`swagger_20260702T205206Z.json`). Every swagger-sourced claim in this document now
  traces to a saved, committed-probe-produced artifact instead of an unfetched assertion.

### Tracked follow-ups (genuinely cannot complete without days-out timing or the owner's Gmail)

- **Behavioral stop-on-reply proof (probe #2):** confirm a stage does *not* fire after an
  actual reply. Timing-dependent (days out) — and now confirmed the API has **no read
  endpoint** that could shortcut this; live behavior on the actual send date is the only
  proof that will ever exist. Parameter-acceptance is verified; behavior is not yet.
- **Threading visual check (probe #5):** owner confirms in Gmail that the send-as-reply to
  `+testmass3` nests in the original conversation (API accepted it; visual not yet checked).
- **`createDrafts:true` suppresses send** — assumed in the casing probe. If wrong it only
  ever hit the guarded `+testmass1`, so no real-lead risk; worth confirming no email fired.
- **Attachment delivery check (new, closes S3 from the iron-audit):** owner confirms the PDF
  is actually present and openable on the delivered `+testmass5` email (campaign 52126285,
  subject "slap probe verify (real send + attachment)"). The API attaches
  without ever echoing attachment content back in any response — this cannot be checked
  programmatically and is a permanent manual check for every real send, not just this probe.

---

## Configuration knobs (from brief — populated as built)

- Personas / cadences (fixed): `hiring_manager [2,4,6]`, `recruiter [2,3,5]`, `founder [2,5,7]`.
- Schedule: `fire_window_start/end` 09:00–09:15, `send_delay_min/max` 10–15s, `daily_cap` 500, `drain_retries` 3.
- Tracking: `consumer_domains_file` = `consumer_domains.txt`.
- **The one hard gate:** résumé > 1 page (§9) — forces fix or explicit "send N pages anyway".
- Secrets: `GMASS_API_KEY` in `.env` (gitignored); `.env.example` shipped.

---

## Project skeleton (Build Order step 2)

- `slap.py` (repo root) — CLI entrypoint. `argparse` subparsers for the six §11 commands
  (`list`, `send <campaign> [--now]`, `dashboard`, `doctor`, `domains`, `rebuild`). Every
  handler is currently a fail-loud stub (`sys.exit("... not yet implemented — Build Order
  step N ...")`) — no partial logic anywhere, including `doctor`, which stays a pure stub
  until its full step-12 preflight spec is built (a half-built doctor would be misleading).
  `send`'s real argument shape (`campaign` positional + `--now` flag) is wired now since
  that's the CLI-surface shape itself, not future logic.
- `requirements.txt` — `requests==2.34.2`, `python-dotenv==1.2.2` (pinned to what Phase-0
  probes already ran against), `PyYAML==6.0.2` (needed from step 3), `Flask==3.1.0` (the
  step-11 dashboard's web framework — chosen over FastAPI+uvicorn: §8's dashboard is
  server-rendered panels + an on-open poll + one write action, no async/SPA need).
- `requirements-dev.txt` — `-r requirements.txt` + `pytest==8.3.4`. Kept separate so
  `requirements.txt` matches §11's explicit runtime list exactly.
- `consumer_domains.txt` (repo root) — seeded per §6, 11 domains, one per line, no
  comments: `gmail.com outlook.com yahoo.com icloud.com proton.me protonmail.com
  hotmail.com aol.com gmx.com live.com msn.com`.
- `tests/test_cli_skeleton.py` — smoke test scoped only to this step: `--help` lists all
  six subcommands; each no-arg command fails loud with "not yet implemented"; `send` with
  no campaign hits argparse's own usage error (exit 2, distinct from the stub path); `send
  <campaign>` hits the stub. Runs via the existing `.claude/settings.json` PostToolUse
  `pytest -q` hook, which was previously a permanent no-op.

### Package layout (target — files created only when their Build Order step lands)

No `slap/` package exists yet — there's nothing to extract into modules until step 3's
config loader. Rather than pre-creating empty stub files (half-finished-implementation
territory), the target shape is recorded here once so later steps have a settled
destination:

| File | Created at step | Owns |
|---|---|---|
| `slap.py` | 2 (done) | CLI entrypoint, argparse dispatch |
| `slap/__init__.py` | 3 | created alongside the first submodule |
| `slap/config.py` | 3 | config.yaml + campaign.yaml loading, auto-discovery, fail-loud validation |
| `slap/templates.py` | 4 | drop parser (first-colon partition) + `{{key}}` fill |
| `slap/tracking.py` | 5 | SQLite `events`/`recipients` schema, append, `rebuild` |
| `slap/gmass.py` | 6 | GMass client (campaigndrafts → campaigns, reports polling) |
| `slap/domains.py` | 7 | domain/recipient dedup logic backing `domains` |
| `slap/latex.py` | 8 | xelatex compile loop, >1-page hard gate |
| `slap/runner.py` | 9 & 10 | queue drain (launchd + `send --now`) and OOO send-as-reply |
| `slap/dashboard.py` (+ templates) | 11 | Flask app for the localhost dashboard |
| `slap/doctor.py` | 12 | preflight checks wired into send + drain |

At step 3, `slap.py`'s stub bodies switch to real `from slap.config import ...` calls one
at a time as each module lands — the dispatch shape itself doesn't change.
