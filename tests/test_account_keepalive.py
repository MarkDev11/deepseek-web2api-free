"""Keep-alive + credential auto-sync (v3.3.11).

The upstream mints a bearer token only at login, so "auto update" cannot mean
"renew the token". What the pool does automatically is narrower but real:

* every completed turn mirrors the rotating cookies from the adapter's jar back
  into the account record (throttled storage writes), so a cold start resumes
  the freshest session instead of the values typed in when the account was
  added;
* an account whose last check is stale is re-verified in the background the
  next time it is picked, so a rejected token is quarantined before a user
  request pays for it.

Everything is faked at the adapter level; no test touches the network.
"""
import json
import time

import pytest

import account_pool
from account_pool import AccountPool


class JarAdapter:
    """Fake adapter whose cookie jar can change between calls."""

    def __init__(self, token="opaque-token", cookies="sid=old", jar="sid=old", check=None):
        self.token = token
        self.cookies = cookies
        self.jar = jar
        self._check = check
        self.checks = 0

    def cookie_blob(self):
        return self.jar

    def sync_cookies(self):
        if self.jar == self.cookies:
            return False
        self.cookies = self.jar
        return True

    def check_credentials(self):
        self.checks += 1
        if self._check is not None:
            return dict(self._check)
        return {"ok": True, "code": 0, "msg": "",
                "path": "/api/v0/users/settings", "detail": ""}


REJECTED = {"ok": False, "code": 40003,
            "msg": "Authorization Failed (invalid token)",
            "path": "/api/v0/users/settings", "detail": ""}
NETWORK = {"ok": False, "code": None, "msg": "",
           "path": "/api/v0/users/settings",
           "detail": "ConnectionError: upstream unreachable"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.delenv("DEEPSEEK_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(account_pool, "TOKEN_REFRESH_ENABLED", True)
    monkeypatch.setattr(account_pool, "TOKEN_REFRESH_MIN_INTERVAL_SECS", 0)
    monkeypatch.setattr(account_pool.adapter_module, "REFRESH_PATHS", [])
    monkeypatch.setattr(account_pool, "ACCOUNT_KEEPALIVE_SECS", 900.0)
    monkeypatch.setattr(account_pool, "ACCOUNT_COOKIE_PERSIST_SECS", 0.0)
    return tmp_path


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def account(pool, adapter):
    acct = pool.add(token="opaque-token", cookies="sid=old", email="acct")
    acct._adapter = adapter
    return acct


def stored(tmp_path):
    """Read the store, tolerating the millisecond a replace takes on Windows."""
    last = None
    for _ in range(40):
        try:
            return json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError, json.JSONDecodeError) as e:
            last = e
            time.sleep(0.05)
    raise AssertionError("store never became readable: %r" % (last,))


class TestQuarantineOnRejection:
    def test_rejected_token_leaves_rotation(self, env):
        pool = AccountPool()
        dead = account(pool, JarAdapter(check=REJECTED))
        alive = AccountPool  # a second, healthy account is what acquire() must find
        healthy = pool.add(token="opaque-token-2", cookies="sid=2", email="ok")
        healthy._adapter = JarAdapter(cookies="sid=2", jar="sid=2")

        ok, msg = pool.refresh_by_id(dead.id)

        assert ok is False
        assert "copy a fresh token" in msg
        assert dead.state == "error"
        assert "invalid token" in dead.last_error
        # The whole point: the dead account is no longer handed to requests.
        for _ in range(3):
            acct = pool.acquire()
            assert acct is healthy
            pool.release(acct)

    def test_transient_failure_keeps_the_account_in_rotation(self, env):
        pool = AccountPool()
        acct = account(pool, JarAdapter(check=NETWORK))

        ok, _ = pool.refresh_by_id(acct.id)

        assert ok is False
        assert acct.state == "idle"
        assert acct.last_refresh_status == "failed"

    def test_a_later_positive_check_lifts_the_quarantine(self, env):
        pool = AccountPool()
        acct = account(pool, JarAdapter(check=REJECTED))
        pool.refresh_by_id(acct.id)
        assert acct.state == "error"

        acct.adapter._check = None  # the user pasted fresh credentials
        acct.state = "idle"  # ...or the panel reset it; the check must agree
        ok, msg = pool.refresh_by_id(acct.id)

        assert ok is True and "valid" in msg
        assert acct.state == "idle" and acct.error_count == 0


class TestCookieAutoSync:
    def test_successful_turn_persists_the_rotated_jar(self, env):
        pool = AccountPool()
        adapter = JarAdapter(cookies="sid=old", jar="sid=old")
        acct = account(pool, adapter)
        pool.refresh_by_id(acct.id)

        adapter.jar = "sid=new; aws-waf-token=abc"
        pool.record_upstream_ok(acct)

        assert acct.cookies == "sid=new; aws-waf-token=abc"
        assert acct.last_ok_at > 0
        assert acct.to_dict()["last_ok_at"] == acct.last_ok_at
        # The write itself happens on a background thread; give it a moment.
        time.sleep(0.5)
        assert stored(env)["accounts"][0]["cookies"] == "sid=new; aws-waf-token=abc"

    def test_unchanged_jar_does_not_rewrite_the_store(self, env):
        pool = AccountPool()
        acct = account(pool, JarAdapter(cookies="sid=old", jar="sid=old"))
        before = (env / "accounts.json").read_text(encoding="utf-8")

        pool.record_upstream_ok(acct)
        time.sleep(0.2)

        assert acct.last_ok_at > 0
        assert (env / "accounts.json").read_text(encoding="utf-8") == before

    def test_storage_writes_are_throttled(self, env, monkeypatch):
        monkeypatch.setattr(account_pool, "ACCOUNT_COOKIE_PERSIST_SECS", 300.0)
        pool = AccountPool()
        adapter = JarAdapter(cookies="sid=old", jar="sid=old")
        acct = account(pool, adapter)

        adapter.jar = "sid=one"
        pool.record_upstream_ok(acct)
        time.sleep(0.5)
        assert stored(env)["accounts"][0]["cookies"] == "sid=one"

        adapter.jar = "sid=two"
        pool.record_upstream_ok(acct)
        time.sleep(0.3)

        # In memory the account is current; the store waits for the throttle
        # window so a chatty client cannot trigger a KV write per request.
        assert acct.cookies == "sid=two"
        assert stored(env)["accounts"][0]["cookies"] == "sid=one"


class TestKeepAliveRecheck:
    def test_stale_account_is_verified_off_the_request_path(self, env, monkeypatch):
        seen = []
        pool = AccountPool()
        acct = account(pool, JarAdapter())
        acct.last_refresh_at = int(time.time()) - 3600
        monkeypatch.setattr(pool, "_refresh_worker", lambda a, reason: seen.append(reason))

        picked = pool.acquire()

        assert picked is acct
        assert wait_for(lambda: seen == ["keepalive"])
        pool.release(acct)

    def test_fresh_account_is_not_rechecked(self, env, monkeypatch):
        seen = []
        pool = AccountPool()
        acct = account(pool, JarAdapter())
        acct.last_refresh_at = int(time.time())
        monkeypatch.setattr(pool, "_refresh_worker", lambda a, reason: seen.append(reason))

        pool.acquire()
        time.sleep(0.2)

        assert seen == []
        pool.release(acct)

    def test_disabled_keepalive_never_schedules(self, env, monkeypatch):
        seen = []
        monkeypatch.setattr(account_pool, "ACCOUNT_KEEPALIVE_SECS", 0.0)
        pool = AccountPool()
        acct = account(pool, JarAdapter())
        acct.last_refresh_at = int(time.time()) - 3600
        monkeypatch.setattr(pool, "_refresh_worker", lambda a, reason: seen.append(reason))

        pool.acquire()
        time.sleep(0.2)

        assert seen == []
        pool.release(acct)
