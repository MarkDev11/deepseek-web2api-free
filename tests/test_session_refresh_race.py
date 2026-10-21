"""First-request-after-cold-start must not 502 on a refresh race.

The request path and the background keep-alive share one `_refreshing` mutex
on the account. When both fire at the same moment the request used to give up
with "credential refresh already in progress" and answer 502 — observed live on
the first call after the v3.3.11 deployment. Waiting the mutex out turns that
into a normal answer whenever the account is actually alive.
"""
from __future__ import annotations

import time

import pytest

import account_pool
import server
from account_pool import AccountPool
from session_cache import SessionCache


class _FlakyAdapter:
    """create_session() raises the auth error once, then works."""

    def __init__(self, succeed_on_attempt=2):
        self.calls = 0
        self.succeed_on_attempt = succeed_on_attempt

    def create_session(self) -> str:
        self.calls += 1
        if self.calls < self.succeed_on_attempt:
            raise account_pool.adapter_module.UpstreamAuthError("invalid token")
        return f"session-{self.calls}"


class _Account:
    def __init__(self, adapter):
        self.id = "account-a"
        self.adapter = adapter
        self.state = "idle"


class _Pool:
    """Stand-in for AccountPool with the two methods create_session() uses."""

    def __init__(self, refresh_result, waited=True):
        self.refresh_result = refresh_result
        self.waited = waited
        self.wait_calls = []

    def refresh_account(self, acct):
        return self.refresh_result

    def wait_for_refresh(self, acct, timeout=15.0):
        self.wait_calls.append(timeout)
        return self.waited

    def mark_recovered(self, acct):
        pass


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "SESSION_CACHE", SessionCache(ttl=60))
    # Real AccountPools must not read or extend data/accounts.json.
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.delenv("DEEPSEEK_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(account_pool, "ACCOUNT_KEEPALIVE_SECS", 0.0)


def test_colliding_keepalive_is_waited_out_instead_of_failing():
    adapter = _FlakyAdapter(succeed_on_attempt=2)
    fake_pool = _Pool((False, "credential refresh already in progress"))
    acq = server.AcquiredAccount(_Account(adapter), cache_key="conv:race")

    original = server.pool
    server.pool = fake_pool
    try:
        assert acq.create_session() == "session-2"
    finally:
        server.pool = original

    assert fake_pool.wait_calls, "the mutex must be waited out, not skipped"
    assert fake_pool.wait_calls[0] > 0


def test_a_genuinely_dead_account_still_reports_the_reason():
    adapter = _FlakyAdapter(succeed_on_attempt=99)
    fake_pool = _Pool((False, "token rejected: copy a fresh token from the browser"))
    acq = server.AcquiredAccount(_Account(adapter), cache_key="conv:dead")

    original = server.pool
    server.pool = fake_pool
    try:
        with pytest.raises(account_pool.adapter_module.UpstreamAuthError) as exc:
            acq.create_session()
    finally:
        server.pool = original

    assert "copy a fresh token" in str(exc.value)
    assert fake_pool.wait_calls == []


def test_wait_for_refresh_returns_immediately_when_idle():
    pool = AccountPool()
    acct = pool.add(token="opaque-token-idle", cookies="sid=idle", email="idle")

    started = time.time()
    assert pool.wait_for_refresh(acct, timeout=5.0) is True
    assert time.time() - started < 0.5


def test_wait_for_refresh_times_out_on_a_stuck_refresh():
    pool = AccountPool()
    acct = pool.add(token="opaque-token-stuck", cookies="sid=stuck", email="stuck")
    acct._refreshing = True
    try:
        started = time.time()
        assert pool.wait_for_refresh(acct, timeout=0.3) is False
        assert time.time() - started >= 0.29
    finally:
        acct._refreshing = False

    assert pool.wait_for_refresh(acct, timeout=0.1) is True
