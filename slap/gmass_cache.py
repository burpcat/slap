"""Redis-backed cache for the dashboard's GMass-derived widgets (post-launch
feature). Generic Redis primitives ONLY — this module has no dependency on
slap.dashboard, deliberately, to avoid a circular import (dashboard.py calls
INTO this module; this module never calls back into dashboard.py). The
actual "what to cache" logic (running sync_reports() + the four GMass-
dependent widget functions) lives in slap/dashboard.py itself, composed with
the primitives here.

**Redis is a cache, never a new source of truth** — the exact same
principle as the `recipients` table (slap/tracking.py): SQLite's `events`
table is the one place facts live. Flushing this cache and letting the next
scheduled refresh repopulate it produces identical results with zero data
loss, by construction: every value ever written here is a pure function of
`events` at the moment it was computed (see slap.dashboard's
`compute_gmass_dependent_data`), never data that only exists in Redis.

**Locking is FENCED, not just atomic-to-acquire — an iron-audit BLOCKER
fix.** `acquire_lock()` writes a random token as the lock's VALUE (not a
fixed sentinel) and returns it; `release_lock()` only deletes the lock if
it still holds that EXACT token. Without this, a refresh whose lock TTL
expired while it was still legitimately running (a slow GMass sweep, or a
hung call before `slap.gmass.DEFAULT_TIMEOUT` existed) would have its
eventual `finally: release_lock()` blindly delete WHATEVER is currently in
the lock key — which, by then, could be a *different*, later caller's own
lock, letting a third refresh start concurrently. `refresh_with_lock()` is
still the ONE place a refresh can be triggered from — both `slap.py sync`
(the hourly job) and the dashboard's on-open fallback call it with the
same refresh_fn shape, never a parallel implementation.

**Cache invalidation vs. a concurrent in-flight refresh — the other
half of the same class of bug.** A reply tag (slap.dashboard's
`/reply/<recipient>/tag` route) calls `invalidate()` to make sure the
owner's own action is reflected immediately, not up to an hour later. But
if an hourly refresh had ALREADY read `actionable_replies()` (with the
not-yet-tagged reply still open) before the tag landed, its `write_cache()`
call — which finishes AFTER the tag's `invalidate()` — would silently
overwrite the invalidation with that now-stale snapshot, undoing it.
`refresh_with_lock()` records when IT started, and skips its own
`write_cache()` (without discarding the computed result — the caller still
gets to use it for this one render) if an invalidation was requested any
time after that start — see that function's docstring for the (accepted,
low-cost) conservative edge this creates.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import redis as redis_lib

CACHE_KEY = "slap:dashboard:gmass_cache"
LOCK_KEY = "slap:dashboard:gmass_cache:lock"
INVALIDATED_AT_KEY = "slap:dashboard:gmass_cache:invalidated_at"

# How old cached data can be before a dashboard load treats it as stale and
# triggers a synchronous refresh instead of rendering it directly.
MAX_AGE = timedelta(hours=1)

# Redis TTL on the cache entry itself — a safety net, not the freshness
# mechanism (that's MAX_AGE, checked against the entry's own `cached_at`).
# Long enough that a merely-slow hourly job never loses the previous cycle's
# data out from under a dashboard load; short enough that a permanently
# stopped sync job doesn't leave stale data lingering forever.
CACHE_TTL_SECONDS = 6 * 3600

# Upper bound on how long a single refresh could plausibly take. Every real
# HTTP call slap.gmass makes is now bounded by DEFAULT_TIMEOUT (20s) — an
# iron-audit BLOCKER fix found alongside this one, since an untimed call
# would have made ANY fixed lock TTL meaningless as a safety bound. Sized
# with real headroom above a realistic full sweep (dozens to a few hundred
# campaigns, growing over time) even if several calls hit their full
# timeout, while still being a bounded, self-healing limit on how long a
# genuinely crashed/killed refresh can hold the lock.
LOCK_TTL_SECONDS = 900


class RedisUnavailable(Exception):
    """Raised when Redis itself can't be reached at all (connection refused,
    timeout, DNS failure, ...) — distinct from a normal, expected "no cache
    entry exists yet" (which is not an error, just a cache miss). Callers
    use this distinction to decide whether to even attempt the lock/cache
    dance (skip it entirely and fall back to a live poll) or proceed
    normally (cache miss just means "go refresh, then cache the result")."""


def redis_client_from_url(url: str) -> "redis_lib.Redis":
    """One client per process is the correct, documented usage of redis-py
    (unlike sqlite3 connections, a redis.Redis client is thread-safe and
    pools its own connections internally) — callers build this ONCE, not
    per-request. Short, explicit timeouts are the whole point of this
    feature existing: a hung/unreachable Redis must fail FAST, not silently
    block a dashboard page load the way an untimed GMass call already did
    once before (see CONTROL_SHEET.md)."""
    return redis_lib.Redis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)


def _wrap_redis_errors(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except redis_lib.exceptions.RedisError as e:
        raise RedisUnavailable(str(e)) from e


def ping(redis_client) -> None:
    """Raises RedisUnavailable if Redis can't be reached at all. Used by
    both slap.doctor.check_redis and get_gmass_dependent_data's
    Redis-down-at-read-time fallback."""
    _wrap_redis_errors(redis_client.ping)


def read_cache(redis_client) -> dict | None:
    """The cached blob, or None if there's genuinely nothing cached yet
    (a normal, expected state — e.g. before the hourly job has ever run).
    Raises RedisUnavailable if Redis itself can't be reached — a real
    failure, handled differently by the caller (see slap.dashboard.
    get_gmass_dependent_data)."""
    raw = _wrap_redis_errors(redis_client.get, CACHE_KEY)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Corrupted entry (shouldn't happen — only this module ever writes
        # this key) is treated exactly like "nothing cached yet," never a
        # crash: the next refresh overwrites it cleanly either way.
        return None


def write_cache(redis_client, data: dict) -> None:
    _wrap_redis_errors(redis_client.set, CACHE_KEY, json.dumps(data), ex=CACHE_TTL_SECONDS)


def invalidate(redis_client) -> None:
    """Deletes the cached entry outright — called after a reply is tagged
    (slap.dashboard's /reply/<recipient>/tag route), since the tag can
    change actionable_replies()'s output and the owner reasonably expects
    that reflected on the very next page load, not up to an hour later.
    Forces the next dashboard load to take the same stale/missing-cache
    fallback path as any other cache miss — no separate "partial update"
    logic. Best-effort: if Redis is unreachable there's nothing to
    invalidate anyway (the dashboard already falls back to a live poll in
    that case), so this silently no-ops rather than failing the tag
    action over a caching concern.

    ALSO records when this happened (`INVALIDATED_AT_KEY`) so a refresh
    that was already in flight before this call, but finishes after it,
    can tell its own snapshot might already be stale relative to the tag —
    see refresh_with_lock()'s own docstring for why that matters."""
    try:
        redis_client.delete(CACHE_KEY)
        redis_client.set(INVALIDATED_AT_KEY, datetime.now(timezone.utc).isoformat(), ex=CACHE_TTL_SECONDS)
    except redis_lib.exceptions.RedisError:
        pass


def is_fresh(cached: dict, *, max_age: timedelta = MAX_AGE, now: datetime = None) -> bool:
    now = now or datetime.now(timezone.utc)
    cached_at = datetime.fromisoformat(cached["cached_at"])
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    return (now - cached_at) < max_age


def acquire_lock(redis_client, *, ttl: int = LOCK_TTL_SECONDS) -> str | None:
    """Atomic set-if-absent-with-expiry — a single `SET key val NX EX ttl`
    call (not a SETNX-then-EXPIRE pair), so there's no window where the
    lock exists without a TTL. Returns a random token identifying THIS
    acquisition if successful, or None if another refresh already holds
    the lock. The token MUST be passed to release_lock() — an unconditional
    delete there would let a refresh whose TTL already expired accidentally
    delete a DIFFERENT, later caller's lock (an iron-audit BLOCKER: this is
    what actually happened before this fix), letting a third refresh start
    concurrently with the second."""
    token = str(uuid.uuid4())
    acquired = _wrap_redis_errors(redis_client.set, LOCK_KEY, token, nx=True, ex=ttl)
    return token if acquired else None


def release_lock(redis_client, token: str) -> None:
    """Best-effort, and OWNERSHIP-CHECKED: only deletes the lock if it still
    holds the exact token acquire_lock() gave this caller — never blindly
    deletes whatever's currently there (see acquire_lock's own docstring for
    why an unconditional delete is a real bug, not a theoretical one). Not
    perfectly atomic (a true distributed lock would compare-and-delete via a
    Lua script) — for this app's actual concurrency (essentially two
    possible callers: the hourly job and an occasional dashboard load), the
    residual check-then-delete race is not worth shipping and testing a Lua
    script through a real Redis client. The lock's TTL is what actually
    guarantees it can never be stuck forever even if this is never reached
    (e.g. the process is killed mid-refresh); this just lets the NEXT
    refresh proceed sooner than waiting out the full TTL."""
    try:
        if redis_client.get(LOCK_KEY) == token:
            redis_client.delete(LOCK_KEY)
    except redis_lib.exceptions.RedisError:
        pass


def _invalidated_since(redis_client, since: datetime) -> bool:
    try:
        raw = redis_client.get(INVALIDATED_AT_KEY)
    except redis_lib.exceptions.RedisError:
        return False
    if raw is None:
        return False
    invalidated_at = datetime.fromisoformat(raw)
    if invalidated_at.tzinfo is None:
        invalidated_at = invalidated_at.replace(tzinfo=timezone.utc)
    # >=, not > : if the two timestamps tie exactly (possible — datetime.now()
    # has finite resolution, and these are two independent calls), the safe
    # default is to assume an invalidation MIGHT have raced this refresh and
    # skip the write — a missed write self-heals on the very next trigger,
    # while a wrongly-kept stale write would sit visible for up to an hour.
    return invalidated_at >= since


def refresh_with_lock(redis_client, refresh_fn, *, ttl: int = LOCK_TTL_SECONDS) -> dict | None:
    """Runs refresh_fn() (no arguments — the caller closes over whatever it
    needs) and writes its return value to the cache, but ONLY if this call
    successfully acquires the shared refresh lock first. Returns the fresh
    data on success, or None if another process (the hourly `sync` job, or
    a concurrent dashboard-open fallback) is already mid-refresh.

    Deliberately does NOT wait/retry/poll for the other refresh to finish —
    running refresh_fn() a second time concurrently is exactly the race this
    lock exists to prevent (two overlapping GMass polls could each read the
    same "not yet seen" dedup state before either commits its own writes,
    risking a duplicate event — see slap.dashboard's per-report-type dedup
    docstrings). A caller with no cached data to fall back to on a None
    result renders an honest empty/loading state for one page load rather
    than forcing a second refresh.

    This is the ONE place a refresh can be triggered from — both
    `slap.py sync` (the hourly job) and the dashboard's on-open fallback
    call this exact function with the exact same refresh_fn shape, never a
    parallel implementation, so the lock can never be accidentally
    bypassed on one path but not the other.

    **Skips its own write if invalidated mid-flight** (an iron-audit
    SHOULD-FIX): if `invalidate()` (e.g. a reply tag) was called any time
    after THIS call started, this refresh's own snapshot may already be
    stale relative to that action — writing it anyway would silently undo
    the invalidation for up to another hour. The computed data is still
    returned (so the caller can use it for this one render — it's a
    perfectly valid GMass snapshot, just not safe to treat as "the current
    cache" going forward), but the cache itself is left empty, so the NEXT
    read triggers a fresh, correctly-ordered refresh instead. This is
    deliberately conservative: an invalidation that happened DURING
    sync_reports() (well before actionable_replies() itself is read) may
    not have actually raced anything, but distinguishing that precisely
    isn't worth the added complexity for a low-cost outcome (one extra
    refresh, never a wrong one)."""
    token = acquire_lock(redis_client, ttl=ttl)
    if token is None:
        return None
    started_at = datetime.now(timezone.utc)
    try:
        data = refresh_fn()
        if not _invalidated_since(redis_client, started_at):
            write_cache(redis_client, data)
        return data
    finally:
        release_lock(redis_client, token)
