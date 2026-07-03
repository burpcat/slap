# slap

A personal cold job-outreach CLI over the [GMass](https://www.gmass.co/) API. `slap`
fills an email template from a pasted "drop" (line-by-line `key: value`), optionally
compiles a pasted LaTeX résumé and attaches it, then sends via GMass — which relays
through your own Gmail account and runs the follow-up cadence on GMass's servers.
Everything sent is tracked in local SQLite, with a localhost dashboard for status and
reply triage.

Single-owner, personal-use tool. Not multi-tenant, not a SaaS product.

## Requirements

- macOS (the unattended runner is scheduled via **launchd**, and the LaTeX loop shells
  out to macOS's `open -a Preview`).
- Python 3.11+.
- A [GMass](https://www.gmass.co/) account with an API key, connected to the Gmail
  account you want to send from. Follow-up cadences (stage 2/3 emails) need GMass
  Premium.
- If you plan to use LaTeX-compiled résumés (`latex.enabled: true` in a campaign):
  [`xelatex`](https://www.tug.org/xetex/) and the [`code`](https://code.visualstudio.com/docs/editor/command-line)
  CLI (VS Code), both on your `PATH`. `slap.py doctor` checks for both — see below.

## Setup

```bash
git clone <this-repo>
cd slap
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # includes requirements.txt + pytest
```

Copy the two example files and fill them in:

```bash
cp .env.example .env
cp config.yaml.example config.yaml
```

- **`.env`** — set `GMASS_API_KEY` to your real GMass API key. `.env` is gitignored;
  never commit it.
- **`config.yaml`** — set `sender.from_name` to your name (`sender.from_email` should
  already match the Gmail account behind your API key). The persona cadences
  (`hiring_manager`/`recruiter`/`founder`) and `schedule` defaults are reasonable
  starting points — see the comments in the file for what each knob does.

Run the preflight check to confirm everything's wired up:

```bash
python slap.py doctor
```

`doctor` verifies: `GMASS_API_KEY` is set, `config.yaml`'s sender fields are filled in,
the SQLite tracking DB is reachable, and `consumer_domains.txt` is present (it seeds the
default consumer-email-provider list automatically if missing — the only check here that
writes anything; everything else only checks, never installs). It then validates every
campaign under `campaigns/` (see below) and reports pass/fail per check, exiting non-zero
if anything needs attention.

## Setting up a campaign

Campaigns are auto-discovered: any folder under `campaigns/` with a valid
`campaign.yaml` is live. No registry to update.

```
campaigns/
  coldpost-recruiter/
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

The number of `stageN.txt` files must exactly match the chosen persona's cadence length
(e.g. `recruiter`'s default `[2, 3, 5]` needs `stage1.txt`/`stage2.txt`/`stage3.txt`) —
`doctor` and `send` both fail loud if it doesn't.

## Commands

| Command | What it does |
|---|---|
| `python slap.py list` | Lists every auto-discovered campaign (persona, LaTeX on/off). |
| `python slap.py send <campaign> [--now]` | Interactive prep: paste a drop, optionally compile/preview a LaTeX résumé, see domain-dedup warnings and a preview, then stage the send to the queue. `--now` also drains the queue immediately afterward instead of waiting for the scheduled runner. |
| `python slap.py dashboard` | Starts the localhost dashboard at `http://127.0.0.1:5000` — today/week stats, engagement metrics, replies needing triage (tag as real/OOO/not-interested), pipeline, and recent run history. |
| `python slap.py doctor` | Preflight checks — see Setup above. Safe to run any time. |
| `python slap.py domains` | Regenerates and prints a read-only domain index from tracked events (who you've contacted, grouped by email domain) — for manual inspection, not itself a source of truth. |
| `python slap.py rebuild` | Rebuilds the `recipients` cache table by replaying the full `events` log. Use this if the cache ever looks wrong — `events` is always the source of truth. |
| `python slap.py runner` | The unattended drain — asks the DB what's queued and due, and sends it. Meant to be triggered by **launchd**, not run by hand day-to-day. See [`LAUNCHD.md`](LAUNCHD.md) for setup and the one-time manual test. |

Typical flow: `send` a few recipients through the interactive prep loop (staging them to
the queue, not sending yet) → either `send --now` to drain immediately, or let the
launchd-scheduled `runner` pick them up on its normal daily cadence → check `dashboard`
periodically for replies and to tag OOO auto-responses (which re-queues the next stage as
a threaded reply).

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

Neither suite makes real GMass API calls. The Phase-0 API probes in `probes/run.py` do —
run standalone via `python probes/run.py <probe>`, never as part of `pytest` — and are
guarded to only ever target the owner's own inbox via Gmail plus-addressing
(`+testmass{N}@gmail.com`); any other recipient raises before any network call.
