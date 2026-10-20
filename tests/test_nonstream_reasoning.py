"""Regression: non-streaming responses must expose the reasoning chain.

The adapter has always returned ``(content, thinking)`` for non-streaming calls,
and the streaming OpenAI path has always emitted ``reasoning_content`` deltas —
but the non-streaming OpenAI path collected ``thinking`` and then dropped it, so
``thinking_mode=true`` + ``stream=false`` silently lost the chain while
``stream=true`` showed it. Observed live: the same prompt returned 122 reasoning
chars when streaming and 0 when not.
"""
from __future__ import annotations

import json

import server


class _Adapter:
    def __init__(self, content: str, thinking: str):
        self._content = content
        self._thinking = thinking
        self.calls: list[dict] = []

    def create_session(self) -> str:
        return "sess-1"

    def chat(self, session_id, prompt, **kwargs):
        self.calls.append({"session_id": session_id, "prompt": prompt, **kwargs})
        # adapter.chat() fills ready_out so the server can persist the parent id.
        ready_out = kwargs.get("ready_out")
        if isinstance(ready_out, dict):
            ready_out["response_message_id"] = 2
        return self._content, self._thinking


class _Account:
    def __init__(self, adapter: _Adapter):
        self.id = "account-a"
        self.adapter = adapter


def _run(monkeypatch, content: str, thinking: str, **kwargs):
    adapter = _Adapter(content, thinking)
    monkeypatch.setattr(
        server, "_acquire", lambda **_: server.AcquiredAccount(_Account(adapter))
    )
    # Keep the real account pool and the global upstream semaphore out of the
    # picture: _acquire() was stubbed, so nothing was ever acquired and
    # release() would over-release the limiter. Only the response assembly (and
    # the capability hand-off to the adapter) is under test here.
    monkeypatch.setattr(server.AcquiredAccount, "release", lambda self: None)
    payload = server._handle_nonstream("chatcmpl-test", "User: hi", **kwargs)
    return payload, adapter


def test_reasoning_chain_is_returned_when_present(monkeypatch):
    payload, adapter = _run(
        monkeypatch, "答案是 9。", "先看题意：除了 9 只以外都跑了……", thinking_mode=True
    )
    message = payload["choices"][0]["message"]
    assert message["content"] == "答案是 9。"
    assert message["reasoning_content"] == "先看题意：除了 9 只以外都跑了……"
    assert message["role"] == "assistant"
    assert payload["choices"][0]["finish_reason"] == "stop"
    # the capability really was forwarded to the adapter
    assert adapter.calls[0]["thinking_enabled"] is True


def test_reasoning_content_is_omitted_when_thinking_was_off(monkeypatch):
    payload, adapter = _run(monkeypatch, "答案是 9。", "", thinking_mode=False)
    message = payload["choices"][0]["message"]
    assert message["content"] == "答案是 9。"
    assert "reasoning_content" not in message
    assert adapter.calls[0]["thinking_enabled"] is False


def test_reasoning_content_is_omitted_when_upstream_sent_no_chain(monkeypatch):
    # thinking_mode=true but the model answered directly (no THINK fragment):
    # `reasoning_content` must not appear as a misleading empty string.
    payload, _ = _run(monkeypatch, "2", "", thinking_mode=True)
    message = payload["choices"][0]["message"]
    assert message["content"] == "2"
    assert "reasoning_content" not in message


def test_tool_call_path_is_unaffected(monkeypatch):
    # A DSML tool call carries no user-facing answer; the tool_calls response
    # shape must stay exactly as it was.
    dsml = (
        "<|DSML|tool_calls>\n"
        '<|DSML|invoke name="get_weather">\n'
        '<|DSML|parameter name="city" string="true">Beijing</|DSML|parameter>\n'
        "</|DSML|invoke>\n"
        "</|DSML|tool_calls>"
    )
    tools = [
        server.ToolDef(
            **{
                "type": "function",
                "function": {"name": "get_weather", "description": "d", "parameters": {}},
            }
        )
    ]
    payload, _ = _run(monkeypatch, dsml, "思考中", tools=tools, thinking_mode=True)
    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert "reasoning_content" not in choice["message"]


def test_streaming_path_still_emits_reasoning_deltas(monkeypatch):
    """Sanity check that the two paths now agree on the field name."""
    payload, _ = _run(monkeypatch, "答案", "思考", thinking_mode=True)
    assert "reasoning_content" in payload["choices"][0]["message"]
    # the field name matches what _openai_chunk() puts in the SSE delta
    chunk = server._openai_chunk("id", reasoning_content="x")
    assert json.loads(chunk.split("data: ", 1)[1])["choices"][0]["delta"][
        "reasoning_content"
    ] == "x"
