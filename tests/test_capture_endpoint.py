"""One-click capture endpoint (bookmarklet -> hosted pool).

The hosted panel cannot run a browser and no software may be installed on the
user's machine, so the browser POSTs its own credentials from the DeepSeek
origin. Pinned here: secret gating, cookie rotation on the same token
(no duplicate account, HttpOnly values preserved), payload validation, the
per-IP rate limit, and the cross-origin header that lets the bookmarklet
read the answer.
"""
import asyncio

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

import admin
from account_pool import AccountPool


def _post_request() -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/admin/api/capture",
        "headers": [(b"content-type", b"text/plain")],
        "query_string": b"",
    })


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
    import json
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
        admin.CaptureRequest(raw=_blob(), secret=secret), _post_request(), res))
    assert out["ok"] and out["updated"] is False
    assert pool.count() == 1
    assert res.headers["access-control-allow-origin"] == "*"


def test_capture_wrong_secret_rejected(pool):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.capture(
            admin.CaptureRequest(raw=_blob(), secret="nope"), _post_request(), Response()))
    assert exc.value.status_code == 401
    assert pool.count() == 0


def test_capture_same_token_rotates_cookies_no_duplicate(pool):
    secret = admin._capture_secret()
    asyncio.run(admin.capture(
        admin.CaptureRequest(raw=_blob(cookie="ds_session_id=S3CRET;a=1"),
                             secret=secret), _post_request(), Response()))
    # A bookmarklet only sees document.cookie: no ds_session_id in the blob.
    out = asyncio.run(admin.capture(
        admin.CaptureRequest(raw=_blob(cookie="a=2;b=9"),
                             secret=secret), _post_request(), Response()))
    assert out["updated"] is True
    assert pool.count() == 1
    cookies = pool._accounts[0].cookies
    assert "a=2" in cookies and "b=9" in cookies
    assert "ds_session_id=S3CRET" in cookies  # HttpOnly value survived the merge


def test_capture_garbage_payload_rejected(pool):
    secret = admin._capture_secret()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.capture(
            admin.CaptureRequest(raw="???totally unknown???", secret=secret),
            _post_request(), Response()))
    assert exc.value.status_code == 400
    assert pool.count() == 0


def test_capture_rate_limited(pool):
    import time
    secret = admin._capture_secret()
    key = admin._client_ip(_post_request())
    admin._capture_attempts[key] = [time.time()] * admin._CAPTURE_MAX
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.capture(
            admin.CaptureRequest(raw=_blob(), secret=secret), _post_request(), Response()))
    assert exc.value.status_code == 429


def test_capture_disabled_without_a_base_secret(pool, monkeypatch):
    monkeypatch.setattr(admin, "_TOKEN_SECRET", b"")
    out = asyncio.run(admin.capture_code(_get_request(_login())))
    assert out["enabled"] is False
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.capture(
            admin.CaptureRequest(raw=_blob(), secret="anything"),
            _post_request(), Response()))
    assert exc.value.status_code == 503


def test_capture_secret_survives_instance_restart(pool):
    # Pure function of the base secret: two "instances" derive the same value.
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
