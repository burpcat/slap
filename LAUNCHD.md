# launchd setup for the unattended runner (Build Order step 9)

`slap.py runner` is what fires the queue unattended. It's meant to be triggered by
**macOS launchd**, not cron — see SLAP_BUILD_PROMPT.md §10: plain cron does **not**
catch up if the Mac was asleep at the scheduled time; a launchd `StartCalendarInterval`
LaunchAgent does.

## Configurable scheduler days (`schedule.active_days`)

`config.yaml`'s `schedule.active_days` lists which weekdays the unattended runner is
allowed to drain, e.g.:

```yaml
schedule:
  active_days: [mon, tue, wed, thu, fri] # skip weekends
```

This is enforced in **two places**, so it stays correct even if they ever drift out of
sync:

1. **The generated plist** (`python slap.py plist`) emits one `StartCalendarInterval`
   entry per active day — launchd simply never invokes the runner on an inactive day.
2. **The runner itself** (`runner.is_active_day()`) re-checks `active_days` at drain time
   and exits without draining if today isn't listed, even if it somehow got invoked
   anyway. This does *not* apply to a manual `send --now` — that's an explicit human
   action, never silently skipped by a scheduling preference.

**Note the asymmetry**: the runner-side guard only protects against the plist being
*too permissive* (still has a Weekday entry for a day you've since removed from
`active_days`). If you *add* a day to `active_days`, the plist has no entry for it until
you regenerate and reload — the runner is never even invoked that day. **Always
regenerate + reload after changing `active_days` or `fire_window_start`** (see below).

## GMass-side follow-up scheduling (`gmass.allowed_days` / `gmass.skip_holidays`)

`active_days` above only ever gates SLAP's own local `runner` — it has zero effect on
GMass's own follow-up stages, which fire server-side on the cadence baked in at initial
send (confirmed by a real investigation after Sunday follow-up activity was seen despite
a weekday-only schedule — see `SLAP_PROJECT_CONTEXT.md` §5). If you actually want to pause
GMass's *own* follow-up firing, that's a separate, independent knob:

```yaml
gmass:
  allowed_days: [mon, tue, wed, thu, fri]
  skip_holidays: false
```

- **`allowed_days`** — confirmed directly against GMass support: it reschedules which
  days GMass's own scheduler may fire follow-up stages for recipients queued from that
  point on. Locked in per-recipient at send time, so editing it never affects anyone
  already sent. Omit entirely to leave GMass fully unrestricted (no `allowedDays` field
  sent at all).
- **`skip_holidays`** — tri-state, not a plain boolean default. Left out entirely, GMass's
  own server-side default is actually `true` (holidays are already skipped with no config
  here at all, confirmed by GMass support) — set it to `false` explicitly if you want
  holidays *not* skipped.

Both are independent of `schedule.active_days`: one is a low-stakes local preference for
when SLAP itself wakes up, the other is locked in per-recipient at send time with no way
to retroactively change it.

## Install

1. Generate the plist from your current `config.yaml` and copy it into place:

   ```
   python slap.py plist > ~/Library/LaunchAgents/com.slap.runner.plist
   ```

   No manual path-editing needed — the generator fills in the Python interpreter that
   ran it, the absolute repo path, and one `StartCalendarInterval` entry per
   `schedule.active_days` day, all anchored at `schedule.fire_window_start`.
   `com.slap.runner.plist.example` (repo root) shows the resulting shape for reference
   only — it's not meant to be hand-copied anymore.

2. Load it:

   ```
   launchctl load ~/Library/LaunchAgents/com.slap.runner.plist
   ```

3. **Any time `config.yaml`'s `schedule.active_days` or `fire_window_start` changes**,
   regenerate and reload:

   ```
   python slap.py plist > ~/Library/LaunchAgents/com.slap.runner.plist
   launchctl unload ~/Library/LaunchAgents/com.slap.runner.plist
   launchctl load ~/Library/LaunchAgents/com.slap.runner.plist
   ```

## The hourly cache-sync job (post-launch feature)

`slap.py sync` refreshes the dashboard's Redis-backed cache of GMass reply/click/bounce
data (see CONTROL_SHEET.md) — a separate, simpler launchd job from the runner above: a
plain hourly interval, no active-days restriction, no fire-window randomization (there's
no send-volume/deliverability reason to ever skip an hour of refreshing a read-only
cache). Requires Redis running locally — `python slap.py doctor` reports whether it's
reachable; this app never installs or starts Redis itself ("check, don't install").

Install alongside the runner's own plist, same pattern:

```
python slap.py plist --job sync > ~/Library/LaunchAgents/com.slap.sync.plist
launchctl load ~/Library/LaunchAgents/com.slap.sync.plist
```

Logs land at `sync.log`/`sync.err.log`. If Redis isn't running, `sync` fails loud (fast —
a short connect timeout, not a hang) and the queue/dashboard are unaffected either way:
the dashboard's on-open fallback still works without this job ever running at all, just
by polling GMass live whenever the cache turns out to be stale — this job only exists to
make that the *uncommon* case instead of the *every* case.

## How the timing works

- Each `StartCalendarInterval` array entry fires at a **fixed anchor time** (the
  template's `Hour`/`Minute`, taken from `fire_window_start`) on its one active weekday —
  this is what gives the wake-catch-up guarantee: if the Mac is asleep at that time on an
  active day, launchd runs the job as soon as it wakes.
- `slap.py runner` itself then rolls a **random moment** within
  `schedule.fire_window_start`–`fire_window_end` (`config.yaml`, default `09:00`–`09:15`)
  and sleeps until it — or, if that moment has already passed (e.g. the Mac woke up at
  09:20), fires **immediately** rather than waiting for tomorrow.
- Before any of that, `runner.is_active_day()` checks `config.yaml`'s current
  `active_days` and exits immediately (no drain, no queue touched) if today isn't listed.
- Logs land at `runner.log`/`runner.err.log` (paths set in the plist) — check these
  first if a scheduled run doesn't seem to have happened.

## One-time manual test checklist (this behavior can't be unit-tested)

launchd's actual sleep/wake catch-up behavior only happens on real hardware — do this
once after installing, to prove it actually works on your Mac:

1. In `config.yaml`, temporarily set `schedule.fire_window_start`/`fire_window_end` to a
   ~1-minute window starting ~2 minutes from now (e.g. if it's 14:32, use `14:34`–`14:35`),
   and make sure **today's weekday is in `active_days`** (add it temporarily if not).
2. Regenerate and reload the plist (see step 3 above).
3. Put the Mac to sleep (Apple menu → Sleep, or close the lid) **before** the scheduled
   minute arrives.
4. Wake the Mac **after** the scheduled minute has passed.
5. Within a few seconds of waking, check `runner.log` — it should show a fresh
   `runner` invocation with a timestamp at or shortly after wake, not at the original
   scheduled minute (proving it ran *on wake*, not that it silently missed the window).
6. Also check the dashboard / `slap.db` for a `run_started`/`run_completed` event pair
   with a timestamp matching the wake time. If you instead see `run_started` followed by
   a `run_failed` (no `run_completed`), the job *did* fire on wake correctly — that part
   worked — but `doctor`'s preflight failed (see the `run_failed` event's `meta.error` for
   which check, or just run `python slap.py doctor` by hand). The queue stays untouched
   either way; it'll retry itself on the next scheduled fire.
7. **Revert** `config.yaml`'s `fire_window_start`/`fire_window_end` (and `active_days`, if
   you temporarily changed it) back to your real values, then regenerate + reload again.

If the job does *not* fire on wake at all (no `run_started` event, nothing in the logs),
common causes: the plist wasn't regenerated/reloaded after a `config.yaml` change,
today's weekday isn't in `active_days`, or the Python interpreter path in the plist
doesn't point at this repo's `.venv` (regenerate with `slap.py plist` run from inside that
venv to fix this automatically).
