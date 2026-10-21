"""Pacing (per-account cooldown) and upstream-mute handling in AccountPool.

DeepSeek enforces two different penalties: a per-IP/WAF rate limit that clears
by itself, and a per-account mute ("user is muted") that only expires. The pool
used to keep serving a muted account, which repeated the exact behaviour being
punished; these tests pin the replacement behaviour.
"""
import json
import time

import pytest

import account_pool


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.setattr(account_pool, "ACCOUNT_COOLDOWN_SECS", 0.0)
    monkeypatch.setattr(account_pool, "ACCOUNT_COOLDOWN_MAX_WAIT_SECS", 20.0)
    monkeypatch.setattr(account_pool, "ACCOUNT_MUTE_MIN_SECS", 300.0)
    return monkeypatch


def _pool(n: int = 1):
    pool = account_pool.AccountPool()
    accounts = [
        pool.add(token=f"tok-{i}", cookies=f"cookie-{i}", email=f"acc-{i}")
        for i in range(n)
    ]
    return pool, accounts


def test_muted_account_is_out_of_rotation_until_the_penalty_expires(clean_env):
    pool, (acct,) = _pool()
    first = pool.acquire()
    assert first.id == acct.id
    pool.release(first)

    pool.mark_muted(acct, time.time() + 60, "user is muted")

    # The whole point: the next request must not land on the punished account.
    assert pool.acquire() is None

    acct.muted_until = int(time.time()) - 1
    again = pool.acquire()
    assert again is not None and again.id == acct.id


def test_muted_account_is_not_used_for_an_offline_continuation(clean_env):
    pool, (acct,) = _pool()
    pool.mark_muted(acct, time.time() + 60, "user is muted")

    assert pool.acquire_by_id(acct.id) is None


def test_mark_muted_applies_a_floor_when_upstream_sends_no_timestamp(clean_env):
    pool, (acct,) = _pool()
    before = time.time()

    until = pool.mark_muted(acct, None, "user is muted")

    assert until >= int(before + account_pool.ACCOUNT_MUTE_MIN_SECS)
    assert acct.muted_until == until


def test_mark_muted_never_shortens_a_longer_upstream_penalty(clean_env):
    pool, (acct,) = _pool()
    far = int(time.time()) + 3600

    assert pool.mark_muted(acct, far, "muted") == far


def test_mute_expiry_survives_a_restart(clean_env, tmp_path):
    pool, (acct,) = _pool()
    until = pool.mark_muted(acct, time.time() + 120, "user is muted")

    stored = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
    assert stored["accounts"][0]["muted_until"] == until

    reloaded = account_pool.AccountPool()
    assert reloaded.get_all()[0]["muted_until"] == until
    # A restarted process must not serve the muted account either.
    assert reloaded.acquire() is None


def test_cooldown_skips_an_account_used_a_moment_ago(clean_env):
    clean_env.setattr(account_pool, "ACCOUNT_COOLDOWN_SECS", 60.0)
    pool, (acct,) = _pool()
    first = pool.acquire()
    pool.release(first)

    assert pool.acquire(wait_timeout=0) is None

    acct.last_used = time.time() - 61
    again = pool.acquire(wait_timeout=0)
    assert again is not None and again.id == acct.id


def test_cooldown_rotates_across_accounts_before_reusing_one(clean_env):
    clean_env.setattr(account_pool, "ACCOUNT_COOLDOWN_SECS", 60.0)
    pool, accounts = _pool(2)

    first = pool.acquire(wait_timeout=0)
    second = pool.acquire(wait_timeout=0)

    assert first is not None and second is not None
    assert first.id != second.id
    pool.release(first)
    pool.release(second)
    # Both are inside their cooling window and both are idle: no third slot.
    assert pool.acquire(wait_timeout=0) is None


def test_cooldown_waits_for_the_soonest_slot_instead_of_failing(clean_env):
    clean_env.setattr(account_pool, "ACCOUNT_COOLDOWN_SECS", 0.4)
    pool, (acct,) = _pool()
    first = pool.acquire(wait_timeout=0)
    pool.release(first)

    started = time.time()
    again = pool.acquire(wait_timeout=5)

    assert again is not None and again.id == acct.id
    assert time.time() - started >= 0.2


def test_all_busy_still_fails_fast(clean_env):
    pool, _ = _pool(1)
    held = pool.acquire()
    assert held is not None

    started = time.time()
    assert pool.acquire() is None
    # No account is cooling, so this must not sleep waiting for one.
    assert time.time() - started < 0.3


def test_editing_credentials_clears_an_old_mute(clean_env):
    pool, (acct,) = _pool()
    pool.mark_muted(acct, time.time() + 600, "user is muted")

    pool.update(acct.id, token="new-token", cookies="new-cookie")

    assert acct.muted_until == 0
    assert pool.acquire(wait_timeout=0) is not None
