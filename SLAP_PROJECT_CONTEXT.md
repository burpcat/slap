# SLAP — Project Context for a Fresh Claude

*Onboarding brief. Read this first when starting on SLAP in a new session. It captures
the mission, architecture, every locked decision, the real (probe-verified) GMass API
facts, current build state, known landmines, and the working rhythm — so you can be
useful without re-deriving any of it.*

*Snapshot date: 2026-07-09. Build state moves; treat "Current state" (§8) as of this
date and confirm against `CONTROL_SHEET.md` in the repo, which is the live source of
truth. This revision was produced by re-reading the actual code, schema, `campaign.yaml`
files, and `CONTROL_SHEET.md` end to end — not by rewriting the prior draft from memory —
specifically to close several gaps that caused real confusion in a prior design session
(see §11, "Known open questions," for what's still genuinely unresolved).*

---

## 1. What SLAP is (one paragraph)

`slap.py` is a personal cold-outreach CLI for a job search. The owner pastes a "drop"
(line-by-line `key: value` data) per recipient; for some campaigns they also paste a
LaTeX résumé that gets compiled and attached per-recipient. SLAP fills an email template
from the drop, attaches the résumé, and **queues** the send. A scheduled runner fires the
queue each morning via the **GMass API** (which sends through the owner's Gmail and
handles click tracking + automatic follow-ups). Everything sent is tracked locally in
SQLite; a localhost dashboard (plus a separate all-campaigns Reach-outs page) shows
status and lets the owner tag replies. It is a solo tool, run locally on a Mac, not a
product for others — though as of the distribution feature (§8), it's also packaged as a
clean, installable open-source repo (`slap-dist`) for anyone else who wants to run their
own copy against their own GMass account.

---

## 2. Design philosophy — "iron"

Every decision serves this. When unsure, these win:

- **One source of truth.** Never store the same fact twice where it can drift. Derived
  data is regenerable, never hand-maintained.
- **Append-only.** The `events` table is never mutated/deleted, only appended. Any cache
  (the `recipients` table) is rebuildable by replaying events.
- **Fail loud.** Missing config/files/state → stop with a clear message. Never guess.
- **Warn, don't block — one exception.** Everything risky prompts and proceeds on
  confirm, EXCEPT the >1-page résumé gate, which is the single non-overridable hard stop.
- **Idempotent, one-recipient blast radius.** A failure can't affect more than one
  recipient and can't double-send. Draft ID is recorded before the send call fires.
- **Check, don't install.** System deps (xelatex, code, launchd) are verified, never
  auto-installed.

If you find yourself softening one of these "to be helpful," stop — that's the signal
you're about to introduce the class of bug this project is built to avoid.

---

## 3. How it works, end to end

1. **Prep (interactive, owner-driven):** `python slap.py send <campaign> [--now]` →
   paste drop → (LaTeX campaigns: paste + compile résumé, with a compile loop and the
   >1-page gate) → domain/recipient dedup check (and, for a static campaign hitting the
   *soft* warn, an offer to reuse a previously-archived résumé for that company) →
   preview → confirm → **queued** (writes a `queued` event + stages files). This loops
   ("Add another? [Y/n]") for as many recipients as you paste, all for the one campaign
   named on the command line. Nothing sends during this loop.
2. **Fire (unattended by default, or immediate via `--now`):**
   - **`--now` belongs to `send`, not to a separate command.** It is a boolean flag on
     `send <campaign> --now`. Once the whole interactive prep loop above finishes (every
     recipient you pasted staged, then you answer `n` to "Add another?"), `--now` calls
     `runner.drain(conn, global_config, api_key)` **directly and immediately** — the
     *exact same* drain function the scheduled `runner` command calls. **Its scope is the
     entire due queue across every campaign**, not just the recipient(s) you just staged
     in this session: `drain()`'s query (`due_recipients()` + `due_for_ooo_resend()`) has
     no campaign filter and no "just now" filter — it asks "what, anywhere, is due?" and
     sends all of it, up to the daily cap. So `send campaignA --now` can and will also
     fire a recipient from `campaignB` that was staged yesterday and is still due. The
     only things `--now` skips (that the scheduled path does first) are the
     `is_active_day()` check and `wait_for_fire_window()`'s random-moment sleep — it
     drains right now, unconditionally.
   - **Scheduled path:** macOS **launchd** launches `python slap.py runner` daily,
     anchored at `schedule.fire_window_start` (per `active_days`, e.g. skip weekends).
     `runner` first checks `is_active_day()` (exits cleanly, no DB touched, if today isn't
     configured active), then `wait_for_fire_window()` (sleeps to a random moment inside
     `fire_window_start`–`fire_window_end`), then calls the same `runner.drain()`.
   - Every send = two GMass calls: create draft (with attachment) → send campaign (with
     follow-up stage settings). Draft ID is recorded the instant step 1 returns.
3. **GMass takes over (their servers, laptop can be off):** sends from the owner's Gmail,
   tracks clicks, and is *told* (via `stageNAction: "r"`) to fire follow-up stages on the
   persona cadence and stop firing further stages once the recipient replies. SLAP builds
   none of the follow-up scheduling — it's set at send time and GMass runs it. **Whether
   GMass's native stop-on-reply actually behaves this way has never been behaviorally
   confirmed** — see §11.
4. **Monitor:**
   - `python slap.py dashboard` → localhost page at `http://127.0.0.1:5050`, polls GMass
     reports on open (replies/clicks/bounces/blocks), shows sends/clicks/replies/
     bounces-and-blocks + engagement + pipeline + today's runs. Owner tags replies
     (real / OOO / not-interested) here; OOO re-queues the next stage as a send-as-reply
     (see below and §5). This page also links to:
   - `/reachouts` — a separate, read-only, all-campaigns filterable page (one row per
     recipient, every campaign, filter/sort entirely client-side, zero extra network
     calls once loaded). See §8.
5. **OOO re-queue (a safety net, not GMass's own mechanism):** when the owner tags a
   reply "OOO" in the dashboard, SLAP itself — independent of whatever GMass's native
   cadence is doing — resends that recipient's *next* stage as a threaded reply
   (`sendAsReply: true` + `campaignIdToReplyTo`, an integer, on the normal runner
   cadence/cap/gap, batched alongside ordinary initial sends in the same `drain()` call).
   This exists because GMass usually filters real out-of-office auto-responders itself,
   but not always — this is the manual catch for the ones that slip through and get
   recorded as a genuine `reply`. **This resend never checks or depends on GMass's own
   follow-up state** — it's a one-off manual send driven entirely by local `events`, not
   an "enrollment" into anything GMass-side. See §11 for the still-open question of
   whether GMass's *native* stages are actually guaranteed to stop once an OOO auto-reply
   arrives.
6. **Housekeeping:** `python slap.py cleanup [--confirm] [--min-days-idle N]` clears
   stale compiled résumé PDFs after 15 idle days by default (keeps `.tex`; never deletes a
   PDF a live `RESUME_ARCHIVE_DIR` symlink still points at). `python slap.py doctor`
   health-checks. `python slap.py rebuild` reconstructs the recipients cache from events.
   `python slap.py plist` (re)generates the launchd `.plist` from the current
   `config.yaml`. `python slap.py init` is the interactive installer (see §8).

Mental model: **prep stages → `--now` or the scheduled `runner` drains the WHOLE due
queue → GMass sends + follows up + tracks → check dashboard/Reach-outs, tag replies →
OOO tags trigger SLAP's own threaded resend, independent of GMass's native cadence.**

---

## 4. Stack & repo layout

Python CLI. Local SQLite. Flask for the localhost dashboard. `requests` for GMass.
`python-dotenv`, PyYAML, `pypdf` (LaTeX page-count gate), `rich` (terminal
colorization). xelatex (system) for résumé compile. macOS launchd for scheduling.

```
slap.py                     # entry point — real CLI surface (13 commands, not the brief's original 6):
                             #   list | send <campaign> [--now] | dashboard | doctor | init |
                             #   domains | rebuild | runner | sync | plist | cleanup [--confirm] [--min-days-idle N] |
                             #   template-reload
slap/                       # package:
    config.py                 #   config.yaml + campaign.yaml loading, auto-discovery, fail-loud validation
    templates.py               #   drop parser + {{key}} fill + merge_config_values() (signature)
    tracking.py                #   events/recipients SQLite schema, append_event, rebuild
    gmass.py                   #   GMass client (campaigndrafts → campaigns, reports polling)
    gmass_cache.py               #   Redis-backed cache of GMass-dependent dashboard data, hourly refresh
    domains.py                  #   domain/recipient dedup (hard/soft warn), `domains` command
    latex.py                    #   xelatex compile loop, >1-page hard gate, recipient_workdir()
    queue.py                    #   stage_recipient(), due_recipients(), OOO tag_ooo()/due_for_ooo_resend()
    runner.py                   #   drain(), wait_for_fire_window(), is_active_day(), OOO resend
    reload.py                    #   template-reload: scan()/apply_changes()/write_failures()/load_failures()
    dashboard.py + dashboard_templates/{dashboard,reachouts,template_failures}.html   # Flask app, all pages
    doctor.py                    #   preflight checks (global + per-campaign), wired into send + drain
    cleanup.py                   #   stale-PDF classification/deletion (`cleanup` command)
    archive.py                   #   résumé archive (RESUME_ARCHIVE_DIR) + résumé-reuse lookup
    init.py                      #   interactive installer (`init` command)
    launchd.py                   #   render_plist() — generates the launchd .plist from config.yaml
    display.py                    #   rich-based terminal colorization (warn/error/success/preview panel)
config.yaml                 # global: sender, gmass key env, signature, persona cadences, schedule, tracking
config.yaml.example
campaigns/<name>/           # per campaign, auto-discovered:
    campaign.yaml           #   persona, latex{enabled,attachment_name}, attachment_file, fields[]
    initial.txt             #   "Subject: ..." line + blank line + body, {{key}} + {{signature}} placeholders
    stage1.txt stage2.txt stage3.txt   # follow-up bodies (no subject; thread as replies; end in {{signature}})
    resume.pdf              #   static attachment (latex-off campaigns only)
workdir/<campaign>/<recipient>/   # per-recipient working files (gitignored) — resume.tex, compiled/reused
                             #   PDF (latex-on and reused-résumé recipients only — see §5), .pdf.hash, staged.json
                             #   (staged.json also carries `field_values`, the raw pre-fill drop dict, added when
                             #   template-reload shipped — see §5's entry — None for recipients staged before that)
consumer_domains.txt        # editable exclusion list for domain-level dedup
slap.db                     # SQLite: events (append-only) + recipients (derived cache)
template_reload_failures.json   # gitignored (real recipient emails). template-reload's failure report,
                             #   fully overwritten every run — see §5's template-reload entry
CONTROL_SHEET.md            # gitignored, local-only. LIVE source of truth for every knob + build history
SLAP_BUILD_PROMPT.md        # gitignored, local-only. The original build brief (§ references point here)
SLAP_PROJECT_CONTEXT.md     # THIS FILE — tracked in slap.git, deliberately excluded from the slap-dist export
probes/ (probes/findings/)  # Phase-0 API-truth probes + captured evidence (also excluded from slap-dist)
.env / .env.example          # GMASS_API_KEY, RESUME_ARCHIVE_DIR (never committed)
.claude/                    # agents/iron-auditor.md, settings.json (hooks, permissions)
com.slap.runner.plist.example + LAUNCHD.md   # scheduler reference (real plist is generated by `plist`, not hand-copied)
conftest.py, tests/         # pytest suite (595 tests passing as of this snapshot; `pytest -m slow` for real-xelatex/real-socket tests)
```

---

## 5. Locked design decisions

**Config:** folder-per-campaign, auto-discovered (any folder with a valid `campaign.yaml`
is live; no central registry). Persona cadences are FIXED per persona in `config.yaml`'s
`personas:` block: `hiring_manager [2,4,6]`, `recruiter [2,3,5]`, `founder [2,5,7]` are
the three actually in use by the three real campaigns. **The owner's real `config.yaml`
also currently defines a fourth persona, `vibe: [2,4]`, that no campaign uses and that
appears nowhere else in the docs — status unknown, see §11.** Subject lives as the first
line of `initial.txt` (`Subject: ...` + blank line). Local `{{key}}` fill.

**Signature (config-driven, not per-campaign):** every campaign template can end with
`{{signature}}`. It is filled from `config.yaml`'s `signature:` key (`GlobalConfig.
signature`), not from the pasted drop and not from a per-campaign `fields` entry —
`slap/templates.py::merge_config_values(values, *, signature)` merges it into the fill
context right before every `fill_template()` call. The key is **required** (missing it
entirely fails loud via `_require`, before any preview/send) but an explicit empty string
`""` is allowed and renders no signature. `CONFIG_SOURCED_KEYS = {"signature"}` in
`slap/templates.py` is what lets `config.py`'s "every placeholder must match a declared
field" check make an exception for it — extend that set (in both places) if a second
config-sourced constant is ever added. Note this is distinct from the per-recipient
`byebye` drop field (label "Signoff") that every real campaign already has — a real
`initial.txt` ends `{{byebye}},\n{{signature}}` (e.g. "Best,\n<name+links>"); `stageN.txt`
files, which had no sign-off at all before this feature, now end with `{{signature}}`
alone.

**Sending model:** prep (`send`) stages; fire is either the unattended launchd-scheduled
`runner`, or `send --now`, which calls the identical `runner.drain()` immediately and
drains the WHOLE due queue across every campaign — see §3, item 2, for the exact scope
(this is the single most load-bearing clarification in this revision — it caused real
confusion in a prior session). Drain is cap-aware (overflow re-queues to next day, i.e.
`send_failed` recipients naturally retry on the next drain), and 10–15s apart between
sends. A drain-time *preflight* failure (e.g. missing API key) retries per
`drain_retries` then gives up loud (`run_failed`, queue untouched) — this retry policy is
NOT the same as a per-recipient send failure, which just writes `send_failed` and moves
on with no immediate retry.

**GMass send path:** per-recipient (one campaign per recipient) via `campaigndrafts` →
`campaigns/{draftId}` (draft id in the URL path, not the body). Transactional endpoint is
NOT used (no attachments/follow-ups on it). Outgoing mail is HTML (not plain text) with
auto-linkified bare URLs, specifically so GMass's click-tracking rewrite (which only
rewrites an `<a href>` whose visible text differs from its own href) actually fires — see
§7's click-tracking landmine for why plain text silently defeated this.

**Tracking — real schema** (`slap/tracking.py`, SQLite, event-sourced; `CREATE TABLE IF
NOT EXISTS` at every `connect()`, idempotent):

```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,        -- ISO 8601, always UTC, timezone-aware (naive datetimes rejected)
    recipient TEXT,                 -- NULL only for run-level events (run_started/run_completed/run_failed)
    campaign TEXT,
    type TEXT NOT NULL CHECK (type IN (
        'queued','draft_created','sent','click','reply','bounce','ooo_tagged','requeued',
        'reply_reviewed','run_started','run_completed','send_failed','run_failed'
    )),
    stage INTEGER,
    gmass_campaign_id TEXT,         -- stored as TEXT; coerced to int only when building the
                                     -- GMass campaignIdToReplyTo payload (GMass's own contract requires int)
    gmass_draft_id TEXT,
    meta TEXT                       -- JSON blob, event-type-specific (see below)
);

CREATE TABLE recipients (            -- derived cache, rebuildable via `rebuild` by replaying events in id order
    recipient TEXT PRIMARY KEY,
    campaign TEXT,
    persona TEXT,
    status TEXT,                    -- REAL values only: active | done | replied | bounced | ooo_requeued
                                     -- (there is deliberately no "queued"/"failed"/"sent" status — see below)
    current_stage INTEGER,
    last_gmass_campaign_id TEXT,
    first_sent_at TEXT,             -- first-write-wins (never overwritten by a later send)
    last_event_at TEXT,             -- last-write-wins
    replied_at TEXT                 -- first-write-wins
);
```

The `events.type` `CHECK` constraint is baked into every already-populated `slap.db` **at
table-creation time** — `CREATE TABLE IF NOT EXISTS` does NOT retroactively widen it.
Adding a genuinely new event type would require a live-data migration (SQLite has no
`ALTER TABLE ... ADD CHECK VALUE`, only rename-recreate-copy-drop) on every owner's real
database. This is why the bounces/blocks fix (§8) reused the existing `bounce` type with
a `meta["category"]` discriminator instead of adding a `"block"` type — see the general
pattern below. `template-reload` (§8) hit the identical constraint from the other
direction: rather than force ANY existing event type into meaning "reload failed" via a
discriminator, its per-recipient failure reports live entirely outside `events`, in a
small JSON file (`template_reload_failures.json`, §4) that's disposable by design (fully
overwritten every run, never a second source of durable truth) — a legitimate escape
hatch for diagnostic-only state that was never going to need replaying anyway.

**`meta` JSON shape by event type** (additive/backward-compatible — a reader always
treats a missing key as blank/unknown, never fabricates one):
- `queued`: `{"persona": ..., "company": ..., "role": ..., "req_id": ...}` — `company`/
  `role`/`req_id` are the drop-parsed values, persisted here specifically so the
  Reach-outs page and the résumé archive can use them; only present for recipients staged
  *after* the résumé-archive/Reach-outs features shipped — older `queued` events lack
  them.
- `sent`: `is_final_stage: true` optionally, meaning "this was the persona's last
  configured stage" → cache status becomes `done` instead of `active`.
- `click`: `{"url": ..., "click_time": ...}`.
- `reply`: `{"reply_id": ..., "reply_time": ...}`.
- `bounce`: `{"bounce_reason": ..., "bounce_time": ..., "category": "bounce" | "block"}`
  — GMass's `/blocks` report items are mapped into the SAME `bounce_reason`/`bounce_time`
  keys (not a parallel `block_reason`/`block_time` pair), with `category` as the only
  discriminator. Events recorded before this field existed simply lack `category` and are
  treated as `"bounce"` by default (`_latest_bounce_category()`).
- `reply_reviewed`: `{"tag": "real" | "ooo" | "not_interested"}` — the precedent the
  bounce/block `category` discriminator above deliberately copied.
- `ooo_tagged`/`requeued`/`draft_created`/`run_*`: no meaningful meta beyond the
  event-level columns.

**Real `recipients.status` vocabulary is `active`/`done`/`replied`/`bounced`/
`ooo_requeued` only** — there is no `queued`/`sent`/`failed` status. "Queued" (staged,
never actually sent) is *derived*, not stored: `status = 'active' AND first_sent_at IS
NULL`. There is deliberately no `failed` status either — `send_failed` is a transient
per-attempt event, always retried by the next drain, never a durable resting state.

**Domain/recipient dedup:** derived live by querying the `recipients` cache (itself
derived from `events` — no third store). Exact-recipient → hard warn (always, even on a
consumer domain). Same non-consumer domain, different person → soft warn. Both warn-
don't-block. Consumer domains excluded from the soft warn via `consumer_domains.txt`.

**Résumé archive (`RESUME_ARCHIVE_DIR`, `slap/archive.py`) — shipped, not a proposal.**
Set `RESUME_ARCHIVE_DIR` in `.env` to a folder path; every résumé staged gets a
**symlink** (never a copy) dropped into it, named `<company>-<role>-<date>.pdf`
(slugified; date = queue time). `company`/`role` come from `values.get("company", "")` /
`values.get("role_catted", "")` — see the per-campaign field mapping in §8; all three
real campaigns already share these two exact keys. Off by default, never blocks a send
(`doctor` reports its status separately, never inside `run_global_checks()` — a stale
archive folder can never fail a send/drain). `cleanup` keeps (never deletes) a PDF a live
archive symlink still points at.

**Résumé reuse on the domain soft-warn (static campaigns only) — shipped.** When the
*soft* warn fires (never the hard warn) for a `latex.enabled: false` campaign, and the
archive has at least one entry matching the new recipient's company
(`archive.find_matches_for_company()`), `send` offers a numbered choice to reuse one of
those PDFs instead of the campaign's default `attachment_file`. Default (`0`/blank) is
decline. A chosen entry is resolved, validated as a real non-empty readable PDF, and
**copied** (never symlinked) into the new recipient's own `workdir/` — so it's correct
regardless of what `cleanup` later does to the *original* recipient's files, and the
reused résumé gets its **own** fresh archive entry under the new send's company/role/
date (two people, one résumé's content, two archive entries). Never offered for LaTeX
campaigns (no clean way to skip the paste/compile loop). A broken pick fails loud for
that one recipient only.

**Reach-outs page (`/reachouts`) — shipped, not "pending."** Separate, read-only,
all-campaigns page: one row per recipient (the `recipients` cache's natural grain — most
recently associated campaign, not one row per historical campaign contact), filterable
and sortable, zero GMass calls (`/reachouts` never calls `sync_reports()` at all). Filter
dimensions: campaign/persona/domain (exact dropdowns), status (`queued`/`active`/`done`/
`replied`/`bounced`/`ooo_requeued` — `queued` here is the same derived
`first_sent_at IS NULL` definition above), engagement (replied/clicked-no-reply/none),
reply tag (real/ooo/not_interested/untagged — "untagged" means "replied, pending
triage," never "never replied at all"), req-id-present, date range (first-sent-or-
last-event, whichever exists), and free-text search (recipient + company). `company`/
`req_id` show blank for any recipient staged before this feature landed (see the `meta`
backward-compatibility note above) — never guessed. `reachouts_rows()` and
`filter_reachouts()` in `slap/dashboard.py` are pure, fully-tested Python; the actual
`/reachouts` route renders every row unfiltered in one response, and a hand-written
vanilla-JS block in `reachouts.html` mirrors `filter_reachouts()`'s semantics for real
client-side interaction (this is the one place in the whole dashboard with any
JavaScript at all).

**Dashboard — current shipped state, not a wishlist (see §8 for full detail).** The
"dashboard reorg + new widgets" this doc's older draft called "pending" shipped well
before this revision. Confirm any future dashboard claim against
`slap/dashboard_templates/dashboard.html` directly rather than this doc's prose, but as
of this snapshot the panels are: Metrics (today/week merged), Next drain, Engagement
intelligence, Warm but silent, Replies needing triage, Bounces & blocks, Companies
contacted, Pipeline, Today's runs — plus a link to `/reachouts`, and, only while at least
one unresolved `template-reload` failure exists, a link to `/template-failures` (see this
section's `template-reload` entry).

**Bounces vs. blocks — a real, fixed bug, not an open item.** GMass reports bounces and
blocks as two separate report categories (`/api/reports/{id}/bounces` vs. `.../blocks`,
different field names). The app used to poll only `/bounces`, silently missing every
block. Fixed by adding `_sync_blocks()` (mirrors `_sync_bounces()`), writing
`type="bounce"` + `meta["category"]="block"` — deliberately reusing the existing
`bounce` event type rather than adding a new one, for the live-migration reason explained
in the schema section above. The dashboard's "Bounces" widget is now "Bounces & blocks"
with a Type column.

**OOO re-queue:** GMass usually filters auto-responders itself, so this is a safety net.
Tagging a reply "OOO" fires that recipient's next stage as a send-as-reply
(`sendAsReply: true` + `campaignIdToReplyTo`, coerced to int) on the normal drain
cadence — see §3 item 5 and §11 for what's proven vs. not.

**LaTeX loop:** app owns compilation (deterministic, gives page count + guaranteed PDF);
`code` + macOS Preview are surfaces. Compile loop `[r]ecompile / [o]pen editor / [d]one /
[a]bort`. On done: **>1-page forces a decision** (the one hard gate, exact-phrase
confirmation, no y/n shortcut), then rename to `attachment_name`, attach.

**Tracking flags:** `openTracking: false`, `clickTracking: true` (requires HTML mail with
mismatched anchor text — see §5's GMass send path note and §7).

**Static-campaign attachments are not duplicated per recipient.** A static
(`latex.enabled: false`) campaign's shared `resume.pdf` is never copied into a
recipient's `workdir/` — the manifest records `attachment_source` (the shared file's own
path), and the runner reads bytes from there fresh **at drain time**, not frozen at stage
time (so editing `campaigns/<name>/resume.pdf` between staging and draining changes what
already-staged-but-unsent recipients get — an intentional choice, not a bug). LaTeX-
enabled campaigns, and any résumé-reuse recipient (which is genuinely per-recipient
state), still get their own file copied into their own `workdir/`.

**Template reload (`template-reload`, `slap/reload.py`) — shipped, not a proposal.** The
attachment note directly above was already true; the *text* (subject/body/stage bodies)
was the opposite — frozen at stage time with no way to pick up a later template edit,
until this shipped. `template-reload` re-renders every recipient `due_recipients()` (§10,
reused verbatim, not re-derived) says has nothing sent at all yet, across every campaign
in one pass, against whatever `initial.txt`/`stageN.txt` currently say — then shows a
summary + sample diffs and asks to confirm before writing anything (mirrors `send`'s own
preview-before-stage pattern). Two things had to be confirmed against the real code
before this could be built at all, not assumed: (1) a recipient's whole follow-up cadence
gets flattened into ONE `send_campaign()` call at initial send (`slap/gmass.py::
build_campaign_settings()`) — there is no second call that could ever update stage 2/3
wording later, which is exactly why the eligibility set is "nothing sent at all," not
"nothing sent for the CURRENT stage"; (2) `staged.json` used to store only the already-
rendered text, never the raw drop values `fill_template()` needs to re-run — so
`stage_recipient()` now also persists `field_values` (§4) going forward, and a recipient
staged before that shipped fails reload with its own distinct reason rather than being
silently skipped or mis-handled. A recipient with an OPEN GMass draft (`create_draft`
succeeded, `send_campaign` didn't — `slap.tracking.latest_open_draft_id`) is excluded
from reload too, for the same reason: that draft's subject/body are already committed to
GMass and the next drain reuses them as-is, so rewriting the manifest here would silently
split-brain the initial email against a follow-up cadence built from the newly-edited
manifest (an iron-audit SHOULD-FIX, caught before shipping — see §7). A campaign whose
config is currently broken fails only THAT campaign's recipients; a recipient whose
stored values don't cover a since-added placeholder fails only THAT recipient — never a
batch-wide abort. Unresolved failures are reported via a small JSON file (§4/§5's
CHECK-constraint note above), surfaced on the dashboard as a **Template Failures** tab,
nav-linked only when at least one exists (deliberately NOT this dashboard's usual "show
an honest empty state" default — the owner explicitly asked for the link itself to
disappear at zero) and reachable directly with a real empty-state page regardless.

**Configurable scheduler days (`schedule.active_days`).** `config.yaml`'s `schedule.
active_days` (e.g. `[mon,tue,wed,thu,fri]`) drives BOTH the generated launchd plist (one
`StartCalendarInterval` entry per active day — an array, since launchd can't express
"only these weekdays" in one dict) AND a second, independent runner-side guard
(`is_active_day()`, checked in `cmd_runner` before touching the DB at all). The guard is
deliberately NOT applied to `drain()` itself or to `send --now` — an explicit human
action should never be silently skipped by a scheduling preference. The real
`config.yaml` currently sets `active_days` to all 7 days (unrestricted).

**`active_days` has ZERO effect on GMass's own native follow-up firing — confirmed by a
real investigation, not a guess, after the owner saw Sunday activity and expected
weekends to be excluded (2026-07-12).** `is_active_day()`/`active_days` only ever gates
whether SLAP's own local `runner` drains NEW, locally-initiated sends on a given day.
GMass fires stage 2/3 of an already-launched campaign entirely server-side, on the
`stageNDays` gaps baked in at initial send — SLAP has no scheduler of its own for that
(§5, "Follow-ups are GMass's job") and, critically, **never writes any local event when
a native follow-up fires** (only the initial `sent` event is ever recorded). The actual
July 12 event log confirms this exactly: the only two events that day are
`run_started`/`run_completed` with `sent: 0` (a genuine no-op — SLAP itself sent
nothing), while 11 recipients initially sent the prior business day (Friday July 10)
had their persona's 2-day first stage land right on that Sunday — GMass fired those,
invisibly to SLAP. `active_days` being unrestricted (all 7 days, above) made this doubly
inevitable but isn't even the deciding factor — even a fully weekday-restricted
`active_days` would not have paused those follow-ups, since they were never subject to
it. See CONTROL_SHEET.md's dedicated write-up for the full investigation. If pausing
follow-ups over weekends is ever wanted, `active_days` cannot do it — that would need a
real design decision (GMass-side controls, if any exist, or accepting the current
behavior), not something this investigation decided unilaterally.

---

## 6. Real GMass API facts (probe-verified — these OVERRIDE the build brief's assumptions)

**These facts are as of the original Phase-0 probes (2026-07-02) and have not been
re-verified live since.** No new probe run is recorded in `CONTROL_SHEET.md` between then
and this snapshot (2026-07-09). Treat anything below as "true as of the last time anyone
checked," and re-probe (`probes/run.py`) before trusting a fact that's become
load-bearing for new work, especially anything about follow-up/stop-on-reply behavior
(see §11 — several of the below were explicitly marked as parameter-accepted but
behaviorally *unproven*, and that remains the case).

- **Auth:** `X-apikey` **header** (query `?apikey=` also works; header preferred to keep
  the key out of logs).
- **Send endpoint:** `POST /api/campaigns/{campaignDraftId}` — draft id in the **PATH**,
  not the body. Body-form returns 400.
- **Draft + attachment:** `POST /api/campaigndrafts`, JSON body,
  `attachments: [{fileName, contentType, base64Content}]` (base64 in JSON — multipart is
  rejected 415). Accepts up to 25 MB; base64 inflates the body ~33%.
- **Follow-up stop-on-reply:** per-stage `stageOneAction: "r"` (also `stageTwoAction`,
  `stageThreeAction`; values r/o/c/s/a). There is **no** single global stop param. The API
  does NOT echo these back (confirmed via the live-fetched OpenAPI spec — no read model
  exposes `campaignSettings` or an `action` field at all), so the setting can't be read
  back — behavior must be proven by watching a real reply. **Still pending — see §11.**
- **Threading:** `sendAsReply: true` + `campaignIdToReplyTo` (an **integer** in the API
  contract; stored as TEXT in this app's SQLite and coerced to int only when building the
  request).
- **Reports:** `GET /api/reports/{campaignId}/{replies|clicks|bounces|blocks|opens|
  recipients|unsubscribes}`, envelope `{metadata{...}, data:[...]}`. Item fields are
  **camelCase** (`emailAddress`, `bounceReason`/`blockReason`, `bounceTime`/`blockTime`,
  ...) per the per-campaign endpoints and the authoritative swagger schema. **The
  `/api/sample/*` endpoints are a separate, inconsistent legacy path** (PascalCase, some
  50 500s) — never build against them; this app never has.
- **Bounces and blocks are separate report categories, not the same thing tagged
  differently** — different endpoints, different field name prefixes. This app polls
  both (`_sync_bounces()` + `_sync_blocks()`) as of the fix described in §5/§8; before
  that fix, blocks were invisible.
- **Send settings** live at the top level of the `POST /api/campaigns/{id}` body (flat
  `stageOneDays`, `openTracking`, etc.) — NOT nested under a `campaignSettings` key in the
  actual request; `campaignSettings` is only the name of the swagger *model* documenting
  the field list, confirmed empirically while building `slap/gmass.py`.
- **Idempotency:** repeating `POST /api/campaigns/{draftId}` on an already-sent draft
  returned the identical `campaignId` and `creationTime` in a live test — GMass appears
  to treat the repeat as idempotent rather than double-sending, though no manual Gmail
  check ever confirmed the physical inbox had exactly one message.

---

## 7. Landmines / gotchas (things that already bit, or will)

- **`{{double braces}}`, not `{single}`.** Templates fill on double braces. Single-brace
  drafts render literally. (Bit us once.)
- **`req_id` is inline** (in body and subject) and is NOT an optional-line-drop field —
  making it optional would nuke the whole sentence/subject when empty. Always include a
  `req_id` line in the drop; blank when none; **leading space** in the value when present.
- **HTML mail with auto-linkified bare URLs, not plain text.** `messageType` must be
  `"html"`, and a link's visible text must DIFFER from its own href (a naive
  auto-linkify that renders `<a href="X">X</a>` is silently left un-rewritten by GMass's
  click tracker — domain-only display text is sufficient, the full URL as text is not).
  This was a real, shipped bug (`clickTracking: true` present but zero clicks ever
  recorded) before the fix — see `slap/gmass.py::_plain_text_to_html()`.
- **launchd runs with a bare environment.** No shell, no `cd`, no aliases, no auto-loaded
  `.env`. The generated plist (via `slap.py plist`) uses absolute paths; the runner loads
  `.env` itself. **This is the #1 unattended-failure risk** — verify on any real wake test.
- **Two different "times":** the plist `StartCalendarInterval` (launch anchor, fires on
  wake if asleep) vs. `config.yaml`'s `fire_window_start/end` (the jitter window the
  runner sleeps inside, in LOCAL time — deliberately not UTC, since it mirrors a human
  scheduling preference and launchd's own local-time semantics). Different files,
  different jobs.
- **`events.type`'s SQL `CHECK` constraint can't be widened on an existing `slap.db`
  without a full rebuild-and-swap.** This is why bounces/blocks reused the `bounce` type
  with a `meta["category"]` discriminator instead of adding a `"block"` type (§5/§8) — the
  established pattern for "I need a new sub-category" going forward, not "add a new
  event type" by default.
- **`gmass_campaign_id` is TEXT in SQLite but must be an `int` in the GMass request
  body** (`campaignIdToReplyTo`) — coerce at the API-call boundary, don't assume the
  stored string is safe to pass through raw.
- **`company`/`req_id` are blank for any recipient staged before the résumé-archive/
  Reach-outs features shipped** — genuinely unknown, not a bug, never guessed/backfilled.
- **The real `config.yaml` has an undocumented fourth persona, `vibe: [2,4]`,** used by no
  campaign and mentioned nowhere else — see §11.
- **Test-send guard:** probes/tests may send ONLY to a `+testmass{N}` plus-tagged alias of
  the configured `sender.from_email` (owner-derived since the distribution feature, not a
  single hardcoded address anymore — see §8), or create drafts. Hard-coded into
  `probes/run.py::_guard()`/`_guard_body()`, non-overridable.
- **"Nothing sent yet" is not the same as "nothing has happened yet."** Caught by an
  iron-audit before `template-reload` shipped (§5/§8), not by a test that happened to
  fail: `due_recipients()` returning a recipient (no `sent` event) does NOT mean nothing
  is in flight for them — `create_draft` can have already succeeded, with the real
  subject/body committed to an open GMass draft, before `send_campaign` failed. Any future
  feature that treats "not yet sent" as "safe to rewrite locally" needs the same
  `slap.tracking.latest_open_draft_id()` check `template-reload` added, or it'll silently
  split-brain the initial email against whatever gets rebuilt from the edited local state.
- **`active_days`/weekend-pausing only ever covers SLAP's own local initial sends —
  GMass's native follow-up firing (stage 2/3) is completely outside its scope and leaves
  no local event trail at all.** Caused a real, confirmed-not-a-bug false alarm (owner
  saw Sunday activity, expected weekends excluded — see §5's dedicated entry and
  CONTROL_SHEET.md for the full investigation). Don't assume "the owner configured
  weekday-only `active_days`" means no mail goes out on a weekend; it only means SLAP
  itself won't INITIATE a new send that day. An already-launched campaign's follow-ups
  keep firing on GMass's own clock regardless.

---

## 8. Current state (as of 2026-07-09)

**All original 13 Build Order steps are complete, plus a long tail of post-launch
features and real-usage bugfixes** (this app has been in real, personal use for a while
— several of the bugs below were found by actually sending real emails, not by a build-
stage audit).

**Confirmed working / shipped** (verify against code if this list is ever in doubt —
don't assume a feature landed just because a task once existed for it):

- All three real campaigns render correctly; résumé attaches under the shared name
  (static for founder/recruiter, per-recipient compiled for hiringmanager).
- The full send/drain event pipeline, dedup hard/soft warn, OOO re-queue, cleanup,
  colorized terminal output, and the >1-page hard gate.
- **Résumé archive** (`RESUME_ARCHIVE_DIR`, `slap/archive.py`) — symlink-based, real env
  var, real naming scheme (§5).
- **Résumé reuse on the domain soft-warn** (static campaigns only) — real feature, real
  code paths (§5).
- **The Reach-outs page** (`/reachouts`) — real route, real filters (§5).
- **Email signature moved into `config.yaml`** (`signature:` key) — real, required,
  every real campaign template updated to use `{{signature}}` (§5).
- **Bounces vs. blocks fix** — both report categories now polled and distinguished in the
  dashboard (§5).
- **Distribution tooling**: `slap.py init` (9-step interactive installer), the
  config-driven owner test-guard (probes now guard to whoever's `config.yaml` says, not
  one hardcoded address), and `slap-dist` (a separate public repo,
  `github.com/burpcat/slap-dist`, fresh git history, exporting only the generic/
  redistributable subset — real campaigns, `slap.db`, `probes/`, and this file's
  companions `CONTROL_SHEET.md`/`SLAP_BUILD_PROMPT.md`/`SLAP_PROJECT_CONTEXT.md` are all
  deliberately excluded from that export).
- **Configurable scheduler days** (`schedule.active_days`) — real plist generation
  (`slap.py plist`, `slap/launchd.py`), real runner-side guard.
- **Static-campaign attachments no longer duplicated per recipient** — real fix, changed
  behavior around when attachment bytes are read (drain time, not stage time).
- **`template-reload` command + Template Failures dashboard tab** (`slap/reload.py`) —
  real feature, re-renders not-yet-sent recipients against edited templates; see §5's
  dedicated entry for the two things confirmed against real code before it was built and
  the open-draft edge case an iron-audit caught before shipping.

**Genuinely still pending / unproven:**

- **launchd wake test** — the "fires on wake / loads `.env` unattended" guarantee is
  still unproven on real hardware as far as this doc's sources show; do the sleep/wake
  test before trusting real unattended 9am sends if it hasn't been done since the plist
  generation rework.
- **Every GMass behavioral proof flagged as "timing-dependent" in Phase-0 remains
  unproven** — see §6 and §11. Nothing here has changed since the original probes.

**Per-campaign field mapping** (confirmed directly from the real `campaign.yaml` files,
not assumed — this was previously undocumented and had already caused feature-design
confusion):

- **`coldpost-founder`** — latex off, static `resume.pdf`, `founder` persona (cadence
  2/5/7). Fields: `email`, `role_catted` (label "Role"), `company` (label "Company"),
  `req_id`, `founder_name`, `research_source` (optional), `specific_detail` (optional),
  `experience_1`, `experience_2`, `byebye` (label "Signoff").
- **`coldpost-recruiter`** — latex off, static, `recruiter` persona (cadence 2/3/5).
  Fields: `email`, `role_catted`, `company`, `req_id`, `recruiter_name`, `experience_1`,
  `experience_2`, `byebye`.
- **`linkpost-hiringmanager`** — latex ON, `hiring_manager` persona (cadence 2/4/6).
  Fields: `email`, `role_catted`, `company`, `req_id`, `hm_name`, `company_signal`
  (optional), `experience_1/2/3`, `question_a`, `question_b`, `byebye`.

**All three campaigns use exactly the same two keys for "company" and "role": `company`
(label "Company") and `role_catted` (label "Role").** No campaign lacks a clean
equivalent for either concept. (One historical inconsistency: `slap/init.py`'s
*scaffolded example campaign*, offered by `init`'s step 6, and an old USAGE.md snippet
briefly declared this field as `key: role` instead of `role_catted` — this has been fixed
so the scaffold matches the real campaigns' convention; if you ever see `key: role`
anywhere, that's the bug that was fixed, not a second valid convention.)

**Sender:** the app is designed to work for any owner's Gmail (see distribution, above);
the real installed instance's exact address lives only in the real, gitignored
`config.yaml`/`.env` — don't assume it, read `config.yaml` if you need it (real, not
committed).

---

## 9. Working rhythm & the Claude Code setup

Implementation happens in **Claude Code**, in the repo. The pattern is **Route A**: the
main session acts as supervisor + builder (it does the writing/commits), plus a
**read-only `iron-auditor` subagent** (Read/Grep/Glob only) that reviews each change
against the brief + iron principles and returns BLOCKER / SHOULD-FIX / NIT findings.

Per-change loop: **plan → build → audit → fix → commit → `/compact`.** The auditor has
repeatedly earned its place — it caught a relocated defect a single-pass review would
have signed off on, more than once. When the auditor flags something contradicting the
brief, the OWNER adjudicates. **Standing instruction from the owner: fix BLOCKER/
SHOULD-FIX findings by default; leave NITs alone unless asked** — don't spend a fix round
on every NIT the auditor surfaces.

`CLAUDE.md` in the repo encodes the iron rules + points at `SLAP_BUILD_PROMPT.md`. Hooks:
test-after-edit, and a PreToolUse guard blocking edits/reads of `.env`.

The repo also ships to a second remote, `slap-dist` (see §8) — a genuinely public
distribution mirror with its own git history. Propagating a change there is a distinct,
deliberate step (diff-check against dist's current state first, copy files, run dist's
own test suite in its own venv, a security grep gate for real emails/paths/API keys, then
commit+push) — not automatic just because `slap.git` got a commit. A past session caught
and fixed a real privacy leak (real third-party email addresses in a test fixture) via
this grep gate before it ever reached `slap-dist` — the gate is not just a formality.

**If you are a Claude in a web/chat project (not Claude Code):** you can't touch the repo
directly. Your job is design, debugging judgment, interpreting test output/screenshots,
and writing precise prompts for Claude Code. When writing those prompts: demand
root-cause before fix, a test that covers the exact case, and an iron-auditor pass.
Prefer dry-run/reversible defaults for anything destructive. **Verify claims against the
actual repo state before asserting them as fact** — this doc is refreshed periodically,
but it can still drift from the code between refreshes; when a design decision hinges on
an exact behavior (a flag's scope, a schema column, a status value), ask for the relevant
file/function to be checked rather than trusting a paraphrase, including this one.

---

## 10. How to debug well on this project

- **A GMass `200` is not proof of success.** It's a .NET API that silently drops
  unrecognized fields. Prove behavior by inspecting the received email / GMass's own
  report / a GET-back, not the status code.
- **"Drain complete: 0 sent, 0 failed, 0 queued" with a confirmed send = silent loss.**
  Every recipient must end as sent, failed, or queued — never vanish. (This exact bug
  happened once — a stale `first_sent_at IS NULL` proxy silently excluded re-contacted
  recipients from `due_recipients()` — fixed by deriving "due" from the event log
  directly instead.)
- **When a delivery-failure count looks short, check whether it's a bounce-vs-block gap
  before assuming a sync/pagination bug.** GMass's `/bounces` and `/blocks` are separate
  report categories — this exact shape of bug happened once (§5/§8) and is easy to
  reintroduce if a future "new report type" is added without wiring its own `_sync_*`
  function into `sync_reports()`.
- **Check the received email, not just the preview**, for anything about links,
  attachments, threading, or rendering.
- **When a send behaves oddly, look at what's different about that recipient** — the
  silent-loss bug above was isolated by noticing the ones that failed had all been
  contacted before, and the one that sent was fresh.
- **The dashboard trails GMass** by however long since the last on-open poll; stale
  numbers aren't a bug. `/reachouts` never polls at all — it's local-data-only by design.
- **Everything that ever happened is in the `events` log.** If `recipients` looks wrong,
  the log is truth and the cache can be rebuilt (`rebuild`).
- **Before trusting a claim about "what status/column/flag does," check `slap/tracking.py`
  and the relevant module directly** — several sections of this very doc were wrong
  (stale) until this revision cross-checked them against the real code.

---

## 11. Known open questions / unverified

Honest gaps — things nobody has actually confirmed, not things anyone forgot to write
down. Re-check these before designing anything that depends on the answer.

- **Does GMass's native follow-up cadence actually stop once a recipient's OOO
  auto-reply is detected?** The app sets `stageOneAction: "r"` (etc.) at send time,
  which *should* mean GMass stops firing later stages once it sees any reply — but the
  GMass API has **no read endpoint** that echoes back which action registered, and
  `autoFollowups` stays empty even after a campaign settles to `"sent"`. The only proof
  that could ever exist is watching real stage-firing behavior after a real reply, days
  out — never done as of this snapshot. **Concretely**: if GMass's own auto-responder
  filtering does NOT treat an out-of-office auto-reply as a stop-triggering "reply," a
  recipient tagged OOO in the dashboard could receive BOTH GMass's own natively-fired
  next stage AND SLAP's own manual threaded resend — a real potential duplicate-send this
  has never been proven not to happen. SLAP's own OOO resend logic doesn't check or wait
  on GMass's state at all before firing (see §3/§5), so this isn't something the app's
  code could self-detect even if it started happening.
- **Does `sendAsReply` actually thread visually in Gmail?** API-accepted (200), never
  visually confirmed in an actual Gmail thread as of this snapshot.
- **Does `createDrafts: true` actually suppress a real send?** Assumed true in probe
  code; if wrong, the blast radius was always limited to a guarded `+testmass` address,
  so low risk, but still unconfirmed.
- **Is the compiled/static PDF actually present on delivered mail?** The GMass API never
  echoes attachment content back in any response (draft or sent), on any endpoint — this
  can only ever be a manual "open the delivered email and check" step, not something
  automatable. Do this periodically, not just once.
- **What is `vibe: [2,4]` in the real `config.yaml`'s `personas:` block?** Not used by
  any of the three real campaigns, not mentioned in `CONTROL_SHEET.md`, README.md,
  USAGE.md, or CLAUDE.md. Either a fourth persona being prepared for a future campaign, or
  stray/leftover config. Ask the owner before assuming either way — don't build against
  it, and don't delete it, without confirming which.
- **Has anyone re-run the Phase-0 probes since 2026-07-02?** Not as far as
  `CONTROL_SHEET.md` records. Every fact in §6 is "true as of that date" — GMass is a
  third-party API and could change behavior without this repo knowing.
- **Is the launchd wake-catch-up behavior still proven against the CURRENT plist
  generation path** (`slap.py plist`, post-scheduler-days rework), or only against the
  older hand-copied `.example` plist from before that feature shipped? Worth a fresh
  wake test rather than assuming the old manual test still covers the generated-plist
  code path.
