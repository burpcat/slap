# launchd setup for the unattended runner (Build Order step 9)

`slap.py runner` is what fires the queue unattended. It's meant to be triggered by
**macOS launchd**, not cron — see SLAP_BUILD_PROMPT.md §10: plain cron does **not**
catch up if the Mac was asleep at the scheduled time; a launchd `StartCalendarInterval`
LaunchAgent does.

## Install

1. Copy the template and fill in your real, absolute repo path (three placeholders):

   ```
   cp com.slap.runner.plist.example ~/Library/LaunchAgents/com.slap.runner.plist
   ```

   Then edit `~/Library/LaunchAgents/com.slap.runner.plist` and replace every
   `/ABSOLUTE/PATH/TO/slap` with your actual path (e.g. `/Users/you/Documents/github/slap`).

2. Load it:

   ```
   launchctl load ~/Library/LaunchAgents/com.slap.runner.plist
   ```

3. To reload after editing the plist:

   ```
   launchctl unload ~/Library/LaunchAgents/com.slap.runner.plist
   launchctl load ~/Library/LaunchAgents/com.slap.runner.plist
   ```

## How the timing works

- `StartCalendarInterval` fires at a **fixed anchor time** (09:00 in the template) —
  this is what gives the wake-catch-up guarantee: if the Mac is asleep at 09:00, launchd
  runs the job as soon as it wakes.
- `slap.py runner` itself then rolls a **random moment** within
  `schedule.fire_window_start`–`fire_window_end` (`config.yaml`, default `09:00`–`09:15`)
  and sleeps until it — or, if that moment has already passed (e.g. the Mac woke up at
  09:20), fires **immediately** rather than waiting for tomorrow.
- Logs land at `runner.log`/`runner.err.log` (paths set in the plist) — check these
  first if a scheduled run doesn't seem to have happened.

## One-time manual test checklist (this behavior can't be unit-tested)

launchd's actual sleep/wake catch-up behavior only happens on real hardware — do this
once after installing, to prove it actually works on your Mac:

1. Edit `~/Library/LaunchAgents/com.slap.runner.plist`'s `Hour`/`Minute` to a time
   **~2 minutes from now** (e.g. if it's 14:32, set `Hour=14`, `Minute=34`).
2. Also temporarily narrow `config.yaml`'s `schedule.fire_window_start`/`fire_window_end`
   to a ~1 minute window starting at that same time, so the random-fire-time roll doesn't
   add much extra delay for this test.
3. Reload the agent (`unload` then `load`, per above).
4. Put the Mac to sleep (Apple menu → Sleep, or close the lid) **before** the scheduled
   minute arrives.
5. Wake the Mac **after** the scheduled minute has passed.
6. Within a few seconds of waking, check `runner.log` — it should show a fresh
   `runner` invocation with a timestamp at or shortly after wake, not at the original
   scheduled minute (proving it ran *on wake*, not that it silently missed the window).
7. Also check the dashboard / `slap.db` for a `run_started`/`run_completed` event pair
   with a timestamp matching the wake time. If you instead see `run_started` followed by
   a `run_failed` (no `run_completed`), the job *did* fire on wake correctly — that part
   worked — but `doctor`'s preflight failed (see the `run_failed` event's `meta.error` for
   which check, or just run `python slap.py doctor` by hand). The queue stays untouched
   either way; it'll retry itself on the next scheduled fire.
8. **Revert** the plist's `Hour`/`Minute` back to your real desired fire time (e.g. 9:00)
   and `config.yaml`'s fire window back to `09:00`–`09:15`, then reload the agent again.

If the job does *not* fire on wake at all (no `run_started` event, nothing in the logs),
common causes: the plist wasn't reloaded after editing, `WorkingDirectory`/paths in the
plist don't match your actual repo location, or the Python interpreter path doesn't point
at this repo's `.venv`.
