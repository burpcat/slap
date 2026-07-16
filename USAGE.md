# USAGE.md — using slap day to day

This assumes you've already run `python slap.py init` (see [`README.md`](README.md)) and
`doctor` passes. This doc covers everything after that: creating campaigns, writing
drops, sending, the dashboard, the scheduler, and deliverability tips.

## Mental model

```
send  ──stages──>  queue (SQLite events)  ──drains──>  GMass  ──>  Gmail
                                                          │
                                                          ├─ sends the initial email
                                                          ├─ fires follow-up stages
                                                          │   on your persona cadence,
                                                          │   stopping automatically
                                                          │   on reply
                                                          └─ tracks opens/clicks/
                                                              replies/bounces/blocks
                                                          │
                                            dashboard  <──┘  (you check status,
                                                              tag replies)
```

- `send <campaign>` is **prep**: interactive, paste a drop, nothing goes out yet — it
  just stages a `queued` event and the message data to local disk.
- The **runner** is **fire**: unattended, fired once a day by launchd (typically ~9am,
  your configured fire window), asks "what's queued and due?" and actually sends.
  `send --now` does the same thing immediately instead of waiting.
- GMass takes it from there — it relays through your Gmail, fires stage 2/3 follow-ups
  on the cadence you set at send time, and stops automatically if the recipient replies.
  **slap never builds or runs its own follow-up scheduler** — that's entirely GMass's job.
- Everything that happens is an event in local SQLite (`slap.db`). The **dashboard**
  reads that log to show status, and is where you tag replies (real / out-of-office /
  not interested).

## Create a new campaign

Campaigns are auto-discovered: any folder under `campaigns/` with a valid
`campaign.yaml` is live — no registry to update, nothing else to wire up.

```
campaigns/
  my-campaign/
    campaign.yaml
    initial.txt      # Subject: line + blank line + body
    stage1.txt       # follow-up bodies — no subject line, they thread as replies
    stage2.txt
    stage3.txt
    resume.pdf       # only needed when latex.enabled is false
```

`campaign.yaml`:

```yaml
persona: recruiter                # -> looks up the FIXED cadence for this persona in
                                   #    config.yaml's personas: block (e.g. [2, 3, 5])
latex:
  enabled: false                  # true = paste + compile a LaTeX resume per recipient
                                   # false = attach the same static PDF every send
  attachment_name: "Firstname_Lastname_Resume.pdf"   # filename the recipient sees
attachment_file: resume.pdf       # required when latex.enabled is false; put the real
                                   # PDF at campaigns/my-campaign/resume.pdf
fields:
  - { key: email,          label: Email }
  - { key: role_catted,    label: Role }
  - { key: company,        label: Company }
  - { key: req_id,         label: Req ID }                       # inline field, see below
  - { key: contact_name,   label: Contact name }
  - { key: company_signal, label: Company signal, optional: true } # whole-line field, see below
```

Key rules, enforced fail-loud by `doctor` and `send`:

- **`persona`** must be one of `config.yaml`'s `personas:` keys — that's what fixes the
  follow-up cadence (number of stages and days between them). Cadences are fixed per
  persona, not per campaign.
- **The number of `stageN.txt` files must exactly equal the persona's cadence length.**
  `hiring_manager: [2, 4, 6]` needs exactly `stage1.txt`/`stage2.txt`/`stage3.txt`; a
  persona with 2 stages needs exactly 2 stage files. Too many or too few fails loud.
  `initial.txt`'s `Subject:` line is only for the initial send — stage files have no
  subject line since they thread as replies into the same conversation.
- **`fields`** must include one with `key: email` — `send` needs it to know who to mail.
  Every other field is a label you'll see when pasting a drop (see below).
- **Static campaigns** (`latex.enabled: false`) need `attachment_file` pointing at a real
  PDF in the campaign folder — the same file is attached (freshly read at drain time) for
  every recipient, never duplicated per recipient.
- **LaTeX campaigns** (`latex.enabled: true`) compile a fresh, genuinely per-recipient
  résumé at send time — see "The send flow" below.
- **`{{signature}}`** is available in every template without declaring it in `fields` —
  see "The shared signature" below.

### The shared signature

Every `initial.txt`/`stageN.txt` can end with `{{signature}}`. Unlike every other
placeholder, it isn't filled from the pasted drop — it comes from `config.yaml`'s
`signature:` key, one place shared by every campaign, so your sign-off (name, links,
whatever else you sign every email with) isn't duplicated and hand-edited across a dozen
template files. `config.yaml`'s `signature:` key is **required** (missing it fails loud,
before any preview or send) but can deliberately be set to an empty string `""` if you'd
rather send with no signature at all.

### Two kinds of field: inline vs. whole-line-optional

This distinction matters and is easy to get backwards:

- A field **without** `optional: true` (e.g. `req_id` above) is a plain inline
  substitution — if the drop leaves it blank, `{{req_id}}` is just replaced with an empty
  string, and everything else on that line stays. Use this for fields embedded inside a
  larger line you never want to disappear (like a subject line). The convention for an
  inline field like `req_id` is to put the punctuation/spacing INSIDE the value itself —
  e.g. type `Req ID:  (Req #4521)` (leading space) in the drop when present, and leave it
  as `Req ID:` (nothing after the colon) when there's no req id — so the template
  (`{{role_catted}} at {{company}}{{req_id}}`) reads naturally either way with no stray gap.
- A field marked **`optional: true`** (e.g. `company_signal` above) drops its **entire
  line** from the rendered message when empty — not just the placeholder. Use this when
  the field lives on its own dedicated line (a personalization sentence, a P.S.) that
  should vanish completely rather than leave a blank line or a dangling "I noticed ." when
  you have nothing to say.

## Write a drop

A "drop" is the pasted block that fills a campaign's placeholders for one recipient.
Plain text, one field per line:

```
Email: jane@acme.com
Role: Staff Engineer
Company: Acme Corp
Req ID:  (Req #4521)
Contact name: Jane
Company signal: you shipped the new search re-ranking system
EOF
```

- Each line is split on the **first colon only** (`Role: Backend: Infra` → key `Role`,
  value `Backend: Infra` — colons after the first are preserved in the value).
- Exactly one space after the colon is stripped; everything else is kept as-is (so a
  leading space for an inline field like `Req ID:  (Req #4521)` above survives).
  Matching against a field's `key` or `label` is case-insensitive.
- Lines with no colon are ignored; unknown keys are ignored; a field missing from the
  drop entirely defaults to an empty string.
- Type `EOF` on its own line to end the paste — this works the same whether you're typing
  live or pasting a multi-line block from your clipboard.
- A declared field left empty prints a warning (`⚠ empty fields: req_id`) right before
  the preview — this is **informational only, it never blocks the send**, since some
  fields (like `req_id`) are legitimately blank often.

## The send flow

```bash
python slap.py send my-campaign
```

1. **Paste the drop** (above), terminated with `EOF`.
2. **If `latex.enabled: true`**: paste the LaTeX `.tex` source next (also terminated with
   `EOF`). It compiles, opens the PDF in Preview and the `.tex` in `code`, then drops you
   into a loop:
   - `[r]ecompile` — after you've edited the `.tex`, recompile and reopen the preview.
   - `[o]pen editor` — reopen the `.tex` in `code` if you closed it.
   - `[d]one` — compiles one final, authoritative time, then:
     - **>1 page is a hard gate — the only one in the whole app.** You must type the
       exact phrase shown (`send N pages anyway`) to proceed with a multi-page résumé, or
       type `r` to go fix it. No y/n shortcut, so an accidental keystroke can never
       silently send an oversized résumé.
     - 1 page proceeds straight through.
   - `[a]bort` — cleans up the workdir and skips this recipient entirely.
3. **Domain/recipient dedup warnings** (never blocking, always shown when relevant):
   - **HARD WARN** — this exact recipient has already been contacted before (any
     campaign). Shows what campaign, current status, and whether they've replied.
   - **SOFT WARN** — a *different* person at the same company domain has already been
     contacted (skipped for common consumer providers like gmail.com — see
     `consumer_domains.txt` below).
   - Either warning prompts `Proceed anyway? [y/N]` — answer `n` to skip this recipient
     without staging anything.
   - If the SOFT WARN fires for a **static** (`latex.enabled: false`) campaign and
     `RESUME_ARCHIVE_DIR` has an archived résumé for that company, `send` also offers to
     **reuse** it instead of the campaign's default résumé — see "Résumé archive
     (optional)" below. `0` (or just pressing enter) declines and uses the default; this
     is an offer, never forced.
4. **Preview** — the exact rendered subject + body, the attachment name (or which archived
   résumé you chose to reuse, if you did), and the cadence about to be applied. Nothing is
   sent yet.
5. **`Stage this send? [y/N]`** — `y` writes a `queued` event and staged message data to
   `workdir/`; the recipient now sits in the queue until the runner (or `--now`) drains
   it. `n` discards this recipient.
6. **`Add another? [Y/n]`** — loops back to step 1 for the next recipient, or exits.

Add `--now` to also drain the queue immediately after staging, instead of waiting for
the next scheduled runner fire:

```bash
python slap.py send my-campaign --now
```

### Editing a template after staging

`send` freezes the rendered subject/body/stage text into the queue the moment you stage a
recipient (step 5 above) — editing `initial.txt`/`stageN.txt` afterward does nothing to
recipients already staged. Run `python slap.py template-reload` to re-render every
not-yet-sent recipient, across every campaign, against whatever the template files
currently say. It shows a summary (how many recipients would change, across which
campaigns, with sample diffs) and asks to confirm before writing anything.

This only ever works for recipients who haven't sent at all yet. The moment a recipient's
initial send actually fires, GMass has already locked in every follow-up stage's wording
for that campaign — there's no API call that can change it afterward, regardless of any
later local template edit. A recipient who's already had an initial send go out is simply
never touched by `template-reload`.

Two things can make one recipient un-reloadable without affecting anyone else in the same
run: they were staged before this feature existed (no raw drop values were kept for them
yet — re-stage them to fix this going forward), or the edited template now references a
field their stored drop doesn't have a value for. Either way they're left exactly as
staged and show up in the dashboard's **Template Failures** tab (only linked from the nav
when there's at least one).

**Before you ever point this at a real lead**, do exactly one real test send using the
`local+testmass1@domain` address `init` printed at the end of setup (a plus-tagged alias
of your own `sender.from_email` — Gmail delivers it straight to your own inbox). Run
through the full `send` flow above with that address as the `Email` field in your drop,
confirm it lands and looks right, then you're clear to send to real recipients.

## Commands

| Command | What it does |
|---|---|
| `python slap.py init` | Interactive installer (config.yaml, .env, schedule, DB, launchd). Re-runnable any time. |
| `python slap.py list` | Lists every auto-discovered campaign (persona, LaTeX on/off). |
| `python slap.py send <campaign> [--now]` | The prep flow above. `--now` also drains immediately. |
| `python slap.py dashboard` | Starts the localhost dashboard at `http://127.0.0.1:5050`, plus the filterable all-campaigns Reach-outs page at `/reachouts`. |
| `python slap.py doctor` | Preflight checks — sender fields, API key, DB, consumer domains file, every campaign's attachment/LaTeX toolchain, and (separately, never blocking) `RESUME_ARCHIVE_DIR`'s validity/dangling symlinks and whether Redis is reachable. Safe to run any time; the core checks also run automatically before every `send` and every drain. |
| `python slap.py domains` | Prints a read-only index of who you've contacted, grouped by email domain — for manual inspection. |
| `python slap.py rebuild` | Rebuilds the `recipients` cache table by replaying the full `events` log from scratch. Use this if the cache ever looks wrong — `events` is always the source of truth, the cache is fully disposable. |
| `python slap.py cleanup [--confirm] [--min-days-idle N]` | Deletes stale *compiled* résumé PDFs (LaTeX campaigns only) for recipients who are done/dead/never-replied and idle 15+ days by default — except a PDF still referenced by a live `RESUME_ARCHIVE_DIR` symlink, which is kept and reported separately. Dry run unless you pass `--confirm`. Never touches the `.tex` source. |
| `python slap.py runner` | The unattended drain — asks the DB what's queued and due, and sends it. Meant to be fired by **launchd** (see Scheduler below), not run by hand day-to-day. |
| `python slap.py sync` | Refreshes the dashboard's optional Redis-backed cache of GMass reply/click/bounce/block data. Meant to be fired hourly by **launchd**, not run by hand day-to-day. Fails loud (fast) if Redis isn't reachable; the dashboard just falls back to live polling either way. |
| `python slap.py plist [--job runner\|sync]` | Prints the launchd `.plist` for `runner` (default) or, with `--job sync`, the hourly cache-sync job — generated fresh from your current `config.yaml`. |
| `python slap.py template-reload` | Re-renders every not-yet-sent recipient across every campaign against whatever `initial.txt`/`stageN.txt` currently say — use this after editing a template you've already staged sends against. Shows a summary and sample diffs, asks to confirm before writing anything. Only ever touches recipients who haven't sent at all yet (see "Editing a template after staging" below); everyone else is untouched by definition. |
| `python slap.py bounced` | Prompts for a corrected address for a bounced/blocked recipient and resends. Same fix as the dashboard's own "Resend to corrected address" Reach-outs row action. |

Typical day-to-day flow: `send` a few recipients through the interactive prep loop →
either `--now` or let the scheduled `runner` pick them up → check `dashboard`
periodically for replies and to tag anything that needs a human decision.

## Résumé archive (optional)

By default, a résumé PDF only lives inside `workdir/<campaign>/<recipient>/` (LaTeX
campaigns) or `campaigns/<name>/` (static campaigns) — there's no single place to browse
"every résumé I've ever sent." Set `RESUME_ARCHIVE_DIR` in `.env` to a folder path to turn
that on:

```
RESUME_ARCHIVE_DIR=/Users/you/slap-resume-archive
```

Every time a recipient is staged, `send` drops a **symlink** (never a copy) into that
folder pointing at the real PDF, named `<company>-<role>-<date>.pdf` (slugified, date =
the day it was staged). Symlinks, not copies, so there's still exactly one real copy of
each PDF's bytes on disk — the archive is just a browsable index into files that already
exist. Re-staging the same recipient doesn't create a duplicate; two different recipients
that land on the same name (same company/role/day) get `-2`, `-3`, ... appended.

- **Unset, or pointing at a folder that doesn't exist / isn't writable → archiving is
  simply skipped, with a warning** — it never blocks a send. `doctor` reports
  `RESUME_ARCHIVE_DIR`'s status and flags any dangling symlink inside it (e.g. after a
  `cleanup` run reclaimed the file it pointed at) separately from every other check, so a
  stale archive folder can never fail a `send` or a scheduled drain.
- **`cleanup` respects the archive**: a PDF `cleanup` would otherwise delete as
  stale/dead is kept instead if a live archive symlink still points at it, and reported in
  its own "kept — still referenced by a résumé archive symlink" line rather than being
  silently deleted out from under the archive.
- If your campaign's `fields` don't include a field with key `company` and/or
  `role_catted`, the archive filename just ends up missing that part (with a warning
  printed) rather than failing — name your fields to match if you want fully descriptive
  archive filenames.

### Résumé reuse (static campaigns only)

With the archive on, `send` offers something extra when the domain SOFT WARN fires (see
"The send flow" above): a numbered choice of every archived résumé matching that
company, to reuse for the new recipient instead of the campaign's usual
`attachment_file`.

- Only offered for **static** (`latex.enabled: false`) campaigns — there's no LaTeX
  paste/compile loop to skip cleanly for a LaTeX campaign, so this doesn't apply there.
- The default answer (`0`, or just pressing enter) is **"use the default resume"** — this
  is an offer, not a nudge toward reusing one.
- Picking one resolves the archive symlink to its real file, validates it's still a real,
  non-empty, readable PDF, and copies (never symlinks) it into the new recipient's own
  workdir — so it stays correct no matter what `cleanup` later does to the *original*
  recipient's files. A broken pick (the archived file went missing or is unreadable)
  fails loud for that one recipient only; it never aborts the rest of the batch.
- The preview says so plainly (`Attachment: reused from <archived-filename>.pdf`) instead
  of silently swapping in a different file than what the campaign normally sends. The
  reused résumé still gets its own fresh archive entry under the new send's own
  company/role/date — two people who got the same résumé content produce two archive
  entries, one per actual send.

## Dashboard + replies

```bash
python slap.py dashboard
```

Opens `http://127.0.0.1:5050`. Five pages share one header/nav bar, a light/dark theme
toggle (top right — cycles Auto → Light → Dark, remembers your choice), and, only when
the most recent `template-reload` run left at least one recipient un-reloaded, a
**"Template Failures"** nav link listing who, in which campaign, and why (see "Editing a
template after staging" above) — it disappears again once a later `template-reload` run
comes back clean.

Every page also shares a **sync/cache-status banner** just below the nav: the last time
the Redis-backed GMass cache was refreshed, a quick tally of what's new since then
(replies/clicks/bounces), and — if the cache is stale or Redis is unreachable — a
"cache unavailable / stale, refreshing" note. A **"Refresh now"** button forces an
immediate re-sync instead of waiting for the next hourly `sync` run. If you don't run the
optional `sync` job at all, this banner just reflects a live poll made on that page open,
the same as before the cache existed.

**Home** (`/`) — the operational pulse, and the one thing that needs a same-visit decision:

- **Metrics** — today/this-week send counts (initial vs. follow-up split), plus a
  daily-cap gauge.
- **Replies needing triage** — every reply that hasn't been tagged yet, with prior-contact
  domain context. Tag each one:
  - **real** — a genuine reply. Pure bookkeeping; no further action from slap.
  - **OOO** — an out-of-office auto-reply. This is the one tag with real consequences:
    it queues slap's own resend of the recipient's *next* stage, sent as a threaded reply
    (`sendAsReply`) on the normal runner cadence — deterministic threading, not reliant on
    GMass's own conversation auto-detection. (GMass usually filters real auto-responders
    itself; this is a manual safety net for the ones that slip through.)
  - **not interested** — pure bookkeeping; stops the row from showing as needing triage.
  - **unreal** — Reach-outs-only (see below), for a Real-tagged reply that later went
    cold. Not offered here since a recipient only reaches this widget while their reply
    is still untagged.
- **Next drain** — when the runner is next scheduled to fire.
- **Today's runs** — each drain that actually did something today (fired, sent, failed
  counts) — a drain that found nothing queued is omitted as noise.

**Pipeline** (`/pipeline`) — the live-recipient work queue, always instant (nothing here
depends on GMass's own report data):

- **Active leads** — a roster of every recipient currently tagged Real: company,
  campaign, persona, and when you marked them real. This is a live "who's actually a
  real opportunity right now" list, separate from the triage widget above (which is
  about *tagging* a reply, not tracking it afterward).
- **Follow-up reminders** — the same Active Leads recipients, but framed as a nag: how
  many days since you marked each one Real, worst-first. Once someone's marked Real,
  GMass's own automated follow-ups have already stopped for them (it stops firing on any
  reply) — this is the one place that reminds you to personally follow up, since nothing
  else on the dashboard tracks that.
- **Pipeline** — who's mid-sequence at which stage, and what's scheduled to fire today/tomorrow.
- **Companies contacted** — a rollup by company domain.

**Engagement** (`/engagement`):

- **Engagement intelligence** — reply rate by persona, replies/clicks by stage, and a
  time-to-first-reply distribution.
- **Warm but silent — clicked, no reply** — the highest-value signal on the whole
  dashboard: someone opened a tracked link but hasn't replied yet. The message landed and
  was read; it's just unanswered. Worth a manual nudge.

**Deliverability** (`/deliverability`) — why a recipient stopped moving:

- **Bounces & blocks** — every delivery failure GMass reports back, tagged **Bounced** or
  **Blocked**. GMass tracks these as two separate categories (different report endpoints,
  different reasons) — both are shown here rather than one blended into the other, so a
  recipient whose mail got blocked by a spam filter doesn't look identical to one whose
  address just doesn't exist. A **Reason** column shows GMass's actual reason text for
  each one (truncated in the table; hover for the full text) — no more guessing why
  something bounced from the Bounced/Blocked label alone. The same reason text shows up
  next to a recipient's status on the Reach-outs page too.
- **Stopped outreach** — every recipient you've permanently halted follow-ups to (see
  **Stop outreach** under Reach-outs below), with company/campaign/when. A roster, not a
  reply-tag view — a stopped recipient can independently still be tagged Real elsewhere;
  this just tracks the stop itself.

## Reach-outs (all campaigns, filterable)

```
http://127.0.0.1:5050/reachouts
```

One row per recipient across every campaign, filterable and sortable, for when you want
to slice "everyone I've contacted" by whatever you care about that day instead of hunting
through per-campaign panels. Never makes a GMass call on its own — everything here is
already-synced local data.

Each row has a single **⋯** button on the right — click it to open that row's action
menu, rather than a wall of always-visible buttons. Inside, as applicable: **Mark OOO**
(always available), **Real / Not interested** (only once that recipient has actually
replied), **Resend to corrected address** (only on a bounced row), and two more:

- **Unreal** — shown only once a recipient is already tagged Real (sits right next to
  the Real/Not interested items). Use it when a Real lead goes cold later — the deal
  fell through, they stopped responding, whatever. It's local-only: no GMass call, no
  suppression, nothing sent. The recipient drops out of the Active Leads / Follow-up
  reminders widgets on the Pipeline page, but their reply-tag history isn't erased —
  it's recorded as its own event, same as every tag change here.
- **Stop outreach** — offered until the row is already stopped. Permanently halts further
  follow-ups to *this one recipient* (e.g. you got rejected for the specific role they
  were contacted about). This is a real, irreversible suppression — same account-wide
  GMass unsubscribe Mark OOO/Not interested use — so it asks for confirmation first, the
  same way those do. Once stopped, the row shows a **Stopped** chip and the menu item
  disappears (nothing left to stop); the recipient also drops out of the follow-up-
  reminder/Active-Leads widgets and shows up instead on the Deliverability page's Stopped
  outreach roster. This only ever affects the one recipient you clicked it on — it does
  NOT stop the whole campaign or every contact at that company.

Filter controls (all combine with AND — narrowing by campaign AND status AND date range,
for instance, not any of them):

- **Campaign, persona, domain** — exact-match dropdowns, built from your actual data.
- **Status** — `queued` (staged, nothing sent yet), `active` (sent at least once, still
  mid-sequence), `done`, `replied`, `bounced`, `ooo_requeued`.
- **Engagement** — replied / clicked-no-reply / no engagement yet.
- **Reply tag** — real / unreal / OOO / not-interested / untagged (untagged means
  "replied, pending triage" — someone who's never replied at all just won't match any of
  these).
- **Req ID** — present vs. blank.
- **Date range** — two date pickers; matches whichever of "first sent" or "last event"
  a recipient actually has (a queued-but-never-sent recipient still gets a date).
- **Search** — free text across recipient email and company name.

A count line ("N of M reach-outs shown") tracks the current filter. Filtering and sorting
happen instantly in the browser — no page reload, no extra GMass calls; the page only
reads local data already synced.

**Company and Req ID columns can show blank** for recipients staged before this page's
underlying data capture existed — never guessed, just genuinely unknown for older sends.

## Scheduler (launchd)

The unattended `runner` is fired by **macOS launchd**, not cron — cron does not catch up
if your Mac was asleep at the scheduled time; a launchd `StartCalendarInterval` LaunchAgent
does (it fires as soon as the Mac wakes, if it missed the exact moment).

Install (also shown by `init`'s step 8):

```bash
python slap.py plist > ~/Library/LaunchAgents/com.slap.runner.plist
launchctl load ~/Library/LaunchAgents/com.slap.runner.plist
```

Any time you change `config.yaml`'s `schedule.active_days` or `fire_window_start`,
regenerate and reload (unload, then load again) — see [`LAUNCHD.md`](LAUNCHD.md) for the
exact steps and a one-time wake-test checklist (this behavior can only be verified on real
hardware, not in a test suite).

Knobs, all in `config.yaml`'s `schedule:` block:

- **`fire_window_start` / `fire_window_end`** — the runner rolls a random moment inside
  this window each day, rather than firing at one fixed second every day.
- **`active_days`** — which weekdays the runner is allowed to drain (e.g. skip weekends).
  Enforced twice: the generated plist only has entries for these days, AND the runner
  re-checks `active_days` itself at drain time — so it stays correct even if you edit
  `config.yaml` without regenerating/reloading the plist yet. **Only covers SLAP's own
  local initial sends** — GMass's own follow-up stages fire server-side regardless (see
  `gmass.allowed_days` below).
- **`daily_cap`** — a hard ceiling on sends per day (initial + follow-ups combined); the
  drain stops here and overflow simply stays queued for the next run.
- **`send_delay_min` / `send_delay_max`** — seconds to randomly wait between individual
  sends during a drain.
- **`drain_retries`** — how many times a drain-time preflight failure (e.g. a missing API
  key) retries before giving up loud for that run; a per-recipient send failure is not
  affected by this and simply retries on the next scheduled drain.

Separately, `config.yaml`'s `gmass:` block has its own **`allowed_days`/`skip_holidays`**
pair — unlike everything above, these reach GMass's own server-side follow-up scheduler,
not just SLAP's local runner. See [`LAUNCHD.md`](LAUNCHD.md) for the full detail.

**Your Mac needs to be on and logged in (sleep is fine, fully shut down is not) at some
point during the fire window** — launchd can wake a sleeping Mac for a scheduled job, but
can't run anything if the machine is powered off or the user isn't logged in.

## Tips

- **Keep daily volume low.** Cold outreach from a personal Gmail account has real
  deliverability risk — `init` defaults `daily_cap` to 50 for a reason. Ramping up too
  fast is how a personal Gmail account gets flagged.
- **Pick the persona that matches who you're actually emailing.** Cadences are fixed per
  persona (`config.yaml`'s `personas:` block) specifically so a recruiter, a hiring
  manager, and a founder each get a follow-up rhythm suited to how they actually work —
  don't reuse one persona for every audience just because a campaign already exists.
- **Click tracking is what makes "warm but silent" possible** — it depends on the message
  actually being sent as HTML with a real link in it. If that dashboard panel is always
  empty, first check that clicks are showing up at all in **Engagement intelligence**.
- **Mind the dedup warnings.** A HARD WARN (exact recipient, already contacted) is worth
  reading every time before you proceed anyway — it's not just noise. A SOFT WARN (same
  company domain, different person) is often fine, but useful context before you send.
- **`consumer_domains.txt`** lists domains excluded from the SOFT WARN (gmail.com,
  outlook.com, etc. — many unrelated people legitimately share these). Edit it directly if
  you want to add or remove providers; `doctor` seeds a sensible default list if it's ever
  missing.
