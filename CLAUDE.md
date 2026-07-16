# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

The app is built and in real, personal use — not a spec waiting to be implemented. 13 CLI
commands (`list, send, dashboard, doctor, init, domains, rebuild, template-reload,
runner, sync, plist, cleanup, bounced`), 730 tests (`pytest -q` / `pytest -m slow -q`),
a multi-page localhost dashboard, and a long tail of post-launch features beyond the
original build brief (Redis-backed dashboard cache, résumé archive/reuse, Reach-outs,
Active Leads/Stop outreach, manual OOO marking, `gmass.allowed_days`/`skip_holidays`).

For current, detailed state, read (in this order): `README.md` and `USAGE.md` (setup +
day-to-day usage), `ARCHITECTURE.md` (engineering decisions, known limitations, bugs
found), `SLAP_PROJECT_CONTEXT.md` (full onboarding brief — mission, every locked design
decision, probe-verified GMass API facts, landmines, open questions). Don't assume
`CONTROL_SHEET.md` or `SLAP_BUILD_PROMPT.md` are present — both are gitignored/local-only
(never committed) and won't exist in a fresh worktree; treat `SLAP_PROJECT_CONTEXT.md` as
the source of truth when they're absent.

## What this is

`slap.py` — a personal cold job-outreach CLI over the **GMass API** (Python). It fills an
email template from a pasted "drop" (line-by-line `key : value`), optionally compiles a
pasted LaTeX résumé and attaches it, then sends via GMass (which relays through the owner's
Gmail and runs follow-ups on GMass's own servers). Everything sent is tracked in local
SQLite; a localhost dashboard shows status and lets the owner tag replies.

## Design philosophy ("iron") — governs every decision

These are load-bearing. When a choice is already determined by these or by an existing
locked decision (`SLAP_PROJECT_CONTEXT.md` §5), **do it — don't ask.** Only ask on a
genuine unresolved fork.

- **One source of truth.** Derived data is regenerated, never hand-maintained in parallel.
- **Append-only event log.** The `events` table is never mutated/deleted. Any cache is
  rebuildable by replaying it.
- **Fail loud, never silent.** Missing config/files/bad state → stop with a clear message.
- **Warn, don't block — with exactly ONE hard gate:** the >1-page résumé gate. All
  other risky actions prompt the human and proceed on explicit confirm.
- **Idempotent, one-recipient blast radius.** A failure can affect at most one recipient
  and can never double-send.
- **Check, don't install.** System deps are verified (`doctor`), never auto-installed.

## Architecture (the big picture)

- **Event-sourced tracking.** One SQLite file, two tables. `events` is truth
  (append-only). `recipients` is a *derived cache* for fast current-state, fully
  rebuildable via the `rebuild` command by replaying `events`. Tests must prove a rebuilt
  cache equals the live one. **All timestamps are UTC**; convert to local only at
  dashboard display.
- **Queue = more events.** `send` stages a recipient as a `queued` event (+ staged
  files) and does **not** send. The `runner` is **stateless**: it asks the DB "what's
  queued and due?" and drains. No separate queue store.
- **Prep vs Fire split.** Prep (`slap.py send`) is interactive (drop paste, LaTeX
  loop, domain check, preview, confirm → stage). Fire (`runner`, via macOS **launchd**) is
  unattended. Use a `StartCalendarInterval` LaunchAgent so a sleeping Mac runs the job
  **on wake** — plain cron does NOT catch up on wake; do not use it.
- **Idempotent two-call send.** Per recipient, always: (1) `POST /api/campaigndrafts`
  (creates draft, carries the attachment → returns draft ID), then (2) `POST /api/campaigns`
  (sends + sets follow-up cadence). Record the draft ID in the event log **the instant
  step 1 returns, before step 2 fires** — so a step-2 failure is retryable with no orphan
  and no double-create.
- **Follow-ups are GMass's job.** Owner has GMass Premium; we set stage cadence at send
  time and GMass fires stages 1–3, stopping on reply. We build **no** follow-up scheduler
  of our own — `gmass.allowed_days`/`skip_holidays` reschedules GMass's *own* firing, it
  doesn't replace it.
- **Domain/recipient dedup is derived live from `events`** — no parallel store. Exact
  recipient already contacted → **hard warn** (always). Same non-consumer domain, different
  person → **soft warn**. Both warn, never block. Consumer domains
  (`consumer_domains.txt`) are excluded from the soft warn only.
- **OOO handling has two mechanisms, both real, not one.** (1) Reply-triggered re-queue:
  owner tags a reply OOO in the dashboard → app sends the next stage itself as
  `sendAsReply: true` + `campaignIdToReplyTo` on the normal runner cadence — deterministic
  threading, never relying on GMass "last conversation" auto-detection. (2) Manual,
  date-based marking: any Reach-outs row can be marked OOO directly and unconditionally,
  with an owner-chosen resume date, via GMass's account-wide unsubscribe endpoint — this
  deliberately goes further than "no per-recipient scheduling," a considered addition,
  not scope creep.
- **App owns LaTeX compilation.** Compile with `xelatex` (twice) or `latexmk -xelatex`
  in a per-recipient workdir. `code`/Preview are human surfaces only. The staged PDF is
  tied to a hash of the accepted `.tex` so a stale/broken PDF is never attached.
- **Dashboard GMass-data cache is optional, never gating.** `slap/gmass_cache.py`
  refreshes a Redis-backed cache hourly (`sync`, its own launchd job); if Redis is
  unreachable, every page just falls back to a live GMass poll on open, exactly as if the
  cache never existed.

## Phase 0 — verify before building (historical, but the safety rule still applies)

The original build verified the GMass API contract against the live API via isolated
`probes/` scripts before any production code was written — this caught real discrepancies
between the vendor's docs and actual behavior (see `SLAP_PROJECT_CONTEXT.md` §6 for the
resolved facts). If you ever need to re-probe something (the API is third-party and could
change), the same hard safety rule still applies, baked into probe code, not a config
knob:

**Any probe that sends may target only the owner's own inbox via Gmail plus-tags, or
create drafts without sending. Any other `to` value must raise before any network call.**

## Config & layout

- `config.yaml` (global): sender, `gmass.api_key_env`, `gmass.allowed_days`/
  `skip_holidays`, fixed **persona cadences** (`hiring_manager [2,4,6]`,
  `recruiter [2,3,5]`, `founder [2,5,7]` — plus an unexplained fourth, `vibe`, see
  `SLAP_PROJECT_CONTEXT.md` §11), `signature`, schedule knobs, tracking, optional Redis.
- `campaigns/<name>/` is **auto-discovered** — any folder with a valid `campaign.yaml` is
  live. No central registry. Contains `campaign.yaml`, `initial.txt`, `stageN.txt`, and
  `resume.pdf` (when latex disabled).
- Loader is **fail-loud**: `initial.txt` needs a `Subject:` first line + blank-line
  separator; the number of `stageN.txt` files must equal the persona cadence length.
- **Template fill is local** (`{{key}}`), not GMass merge. A field marked `optional: true`
  that is empty **drops its whole line**.
- **Drop parser** (preserve exactly): split each line on the **first** colon
  (`line.partition(":")`); strip exactly one space after the colon, preserve the rest;
  ignore colon-less lines; unknown keys ignored; missing keys default empty. Paste-only.

## CLI surface

See `README.md`'s Commands table for the full, current list with flags — don't hardcode a
second copy here, it will drift. `doctor` preflight also auto-runs before any send and
any drain. Secrets: `.env` holds `GMASS_API_KEY` (python-dotenv), `.env` gitignored from
first commit, ship `.env.example`. Deps pinned in `requirements.txt`, kept minimal.

## CONTROL_SHEET.md — local-only, not shipped

`CONTROL_SHEET.md` is gitignored and exists only in the main checkout, never committed
and never present in a fresh worktree. When present, it's organized into: GMass API
Contract, Config Knobs, Confirmation Gates, File/Module Locations, Event Schema, CLI
Command Reference, and a chronological Changelog/Decision Log of post-launch features.
Treat `SLAP_PROJECT_CONTEXT.md` as the source of truth whenever `CONTROL_SHEET.md` isn't
available.

## Commands

- Install deps: `pip install -r requirements-dev.txt` (includes `requirements.txt` + pytest)
- Run: `python slap.py <command>` (e.g. `python slap.py doctor`, `python slap.py list`)
- Tests: `pytest -q` (fast suite, default) / `pytest -m slow -q` (real `xelatex` compiles
  + a real threaded HTTP server). Neither suite makes real GMass API calls or sends
  anything — that's what the standalone, hand-run Phase-0 probes are for.
