"""Regression tests for the multi-turn context fixes (v3.3.8).

Bug 1: a client that resends only the newest user message (so its history does
not extend the previously sent prompt) used to have its cached upstream session
invalidated, which silently dropped the whole conversation context. The fix
keeps the session and parent chain and resends the full prompt.

Also covered: the stream/non-stream cache namespace merge, and the
model_type/thinking resolution after the quick-vs-expert mode merge.
"""
from __future__ import annotations

import json

import server
from session_cache import SessionCache


class _Adapter:
    def __init__(self, name: str):
        self.name = name
        self.created = 0

    def create_session(self) -> str:
        self.created += 1
        return f"session-{self.name}-{self.created}"


class _Account:
    def __init__(self, name: str):
        self.id = name
        self.adapter = _Adapter(name)


def _fresh_cache(monkeypatch):
    cache = SessionCache(ttl=60)
    monkeypatch.setattr(server, "SESSION_CACHE", cache)
    return cache


def test_divergent_history_keeps_the_session_and_parent_chain(monkeypatch):
    """Minimal-style client: turn 1 then only the new user message."""
    _fresh_cache(monkeypatch)
    account = _Account("account-a")
    key = "conv:regression-divergence"

    first = server.AcquiredAccount(account, cache_key=key)
    assert first.prepare_prompt("User: 我叫小红") == "User: 我叫小红"
    first.create_session()
    first.record_message_id(2, full_prompt="User: 我叫小红")

    # Second turn: history does NOT extend the first prompt -> divergence.
    second = server.AcquiredAccount(account, cache_key=key)
    sent = second.prepare_prompt("User: 我叫什么名字？")
    # The full prompt is resent (it carries the only context available)...
    assert sent == "User: 我叫什么名字？"
    # ...on the SAME session, with the parent chain intact.
    assert second.create_session() == "session-account-a-1"
    assert second.parent_message_id == 2
    assert server.SESSION_CACHE.get(key) is not None

    second.record_message_id(4, full_prompt="User: 我叫什么名字？")
    cached = server.SESSION_CACHE.get(key)
    assert cached.chat_session_id == "session-account-a-1"
    assert cached.parent_message_id == 4
    assert cached.diverged_turns == 1


def test_divergence_does_not_create_a_second_upstream_session(monkeypatch):
    _fresh_cache(monkeypatch)
    account = _Account("account-a")
    key = "conv:regression-no-new-session"

    first = server.AcquiredAccount(account, cache_key=key)
    first.prepare_prompt("User: 第一句")
    first.create_session()
    first.record_message_id(2, full_prompt="User: 第一句")

    second = server.AcquiredAccount(account, cache_key=key)
    second.prepare_prompt("User: 完全不同的第二句")
    second.create_session()
    assert account.adapter.created == 1, "divergence must not burn a new upstream session"


def test_continuation_still_sends_only_the_new_tail(monkeypatch):
    """Agent-style client resending the whole history keeps the delta prompt."""
    _fresh_cache(monkeypatch)
    account = _Account("account-a")
    key = "conv:regression-delta"

    first = server.AcquiredAccount(account, cache_key=key)
    first.prepare_prompt("User: 你好")
    first.create_session()
    first.record_message_id(2, full_prompt="User: 你好")

    second = server.AcquiredAccount(account, cache_key=key)
    full = "User: 你好\nUser: 天气如何"
    assert second.prepare_prompt(full) == "User: 天气如何"
    assert second.create_session() == "session-account-a-1"
    assert second.parent_message_id == 2


def test_every_divergence_resends_the_full_prompt(monkeypatch):
    """A minimal-style client stays on the same chain turn after turn."""
    _fresh_cache(monkeypatch)
    account = _Account("account-a")
    key = "conv:regression-always-diverges"

    first = server.AcquiredAccount(account, cache_key=key)
    first.prepare_prompt("User: 第一句")
    first.create_session()
    first.record_message_id(2, full_prompt="User: 第一句")

    parent = 2
    for turn, text in enumerate(["User: 第二句", "User: 第三句"], start=1):
        acq = server.AcquiredAccount(account, cache_key=key)
        assert acq.prepare_prompt(text) == text          # full prompt, every time
        assert acq.create_session() == "session-account-a-1"
        assert acq.parent_message_id == parent
        parent += 2
        acq.record_message_id(parent, full_prompt=text)

    assert account.adapter.created == 1
    assert server.SESSION_CACHE.get(key).diverged_turns == 2


def test_exact_replay_is_resent_and_keeps_session(monkeypatch):
    _fresh_cache(monkeypatch)
    account = _Account("account-a")
    key = "conv:regression-replay"

    first = server.AcquiredAccount(account, cache_key=key)
    first.prepare_prompt("User: 重试这一句")
    first.create_session()
    first.record_message_id(2, full_prompt="User: 重试这一句")

    second = server.AcquiredAccount(account, cache_key=key)
    assert second.prepare_prompt("User: 重试这一句") == "User: 重试这一句"
    assert second.create_session() == "session-account-a-1"


def test_stream_and_nonstream_share_one_cache_entry():
    """Flipping `stream` mid-thread must not start a new upstream conversation."""
    request = type("Request", (), {"headers": {"Authorization": "Bearer api-key"}})()

    key = server._cache_key(request, "user:alice")
    assert key is not None and key.startswith("conv:")

    # The legacy positional form (request, mode, conversation_id) is still
    # accepted and resolves to the very same key.
    assert server._cache_key(request, "nonstream", "user:alice") == key
    assert server._cache_key(request, "stream", "user:alice") == key


def test_cache_key_still_scoped_per_caller_and_conversation():
    a = type("Request", (), {"headers": {"Authorization": "Bearer api-key-a"}})()
    b = type("Request", (), {"headers": {"Authorization": "Bearer api-key-b"}})()
    assert server._cache_key(a, "user:alice") != server._cache_key(b, "user:alice")
    assert server._cache_key(a, "user:alice") != server._cache_key(a, "user:bob")


# ── model_type / thinking resolution after the mode merge ────────────────


def _decision(model="deepseek-chat", raw_routes=None, **kwargs):
    router = server.ModelRouter(
        json.dumps(raw_routes or {"deepseek-chat": "default", "deepseek-reasoner": "thinking"})
    )
    return router.route_for(model), kwargs


def test_route_thinking_preset_beats_env_and_client():
    decision, _ = _decision("deepseek-reasoner")
    model_type, thinking, search = server.resolve_model_controls(
        decision, req_thinking=False, req_search=True
    )
    assert thinking is True          # route preset wins over the client field
    assert search is True            # search untouched by the route
    assert model_type == "default"   # protocol field, never "expert"


def test_client_field_applies_when_route_says_nothing():
    decision, _ = _decision("some-unknown-model")
    _, thinking, search = server.resolve_model_controls(
        decision, req_thinking=True, req_search=True
    )
    assert thinking is True
    assert search is True


def test_thinking_env_forces_both_directions(monkeypatch):
    decision, _ = _decision("some-unknown-model")

    monkeypatch.setattr(server, "THINKING", "disabled")
    assert server.resolve_model_controls(decision, req_thinking=True)[1] is False

    monkeypatch.setattr(server, "THINKING", "enabled")
    assert server.resolve_model_controls(decision, req_thinking=False)[1] is True


def test_legacy_mode_expert_maps_to_thinking(monkeypatch):
    decision, _ = _decision("some-unknown-model")
    monkeypatch.setattr(server, "THINKING", "auto")
    monkeypatch.setattr(server, "MODE", "expert")
    assert server.resolve_model_controls(decision, req_thinking=False)[1] is True

    monkeypatch.setattr(server, "MODE", "quick")
    assert server.resolve_model_controls(decision, req_thinking=True)[1] is False

    monkeypatch.setattr(server, "MODE", "auto")
    assert server.resolve_model_controls(decision, req_thinking=True)[1] is True


def test_model_type_is_never_expert_for_shorthand_routes():
    """meta: no application code may resurrect the dead expert model selector."""
    for name in ("deepseek-chat", "deepseek-reasoner"):
        decision, _ = _decision(name)
        assert decision.model_type == "default"
