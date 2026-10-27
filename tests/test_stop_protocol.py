"""Stop-protocol parity with the dsh-plugin (v3.3.14).

The plugin makes the upstream close every tools turn with a tool block or
[done]; without one the server keeps asking (cap 20) and the [done] token
never reaches the client. These tests pin the same behaviour on the proxy:
prompt injection, scrubbing, and the auto-continue loops on the streaming,
OpenAI non-streaming and Anthropic non-streaming paths.
"""
from __future__ import annotations

import json

import pytest

import server
from stop_protocol import (DONE_RE, auto_continue_max, has_done, REMINDER_TEXT,
                           STOP_PROTOCOL_TEXT, strip_done, scrub_stream)

TOOL = server.ToolDef(function=server.FunctionDef(name="get_weather", description="w",
                                               parameters={"type": "object", "properties": {}}))


def _msgs(user="cuaca jakarta?"):
    return [server.ChatMessage(role="user", content=user)]


# ---- helpers ---------------------------------------------------------------

def test_done_detection_and_strip():
    assert has_done("selesai ya. [done]")
    assert has_done("[ DONE ]")
    assert not has_done("saya akan cek dulu")
    assert strip_done("Halo. [done]") == "Halo."
    assert strip_done("[done]") == ""
    assert auto_continue_max() == 20


def test_scrub_holds_partial_token_across_deltas():
    emit, pend = scrub_stream("Selesai [do", "")
    assert emit == "Selesai " and pend == "[do"
    emit2, pend2 = scrub_stream("ne] thanks!", pend)
    assert "thanks!" in emit2 and "[done]" not in emit2 + pend2
    # a stray bracket that never completes is eventually emitted
    emit3, pend3 = scrub_stream("x", "[y")
    assert emit3 == "[yx" and pend3 == ""


# ---- prompt injection ------------------------------------------------------

def test_tool_prompt_carries_the_stop_protocol():
    p = server._build_prompt(_msgs(), tools=[TOOL])
    assert "STOP PROTOCOL" in p and "[done]" in p


def test_no_tools_means_no_stop_protocol():
    assert "STOP PROTOCOL" not in server._build_prompt(_msgs())


# ---- fake session plumbing -------------------------------------------------

class _ScriptedAdapter:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple] = []

    def create_session(self):
        return "session-fake"

    def chat(self, sid, prompt, **kw):
        self.calls.append(prompt)
        out = self.script.pop(0) if self.script else ""
        ro = kw.get("ready_out")
        if ro is not None:
            ro["response_message_id"] = 10 + len(self.calls)
        return out, ""

    def chat_stream(self, sid, prompt, **kw):
        self.calls.append(prompt)
        out = self.script.pop(0) if self.script else ""
        ro = kw.get("ready_out")
        if ro is not None:
            ro["response_message_id"] = 10 + len(self.calls)
        yield out
        yield {"__type": "status", "status": "FINISHED"}


class _FakeAcq:
    def __init__(self, adapter):
        self.adapter = adapter
        self.acct = object()
        self.parent_message_id = None

    def prepare_prompt(self, prompt):
        return prompt

    def create_session(self):
        return self.adapter.create_session()

    def record_message_id(self, mid, session_id=None, full_prompt=None):
        self.parent_message_id = mid

    def release(self):
        pass


def _install(monkeypatch, script):
    adapter = _ScriptedAdapter(script)
    acq = _FakeAcq(adapter)
    monkeypatch.setattr(server, "_acquire", lambda cache_key=None: acq)
    monkeypatch.setattr(server, "get_stats", lambda: type("_S", (), {"record": lambda *a, **k: None})())
    return adapter


# ---- non-stream loop -------------------------------------------------------

def test_nonstream_keeps_asking_until_done(monkeypatch):
    monkeypatch.setenv("PROXY_AUTO_CONTINUE_MAX", "3")
    adapter = _install(monkeypatch, ["saya lanjut", "Beres. [done]"])
    res = server._handle_nonstream("id-1", "User: halo", tools=[TOOL])
    assert adapter.calls == ["User: halo", REMINDER_TEXT]
    content = res["choices"][0]["message"]["content"]
    assert "Beres." in content and "saya lanjut" in content
    assert "[done]" not in json.dumps(res)


def test_nonstream_stops_at_the_cap(monkeypatch):
    monkeypatch.setenv("PROXY_AUTO_CONTINUE_MAX", "2")
    adapter = _install(monkeypatch, ["a", "b", "c"])
    res = server._handle_nonstream("id-2", "User: halo", tools=[TOOL])
    assert len(adapter.calls) == 3  # main + 2 nudges
    assert res["choices"][0]["message"]["content"] == "abc"


def test_nonstream_returns_later_round_tool_calls(monkeypatch):
    block = ('<|DSML|tool_calls>\n<|DSML|invoke name="get_weather">'
             '<|DSML|parameter name="city"><![CDATA[Jakarta]]></|DSML|parameter>'
             '</|DSML|invoke>\n</|DSML|tool_calls>')
    adapter = _install(monkeypatch, ["saya cek", block])
    res = server._handle_nonstream("id-3", "User: halo", tools=[TOOL])
    assert res["choices"][0]["finish_reason"] == "tool_calls"
    tc = res["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "get_weather"


def test_nonstream_without_tools_is_untouched(monkeypatch):
    adapter = _install(monkeypatch, ["Halo saja."])
    res = server._handle_nonstream("id-4", "User: halo")
    assert len(adapter.calls) == 1
    assert res["choices"][0]["message"]["content"] == "Halo saja."


# ---- stream loop -----------------------------------------------------------

def _collect_stream(coro) -> str:
    import asyncio

    async def _run():
        res = await coro
        body = res.body_iterator
        out = []
        async for item in body:
            out.append(item)
        return "".join(out)

    return asyncio.run(_run())


def test_stream_reasks_without_done_and_scrubs_it(monkeypatch):
    monkeypatch.setenv("PROXY_AUTO_CONTINUE_MAX", "3")
    adapter = _install(monkeypatch, ["saya lanjut", "Sudah beres. [done]"])
    text = _collect_stream(server._handle_stream("id-5", "User: halo", tools=[TOOL]))
    assert adapter.calls == ["User: halo", REMINDER_TEXT]
    assert "[done]" not in text
    assert "Sudah beres." in text
    assert text.rstrip().endswith("data: [DONE]")


def test_stream_first_round_tool_call_needs_no_nudge(monkeypatch):
    block = ('<|DSML|tool_calls>\n<|DSML|invoke name="get_weather">'
             '<|DSML|parameter name="city"><![CDATA[Bandung]]></|DSML|parameter>'
             '</|DSML|invoke>\n</|DSML|tool_calls>')
    adapter = _install(monkeypatch, [block])
    text = _collect_stream(server._handle_stream("id-6", "User: halo", tools=[TOOL]))
    assert len(adapter.calls) == 1
    assert "tool_calls" in text


# ---- admin capture helper --------------------------------------------------

def test_capture_login_requires_local(monkeypatch, tmp_path):
    monkeypatch.setenv("VERCEL", "1")
    out = __import__("admin").capture_login_credentials()
    assert out["ok"] is False and out["status"] == 400


def test_capture_login_imports_latest_account(monkeypatch, tmp_path):
    store = tmp_path / "dsweb-accounts.json"
    store.write_text(json.dumps({"version": 1, "accounts": [
        {"id": "a", "email": "old@x", "token": "tok-old", "cookies": "c=1"},
        {"id": "b", "email": "new@x", "token": "tok-new", "cookies": "c=2"},
    ]}), encoding="utf-8")
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "node")
    import admin, os
    real_exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: True if p.endswith("login.mjs") or p.endswith("dsweb-accounts.json") else real_exists(p))

    class _Proc:
        returncode = 0
        stderr = ""
    out = admin.capture_login_credentials(run=lambda *a, **k: _Proc(), store_path=str(store))
    assert out == {"ok": True, "token": "tok-new", "cookies": "c=2", "email": "new@x"}
