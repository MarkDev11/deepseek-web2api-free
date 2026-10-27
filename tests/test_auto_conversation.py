"""Regression tests for automatic conversation continuity (v3.3.15).

Clients without an explicit conversation id (agent harnesses, plain SDK
scripts) used to burn a brand-new DeepSeek chat on EVERY turn ("ngespam
sesi baru").  _resolve_cache_key now matches a resent, growing history
against the caller's recent auto-conversations; sticky mode keeps even
compacted/edited histories on one thread per caller.  Explicit ids and
cross-caller isolation are untouched.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request as StarletteRequest

import server
from session_cache import ChatSession, SessionCache


def _request(ip: str = "10.0.0.1", auth: str | None = None) -> StarletteRequest:
    headers = []
    if auth:
        headers.append((b"authorization", auth.encode("utf-8")))
    return StarletteRequest({
        "type": "http", "http_version": "1.1", "method": "POST",
        "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
        "query_string": b"", "root_path": "", "scheme": "http",
        "server": ("test", 80), "client": (ip, 1234), "headers": headers,
    })


@pytest.fixture()
def auto_on(monkeypatch):
    cache = SessionCache(ttl=60)
    monkeypatch.setattr(server, "SESSION_CACHE", cache)
    monkeypatch.setattr(server, "SESSION_AUTO_MATCH", True)
    monkeypatch.setattr(server, "SESSION_AUTO_STICKY", True)
    return cache


def _sticky_off(monkeypatch):
    cache = SessionCache(ttl=60)
    monkeypatch.setattr(server, "SESSION_CACHE", cache)
    monkeypatch.setattr(server, "SESSION_AUTO_MATCH", True)
    monkeypatch.setattr(server, "SESSION_AUTO_STICKY", False)
    return cache


def _seed(cache: SessionCache, key: str, sent_prompt: str) -> None:
    sess = ChatSession(chat_session_id=f"up-{key[:8]}", account_id="acct-a")
    sess.sent_prompt = sent_prompt
    cache.put(key, sess)


def test_first_turn_mints_a_key_and_extension_reuses_it(auto_on):
    req = _request()
    p1 = "User: halo"
    k1 = server._resolve_cache_key(req, None, p1)
    assert k1
    _seed(auto_on, k1, p1)
    assert server._resolve_cache_key(req, None, p1 + "\nUser: apa kabar") == k1


def test_unrelated_prompt_sticks_to_the_latest_session(auto_on):
    req = _request()
    k1 = server._resolve_cache_key(req, None, "User: pertama")
    _seed(auto_on, k1, "User: pertama")
    # DSH-style compaction: the history no longer extends anything stored.
    assert server._resolve_cache_key(req, None, "User: ringkasan sesi kita") == k1


def test_prefix_only_mode_starts_a_new_conversation(monkeypatch):
    cache = _sticky_off(monkeypatch)
    req = _request()
    k1 = server._resolve_cache_key(req, None, "User: obrolan A")
    _seed(cache, k1, "User: obrolan A")
    k2 = server._resolve_cache_key(req, None, "User: obrolan B sama sekali")
    assert k2 != k1
    _seed(cache, k2, "User: obrolan B sama sekali")
    assert server._resolve_cache_key(req, None, "User: obrolan B sama sekali\nUser: lanjut") == k2


def test_longest_extension_wins(monkeypatch):
    cache = _sticky_off(monkeypatch)
    req = _request()
    kb = server._resolve_cache_key(req, None, "User: X")
    _seed(cache, kb, "User: X\nUser: Y")
    ka = server._resolve_cache_key(req, None, "User: Z")  # unrelated -> own key
    _seed(cache, ka, "User: X")
    assert server._resolve_cache_key(req, None, "User: X\nUser: Y\nUser: Z") == kb


def test_feature_off_restores_isolation(monkeypatch):
    monkeypatch.setattr(server, "SESSION_AUTO_MATCH", False)
    assert server._resolve_cache_key(_request(), None, "User: apa pun") is None


def test_explicit_conversation_id_always_wins(auto_on):
    req = _request()
    k1 = server._resolve_cache_key(req, None, "User: auto")
    _seed(auto_on, k1, "User: auto")
    explicit = server._cache_key(req, "user:stable")
    got = server._resolve_cache_key(req, "user:stable", "User: auto\nUser: nyambung juga")
    assert got == explicit != k1


def test_ip_scopes_do_not_share_auto_conversations(auto_on):
    ka = server._resolve_cache_key(_request(ip="10.0.0.1"), None, "User: sama")
    _seed(auto_on, ka, "User: sama")
    kb = server._resolve_cache_key(_request(ip="10.0.0.2"), None, "User: sama")
    assert kb != ka


def test_api_key_scopes_do_not_share_auto_conversations(auto_on):
    ka = server._resolve_cache_key(_request(auth="Bearer key-1"), None, "User: sama")
    _seed(auto_on, ka, "User: sama")
    kb = server._resolve_cache_key(_request(auth="Bearer key-2"), None, "User: sama")
    assert kb != ka


def test_reused_auto_session_creates_no_second_upstream_session(auto_on):
    class _Adapter:
        def __init__(self):
            self.created = 0

        def create_session(self):
            self.created += 1
            return f"session-{self.created}"

    class _Account:
        id = "acct-a"

        def __init__(self):
            self.adapter = _Adapter()

    req = _request()
    account = _Account()
    p1 = "User: halo"
    key = server._resolve_cache_key(req, None, p1)

    first = server.AcquiredAccount(account, cache_key=key)
    assert first.prepare_prompt(p1) == p1
    first.create_session()
    first.record_message_id(2, full_prompt=p1)
    assert account.adapter.created == 1

    p2 = p1 + "\nUser: siapa namaku"
    assert server._resolve_cache_key(req, None, p2) == key
    second = server.AcquiredAccount(account, cache_key=key)
    assert second.prepare_prompt(p2) == "User: siapa namaku"
    assert second.create_session() == "session-1"
    assert account.adapter.created == 1
