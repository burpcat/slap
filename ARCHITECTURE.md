# Architecture & Implementation Notes

A technical deep-dive into how `slap` is built and why — implementation approach,
architectural decisions, and known limitations/future work. `README.md` covers setup and
usage; this document is for understanding (or describing) the engineering behind it.

## What it is

`slap` is a personal cold job-outreach CLI: it fills an email template from a pasted
"drop," optionally compiles a LaTeX résumé to a guaranteed-correct PDF, sends through the
[GMass](https://www.gmass.co/) API (which relays through the owner's own Gmail and runs a
multi-stage follow-up cadence), and tracks every send/reply/click/bounce in a local,
event-sourced SQLite store with a localhost dashboard for monitoring and reply triage.

## Implementation

**Stack:** Python 3, stdlib `argparse` for the CLI, SQLite for storage, Flask for the
localhost dashboard, `requests` for the GMass HTTP client, `PyYAML` for config, `pypdf`
for LaTeX page-counting. No ORM, no task queue, no background worker framework — the
"runner" is a plain script fired by macOS `launchd`.

**Build approach — verify before building, then build in dependency order.** Rather than
implementing against the GMass API's documented shape, the project started with a
Phase-0 verification pass: small, isolated probe scripts hit the live API and recorded
real responses before any production code was written. This caught three real
discrepancies between the vendor's docs and actual behavior before they could get baked
into the client (see "GMass API contract," below) — a "don't build on unverified
assumptions" discipline that paid for itself immediately. Every probe that could send
mail was hard-guarded (at the network-call boundary, not just the function argument) to
only ever target the owner's own inbox via Gmail plus-addressing.

From there, the app was built in 13 dependency-ordered stages (project skeleton → config
loader → template engine → event store → API client → dedup → LaTeX compiler → queue/
runner → reply handling → dashboard → preflight checks → final polish), each one
independently reviewed against a written spec and a fixed set of design invariants before
moving to the next. Several real bugs were caught this way rather than in later
integration — see "Notable bugs found," below.

**Testing.** 238 fast tests (a few milliseconds total) plus 7 tests marked `slow`
(real `xelatex` subprocess compiles, and one test that spins up a real threaded HTTP
server) kept out of the default run for dev-loop speed. No test suite makes a real GMass
API call — that's what the standalone, hand-run Phase-0 probes are for. Every bug found
during development was turned into a regression test *and verified by deliberately
reverting the fix and confirming the new test actually fails* before restoring it — not
just "the fix makes the test pass," but "the test fails without the fix."

## Architecture

**Event-sourced tracking.** One SQLite file, two tables. `events` is the single source of
truth and is strictly append-only — nothing is ever updated or deleted. `recipients` is a
derived cache for fast current-state reads, and is fully rebuildable by replaying
`events` from scratch (proven in tests: a rebuilt cache must exactly equal the live one).
This means every piece of derived state in the app — dedup history, the domain index,
the dashboard's every panel, cap accounting — is a pure function of one append-only log,
never a second thing that can drift out of sync with it.

**Prep/fire split.** Staging a send (`send`) is interactive and does the risky,
judgment-requiring work — paste the data, compile/preview the résumé, see dedup warnings,
preview the email — but never actually sends anything; it just appends a `queued` event.
Firing (`runner`) is a separate, unattended, stateless process: it asks the DB "what's
queued and due?" and drains it. This split means the thing that runs unsupervised at 9am
via `launchd` has no interactive prompts, no judgment calls, and the smallest possible
surface area — it can only do exactly what was already explicitly staged.

**Idempotent two-call send.** GMass's send is two HTTP calls: create a draft, then send
it. The draft ID is recorded in the event log the instant the first call returns —
*before* the second call ever fires. If the process crashes or the network drops between
the two calls, a retry sees the recorded draft ID and resumes from step two instead of
creating a duplicate draft or double-sending. Combined with per-recipient error isolation
in the drain loop, a single recipient's failure can never take down or duplicate-send to
any other recipient in the same batch (a fixed one-recipient blast radius).

**No separate queue store.** "Queued" is just another event type. The runner's query is
`SELECT recipients due for sending`, computed live from the event log — there's no
parallel queue table that could ever disagree with the source of truth about what's
pending.

**Follow-ups are the vendor's job.** GMass fires stages 2 and 3 of a cadence server-side
on a schedule set at send time; the app does not build or run its own follow-up
scheduler. The one thing the app *does* own unattended is the reply-triggered "out of
office" recovery: when the owner tags a reply as OOO in the dashboard, the app sends the
next stage itself, as an explicit threaded reply (`sendAsReply` + a specific
`campaignIdToReplyTo`) rather than depending on GMass's own conversation-matching
heuristics — deterministic threading was worth the small amount of extra code.

**Dedup is derived, not stored.** "Have I already emailed this person / this company?"
is answered by querying the event log live at send time — an exact-recipient match is a
hard warning (always), a same-domain-different-person match is a soft warning (skipped
for large consumer providers like Gmail, or every send would warn). Both warn and let the
human decide; nothing is ever silently blocked. This mirrors the project's single hard
rule: everything is warn-and-proceed except exactly one gate — a compiled résumé over one
page forces an explicit, precisely-worded confirmation (no accidental y/n) before it can
be sent.

**App-owned LaTeX compilation.** The app compiles the pasted `.tex` itself (twice, for
cross-references) rather than trusting a human-eyeballed PDF, so the page count and the
attached file are provably the same artifact — a hash of the accepted `.tex` is stored
alongside the staged PDF specifically so a stale recompile can never silently ride along
on a later send.

**Fail loud, check-don't-install.** A dedicated preflight command validates every
external dependency the app needs (API key, config, database, required binaries on
`PATH`) and reports pass/fail — it never auto-installs a missing system dependency, with
one deliberate, spec-mandated exception: a missing default data file (the consumer-email-
provider list used for dedup) is auto-seeded, since that's project data, not a system
dependency. This preflight runs automatically before any send and is wired into the
unattended runner's own retry-then-give-up-loud path, so a transient environment problem
degrades to a visible, dashboard-surfaced failure with the queue left completely intact —
never a silent skip, never a crash that loses track of what happened.

## Future expansions / known limitations

Found during development (mostly via structured self-audits run after every build
stage) and deliberately deferred rather than silently left undocumented:

- **A few unverified GMass behaviors**, inherent to a vendor API with no read-back for
  these specific facts: that a stage genuinely stops firing after a real reply; that a
  threaded reply visually appears correctly in Gmail; that a "draft only" mode actually
  suppresses sending; and that an attached PDF is actually present on the delivered
  email. These can only be confirmed by real usage over days, not by more code.
- **The daily-send-cap accounting uses two independent local-date calculations that
  don't yet convert a UTC timestamp to the local calendar day before bucketing it** —
  meaning a send made very late at night local time can, in principle, be counted toward
  the wrong day's cap for follow-up-projection purposes. Low-impact (the cap itself has
  headroom by design), but worth unifying into one date-handling utility.
  Comment: the localhost dashboard's own equivalent panels *do* correctly convert to
  local time — this gap is specifically in the unattended runner's cap-headroom estimate.
- **A hand-crafted API request could tag a reply "out of office" for a recipient with no
  actual unresolved reply**, creating a queue entry that can never resolve. Not reachable
  through the dashboard UI (which only ever renders real, actionable replies), but the
  backend doesn't independently guard against it.
- **Read-your-own-config inconsistency**: one read-only reporting command (`domains`)
  still reads a hardcoded default file path rather than the same configurable path the
  rest of the app now respects, for owners who rename that file. Low-stakes (it only
  affects a cosmetic label in read-only output), tracked for whenever that command needs
  a config object for another reason anyway.
- **No production WSGI server for the dashboard** — it runs on Flask's built-in
  development server, which is appropriate for a single-owner localhost tool but would
  need a real WSGI server (gunicorn, waitress) before ever being exposed beyond
  `127.0.0.1`.
- **No A/B testing or template-level analytics** — the dashboard reports engagement by
  persona and by pipeline stage, but not by which specific subject line or body template
  performed better, which would be a natural next layer on top of the existing
  event-sourced data (nothing new to track, just a new query).
- **Single-owner by design** — the dedup/cap/tracking model assumes one sender identity
  and one local SQLite file; extending to multiple senders would need per-sender
  partitioning throughout, not just a config change.

## Notable bugs found (worth knowing for the "how do you catch bugs" conversation)

- **A real cross-thread SQLite crash that only manual browser testing caught.** The
  dashboard originally opened one database connection at startup and closed over it in
  both Flask routes. That works under Flask's synchronous test client (which runs
  requests on the calling thread) but breaks the instant a real server handles two
  requests on two different threads — `sqlite3` connections are only valid on the thread
  that created them. The automated test suite, which only ever exercised the underlying
  functions directly or through the test client, had no way to catch this; it surfaced
  only when the app was actually run and hit with real HTTP requests. Fixed by opening a
  fresh connection per request (the standard Flask pattern), and then covered with a
  dedicated regression test that spins up a real threaded server on a real socket — the
  kind of test that's slower and heavier than the rest of the suite, but exists
  specifically because the bug class it guards against is invisible to lighter-weight
  tests.
- **A file-path assumption that would have silently written a test artifact into the
  real project on every test run.** A new preflight check needed to verify a config file
  exists, using a path taken from the loaded config object. One test module's config
  fixture used a plain relative filename instead of an isolated temp-directory path — a
  choice that looked harmless because the real project happened to already have a file
  at that name, masking what would otherwise have been every test run quietly writing
  (or in a fresh checkout, seeding) a file into the actual repository. Caught by tracing
  through exactly what path each check would resolve to before trusting a passing test
  suite, not by the test failing (it didn't — that was the point).
