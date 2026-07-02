# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

This repo currently contains only the build brief `SLAP_BUILD_PROMPT.md`. The application
(`slap.py`) has **not been built yet**. The brief is the authoritative spec — read it in
full before writing code, and follow its **Build Order** (§14). Build in that order;
Phase-0 probes come first and gate everything else.

## What this is

`slap.py` — a personal cold job-outreach CLI over the **GMass API** (Python). It fills an
email template from a pasted "drop" (line-by-line `key : value`), optionally compiles a
pasted LaTeX résumé and attaches it, then sends via GMass (which relays through the owner's
Gmail and runs follow-ups on GMass's own servers). Everything sent is tracked in local
SQLite; a localhost dashboard shows status and lets the owner tag replies.

## Design philosophy ("iron") — governs every decision

These are load-bearing. When a choice is already determined by these or by a spec in the
brief, **do it — don't ask.** Only ask on a genuine unresolved fork.

- **One source of truth.** Derived data is regenerated, never hand-maintained in parallel.
- **Append-only event log.** The `events` table is never mutated/deleted. Any cache is
  rebuildable by replaying it.
- **Fail loud, never silent.** Missing config/files/bad state → stop with a clear message.
- **Warn, don't block — with exactly ONE hard gate:** the >1-page résumé gate (§9). All
  other risky actions prompt the human and proceed on explicit confirm.
- **Idempotent, one-recipient blast radius.** A failure can affect at most one recipient
  and can never double-send.
- **Check, don't install.** System deps are verified (`doctor`), never auto-installed.

## Architecture (the big picture)

- **Event-sourced tracking (§5).** One SQLite file, two tables. `events` is truth
  (append-only). `recipients` is a *derived cache* for fast current-state, fully
  rebuildable via the `rebuild` command by replaying `events`. Tests must prove a rebuilt
  cache equals the live one. **All timestamps are UTC**; convert to local only at
  dashboard display.
- **Queue = more events (§10).** `send` stages a recipient as a `queued` event (+ staged
  files) and does **not** send. The `runner` is **stateless**: it asks the DB "what's
  queued and due?" and drains, writing `sent`/`send_failed`/`run_failed` events. No
  separate queue store.
- **Prep vs Fire split (§10).** Prep (`slap.py send`) is interactive (drop paste, LaTeX
  loop, domain check, preview, confirm → stage). Fire (`runner`, via macOS **launchd**) is
  unattended. Use a `StartCalendarInterval` LaunchAgent so a sleeping Mac runs the job
  **on wake** — plain cron does NOT catch up on wake; do not use it.
- **Idempotent two-call send (§3).** Per recipient, always: (1) `POST /api/campaigndrafts`
  (creates draft, carries the attachment → returns draft ID), then (2) `POST /api/campaigns`
  (sends + sets follow-up cadence). Record the draft ID in the event log **the instant
  step 1 returns, before step 2 fires** — so a step-2 failure is retryable with no orphan
  and no double-create.
- **Follow-ups are GMass's job.** Owner has GMass Premium; we set stage cadence at send
  time (`stageOneDays`/`stageOneCampaignText`, etc.) and GMass fires stages 1–3, stopping
  on reply. We build **no** follow-up scheduler.
- **Domain/recipient dedup is derived live from `events` (§6)** — no parallel store. Exact
  recipient already contacted → **hard warn** (always). Same non-consumer domain, different
  person → **soft warn**. Both warn, never block. Consumer domains
  (`consumer_domains.txt`) are excluded from the soft warn only.
- **OOO re-queue (§7).** No GMass re-enroll API. Owner tags a reply OOO in the dashboard →
  app itself sends the next stage as `sendAsReply: true` + `campaignIdToReplyTo` on the
  normal runner cadence. Deterministic threading — never rely on GMass "last conversation"
  auto-detection.
- **App owns LaTeX compilation (§9).** Compile with `xelatex` (twice) or `latexmk -xelatex`
  in a per-recipient workdir. `code`/Preview are human surfaces only. The staged PDF is
  tied to a hash of the accepted `.tex` so a stale/broken PDF is never attached.

## Phase 0 — verify before building (§2)

The GMass API contract in the brief comes from docs and **must be verified against the live
API first** via isolated `probes/` scripts, with real responses recorded into
`CONTROL_SHEET.md`. Do not build dependent code on any unverified assumption. Most
load-bearing unknown: the exact **stop-on-reply** parameter (probe #2).

**Hard safety rule baked into probe code (not a config knob, not overridable):** any probe
that sends may target **only** the owner's own inbox via Gmail plus-tags
(`everythingforgenius+testmass{N}@gmail.com`) or create drafts without sending. Any other
`to` value must raise **before any network call**.

## Config & layout (§4)

- `config.yaml` (global): sender, `gmass.api_key_env`, fixed **persona cadences**
  (`hiring_manager [2,4,6]`, `recruiter [2,3,5]`, `founder [2,5,7]`), schedule knobs,
  tracking.
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

## CLI surface (§11)

`list` · `send <campaign> [--now]` · `dashboard` · `doctor` · `domains` · `rebuild`.
`doctor` preflight also auto-runs before any send and any drain. Secrets: `.env` holds
`GMASS_API_KEY` (python-dotenv), `.env` gitignored from first commit, ship `.env.example`.
Deps pinned in `requirements.txt`, kept minimal.

## Standing deliverable: CONTROL_SHEET.md (§12)

Maintain `CONTROL_SHEET.md` as the single reference of every knob, toggle, default,
confirmation gate, file location, and Phase-0 probe finding — updated as each piece is
built, and populated with resolved Phase-0 API facts.

## Commands

No build/test tooling exists yet. When scaffolding, expect:
- Run: `python slap.py <command>` (e.g. `python slap.py doctor`, `python slap.py list`)
- Install deps: `pip install -r requirements.txt`
- Tests per the §13 plan (probes run first, self-send-only). Wire the chosen test runner
  and document the single-test invocation here once it exists.
