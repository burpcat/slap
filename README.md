# slap

A personal cold job-outreach CLI over the [GMass](https://www.gmass.co/) API. `slap`
fills an email template from a pasted "drop" (line-by-line `key: value`), optionally
compiles a pasted LaTeX résumé and attaches it, then sends via GMass — which relays
through your own Gmail account and runs the follow-up cadence on GMass's servers.
Everything sent is tracked in local SQLite, with a localhost dashboard for status, reply
triage, and an all-campaigns filterable Reach-outs view.

Single-owner, personal-use tool. Not multi-tenant, not a SaaS product.

Each user runs their own install against their own GMass account — there's no shared
server or shared data. See [`USAGE.md`](USAGE.md) for the full day-to-day guide once
you're set up.

## Requirements

- macOS (the unattended runner is scheduled via **launchd**, and the LaTeX loop shells
  out to macOS's `open -a Preview`).
- Python 3.11+.
- Your **own** [GMass](https://www.gmass.co/) account with an API key, connected to
  your **own** Gmail account (the one you want to send from). Follow-up cadences (stage
  2/3 emails) need GMass Premium.
- If you plan to use LaTeX-compiled résumés (`latex.enabled: true` in a campaign):
  [MacTeX](https://www.tug.org/mactex/) (for `xelatex`) and the
  [`code`](https://code.visualstudio.com/docs/editor/command-line) CLI (VS Code), both
  on your `PATH`. `slap.py doctor` checks for both — see below.

## Setup

```bash
git clone <this-repo>
cd slap
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # includes requirements.txt + pytest
```

Then run the interactive installer:

```bash
python slap.py init
```

`init` is the entry point for everything else in this section — it's re-runnable any
time (it asks before overwriting anything real) and walks through:

1. **Preflight** — checks python3/venv/macOS/`xelatex`/`code`, printing exact install
   instructions for anything missing. Nothing is auto-installed; LaTeX/`code` are only
   required if you turn on a LaTeX campaign.
2. **Sender** — writes `config.yaml` and asks for your Gmail address and name.
   **Your `from_email` must be the exact Gmail account your GMass API key is connected
   to** — GMass sends by relaying through that account.
3. **GMass key** — writes `.env` and asks for your API key (never echoed back in full).
   Confirms `.env` is gitignored.
4. **Owner test-guard** — confirms that self-tests are now guarded to
   `you+testmass{N}@yourdomain`, derived from step 2 — never a hardcoded address, and
   never overridable.
5. **Schedule** — fire window, daily send cap (defaults to a conservative 50/day — keep
   cold-outreach volume low), and which weekdays the unattended runner is allowed to fire.
6. **First campaign (optional)** — offers to scaffold an example campaign folder to edit.
7. **Database** — creates an empty `slap.db` if one doesn't exist.
8. **Launchd** — prints the generated `.plist` and the exact `cp`/`launchctl load`
   commands to install the unattended runner (see [`LAUNCHD.md`](LAUNCHD.md) for the
   one-time wake-test — this can only be verified on real hardware).
9. **Finish** — runs the same checks as `doctor`, prints your safe self-test address, and
   mentions the optional résumé archive (see below).

**Before sending anything real**, test-send to yourself using the `local+testmass1@domain`
address `init` prints at the end — this guarantees a test send can only ever reach your
own inbox, never a real lead, no matter what campaign you point it at.

**Also fill in `config.yaml`'s `signature:` key** — every campaign template ends with
`{{signature}}`, filled from this one place instead of being hardcoded per template.
`init` doesn't prompt for it, so a freshly-scaffolded `config.yaml` only has the
`config.yaml.example` placeholder text (`<Your Name>` / `<link>`) — edit it before your
first real send, or that placeholder is exactly what goes out. The key must be present
(an empty string `""` is a valid, deliberate "no signature" — just not a missing key).

`doctor` (step 9, and re-runnable any time via `python slap.py doctor`) verifies:
`GMASS_API_KEY` is set, `config.yaml`'s sender fields are filled in, the SQLite tracking
DB is reachable, and `consumer_domains.txt` is present (it seeds the default
consumer-email-provider list automatically if missing — the only check here that writes
anything; everything else only checks, never installs). It then validates every campaign
under `campaigns/` and reports pass/fail per check, exiting non-zero if anything needs
attention.

## Setting up a campaign

Campaigns are auto-discovered: any folder under `campaigns/` with a valid
`campaign.yaml` is live. No registry to update.

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
persona: recruiter                # -> derives the fixed cadence from config.yaml
latex:
  enabled: true                   # false -> use the static attachment_file instead
  attachment_name: "Firstname_Lastname_Resume.pdf"   # filename the recipient sees
attachment_file: resume.pdf       # used only when latex.enabled is false
fields:
  - { key: email,        label: Email }
  - { key: role_catted,  label: Role }
  - { key: company,      label: Company }
  - { key: req_id,       label: Req ID, optional: true }
```

Every field except `email` is optional per-campaign, but at least one field with
`key: email` is required — `send` needs it to know who to mail. Body files (`initial.txt`,
`stageN.txt`) use `{{key}}` placeholders filled from the pasted drop; a field marked
`optional: true` that's empty drops its whole line instead of leaving a gap.

Every template can also end with `{{signature}}` — filled from `config.yaml`'s
`signature:` key (one place, shared by every campaign), not a per-campaign `fields`
entry. No campaign.yaml declaration needed for it.

The number of `stageN.txt` files must exactly match the chosen persona's cadence length
(e.g. `recruiter`'s default `[2, 3, 5]` needs `stage1.txt`/`stage2.txt`/`stage3.txt`) —
`doctor` and `send` both fail loud if it doesn't.

## Commands

See [`USAGE.md`](USAGE.md) for the full guide (writing drops, the send flow, the
dashboard, deliverability tips). Quick reference:

| Command | What it does |
|---|---|
| `python slap.py init` | Interactive installer — see Setup above. Safe to re-run any time. |
| `python slap.py list` | Lists every auto-discovered campaign (persona, LaTeX on/off). |
| `python slap.py send <campaign> [--now]` | Interactive prep: paste a drop, optionally compile/preview a LaTeX résumé, see domain-dedup warnings (and, for a static campaign, an offer to reuse an archived résumé) and a preview, then stage the send to the queue. `--now` also drains the queue immediately afterward instead of waiting for the scheduled runner. |
| `python slap.py dashboard` | Starts the localhost dashboard at `http://127.0.0.1:5050` — today/week stats, engagement metrics, replies needing triage (tag as real/OOO/not-interested), bounces & blocks, pipeline, recent run history, and a link to the all-campaigns **Reach-outs** page (`/reachouts`, filterable, read-only). |
| `python slap.py doctor` | Preflight checks — see Setup above. Safe to run any time. |
| `python slap.py domains` | Regenerates and prints a read-only domain index from tracked events (who you've contacted, grouped by email domain) — for manual inspection, not itself a source of truth. |
| `python slap.py rebuild` | Rebuilds the `recipients` cache table by replaying the full `events` log. Use this if the cache ever looks wrong — `events` is always the source of truth. |
| `python slap.py runner` | The unattended drain — asks the DB what's queued and due, and sends it. Meant to be triggered by **launchd**, not run by hand day-to-day. See [`LAUNCHD.md`](LAUNCHD.md) for setup and the one-time manual test. Guards itself against `config.yaml`'s `schedule.active_days` — exits without draining on a day that isn't listed. |
| `python slap.py plist` | Prints the launchd `.plist` for `runner`, generated from the current `config.yaml` (one `StartCalendarInterval` entry per `schedule.active_days` day) — redirect it into `~/Library/LaunchAgents/`. See [`LAUNCHD.md`](LAUNCHD.md). |
| `python slap.py cleanup [--confirm]` | Deletes stale compiled résumé PDFs for recipients who are done/dead/never replied and have been idle 15+ days — except one still referenced by a live `RESUME_ARCHIVE_DIR` symlink, which is kept. Dry run by default; `--confirm` actually deletes. Never touches `resume.tex`. |
| `python slap.py template-reload` | Re-renders every not-yet-sent recipient's staged content (across every campaign) against whatever `initial.txt`/`stageN.txt` currently say — for when you edit a template after already staging sends. Shows a summary + sample diffs and asks to confirm before writing anything. Only touches recipients who haven't sent at all yet: once a recipient's initial send fires, GMass has already locked in every follow-up stage's wording, so editing templates afterward can't change it. A recipient staged before this feature existed, or whose stored drop values don't cover a newly-added placeholder, is left untouched and reported in the dashboard's **Template Failures** tab instead. |

Typical flow: `send` a few recipients through the interactive prep loop (staging them to
the queue, not sending yet) → either `send --now` to drain immediately, or let the
launchd-scheduled `runner` pick them up on its normal daily cadence → check `dashboard`
periodically for replies and to tag OOO auto-responses (which re-queues the next stage as
a threaded reply).

## Résumé archive (optional)

Set `RESUME_ARCHIVE_DIR` in `.env` to a folder path and every résumé you send gets
symlinked there (never copied) as `<company>-<role>-<date>.pdf` — one place to browse
everything you've ever sent, while the real bytes stay in exactly one place. Off by
default and never blocks a send — a missing/unwritable folder just warns. `doctor` reports
its status separately, and `cleanup` won't delete a PDF a live archive symlink still
points at.

With the archive on, `send` also offers to **reuse** a previously-sent résumé when the
domain soft-warn fires (a different person at a company you've already emailed) — for
static (`latex.enabled: false`) campaigns only. Purely an offer: the default answer is
"use this campaign's normal resume," never forced. See
[`USAGE.md`](USAGE.md#résumé-archive-optional) for both features in full.

## Unattended sending (launchd)

`runner` is designed to be fired once a day by a macOS launchd LaunchAgent — not cron,
since cron doesn't catch up if your Mac was asleep at the scheduled time. Setup
instructions, how the wake-catch-up timing works, and a one-time manual test checklist
(this behavior can only be verified on real hardware) are in [`LAUNCHD.md`](LAUNCHD.md).

## Running the tests

```bash
pytest -q              # fast suite (default) — no real xelatex compiles or real servers
pytest -m slow -q      # slow suite — real xelatex compiles + a real threaded HTTP server
```

Neither suite makes real GMass API calls or sends anything.
