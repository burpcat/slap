"""Tests for slap/gmass_cache.py (post-launch feature: Redis-backed cache
for the dashboard's GMass-derived widgets). No real Redis server is used —
FakeRedis is an in-memory stand-in covering exactly the operations this
module needs, mirroring this project's existing "mock the external service
at the boundary" convention (see test_gmass.py's mocked requests.post/get).
"""
from datetime import datetime, timedelta, timezone

import pytest
import redis as redis_lib

from slap.gmass_cache import (
    RedisUnavailable, acquire_lock, invalidate, is_fresh, read_cache, refresh_with_lock,
    release_lock, write_cache,
)


class FakeRedis:
    def __init__(self):
        self._store = {}

    def ping(self):
        return True

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    def delete(self, key):
        self._store.pop(key, None)


class FakeRedisDown:
    def _raise(self, *a, **k):
        raise redis_lib.exceptions.ConnectionError("simulated: redis unreachable")

    def ping(self):
        self._raise()

    def get(self, key):
        self._raise()

    def set(self, key, value, *, ex=None, nx=False):
        self._raise()

    def delete(self, key):
        self._raise()


# --- read_cache / write_cache -----------------------------------------------

def test_write_then_read_cache_round_trips():
    client = FakeRedis()
    data = {"cached_at": "2026-08-01T00:00:00+00:00", "engagement": {"has_data": True}}
    write_cache(client, data)
    assert read_cache(client) == data


def test_read_cache_returns_none_when_nothing_cached_yet():
    assert read_cache(FakeRedis()) is None


def test_read_cache_returns_none_for_corrupted_entry():
    client = FakeRedis()
    client.set("slap:dashboard:gmass_cache", "not valid json{{{")
    assert read_cache(client) is None


def test_read_cache_raises_redis_unavailable_when_redis_is_down():
    with pytest.raises(RedisUnavailable):
        read_cache(FakeRedisDown())


def test_write_cache_raises_redis_unavailable_when_redis_is_down():
    with pytest.raises(RedisUnavailable):
        write_cache(FakeRedisDown(), {"cached_at": "2026-08-01T00:00:00+00:00"})


# --- is_fresh ----------------------------------------------------------------

def test_is_fresh_true_within_max_age():
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    cached = {"cached_at": (now - timedelta(minutes=30)).isoformat()}
    assert is_fresh(cached, now=now) is True


def test_is_fresh_false_past_max_age():
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    cached = {"cached_at": (now - timedelta(hours=2)).isoformat()}
    assert is_fresh(cached, now=now) is False


def test_is_fresh_exactly_at_boundary_is_false():
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    cached = {"cached_at": (now - timedelta(hours=1)).isoformat()}
    assert is_fresh(cached, now=now) is False


# --- locking (fenced — iron-audit BLOCKER fix) ------------------------------
# acquire_lock() returns a random token (or None if already held);
# release_lock() only deletes the lock if it still holds that exact token.

def test_acquire_lock_succeeds_when_free_and_returns_a_token():
    token = acquire_lock(FakeRedis())
    assert token is not None
    assert isinstance(token, str)


def test_acquire_lock_fails_when_already_held():
    client = FakeRedis()
    assert acquire_lock(client) is not None
    assert acquire_lock(client) is None


def test_acquire_lock_returns_different_tokens_each_time():
    client = FakeRedis()
    token1 = acquire_lock(client)
    release_lock(client, token1)
    token2 = acquire_lock(client)
    assert token1 != token2


def test_release_lock_allows_reacquiring():
    client = FakeRedis()
    token = acquire_lock(client)
    release_lock(client, token)
    assert acquire_lock(client) is not None


def test_release_lock_with_wrong_token_does_not_release_the_lock():
    # The actual bug this fixes: a refresh whose TTL already expired must
    # NEVER be able to delete a DIFFERENT, later caller's lock just because
    # its own (stale) release_lock() call finally runs.
    client = FakeRedis()
    real_token = acquire_lock(client)
    release_lock(client, "some-other-caller's-token")
    assert acquire_lock(client) is None  # still held by the ORIGINAL token
    release_lock(client, real_token)
    assert acquire_lock(client) is not None  # only the real owner can release it


def test_release_lock_after_another_caller_already_acquired_it_does_not_steal_it():
    # Simulates: refresh A's lock TTL expires while A is still legitimately
    # running; refresh B then acquires a FRESH lock (a new token). A's
    # delayed `finally: release_lock(token_a)` must not delete B's lock.
    client = FakeRedis()
    token_a = acquire_lock(client)
    # Simulate A's TTL expiring by directly clearing the key (FakeRedis
    # doesn't model real time-based expiry), then B acquiring fresh.
    client.delete("slap:dashboard:gmass_cache:lock")
    token_b = acquire_lock(client)
    assert token_b is not None
    assert token_b != token_a

    release_lock(client, token_a)  # A's stale, delayed release
    assert acquire_lock(client) is None  # B's lock must still be held


def test_acquire_lock_raises_redis_unavailable_when_redis_is_down():
    with pytest.raises(RedisUnavailable):
        acquire_lock(FakeRedisDown())


def test_release_lock_is_best_effort_when_redis_is_down():
    release_lock(FakeRedisDown(), "any-token")  # must not raise


# --- invalidate ----------------------------------------------------------------

def test_invalidate_removes_cached_entry():
    client = FakeRedis()
    write_cache(client, {"cached_at": "2026-08-01T00:00:00+00:00"})
    invalidate(client)
    assert read_cache(client) is None


def test_invalidate_is_best_effort_when_redis_is_down():
    invalidate(FakeRedisDown())  # must not raise


# --- refresh_with_lock ---------------------------------------------------------

def test_refresh_with_lock_runs_refresh_fn_and_caches_result():
    client = FakeRedis()
    calls = []

    def refresh_fn():
        calls.append(1)
        return {"cached_at": "2026-08-01T00:00:00+00:00", "engagement": {"has_data": True}}

    result = refresh_with_lock(client, refresh_fn)
    assert len(calls) == 1
    assert result == {"cached_at": "2026-08-01T00:00:00+00:00", "engagement": {"has_data": True}}
    assert read_cache(client) == result


def test_refresh_with_lock_releases_lock_after_success():
    client = FakeRedis()
    refresh_with_lock(client, lambda: {"cached_at": "2026-08-01T00:00:00+00:00"})
    assert acquire_lock(client) is not None  # lock was released, so a fresh acquire succeeds


def test_refresh_with_lock_releases_lock_even_if_refresh_fn_raises():
    client = FakeRedis()

    def failing_refresh():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        refresh_with_lock(client, failing_refresh)
    assert acquire_lock(client) is not None  # not stuck held despite the exception


def test_refresh_with_lock_returns_none_and_skips_refresh_fn_when_lock_already_held():
    client = FakeRedis()
    acquire_lock(client)  # simulates another process (e.g. the hourly job) already refreshing
    calls = []

    result = refresh_with_lock(client, lambda: calls.append(1))
    assert result is None
    assert calls == []  # never ran a second, concurrent refresh


def test_refresh_with_lock_only_releases_its_own_lock_not_a_later_callers(monkeypatch):
    # End-to-end version of the fencing fix: a refresh whose OWN lock has
    # (for whatever reason) already been replaced by a later caller's lock
    # must not delete that later lock when it finishes.
    client = FakeRedis()
    captured_token = {}

    real_release = release_lock

    def spying_release(redis_client, token):
        captured_token["value"] = token
        # Simulate a DIFFERENT caller having taken over the lock key in the
        # meantime (as if this refresh's TTL had expired mid-run).
        redis_client._store["slap:dashboard:gmass_cache:lock"] = "someone-else-entirely"
        real_release(redis_client, token)

    import slap.gmass_cache as gmass_cache_module
    monkeypatch.setattr(gmass_cache_module, "release_lock", spying_release)

    refresh_with_lock(client, lambda: {"cached_at": "2026-08-01T00:00:00+00:00"})
    # The other caller's lock must have survived our (fenced) release attempt.
    assert client.get("slap:dashboard:gmass_cache:lock") == "someone-else-entirely"


# --- refresh_with_lock vs. a concurrent invalidate() (SHOULD-FIX) -----------

def test_refresh_with_lock_skips_its_own_write_if_invalidated_after_it_started():
    # The race: a refresh reads stale (pre-tag) data, THEN a reply gets
    # tagged (invalidate() called), THEN the refresh finishes and would
    # otherwise silently overwrite the invalidation with its stale snapshot.
    client = FakeRedis()

    def refresh_fn():
        invalidate(client)  # simulates the tag landing WHILE this refresh is running
        return {"cached_at": "2026-08-01T00:00:00+00:00", "replies": ["stale, pre-tag snapshot"]}

    result = refresh_with_lock(client, refresh_fn)
    assert result == {"cached_at": "2026-08-01T00:00:00+00:00", "replies": ["stale, pre-tag snapshot"]}
    # The caller still gets the computed data for this one render, but it
    # must NOT have been written back into the cache.
    assert read_cache(client) is None


def test_refresh_with_lock_writes_normally_when_no_invalidation_occurs():
    client = FakeRedis()
    result = refresh_with_lock(client, lambda: {"cached_at": "2026-08-01T00:00:00+00:00", "replies": []})
    assert read_cache(client) == result


def test_refresh_with_lock_writes_normally_when_invalidation_happened_before_it_started():
    # An invalidation that already happened BEFORE this refresh even began
    # is exactly what triggered it (the normal stale/missing-cache path) —
    # must not spuriously block every future write forever.
    client = FakeRedis()
    invalidate(client)  # e.g. from an earlier, already-resolved tag
    result = refresh_with_lock(client, lambda: {"cached_at": "2026-08-01T00:00:00+00:00", "replies": []})
    assert read_cache(client) == result
