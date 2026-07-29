# USAGE.md — using slap day to day

This assumes you've already run `python slap.py init` (see [`README.md`](README.md)),
built the dashboard's frontend once (`npm --prefix slap/frontend install && npm --prefix
slap/frontend run build` — see README's Setup), and `doctor` passes. This doc covers
everything after that: creating campaigns, writing drops, sending, the dashboard, the
scheduler, and deliverability tips.

**The dashboard is a React + TypeScript + Vite single-page app**, not server-rendered
HTML — `slap.py dashboard` still starts exactly one localhost Flask process, but that
process now serves a pre-built static bundle plus a JSON API (`/api/*`) instead of
rendering Jinja templates. Nothing about the CLI, the queue, or the runner changed; only
the dashboard's own presentation layer did. See "Dashboard + replies" below for the new
tab layout, and [`ARCHITECTURE.md`](ARCHITECTURE.md)'s "Frontend" section for the full
design rationale.

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

The easiest way to create one is the interactive wizard:

```bash
python slap.py onboard-campaign
```

It walks you through: a campaign folder name, picking a persona (which fixes how many
follow-ups you'll author, since a persona's cadence length is fixed), composing the
initial email, declaring its variables (auto-detected from `{{key}}` placeholders as you
paste — you're only asked to label/optional-flag ones it doesn't already know), composing
exactly as many follow-ups as the persona's cadence needs, a review panel showing the
whole template set with every placeholder highlighted, then the rest of `campaign.yaml`
(LaTeX on/off, attachment filename, and — for a static campaign — a path to a real résumé
PDF, or a scaffolded placeholder if you don't have one ready yet). Nothing is written to
disk until you confirm the review. The result is validated the same way `send`/`doctor`
validate any other campaign before it declares success.

`onboard-campaign` only scaffolds this app-facing half — the config and templates
`slap.py` reads. It has no opinion on how you produce the real per-recipient values that
fill those templates (a tailored résumé, this campaign's seed-drop field values,
sometimes a LinkedIn DM) — that's a separate step, done by pasting a generation prompt
into a Claude conversation once per recipient. If you're working in Claude Code,
`prompt-store/CLAUDE.md` holds the reusable template for writing that generation prompt
for a new campaign, plus a browsable symlink index (`prompt-store/<name>.md`) of every
campaign's own `campaigns/<name>/prompt.md`.

The rest of this section describes the folder shape by hand, for reference or if you'd
rather write the files yourself:

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
<<<EOF>>>
```

- Each line is split on the **first colon only** (`Role: Backend: Infra` → key `Role`,
  value `Backend: Infra` — colons after the first are preserved in the value).
- Exactly one space after the colon is stripped; everything else is kept as-is (so a
  leading space for an inline field like `Req ID:  (Req #4521)` above survives).
  Matching against a field's `key` or `label` is case-insensitive.
- Lines with no colon are ignored; unknown keys are ignored; a field missing from the
  drop entirely defaults to an empty string.
- Type `<<<EOF>>>` on its own line to end the paste — this works the same whether you're
  typing live or pasting a multi-line block from your clipboard. (The terminator is
  deliberately an unusual token, not a bare `EOF`, so it can't collide with a stray `EOF`
  line that happens to appear inside a real drop or résumé paste.)
- A declared field left empty prints a warning (`⚠ empty fields: req_id`) right before
  the preview — this is **informational only, it never blocks the send**, since some
  fields (like `req_id`) are legitimately blank often.

## The send flow

```bash
python slap.py send my-campaign
```

1. **Paste the drop** (above), terminated with `<<<EOF>>>`.
2. **If `latex.enabled: true`**: paste the LaTeX `.tex` source next (also terminated with
   `<<<EOF>>>`). It compiles, opens the PDF in Preview and the `.tex` in `code`, then drops you
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
4. **`Follow-ups for this recipient? [0-N, default N]`** — per-recipient cadence override.
   Press enter to keep the persona's full cadence (the default), or type a smaller number
   (down to `0`, meaning only the initial email goes out, no follow-ups at all) if this
   particular recipient doesn't warrant the persona's usual follow-up intensity. This
   truncates the persona's cadence to a PREFIX — there's no way to skip stage 2 but keep
   stage 3, since a cadence is an ordered sequence of day-offsets, not independently
   pickable stages.
5. **Preview** — the exact rendered subject + body, the attachment name (or which archived
   résumé you chose to reuse, if you did), and the cadence that will actually be staged
   (reflecting your answer to the previous prompt). Nothing is sent yet.
6. **`Stage this send? [y/N]`** — `y` writes a `queued` event and staged message data to
   `workdir/`; the recipient now sits in the queue until the runner (or `--now`) drains
   it. `n` discards this recipient.
6. **`Add another? [Y/n]`** — loops back to step 1 for the next recipient, or exits.

Add `--now` to also drain the queue immediately after staging, instead of waiting for
the next scheduled runner fire:

```bash
python slap.py send my-campaign --now
```

Whether triggered by `--now` or the scheduled `runner`, a drain prints one progress line per
recipient as each send resolves (`[i/N] recipient (campaign) -> sent`/`FAILED`) instead of
staying silent until a single summary line at the end — useful since a big batch is throttled
10-15s between sends and can otherwise look stuck for minutes.

### `send custom` — a one-off, editor-authored send

For a genuine one-off message that doesn't warrant a whole `campaigns/<name>/` folder
(no reusable template, maybe no follow-ups at all, or a custom cadence that doesn't match
any persona), use:

```bash
python slap.py send custom
```

`custom` is a reserved campaign token — a real `campaigns/custom/` folder would be
shadowed by it, so don't name a real campaign `custom`. The flow:

1. **Recipient email**, plain prompt.
2. **Initial email**, authored in your configured `editor` (`config.yaml`'s `editor:` key,
   default `"code --wait"` — must be a command that *blocks* until the editor window
   closes, which is exactly why the default has `--wait`; a GUI editor that returns
   immediately would have `slap` read back stale content). The file opens pre-seeded with
   `Subject: \n\n` — same `Subject:` line + blank-line-separator shape every campaign's
   `initial.txt` already uses.
3. **Optional custom follow-ups** — for each one you choose to add, you're asked how many
   days after the previous message it should fire, then your editor opens for that stage's
   body (no subject line, threaded as a reply, like any other stage file). This becomes
   the literal cadence staged for this recipient — there's no persona involved, so there's
   nothing to truncate or default.
4. **Attachment** — pick one of four modes:
   - Pick a PDF already sitting in this send's own working folder.
   - Paste an absolute path to a PDF elsewhere on disk.
   - Write a LaTeX résumé right now (the same compile loop + the app's one hard gate, a
     forced confirmation past one page).
   - No attachment at all.
5. **Dedup warnings** (hard/soft), a preview, and a `Stage this custom send? [y/N]`
   confirm — same shape and same warn-don't-block behavior as a normal `send`.

A custom send is staged and drained through the exact same queue/runner machinery as any
other campaign, tagged internally with a reserved campaign label (`__custom__`) that never
shows up in `slap.py list`/campaign auto-discovery and gets a neutral grey identity color
on the dashboard rather than a random one (see ARCHITECTURE.md). `--now` works the same
way it does for a normal `send`.

`doctor` checks your configured `editor` command is on `PATH`, but only as an
informational, non-gating check — a missing editor should never block an ordinary send or
scheduled drain that never needed one. `send custom`/`remind --new` fail loud up front if
it isn't usable.

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
| `python slap.py send custom [--now]` | The editor-authored one-off flow above (see "`send custom`"). |
| `python slap.py dashboard` | Starts the localhost dashboard at `http://127.0.0.1:5050` — a React SPA (see "Dashboard + replies" below), including the filterable all-campaigns Reach-outs tab. Requires the frontend to be built once first (`npm --prefix slap/frontend run build`). |
| `python slap.py doctor` | Preflight checks — sender fields, API key, DB, consumer domains file, every campaign's attachment/LaTeX toolchain, and (separately, never blocking) `RESUME_ARCHIVE_DIR`'s validity, any dangling symlinks in it, and your configured `editor` command. Safe to run any time; the core checks also run automatically before every `send` and every drain. |
| `python slap.py domains` | Prints a read-only index of who you've contacted, grouped by email domain — for manual inspection. |
| `python slap.py rebuild` | Rebuilds the `recipients` cache table by replaying the full `events` log from scratch. Use this if the cache ever looks wrong — `events` is always the source of truth, the cache is fully disposable. |
| `python slap.py cleanup [--confirm] [--min-days-idle N]` | Deletes stale *compiled* résumé PDFs (LaTeX campaigns only) for recipients who are done/dead/never-replied and idle 15+ days by default — except a PDF still referenced by a live `RESUME_ARCHIVE_DIR` symlink, which is kept and reported separately. Dry run unless you pass `--confirm`. Never touches the `.tex` source. |
| `python slap.py runner` | The unattended drain — asks the DB what's queued and due, and sends it. Meant to be fired by **launchd** (see Scheduler below), not run by hand day-to-day. |
| `python slap.py plist` | Prints the launchd `.plist` for `runner`, generated fresh from your current `config.yaml`. |
| `python slap.py template-reload` | Re-renders every not-yet-sent recipient across every campaign against whatever `initial.txt`/`stageN.txt` currently say — use this after editing a template you've already staged sends against. Shows a summary and sample diffs, asks to confirm before writing anything. Only ever touches recipients who haven't sent at all yet (see "Editing a template after staging" below); everyone else is untouched by definition. |
| `python slap.py interaction <recipient> --channel {linkedin-reply,followed-up} [--off]` | Terminal counterpart to two dashboard toggles: `--channel linkedin-reply` records (or, with `--off`, clears) "this recipient replied on LinkedIn"; `--channel followed-up` resets the follow-up-reminder timer, same as clicking "Followed up" on Home. Both just append an `interaction` event — no GMass call either way. |
| `python slap.py remind [<recipient>] [--list] [--use SLUG] [--new] [--title T]` | Queue a one-shot follow-up ("Remind") for a warm-but-silent, LinkedIn-replied, or already-real recipient — sent as a threaded reply on the next ordinary drain. `--list` shows saved templates under `followups/`; `--use SLUG` sends a saved one verbatim; `--new` opens your editor to author a fresh body; add `--title` to also save what you authored for reuse later. See "Remind" under Reach-outs below. |

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

Opens `http://127.0.0.1:5050`. The dashboard is a React SPA now (see the note at the top
of this doc) — same one Flask process, same localhost-only, same underlying SQLite reads,
but a different page layout than a Jinja-era screenshot might show. First visit shows a
full-screen splash (dismiss by clicking Continue or pressing any key — remembered in your
browser via `localStorage`, so it only shows once per browser). A light/dark theme toggle
sits top-right (cycles Auto → Light → Dark, remembers your choice). Six tabs share one
nav bar — **Home, Campaigns, Engagement, Pipeline, Reach-outs, Commands** — plus two
footer links, **Logs** and, only when the most recent `template-reload` run left at least
one recipient un-reloaded, **Template failures** with a count badge (see "Editing a
template after staging" above) — the link itself disappears again once a later
`template-reload` run comes back clean.

**Home** (`/`) — the operational pulse, and the one thing that needs a same-visit decision:

- **Today** / **This week** — send counts (new vs. follow-up split), replies, clicks, and
  a daily-cap gauge.
- **Replies needing triage** — every reply that hasn't been tagged yet, with prior-contact
  domain context. Tag each one:
  - **Real** — a genuine reply. Pure bookkeeping; no further action from slap.
  - **OOO** — an out-of-office auto-reply. This is the one tag with real consequences:
    it queues slap's own resend of the recipient's *next* stage, sent as a threaded reply
    (`sendAsReply`) on the normal runner cadence — deterministic threading, not reliant on
    GMass's own conversation auto-detection. (GMass usually filters real auto-responders
    itself; this is a manual safety net for the ones that slip through.)
  - **Not real** — pure bookkeeping; stops the row from showing as needing triage.
  - **Unreal** — Reach-outs-only (see below), for a Real-tagged reply that later went
    cold. Not offered here since a recipient only reaches this widget while their reply
    is still untagged.
- **Follow-up reminders** — every recipient currently tagged Real, framed as a nag: how
  many days since you marked each one Real. Once someone's marked Real, GMass's own
  automated follow-ups have already stopped for them (it stops firing on any reply) — this
  is the one place that reminds you to personally follow up. Click **Followed up** to
  reset the timer (same as `slap.py interaction <recipient> --channel followed-up`) —
  it records a fresh `interaction` event; the reminder starts counting again from zero.
- **Pipeline summary** and **Companies contacted** — the same live-queue numbers the
  Pipeline tab shows in full, kept on Home too so they're never buried behind
  lagging-indicator analytics.

**Campaigns** (`/campaigns`) — per-campaign analytics, one filter pill per
auto-discovered campaign (plus "All campaigns"). Each pill is tinted with that campaign's
deterministic identity color (see ARCHITECTURE.md's "Per-campaign color" — a stable hash
of the campaign name, never stored, never changes unless you rename the campaign). Shows
recipient/reply/click/active-lead counts for the selected campaign (or the combined
total), plus that slice's **Active leads marked real** roster.

**Engagement** (`/engagement`) — reply-rate/engagement intelligence and the dashboard's
charts, together on one tab:

- **Reply rate by persona** — as a stat row and, further down, the same data as a bar
  chart.
- **Warm but silent — clicked, no reply** — the highest-value signal on the whole
  dashboard: someone opened a tracked link but hasn't replied yet. The message landed and
  was read; it's just unanswered. Each row has a **Remind** button — see "Remind" under
  Reach-outs below — plus **Hide**/**Unhide** if you don't want a particular recipient
  nagging you here (a "show hidden (N)" toggle reveals what you've hidden).
- **Send / reply trend** (30 days) — a line chart of daily new-sends, follow-up-sends, and
  replies.
- **Bounce & block breakdown**, **Reply rate by persona (chart)**, **Time to first
  reply** — chart versions of the same underlying data shown elsewhere as plain numbers.
- **Weekly goal pacing** — only shown if you've set `schedule.weekly_target` in
  `config.yaml`: a progress gauge for new-recipient sends against your own weekly target.
  Omit the config key to hide this widget entirely.

**Pipeline** (`/pipeline`) — the live-recipient work queue, with deliverability folded in:

- **Mid-sequence, by current stage** and **Follow-ups firing today / tomorrow** — who's
  where in a cadence, and what's about to fire.
- **Active leads** and **Follow-up reminders** — the same rosters Home surfaces, shown in
  full here.
- **Companies contacted** — a rollup by company domain.
- **Bounces & blocks (deliverability)** — every delivery failure GMass reports back,
  tagged **Bounced** or **Blocked** (GMass tracks these as two separate report
  categories with their own reason text — both are shown here rather than blended
  together). The reason text is also visible next to a recipient's status on Reach-outs.
- **Stopped outreach roster** — every recipient you've permanently halted follow-ups to
  (see **Stop outreach** under Reach-outs below), with company/campaign/when. A roster,
  not a reply-tag view — a stopped recipient can independently still be tagged Real
  elsewhere; this just tracks the stop itself.

**Commands** (`/commands`) — a live reference of every `slap.py` subcommand (name, usage,
flags, help text), generated directly from the CLI's own `argparse` definition
(`/api/commands`) rather than hand-maintained — a new subcommand shows up here
automatically the next time you load the page, and this tab can never drift from the real
CLI surface.

**Logs** (footer link, `/logs`) — every event slap has ever recorded, in one place:

- **Events** — every `queued`/`draft_created`/`sent`/`send_failed`/`run_started`/
  `run_completed`/`run_failed`/`click`/`reply`/`bounce`/`interaction`/etc. event, newest
  first, filterable by type/recipient/campaign and free-text search, sortable by clicking
  any column header. Expand a row to see its raw event data (GMass draft/campaign IDs,
  full meta). This is a direct, unfiltered view over the exact same append-only event log
  every other tab's widgets are derived from — nothing here is a separate log store.
- **Raw job output** — the last 200 lines of `runner.log`/`runner.err.log`/`sync.log`/
  `sync.err.log` (the scheduled jobs' actual stdout/stderr, launchd writes these — see
  Scheduler below), newest first. This is the one place a *launchd-level* failure would show
  up — a crash before `slap.py` ever gets far enough to write an event at all — so it's worth
  checking here first if a scheduled run doesn't seem to have happened.

## Reach-outs (all campaigns, filterable)

```
http://127.0.0.1:5050/reachouts
```

One row per recipient across every campaign, sortable and free-text searchable, for when
you want to slice "everyone I've contacted" instead of hunting through per-campaign
panels. Never makes a GMass call on its own — everything here is already-synced local
data. Each row's left edge is tinted with that recipient's campaign's identity color (see
ARCHITECTURE.md's "Per-campaign color") — a quick visual grouping cue without a filter
dropdown.

Click any column header (Recipient/Campaign/Persona/Status/Date) to sort by it, and again
to flip direction. The search box matches recipient email, company, name, domain,
campaign, and persona in one field. A count line ("N of M") tracks the current search.
Both happen instantly in the browser — no page reload, no extra GMass calls.

A dedicated **LinkedIn** column shows a toggle button — click it to mark (or, clicking
again, unmark) that this recipient replied to you on LinkedIn (records an `interaction`
event; no GMass call, since GMass has no visibility into LinkedIn). Same action as
`slap.py interaction <recipient> --channel linkedin-reply`.

Each row also has a single **⋯** button on the right — click it to open that row's
floating action menu (it repositions itself to stay on-screen regardless of where the row
lands in the viewport), rather than a wall of always-visible buttons. Inside, as
applicable: **Mark OOO…** (always available, opens a small date picker for when the
recipient is expected back), **Resend to corrected address…** (only on a bounced row),
**Tag real**, **Tag not interested**, and:

- **Stop outreach** — offered until the row is already stopped. Permanently halts further
  follow-ups to *this one recipient* (e.g. you got rejected for the specific role they
  were contacted about). This is a real, irreversible suppression — same account-wide
  GMass unsubscribe Mark OOO/Not interested use — so it's a distinct, visually separated
  ("danger") menu item. Once stopped, the row shows a **Stopped** chip and the menu item
  disappears (nothing left to stop); the recipient also drops out of the follow-up-
  reminder/Active-Leads widgets and shows up instead on Pipeline's Stopped outreach
  roster. This only ever affects the one recipient you clicked it on — it does NOT stop
  the whole campaign or every contact at that company.

**Remind** — a one-off follow-up to a warm-but-silent, LinkedIn-replied, or already-real
recipient, sent as a threaded reply on the next ordinary drain (the same OOO-resend
mechanism the app already had, reused rather than duplicated — no scheduler of its own).
The **Remind** button currently lives on Engagement's "Warm but silent" rows; the CLI
equivalent, usable for any eligible recipient, is `slap.py remind <recipient> [--use SLUG
| --new [--title T]]` (see "Commands" above). Saved templates live under a project-root
`followups/` directory and are listed with `slap.py remind --list`.

**Company columns can show blank** for recipients staged before this page's underlying
data capture existed — never guessed, just genuinely unknown for older sends.

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
  `config.yaml` without regenerating/reloading the plist yet.
- **`daily_cap`** — a hard ceiling on sends per day (initial + follow-ups combined); the
  drain stops here and overflow simply stays queued for the next run.

**Your Mac needs to be on and logged in (sleep is fine, fully shut down is not) at some
point during the fire window** — launchd can wake a sleeping Mac for a scheduled job, but
can't run anything if the machine is powered off or the user isn't logged in.

**To check whether a scheduled run actually fired**, open the dashboard's **Logs** page
(`/logs`) — it shows every `run_started`/`run_completed`/`run_failed` event alongside the raw
`runner.log`/`runner.err.log` tails, so you don't have to go find and open those files by hand.

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
