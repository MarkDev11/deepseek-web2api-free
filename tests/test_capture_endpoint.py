"""One-click capture endpoint (bookmarklet -> hosted pool).

The hosted panel cannot run a browser and no software may be installed on the
user's machine, so the browser POSTs its own credentials from the DeepSeek
origin. The bookmarklet deliberately sends a "simple" CORS POST (content-type
text/plain, no preflight) whose body is JSON - which FastAPI would NOT parse
as a model on its own, so the endpoint reads the body manually. Pinned here:
that manual parsing, secret gating (with ACAO on errors too, so the alert can
say why), cookie rotation on the same token without duplicates (HttpOnly
values preserved by the merge), payload validation, and the per-IP rate limit.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

import admin
from account_pool import AccountPool


def _post(payload) -> Request:
    """A text/plain POST carrying JSON - exactly what the bookmarklet sends."""
    data = json.dumps(payload).encode() if not isinstance(payload, (str, bytes)) else payload

    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}

    return Request({
        "type": "http", "method": "POST", "path": "/admin/api/capture",
        "headers": [(b"content-type", b"text/plain")],
        "query_string": b"",
    }, receive=receive)


def _get_request(token: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/admin/api/capture-code",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "query_string": b"",
    })


def _login() -> str:
    token = admin._generate_token()
    admin._tokens.add(token)
    return token


def _blob(token="t" * 64, cookie="a=1;b=2"):  # ";" needed for cookie-header shape
    return json.dumps({"value": token}) + "||" + cookie


@pytest.fixture
def pool(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.delenv("DEEPSEEK_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(admin, "_pool", AccountPool())
    monkeypatch.setattr(admin, "_TOKEN_SECRET", b"unit-test-secret")
    admin._capture_attempts.clear()
    yield admin._pool


def test_capture_adds_new_account(pool):
    secret = admin._capture_secret()
    res = Response()
    out = asyncio.run(admin.capture(
        _post({"raw": _blob(), "secret": secret}), res))
    assert out["ok"] and out["updated"] is False
    assert pool.count() == 1
    assert res.headers["access-control-allow-origin"] == "*"


def test_capture_non_json_body_rejected(pool):
    secret = admin._capture_secret()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.capture(_post("plain words, not json"), Response()))
    assert exc.value.status_code == 400
    assert secret  # (the rejected payload never reaches the pool)
    assert pool.count() == 0


def test_capture_wrong_secret_rejected(pool):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.capture(_post({"raw": _blob(), "secret": "nope"}), Response()))
    assert exc.value.status_code == 401
    # the alert in the bookmarklet needs to READ this error cross-origin
    assert {k.lower(): v for k, v in exc.value.headers.items()}["access-control-allow-origin"] == "*"
    assert pool.count() == 0


def test_capture_same_token_rotates_cookies_no_duplicate(pool):
    secret = admin._capture_secret()
    asyncio.run(admin.capture(
        _post({"raw": _blob(cookie="ds_session_id=S3CRET;a=1"), "secret": secret}),
        Response()))
    # A bookmarklet only sees document.cookie: no ds_session_id in the blob.
    out = asyncio.run(admin.capture(
        _post({"raw": _blob(cookie="a=2;b=9"), "secret": secret}), Response()))
    assert out["updated"] is True
    assert pool.count() == 1
    cookies = pool._accounts[0].cookies
    assert "a=2" in cookies and "b=9" in cookies
    assert "ds_session_id=S3CRET" in cookies  # HttpOnly value survived the merge


def test_capture_garbage_payload_rejected(pool):
    secret = admin._capture_secret()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.capture(
            _post({"raw": "???totally unknown???", "secret": secret}), Response()))
    assert exc.value.status_code == 400
    assert pool.count() == 0


def test_capture_rate_limited(pool):
    import time
    secret = admin._capture_secret()
    key = admin._client_ip(_post({}))
    admin._capture_attempts[key] = [time.time()] * admin._CAPTURE_MAX
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.capture(
            _post({"raw": _blob(), "secret": secret}), Response()))
    assert exc.value.status_code == 429


def test_capture_disabled_without_a_base_secret(pool, monkeypatch):
    monkeypatch.setattr(admin, "_TOKEN_SECRET", b"")
    out = asyncio.run(admin.capture_code(_get_request(_login())))
    assert out["enabled"] is False
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.capture(
            _post({"raw": _blob(), "secret": "anything"}), Response()))
    assert exc.value.status_code == 503


def test_capture_secret_is_stable_across_instances(pool):
    # Pure function of the base secret: every instance derives the same value.
    assert admin._capture_secret() == admin._capture_secret()
    assert len(admin._capture_secret()) == 40


def test_capture_code_requires_login(pool):
    token = _login()
    try:
        out = asyncio.run(admin.capture_code(_get_request(token)))
        assert out == {"enabled": True, "secret": admin._capture_secret()}
    finally:
        admin._tokens.discard(token)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.capture_code(_get_request("bogus")))
    assert exc.value.status_code == 401
