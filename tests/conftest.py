"""Shared test configuration.

The pool's keep-alive re-check (ACCOUNT_KEEPALIVE_SECS) deliberately talks to
the upstream the first time an account is picked, because "is this token still
valid" has no local answer. Unit tests must not inherit that: they build pools
with real adapters and fake tokens, so the check would leave the process and
quarantine the account mid-test. Tests that DO cover keep-alive re-enable it
explicitly (see tests/test_account_keepalive.py).
"""
import pytest

import account_pool


@pytest.fixture(autouse=True)
def _no_upstream_keepalive(monkeypatch):
    monkeypatch.setattr(account_pool, "ACCOUNT_KEEPALIVE_SECS", 0.0)
