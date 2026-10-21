"""Regression tests for GET /admin/api/env.

Two separate prod bugs live here:

* Vercel holds several env vars as present-but-empty strings (PORT="" among
  them). int("") raised ValueError, so the whole endpoint 500'd and the
  WebUI Settings page went blank with no clue why.
* the version reported to the panel was hardcoded and drifted from
  server.app.version.

Both are cheap to pin, and both were invisible to the old test suite because
every test runs with the variables unset rather than empty.
"""
import asyncio

import pytest
from starlette.requests import Request

import admin
import server


def _request(token: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/admin/api/env",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "query_string": b"",
    })


@pytest.fixture
def authed():
    token = admin._generate_token()
    admin._tokens.add(token)
    yield token
    admin._tokens.discard(token)


def test_empty_numeric_env_does_not_break_the_endpoint(authed, monkeypatch):
    for name in ["PORT", "SESSION_CACHE_TTL", "CLIENT_RPM_PER_KEY",
                 "CLIENT_RPM_PER_IP", "DSML_MAX_BUFFER_BYTES",
                 "DEEPSEEK_MAX_CONCURRENCY", "DEEPSEEK_CLIENT_SYNC_TTL_SECS"]:
        monkeypatch.setenv(name, "")

    info = asyncio.run(admin.env_info(_request(authed)))

    assert info["port"] == 8080
    assert info["session_cache_ttl"] == 1800
    assert info["rate_limit"]["per_key"] == 60
    assert info["rate_limit"]["per_ip"] == 120


def test_keepalive_and_cookie_persist_rows_are_exposed(authed):
    info = asyncio.run(admin.env_info(_request(authed)))
    rows = {row["name"]: row for row in info["env_overrides"]}

    assert rows["ACCOUNT_KEEPALIVE_SECS"]["value"] == "%g" % admin.account_pool.ACCOUNT_KEEPALIVE_SECS
    assert rows["ACCOUNT_COOKIE_PERSIST_SECS"]["value"] == "%g" % admin.account_pool.ACCOUNT_COOKIE_PERSIST_SECS


def test_reported_version_matches_the_fastapi_app(authed):
    info = asyncio.run(admin.env_info(_request(authed)))
    assert info["server_version"] == server.app.version == admin.SERVER_VERSION
