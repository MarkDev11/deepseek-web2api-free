"""Upstream-parity with the dsh-plugin WebClient (v3.3.14).

The plugin treats these plain-JSON 200 bodies as errors, never as empty
content: biz_code 26 / 'invalid message id' (parent rejected -> drop the
session, retry once without a parent) and biz_code 4 / rate-limit wording
(retry with backoff on a fresh session). The adapter now mirrors both, and
the completion client uses the same 600s timeout as the plugin.
"""
from __future__ import annotations

import pytest

import adapter as A


def test_parent_rejection_detector():
    assert A._is_parent_rejection(26, "whatever")
    assert A._is_parent_rejection(None, "Invalid Message Id")
    assert not A._is_parent_rejection(0, "ok")
    assert not A._is_parent_rejection(None, "")


def test_json_rate_limit_detector():
    assert A._is_json_rate_limit(4, "")
    assert A._is_json_rate_limit(None, "消息发送过于频繁，请稍后重试")
    assert not A._is_json_rate_limit(0, "fine")


def test_biz_envelope_shape():
    code, msg = A._biz_envelope({"code": 0, "data": {"biz_code": 26, "biz_msg": "invalid message id"}})
    assert code == 26 and msg == "invalid message id"
    assert A._biz_envelope({"code": 0, "data": "x"}) == (None, "")
    assert A._biz_envelope("nope") == (None, "")


def _bare_adapter():
    return A.DeepSeekAdapter(token="t", cookies="")


def test_chat_retries_parent_error_without_parent(monkeypatch):
    ad = _bare_adapter()
    seen = []
    calls = {"n": 0}

    def fake_once(session_id, prompt, **kw):
        seen.append((session_id, kw.get("parent_message_id")))
        calls["n"] += 1
        if calls["n"] == 1:
            raise A.UpstreamParentError("rejected")
        return ("ok", "")

    monkeypatch.setattr(ad, "_chat_once", fake_once)
    monkeypatch.setattr(ad, "create_session", lambda: "s2")
    assert ad.chat("s1", "hi", parent_message_id=7) == ("ok", "")
    assert seen[0] == ("s1", 7)
    assert seen[1] == ("s2", None)


def test_chat_stream_retries_parent_error(monkeypatch):
    ad = _bare_adapter()
    calls = {"n": 0}

    def fake_once(session_id, prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise A.UpstreamParentError("rejected")
        yield "x"

    monkeypatch.setattr(ad, "_chat_stream_once", fake_once)
    monkeypatch.setattr(ad, "create_session", lambda: "s2")
    assert list(ad.chat_stream("s1", "hi", parent_message_id=7)) == ["x"]


def test_parent_error_is_empty_error_for_the_server():
    # server.py already maps UpstreamEmptyError to invalidate+502; the
    # parent error inherits that behaviour on purpose.
    assert issubclass(A.UpstreamParentError, A.UpstreamEmptyError)


def test_nonstream_body_biz26_raises_parent_error(monkeypatch):
    ad = _bare_adapter()

    class _Resp:
        text = '{"code": 0, "data": {"biz_code": 26, "biz_msg": "invalid message id"}}'
        status_code = 200
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(ad, "_pow_headers", lambda *a, **k: {})

    class _Session:
        def post(self, *a, **k):
            return _Resp()

        def close(self):
            pass

    # non-stream completion builds its own curl session - fake the factory
    monkeypatch.setattr(A.cffi_requests, "Session", lambda *a, **k: _Session())
    with pytest.raises(A.UpstreamParentError):
        ad._chat_once("s1", "hi")
