"""How AccountPool drives credential refresh: proactive, reactive, manual.

The upstream call is faked at the adapter level, so nothing here touches the
network. Config flags are patched as module attributes because acquire() and
mark_error() read them at call time.
"""
import json
import threading
import pytest
import time

import base64
import json

import account_pool
from account_pool import AccountPool


def make_jwt(exp):
    """A signed-looking JWT whose only meaningful claim is `exp`."""
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "user-1", "exp": exp}).encode())
    return "eyJhbGciOiJIUzI1NiJ9." + payload.decode().rstrip("=") + ".signature"


class FakeAdapter:
    """Stands in for DeepSeekAdapter on one account."""

    def __init__(self, token=None, cookies=None, error=None):
        self.token = token
        self.cookies = cookies
        self.error = error
        self.calls = 0

    def refresh_credentials(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        new_token = self.token or make_jwt(int(time.time()) + 3600)
        return {
            "token": new_token,
            "cookies": self.cookies or "sid=fresh",
            "path": "/api/v0/auth/refresh",
            "expires_at": account_pool._jwt_expires_at(new_token),
            "raw": {},
        }


@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    for name in ["DEEPSEEK_TOKEN", "DEEPSEEK_COOKIES", "DEEPSEEK_EMAIL"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(account_pool, "TOKEN_REFRESH_ENABLED", True)
    monkeypatch.setattr(account_pool, "TOKEN_REFRESH_AHEAD_SECS", 900)
    monkeypatch.setattr(account_pool, "TOKEN_REFRESH_MIN_INTERVAL_SECS", 0)
    monkeypatch.delenv("DEEPSEEK_ENCRYPTION_KEY", raising=False)
    return tmp_path


def _account(pool, exp_offset=7200, adapter=None):
    acct = pool.add(token=make_jwt(int(time.time()) + exp_offset),
                    cookies="sid=old", email="acct")
    if adapter is not None:
        acct._adapter = adapter
    return acct


class TestManualRefresh:
    def test_add_records_expiry_from_the_jwt(self, pool_env):
        pool = AccountPool()
        exp = int(time.time()) + 7200
        acct = pool.add(token=make_jwt(exp), cookies="sid=1", email="acct")
        assert acct.token_expires_at == exp
        data = acct.to_dict()
        assert data["token_expires_at"] == exp
        assert 3600 < data["token_expires_in"] <= 7200
        assert data["last_refresh_status"] == ""

    def test_non_jwt_token_reports_unknown_expiry(self, pool_env):
        pool = AccountPool()
        acct = pool.add(token="opaque-token-value-without-dots", cookies="c", email="a")
        assert acct.token_expires_at == 0
        assert acct.to_dict()["token_expires_in"] is None

    def test_refresh_updates_credentials_and_persists(self, pool_env):
        pool = AccountPool()
        acct = _account(pool, adapter=FakeAdapter(token=make_jwt(int(time.time()) + 5400),
                                                  cookies="sid=fresh"))
        acct.state = "error"  # a refresh that works should clear the penalty
        ok, msg = pool.refresh_by_id(acct.id)

        assert ok is True and "auth/refresh" in msg
        assert acct.cookies == "sid=fresh"
        assert acct.state == "idle" and acct.error_count == 0
        assert acct.last_refresh_status == "ok" and acct.last_refresh_error == ""
        assert acct.token_expires_at > int(time.time())

        stored = json.loads((pool_env / "accounts.json").read_text(encoding="utf-8"))
        assert stored["accounts"][0]["token"] == acct.token
        assert stored["accounts"][0]["last_refresh_status"] == "ok"

    def test_refreshed_token_survives_a_pool_reload(self, pool_env):
        pool = AccountPool()
        acct = _account(pool, adapter=FakeAdapter(token=make_jwt(int(time.time()) + 5400)))
        pool.refresh_by_id(acct.id)

        reloaded = AccountPool()
        assert reloaded.count() == 1
        fresh = reloaded.get_all()[0]
        assert fresh["token_expires_at"] == acct.token_expires_at
        assert fresh["last_refresh_status"] == "ok"

    def test_failure_is_recorded_without_losing_the_old_token(self, pool_env):
        from adapter import CredentialRefreshError
        pool = AccountPool()
        acct = _account(pool, adapter=FakeAdapter(error=CredentialRefreshError("no route")))
        before = acct.token

        ok, msg = pool.refresh_by_id(acct.id)

        assert ok is False and "no route" in msg
        assert acct.token == before
        assert acct.last_refresh_status == "failed"
        assert acct.last_refresh_error == "no route"
        assert acct.last_refresh_at > 0

    def test_unknown_account_id(self, pool_env):
        pool = AccountPool()
        assert pool.refresh_by_id("missing") == (False, "Account not found")

    def test_concurrent_refresh_collapses(self, pool_env):
        pool = AccountPool()
        started = threading.Event()
        release = threading.Event()
        acct = _account(pool)

        class SlowAdapter(FakeAdapter):
            def refresh_credentials(self):
                started.set()
                assert release.wait(5)
                return super().refresh_credentials()

        acct._adapter = SlowAdapter()
        results = []
        thread = threading.Thread(target=lambda: results.append(pool.refresh_account(acct)))
        thread.start()
        assert started.wait(5)
        second = pool.refresh_account(acct)
        release.set()
        thread.join(5)

        assert second[0] is False and "in progress" in second[1]
        assert results and results[0][0] is True


class TestAdminRefreshEndpoint:
    """POST /admin/api/accounts/{id}/refresh, called directly (no HTTP layer)."""

    def _call(self, monkeypatch, pool, account_id):
        import asyncio

        import admin

        monkeypatch.setattr(admin, "get_pool", lambda: pool)
        monkeypatch.setattr(admin, "_check_auth", lambda request: None)
        return asyncio.run(admin.refresh_account(account_id, request=None))

    def test_success_returns_the_updated_account(self, monkeypatch, pool_env):
        pool = AccountPool()
        acct = _account(
            pool, adapter=FakeAdapter(token=make_jwt(int(time.time()) + 5400)))

        resp = self._call(monkeypatch, pool, acct.id)

        assert resp.ok is True
        assert resp.account["id"] == acct.id
        assert resp.account["last_refresh_status"] == "ok"
        assert resp.account["token_expires_in"] > 0

    def test_unknown_account_reports_failure_without_a_snapshot(self, monkeypatch, pool_env):
        pool = AccountPool()
        resp = self._call(monkeypatch, pool, "missing")
        assert resp.ok is False
        assert resp.account is None
    def _spy(self, monkeypatch):
        calls = []
        done = threading.Event()

        def fake_do_refresh(self, acct, reason):
            calls.append((acct.id, reason))
            # The real _do_refresh stamps the attempt; the cooldown reads it.
            acct.last_refresh_at = int(time.time())
            acct._refreshing = False
            done.set()
            return True, "stub"

        monkeypatch.setattr(AccountPool, "_do_refresh", fake_do_refresh)
        return calls, done

    def test_auth_failure_triggers_a_refresh(self, monkeypatch, pool_env):
        pool = AccountPool()
        calls, done = self._spy(monkeypatch)
        acct = _account(pool, exp_offset=86400)  # far expiry: only the error triggers it

        pool.mark_error(acct, "credentials rejected: 40003 - Authorization Failed (invalid token)")

        assert done.wait(5)
        assert calls == [(acct.id, "reactive")]

    def test_other_errors_do_not_trigger_a_refresh(self, monkeypatch, pool_env):
        pool = AccountPool()
        calls, _done = self._spy(monkeypatch)
        acct = _account(pool, exp_offset=86400)

        pool.mark_error(acct, "upstream returned an empty response")

        assert calls == []

    def test_acquire_refreshes_proactively_near_expiry(self, monkeypatch, pool_env):
        pool = AccountPool()
        calls, done = self._spy(monkeypatch)
        acct = _account(pool, exp_offset=300)  # inside the 900s ahead window

        acquired = pool.acquire()
        assert acquired.id == acct.id
        assert done.wait(5)
        assert calls == [(acct.id, "proactive")]

    def test_acquire_leaves_a_fresh_token_alone(self, monkeypatch, pool_env):
        pool = AccountPool()
        calls, _done = self._spy(monkeypatch)
        _account(pool, exp_offset=86400)

        assert pool.acquire() is not None
        assert calls == []

    def test_cooldown_limits_the_refresh_rate(self, monkeypatch, pool_env):
        monkeypatch.setattr(account_pool, "TOKEN_REFRESH_MIN_INTERVAL_SECS", 3600)
        pool = AccountPool()
        calls, done = self._spy(monkeypatch)
        acct = _account(pool, exp_offset=300)

        assert pool.acquire() is not None
        assert done.wait(5)

        pool.release(acct)
        acct._refreshing = False
        assert pool.acquire() is not None
        time.sleep(0.2)
        assert calls == [(acct.id, "proactive")]  # second attempt suppressed

    def test_disabled_by_configuration(self, monkeypatch, pool_env):
        monkeypatch.setattr(account_pool, "TOKEN_REFRESH_ENABLED", False)
        pool = AccountPool()
        calls, _done = self._spy(monkeypatch)
        acct = _account(pool, exp_offset=10)

        assert pool.acquire() is not None
        pool.mark_error(acct, "credentials rejected: 40003")
        time.sleep(0.2)
        assert calls == []
