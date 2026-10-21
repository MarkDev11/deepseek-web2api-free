"""One-paste credential import: parse a browser blob into token + cookies.

The panel exposes a single textarea, so whatever a human copies out of DevTools
has to be understood here: a bare token, a localStorage wrapper, a raw Cookie
header, or a combination joined by "||" or newlines. A cookie header is merged
name-by-name with what the account already stores, because ds_session_id is
HttpOnly and therefore missing from any document.cookie paste.
"""
import asyncio

import pytest
from starlette.requests import Request

import admin
import credentials

TOKEN = "A" * 64
OTHER_TOKEN = "B" * 64


class TestParser:
    def test_bare_localstorage_wrapper_yields_the_token(self):
        raw = '{"value": "%s", "__version": "0"}' % TOKEN
        assert credentials.parse_credential_blob(raw) == (TOKEN, "")

    def test_bare_token_is_recognised(self):
        assert credentials.parse_credential_blob(TOKEN) == (TOKEN, "")

    def test_token_and_cookie_header_joined_together(self):
        raw = TOKEN + " || Cookie: ds_session_id=new; aws-waf-token=waf"
        token, cookies = credentials.parse_credential_blob(
            raw, "OLD", "ds_session_id=keep; cf_clearance=cf")
        assert token == TOKEN
        # Newer values win; unrelated names survive the merge.
        assert "ds_session_id=new" in cookies
        assert "aws-waf-token=waf" in cookies
        assert "cf_clearance=cf" in cookies

    def test_cookie_only_paste_keeps_the_stored_token(self):
        token, cookies = credentials.parse_credential_blob(
            "Cookie: ds_session_id=only; x=1", OTHER_TOKEN, "ds_session_id=old")
        assert token == OTHER_TOKEN
        assert cookies == "ds_session_id=only; x=1"

    def test_newline_separated_paste(self):
        raw = TOKEN + "\nds_session_id=n; aws-waf-token=b"
        assert credentials.parse_credential_blob(raw) == (TOKEN, "ds_session_id=n; aws-waf-token=b")

    def test_unrecognisable_paste_never_blanks_working_credentials(self):
        raw = "hello world, this is not a credential"
        assert credentials.parse_credential_blob(raw, "OLD", "sid=1") == ("OLD", "sid=1")

    def test_empty_paste_is_a_no_op(self):
        assert credentials.parse_credential_blob("   ", "OLD", "sid=1") == ("OLD", "sid=1")


class _FakePool:
    def __init__(self, current=None):
        self.added = None
        self.updated = None
        self._current = current

    def add(self, token, cookies, email=""):
        self.added = {"token": token, "cookies": cookies, "email": email}

        class _Acct:
            def to_dict(self):
                return {"id": "acc_new"}

        return _Acct()

    def get_by_id(self, account_id):
        return self._current

    def update(self, account_id, token=None, cookies=None, email=None):
        self.updated = {"token": token, "cookies": cookies, "email": email}

        class _Acct:
            def to_dict(self):
                return {"id": account_id}

        return _Acct()


class _Existing:
    token = OTHER_TOKEN
    cookies = "ds_session_id=keep; cf_clearance=cf"


def _request(token: str) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/admin/api/accounts",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "query_string": b"",
    })


@pytest.fixture
def authed():
    token = admin._generate_token()
    admin._tokens.add(token)
    yield token
    admin._tokens.discard(token)


def test_add_account_accepts_a_single_pasted_blob(authed, monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(admin, "get_pool", lambda: pool)

    asyncio.run(admin.add_account(admin.AccountAddRequest(
        raw=TOKEN + " || Cookie: ds_session_id=new; aws-waf-token=waf",
        email="new@example.com"), _request(authed)))

    assert pool.added["token"] == TOKEN
    assert "ds_session_id=new" in pool.added["cookies"]
    assert "aws-waf-token=waf" in pool.added["cookies"]


def test_update_account_merges_a_partial_paste_into_stored_cookies(authed, monkeypatch):
    pool = _FakePool(current=_Existing())
    monkeypatch.setattr(admin, "get_pool", lambda: pool)

    asyncio.run(admin.update_account(
        "acc_1", admin.AccountUpdateRequest(raw="Cookie: aws-waf-token=fresh"),
        _request(authed)))

    assert pool.updated["token"] == OTHER_TOKEN
    assert "aws-waf-token=fresh" in pool.updated["cookies"]
    # The HttpOnly value we could not see in the paste must not be lost.
    assert "ds_session_id=keep" in pool.updated["cookies"]
