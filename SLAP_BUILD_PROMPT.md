# Build Brief — `slap.py`: a personal cold-outreach engine over the GMass API

You are building a fresh Python CLI application from scratch. There is **no existing
codebase to migrate** — ignore any prior tool. Read this entire brief before writing
code. Then follow the **Build Order** at the bottom.

---

## 0. Design philosophy — read this first, it governs every decision

The owner calls the target quality **"iron"**: a solid, single-piece thing with no
loose parts. Concretely, that means:

- **One source of truth.** Never maintain the same fact in two places that can drift.
  Derived data is *derived* (regenerable), never independently hand-maintained.
- **Append-only where possible.** The event log is never mutated or deleted, only
  appended to. Any cache is rebuildable by replaying it.
- **Fail loud, never silent.** Missing config, missing files, bad state → stop with a
  clear message. Never guess or paper over.
- **Warn, don't block — except one hard gate** (the >1-page résumé gate, below).
  Everything else that's risky prompts the human and proceeds on explicit confirm.
- **Idempotent, one-recipient blast radius.** A failure can never affect more than one
  recipient and never double-sends.
- **Check, don't install.** System dependencies are verified, never auto-installed.

When a design choice is already determined by these principles or by a spec below,
**do it — don't ask.** Only ask the human when there is a genuine, unresolved fork.

---

## 1. What the app does (mission)

The owner runs personalized cold job-outreach. For each recipient they paste a "drop"
(line-by-line `key : value` data) and, for some campaigns, paste a LaTeX résumé that
gets compiled and attached. The app fills an email template from the drop, attaches the
résumé, and sends via **GMass** (which sends through the owner's Gmail and handles
tracking + automatic follow-ups). Everything sent is tracked locally; a local dashboard
shows status and lets the owner tag replies.

Key properties:
- Sending is **queued at prep time and fired later** by an unattended scheduled runner.
- **Follow-ups are GMass's job** (owner has Premium): we set the stage cadence at send
  time and GMass fires stages 1–3 on its own servers, stopping on reply. We build **no**
  follow-up scheduler.
- The owner is on **free Gmail** (~500 sends/day ceiling, follow-ups count toward it).

---

## 2. Phase 0 — API-truth probes (RUN AND RESOLVE BEFORE BUILDING ANYTHING ELSE)

The GMass API details below come from GMass's blog/docs and **must be verified against
the live API before any dependent code is written.** Build a small, isolated probe
script (`probes/`) that confirms each item and **records the real responses** into the
Control Sheet (§12). If any probe contradicts an assumption here, stop and surface it —
do not build on the guess.

**Hard safety rule, baked into probe code (not a config knob, not overridable):**
every probe that sends may send **only** to the owner's own inbox via Gmail plus-tags —
`everythingforgenius+testmass{N}@gmail.com` — or create drafts without sending. Any
other `to` value must raise before any network call. This makes emailing a real lead
with test data *impossible*, not merely discouraged.

Probes to resolve:
1. **Auth transport** — does GMass accept the API key as an `?apikey=` query param, a
   header, or both? Standardize on the most robust. (Test against `GET /api/sheets`.)
2. **Follow-up stop-behavior parameter** — the EXACT param name + value on the campaign
   send call that makes stages stop **on reply** (vs open / click / all). This is the
   single most load-bearing unknown; the whole follow-up model depends on it.
3. **Attachment mechanism on `/api/campaigndrafts`** — exact field name, encoding
   (multipart vs base64), and size limit.
4. **Exact parameter casing** for the send call (blog shows inconsistent casing, e.g.
   `stageOneDays` vs `UpdateSheet`). Confirm each field's real casing.
5. **`sendAsReply` + `campaignIdToReplyTo` threading** — confirm a single-recipient
   send-as-reply actually threads into a prior campaign's conversation (drives OOO
   re-queue). Test by sending an initial to `+testmass1`, then a send-as-reply to it.
6. **Reports endpoint shapes** — the JSON structure returned for a campaign's replies,
   clicks, and bounces (the dashboard + tracking parse these).

Official docs to consult (fetch live, don't trust memory):
`https://api.gmass.co/docs`, `https://www.gmass.co/blog/gmass-api/`,
`https://www.gmass.co/blog/api-create-send-campaign/`.

---

## 3. GMass API contract (as designed — verify via Phase 0)

Base URL `https://api.gmass.co/api/`. API key from env var `GMASS_API_KEY` (never in a
file). Requires the owner's Premium plan.

**Send path — two calls per recipient, always in this order:**

1. `POST /api/campaigndrafts` → creates the Gmail draft; **carries the attachment**.
   - `emailAddresses`: the single recipient
   - `subject`: filled subject (from `initial.txt`, see §4)
   - `message`: filled initial body
   - `messageType`: plain text (threaded follow-ups require plain text)
   - `fromEmail` / `fromName`: from `config.yaml`
   - `attachments`: the staged PDF — always present (see §4 attachment rule)
   - → returns a **draft ID**

2. `POST /api/campaigns` → sends it and sets the follow-up sequence.
   - `campaignDraftId`: the ID from step 1 (only strictly-required field)
   - `stageOneDays`/`stageOneCampaignText`, `stageTwoDays`/`stageTwoCampaignText`,
     `stageThreeDays`/`stageThreeCampaignText`: from the persona cadence + `stageN.txt`
   - stop-behavior: **stop on reply** (exact param per Phase-0 probe #2)
   - `openTracking: false`, `clickTracking: true`
   - `createDrafts: false`

**OOO re-queue path** (see §7) uses a separate send: `sendAsReply: true` +
`campaignIdToReplyTo: <stored original campaign ID>` to that one recipient, firing the
next stage's body threaded into the original conversation. Deterministic threading —
never rely on "last conversation" auto-detection.

**Reports polling** (dashboard, §8): poll each campaign's replies/clicks/bounces on
demand. We do **not** use webhooks. The transactional endpoint is **not used** (no
attachments / no follow-ups on it).

**Idempotency:** record the draft ID in the event log the instant step 1 returns,
*before* step 2 fires. If step 2 fails, the draft exists and the send is retryable — no
orphan, no double-create.

---

## 4. Config schema

Fresh `config.yaml` at project root + a `campaigns/` directory. **Campaigns are
auto-discovered**: any folder under `campaigns/` containing a valid `campaign.yaml` is
live. No central registry.

```
config.yaml
campaigns/
  coldpost-recruiter/
    campaign.yaml
    initial.txt          # Subject: line + blank line + body
    stage1.txt           # follow-up bodies (no subject; they thread as replies)
    stage2.txt
    stage3.txt
    resume.pdf           # only when latex disabled: the static attachment
  coldpost-founder/
    ...
```

**`config.yaml` (global only):**
```yaml
sender:
  from_email: everythingforgenius@gmail.com
  from_name: <Owner Name>

gmass:
  api_key_env: GMASS_API_KEY        # read from env; never store the key here

personas:                           # cadences are FIXED per persona
  hiring_manager: { stages: [2, 4, 6] }
  recruiter:      { stages: [2, 3, 5] }
  founder:        { stages: [2, 5, 7] }

schedule:
  fire_window_start: "09:00"        # runner rolls a random fire moment in this window
  fire_window_end:   "09:15"
  send_delay_min: 10                # seconds between emails during a drain
  send_delay_max: 15
  daily_cap: 500                    # Gmail ceiling; drain stops here, overflow re-queues
  drain_retries: 3                  # transient-failure retries before surfacing run_failed

tracking:
  consumer_domains_file: consumer_domains.txt
```

**`campaign.yaml` (per campaign):**
```yaml
persona: recruiter                  # -> derives cadence [2,3,5]
latex:
  enabled: true                     # false -> use the static attachment_file
  attachment_name: "Firstname_Lastname_Resume.pdf"   # name the RECIPIENT sees
attachment_file: resume.pdf         # file in the folder; used when latex.enabled = false
fields:
  - { key: email,        label: Email }
  - { key: role_catted,  label: Role }
  - { key: company,      label: Company }
  - { key: req_id,       label: Req ID, optional: true }
  - { key: experience_1, label: Experience 1 }
  - { key: experience_2, label: Experience 2 }
  - { key: byebye,       label: Signoff }
```

**Body files** use `{{key}}` placeholders. `initial.txt` first line is
`Subject: ...`, then a blank line, then the body:
```
Subject: Quick note about the {{role_catted}} role at {{company}}

Hi {{company}} team,
...
{{byebye}}
```

**Loader rules (fail loud):**
- `initial.txt` must have a `Subject:` first line + blank-line separator, else error.
- The number of `stageN.txt` files must match the persona's cadence length, else error.
- `{{key}}` fill is done **locally** (not GMass merge). A field marked `optional: true`
  that is empty **drops its whole line** cleanly (this replaces the old GMass
  leading-space fallback trick — no more whitespace hacks).
- **Attachment rule (both paths converge):** latex on → compile the pasted résumé;
  latex off → use `attachment_file` from the folder. Either way: take the resulting PDF,
  rename to `attachment_name`, attach. Preflight fails loud if the required source is
  absent.

**Drop input parser** — preserve this exact behavior from the owner's prior tool:
- split each line on the **first** colon only (`line.partition(":")`), so values
  containing colons (e.g. `Req ID: 6900`) parse correctly;
- strip exactly one separator space after the colon, preserve the rest;
- ignore lines without a colon; unknown keys ignored; missing keys default to empty.
- **Paste-only.** No interactive field-by-field entry.

---

## 5. Tracking store (SQLite, event-sourced)

One SQLite file. Two tables. The events table is truth; recipients is a derived cache.

**`events` — append-only, never updated or deleted:**
```
id                INTEGER PRIMARY KEY
timestamp         TEXT      -- ISO 8601, UTC
recipient         TEXT
campaign          TEXT      -- config folder name
type              TEXT      -- queued|sent|click|reply|bounce|ooo_tagged|requeued|
                            --   run_started|run_completed|send_failed|run_failed
stage             INTEGER   -- 0 = initial, 1..3 = follow-up; NULL where N/A
gmass_campaign_id TEXT
gmass_draft_id    TEXT
meta              TEXT      -- JSON blob for type-specific data (bounce reason, click url,
                            --   error message, rolled fire-time, counts, retry count)
```

**`recipients` — derived cache, fast current-state, rebuildable from events:**
```
recipient              TEXT PRIMARY KEY
campaign               TEXT
persona                TEXT
status                 TEXT   -- active|replied|ooo_requeued|bounced|done
current_stage          INTEGER
last_gmass_campaign_id  TEXT
first_sent_at          TEXT
last_event_at          TEXT
replied_at             TEXT
```

- All timestamps **UTC**; convert to local only at dashboard display (fixes the
  "sent today" day-boundary ambiguity).
- `rebuild` command regenerates `recipients` entirely by replaying `events`. This is the
  crash-recovery guarantee — prove it in tests (a rebuilt cache must equal the live one).

---

## 6. Domain / recipient dedup (pre-send check)

Derived **live from `events`** — no parallel store. On each send, extract the recipient
and its domain (`email.split('@')[1]`) and check history:
- **Exact recipient already contacted** → **hard warn** (always, even consumer domains),
  showing rich context from events: when, which campaign, what role, whether they replied.
- **Same non-consumer domain, different person** → **soft warn**, same context style.
- Both **warn, never block** — prompt with explicit confirm to proceed.

**Consumer-domain exclusion** is mandatory for the domain-level (soft) warn, or it
false-warns on nearly everyone. Ship an editable `consumer_domains.txt` seeded with
gmail.com, outlook.com, yahoo.com, icloud.com, proton.me/protonmail.com, hotmail.com,
aol.com, gmx.com, live.com, msn.com. Exact-recipient (hard) warn fires regardless of
domain.

Provide a `domains` command that **regenerates** a human-readable domain index from
`events` (for the owner's DIY inspection). It is read-only output, never hand-edited,
never a source of truth.

> Note for tests: the owner's own test addresses are all `@gmail.com` (a consumer
> domain), so the soft domain-warn must correctly *skip* them while the hard
> exact-recipient warn still fires on a repeated `+testmassN`.

---

## 7. OOO re-queue

GMass usually does **not** count an out-of-office auto-reply as a reply (they arrive on
a separate thread; GMass filters auto-responders), so this is a safety net for rare
false positives, not a common path. There is **no** GMass "re-enroll" API. So:

- The dashboard replies section lets the owner tag a reply **real / OOO / not-interested**.
- Tagging **OOO** writes `ooo_tagged`, moves the recipient into a re-queue holder, and
  the app sends that recipient's **next stage itself** as a `sendAsReply: true` +
  `campaignIdToReplyTo` campaign, on the **normal routine** (no special date parsing, no
  per-recipient scheduling — fire on the same runner cadence). Writes `requeued`.
- Conflict-free: GMass has already stopped its own follow-ups for that person (it thinks
  they replied), so app-owned resume for that recipient cannot double-send.

---

## 8. Dashboard (local web page on localhost)

Renders as a small **localhost web page** with clickable reply-tagging. Reads SQLite.
**Polls GMass reports on open** (writes new click/reply/bounce events), then renders.
Show a **"last synced"** timestamp so staleness is visible. Read-only except the single
write action (reply tag → may trigger OOO re-queue).

Panels:
- **Today strip:** active campaign(s); emails sent today (new / follow-up / total);
  **daily-limit gauge** (total today vs `daily_cap`, *including* follow-ups firing today);
  replies today; clicks today.
- **This week:** sent (new/follow-up/total), replies, clicks.
- **Engagement intelligence** (aggregated from events): reply rate by persona;
  reply-by-stage (initial/1/2/3); click-by-stage; time-to-first-reply distribution.
- **Replies section (actionable):** list of received replies with the real/OOO/
  not-interested tagging control; OOO → re-queue; each row shows prior-contact context
  from the domain history.
- **Pipeline:** recipients mid-sequence by stage; follow-ups scheduled to fire
  today/tomorrow.
- **Today's runs:** for each drain — rolled fire-time, count sent, count still queued,
  count failed; `run_failed` shown prominently with reason + retry count;
  per-email `send_failed` rows listed as still-queued-for-retry; current queue depth.

---

## 9. LaTeX loop (latex-enabled campaigns, after the drop paste)

The owner's local xelatex setup is known-good. The **app owns compilation**
(deterministic, gives page count + a guaranteed-correct attachment); `code` and macOS
Preview are just human surfaces; the LaTeX Workshop extension is optional gravy.

1. **LaTeX paste mode** — owner pastes the `.tex`; app writes it to a per-recipient
   workdir, e.g. `workdir/<campaign>/<recipient>/resume.tex`.
2. App runs `xelatex` (twice, or `latexmk -xelatex`), opens the PDF in macOS Preview
   (auto-refreshes), and opens `code resume.tex`.
3. Terminal loop: `[r]ecompile · [o]pen editor · [d]one · [a]bort`. Edit in VSCode →
   save → `r` → Preview refreshes. Repeat.
4. On **done**: authoritative compile, then check page count. **HARD GATE: if > 1 page,
   force a decision** — owner must either `r` (fix) or explicitly confirm "send N pages
   anyway." No silent pass-through. This is the one non-overridable gate in the app.
5. Rename the final PDF to `attachment_name`, stage it for the queued send.
6. **abort** cleans the workdir; no half-campaign.

Namespace workdirs per recipient so the shared `attachment_name` doesn't overwrite
across recipients. Tie the staged PDF to a hash of the accepted `.tex`; never attach a
stale or broken PDF.

---

## 10. Sending model — queue + unattended runner

Split **prep (interactive, needs the human)** from **fire (unattended)**.

- **Prep** (`slap.py send <campaign>`): drop paste → (latex loop if enabled, else stage
  static attachment) → domain/recipient check → preview (filled email + attachment name +
  cadence about to be set) → confirm → **stage into the queue** (`queued` event + staged
  files). It does **not** send. Loops "Add another? [Y/n]".
- **Fire** (`runner`, triggered by macOS **launchd**): drains the queue for items due
  today. For each: `campaigndrafts` → `campaigns`, write events, move on.
  - **Random fire moment** inside `09:00–09:15` each day (not a fixed instant).
  - **10–15s random gap** between sends.
  - **Cap-aware:** send up to `daily_cap` headroom (counting follow-ups firing today);
    leave overflow queued for the next day.
  - **Resilience:** transient failure → retry up to `drain_retries` → on give-up write
    `run_failed` (surfaced on dashboard), leave the queue intact. Per-email failure →
    `send_failed`, item stays queued. Nothing is ever lost.
- **launchd wake-catch-up:** use a `StartCalendarInterval` LaunchAgent so that if the
  Mac was asleep at the scheduled time, the job runs **on wake**. Generate a correct
  `.plist` and install instructions. (Plain cron does NOT catch up on wake — do not use
  it.) This behavior can't be unit-tested normally, so also produce a **one-time manual
  test checklist** (set window ~2 min out, sleep the lid, wake, confirm it fired).
- **Manual override:** `slap.py send --now` (or a `flush` command) drains immediately,
  ignoring the window.

The queue is **just more events** (`queued` → `sent`); the runner is **stateless** (asks
the DB "what's queued and due?"). No new store.

---

## 11. CLI surface (`slap.py`) + secrets + preflight

**Commands:**
- `list` — auto-discovered campaigns (persona + latex on/off)
- `send <campaign> [--now]` — the prep flow (stages to queue; `--now` also drains)
- `dashboard` — launch the localhost web page
- `doctor` — preflight checks (also auto-runs before any send and any drain)
- `domains` — regenerate/print the readable domain index from events
- `rebuild` — rebuild the `recipients` cache by replaying events

**Secrets / environment:**
- `.env` holds `GMASS_API_KEY`; loaded via python-dotenv. `.env` in `.gitignore` from the
  first commit. Ship `.env.example` with key names and no values.
- No Google/Sheets credentials anywhere (that dependency is gone).
- Pin Python deps in `requirements.txt` (requests, a web framework for the localhost
  dashboard, python-dotenv, PyYAML, etc. — keep it minimal).

**Preflight (`doctor`) — fail loud, check-don't-install:**
- `GMASS_API_KEY` present and non-empty
- `config.yaml` valid; `sender.from_email`/`from_name` set
- target `campaign.yaml` valid; stage files match cadence count; `initial.txt` well-formed
- attachment source resolvable (latex on → `xelatex` + `code` on PATH; latex off →
  `attachment_file` exists)
- SQLite DB reachable / initializable
- `consumer_domains.txt` present (seed default if missing)
- At **drain** time, a preflight failure → retry per `drain_retries`, then `run_failed`
  on the dashboard; queue stays intact.

---

## 12. The Control Sheet (a standing deliverable)

Maintain `CONTROL_SHEET.md` in the repo: a single documented reference of **every knob,
toggle, default, confirmation gate, file location, and Phase-0 probe finding** in the
app — what each does, where it lives, its default. Update it as each piece is built. It
is the map so the owner never has to grep source to find where a behavior is set. Seed it
from this brief (schedule knobs, tracking defaults, the one hard gate, the send-behavior
params, file locations, etc.) and record the resolved Phase-0 API facts into it.

---

## 13. Tests

**A. Phase-0 API-truth probes** (§2) — run first, self-send-only, record findings.

**B. Per-layer verification:**
- Config loader: valid load; missing stage file for a defined cadence → fails loud;
  `initial.txt` without Subject/blank-line → fails loud; auto-discovery finds folders.
- Drop parser: first-colon split; one-space strip; `optional` empty → line dropped; the
  `Req ID: 6900` colon-in-value case.
- Template fill: `{{key}}` substitution; optional-empty drops line; subject peeled cleanly.
- LaTeX loop: compiles a sample; **>1-page triggers the hard gate**; abort cleans workdir;
  missing xelatex/code caught by preflight; rename-to-`attachment_name`; latex-off path
  attaches `attachment_file`.
- Domain check: exact-recipient hard warn; non-consumer soft warn; gmail correctly
  skipped for soft warn; warning shows prior-contact context.
- Tracking: events append-only; cache updates on event; **`rebuild` reproduces the cache
  identically** from the log.
- Idempotency: draft ID recorded before send; simulated failure-after-draft → retry does
  not double-create.
- Queue + runner: `send` stages without firing; runner drains; **random fire-time lands
  in 09:00–09:15**; 10–15s gap enforced; **cap-aware** leaves overflow queued;
  `--now` flushes.
- Drain resilience: transient failure → retries → `run_failed` written, queue intact,
  surfaced on dashboard.
- Dashboard: on-open poll writes events; all panels render; reply-tag → OOO → re-queue
  fires a send-as-reply.

**C. launchd wake-catch-up** — verify the `.plist` is correct + produce the one-time
manual test checklist.

---

## 14. Build order

1. **Phase-0 probes** (self-send-only). Resolve all six. Record findings in the Control
   Sheet. **Do not proceed on any unverified assumption.**
2. Project skeleton: `slap.py`, package layout, `requirements.txt`, `.env.example`,
   `.gitignore`, `CONTROL_SHEET.md`, seed `consumer_domains.txt`.
3. Config loader + auto-discovery + validation (fail-loud).
4. Drop parser + template fill (with `optional` line-drop + subject peel).
5. Tracking store: schema, event append, cache update, `rebuild`. Tests.
6. GMass client: campaigndrafts → campaigns (idempotent), reports polling. Wire to
   verified Phase-0 params. Tests against self-send addresses.
7. Domain/recipient dedup + `domains` command. Tests.
8. LaTeX loop (app-owned compile, hard >1-page gate). Tests.
9. Queue + runner + launchd LaunchAgent + `--now`. Tests + manual checklist.
10. OOO re-queue (send-as-reply path).
11. Dashboard (localhost, on-open poll, panels, reply-tagging).
12. `doctor` preflight wiring into send + drain.
13. Final pass: Control Sheet complete; README with setup + the launchd manual test.

Ask the owner only where a genuine fork remains. Otherwise, build to this brief.
