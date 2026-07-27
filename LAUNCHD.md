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

## Troubleshooting: runner silently stops firing

**Symptom:** the dashboard's staleness warning appears (or you just notice no new
`run_started`/`run_completed` events for a while), `runner.log`/`runner.err.log` haven't
grown, and:

```bash
launchctl print gui/$(id -u)/com.slap.runner | grep -E "last exit|runs ="
```

shows `last exit code = 78: EX_CONFIG`.

**Diagnosis.** Check the unified log for the actual spawn attempt:

```bash
/usr/bin/log show --predicate 'process == "launchd"' --last 24h | grep -i slap
```

If you see `posix_spawn(.../.venv/bin/python), error 0x1 - Operation not permitted`
followed by `exited due to exit(78)`, this is **not** a scheduling bug — launchd fired
exactly on time, but macOS refused to even start the Python interpreter, before a single
line of `slap.py` ever ran (which is also why `runner.log`/`runner.err.log` stay empty —
they're only ever written *after* the interpreter starts).

**Root cause: TCC's Documents/Desktop/Downloads-folder protection.** If this repo lives
inside `~/Documents`, `~/Desktop`, or `~/Downloads`, macOS's privacy layer requires a
per-app grant to execute anything there. Interactive shells (Terminal, iTerm2) already
have their own blanket grant for those folders, which is why running `slap.py runner` **by
hand** always works even while this is broken — but `launchd`/`xpcproxy` has no such
inherited trust. It relies on a much narrower grant tied to the *exact code-signing
identity* of the interpreter binary. Homebrew's Python is only ad-hoc signed (no stable
Team ID), so **that identity changes on every `brew reinstall`/`brew upgrade` of the
Python formula**, silently invalidating the grant — and a headless launchd job has no way
to trigger a fresh consent prompt to re-grant it. **Re-signing or reinstalling the
interpreter does not fix this** — it mints a new identity that will eventually break the
same way again (confirmed directly: reinstalling Python and rebuilding the venv did not
resolve a real occurrence of this, verified via a live `launchctl kickstart` test that
failed identically against the brand-new binary).

**Fix: move the whole repo out of the protected folder.** This is the only durable fix —
anywhere outside `~/Documents`/`~/Desktop`/`~/Downloads` (e.g. `~/dev/slap`) sidesteps this
TCC check entirely and permanently, regardless of future Python reinstalls.

```bash
# 1. Unload the current agent so nothing fires mid-move
launchctl bootout gui/$(id -u)/com.slap.runner

# 2. Move the repo (adjust the source path to wherever yours actually lives)
mkdir -p ~/dev
mv ~/Documents/github/slap ~/dev/slap

# 3. If you have any linked git worktrees, repair their metadata — the paths are
#    baked in as absolute and are now stale
cd ~/dev/slap
git worktree list                          # lists every linked worktree
git worktree repair <path-to-each-worktree>  # run once per linked worktree
# and from inside each linked worktree itself:
#   git worktree repair /Users/you/dev/slap

# 4. Recreate the venv — it bakes in absolute paths (shebangs, pyvenv.cfg) and is not
#    portable across a move
python3.14 -m venv --clear .venv     # match whatever Python version you actually use
./.venv/bin/pip install -r requirements.txt

# 5. Regenerate the plist with the new path and load it
./.venv/bin/python slap.py plist > ~/Library/LaunchAgents/com.slap.runner.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.slap.runner.plist

# 6. Verify — the real test is a launchd-triggered spawn, not a manual run
./.venv/bin/python slap.py doctor            # should say "All checks passed"
launchctl kickstart -k gui/$(id -u)/com.slap.runner
/usr/bin/log show --predicate 'process == "launchd"' --last 2m | grep -i slap
# should show "Successfully spawned python[...]" then "exited due to exit(0)" —
# no posix_spawn error. Safe to run even with a live queue: if anything is actually
# due, this triggers a real drain, so only run it once you're ready for that.
```

If you want the old path to keep working out of habit (other aliases, editor bookmarks,
etc.), you can symlink it back afterward (`ln -s ~/dev/slap ~/Documents/github/slap`) —
but make sure the plist itself (step 5) is generated to point at the **real** `~/dev/slap`
path, not the symlink. It's untested whether a plist that resolves through the old
symlinked location would still escape the TCC check.
