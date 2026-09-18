# Changelog

## Unreleased — Transport migration: GMass API → local Gmail SMTP + IMAP

`slap` no longer depends on the GMass API. It sends directly through the owner's
Gmail over **SMTP** and detects replies (and bounces) over **IMAP**, using a
single Gmail **App Password**. The GMass-stable version is preserved on the
`old/gmass` branch; `master` is this migrated version.

### Added

- **`slap/smtp.py` — SMTP send client.** One atomic `send_message()` per
  recipient over `smtp.gmail.com:587` (STARTTLS + App Password), replacing
  GMass's two-call draft→campaign flow. Carries attachments and RFC822
  `In-Reply-To`/`References` threading headers.
- **App-owned follow-up cadence.** `slap.queue.due_for_followup` selects the
  next due stage via `slap.stages.stage_fire_date`, and
  `slap.runner._send_followup` sends it threaded into the recipient's
  conversation — the app now fires stages 1–N itself instead of GMass firing
  them server-side. `stages.py`'s fire-date math became an actual trigger.
- **`slap/imap.py` — IMAP reply detection.** `poll_replies()` matches inbound
  `In-Reply-To`/`References` against our sent `Message-ID`s.
  `runner.ingest_replies` writes `reply` events; **`runner.drain` polls IMAP
  before selecting follow-ups**, so stop-on-reply is enforced at *fire time* (a
  recipient who replied since the last drain is never followed up), not only on
  dashboard open.
- **Bounce (DSN) detection.** `imap.poll_bounces` + `runner.ingest_bounces`
  detect Gmail delivery-failure notifications (header-only: `multipart/report`
  or a mailer-daemon `From`, with `X-Failed-Recipients` matched to one of our
  recipients) and write a `bounce` event, flipping the recipient to
  `status='bounced'`. Restores the "a hard bounce stops the sequence" behaviour
  GMass's bounce report gave and plain SMTP otherwise loses (repeated sends to a
  dead address waste cap and hurt sender reputation).
- **`message_id` column** on `events` + `recipients` (SMTP threading +
  reply/bounce match key), added by an additive, idempotent migration verified
  against the live `slap.db` with zero row loss.
- **`probes/smtp_selfsend.py` — guarded live smoke test.** Sends a real email to
  an owner `+testmassN` self-address only (raises before any network call for
  any other recipient); `poll-replies` confirms IMAP reply detection end to end.

### Changed

- **Credentials:** a single Gmail App Password in `.env` as `GMAIL_APP_PASSWORD`
  covers both SMTP and IMAP, replacing `GMASS_API_KEY`. Create one at
  <https://myaccount.google.com/apppasswords> (requires 2-Step Verification).
- **`doctor`** now checks `GMAIL_APP_PASSWORD` instead of the GMass API key.
- **Config:** the `gmass:` block in `config.yaml` is optional and unused
  (`api_key_env`/`allowed_days`/`skip_holidays` survive only as defaulted,
  inert fields). `python slap.py init` now asks for a Gmail App Password.
- **Route renamed:** `POST /api/gmass/refresh` → **`POST /api/refresh`** (it now
  escalates an IMAP reply poll, not a GMass poll). Frontend hook
  `useGmassRefresh` → `useRefresh`. Rebuild the SPA bundle
  (`npm --prefix slap/frontend run build`).
- **Dashboard write actions** `tag_reply` / `stop_outreach` / `gate_linkedin`
  (routes `/api/reply/<r>/tag`, `/api/reachouts/<r>/stop`,
  `/api/reachouts/<r>/linkedin-replied`) no longer fire a GMass account-wide
  unsubscribe — under SMTP the app owns the whole cadence, so the local event
  alone halts it. These can no longer return `502` from a failed suppression
  call.
- **Threading key:** OOO resends, follow-ups, and reminds thread via the
  recipient's `message_id` (`In-Reply-To`) instead of a GMass
  `campaignIdToReplyTo`.

### Removed

- `slap/gmass.py` (the GMass API client), `tests/test_gmass.py`, and the GMass
  Phase-0 `probes/run.py`.
- **Click tracking** and GMass-sourced bounce/block reports: no free SMTP
  equivalent for clicks. The `click`/`bounce` event types and their dashboard
  widgets remain for historical data; clicks are no longer ingested, and bounces
  now come from IMAP DSN detection instead of GMass reports.

### Migration notes (for the owner)

1. Rename `GMASS_API_KEY=` to `GMAIL_APP_PASSWORD=` in `.env` (and `.env.example`)
   and set the real App Password.
2. `python slap.py doctor` should then pass the credential check.
3. Rebuild the dashboard bundle: `npm --prefix slap/frontend run build`.
4. Verify the real path once with `python probes/smtp_selfsend.py send` → reply
   from Gmail → `python probes/smtp_selfsend.py poll-replies`.
5. Canary the first real batch small before scaling.

### Known trade-offs vs the GMass version

- No click tracking; Gmail's server-side auto-responder filtering is gone (an
  auto-reply now conservatively *stops* the sequence — correct via the
  dashboard's OOO-tagging).
- Narrow at-least-once window on a crash between SMTP `250 OK` and the SQLite
  `sent` commit (GMass's draft-reuse gave at-most-once).
- Still capped at Gmail's ~500/day.
