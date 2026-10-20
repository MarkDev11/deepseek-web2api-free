"""Credential lifetime helpers and adapter.refresh_credentials().

No network: the adapter tests drive a fake curl_cffi client, so the refresh
logic is exercised offline.
"""
import base64
import json
import time

import pytest

import credentials

def make_jwt(exp, extra=None):
    claims = {"sub": "user-1", "exp": exp}
    if extra:
        claims.update(extra)
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + payload + ".signature"


class TestTokenExpiry:
    def test_reads_exp_from_a_jwt(self):
        exp = int(time.time()) + 120
        assert credentials.token_expires_at(make_jwt(exp)) == exp
        assert 100 < credentials.token_expires_in(make_jwt(exp)) <= 120

    def test_accepts_bearer_prefix(self):
        exp = int(time.time()) + 60
        assert credentials.token_expires_at("Bearer " + make_jwt(exp)) == exp

    def test_non_jwt_token_has_unknown_expiry(self):
        assert credentials.token_expires_at("opaque-token-without-dots") == 0
        assert credentials.token_expires_in("opaque-token-without-dots") is None

    def test_should_refresh_uses_the_ahead_window(self):
        now = 1_700_000_000
        assert credentials.should_refresh(now + 300, 900, now) is True
        assert credentials.should_refresh(now + 3600, 900, now) is False
        assert credentials.should_refresh(now - 1, 900, now) is True
        assert credentials.should_refresh(0, 900, now) is False  # unknown expiry
        assert credentials.should_refresh(now + 30, 0, now) is False  # disabled


class TestAuthFailureDetection:
    def test_business_code_40003_is_an_auth_failure(self):
        # Observed: HTTP 200 + {"code": 40003, "msg": "Authorization Failed (invalid token)"}
        assert credentials.is_auth_failure(40003, "Authorization Failed (invalid token)") is True

    def test_other_business_codes_are_not(self):
        assert credentials.is_auth_failure(0, "ok") is False
        assert credentials.is_auth_failure(429, "too many requests") is False

    def test_message_markers_work_without_a_code(self):
        assert credentials.is_auth_failure(None, "credentials rejected: 40003") is True
        assert credentials.is_auth_failure(None, "token expired") is True
        assert credentials.is_auth_failure(None, "upstream_toast_error: rate limit") is False

    def test_non_numeric_code_does_not_raise(self):
        assert credentials.is_auth_failure("40003", "") is True
        assert credentials.is_auth_failure("boom", "") is False


class TestTokenExtraction:
    def test_nested_biz_data_wrapped_value(self):
        long = "x" * 40
        payload = {"code": 0, "data": {"biz_data": {"token": {"value": long, "__version": "0"}}}}
        assert credentials.extract_refreshed_token(payload) == long

    def test_plain_data_token_and_access_token(self):
        long = "y" * 32
        assert credentials.extract_refreshed_token({"data": {"token": long}}) == long
        assert credentials.extract_refreshed_token({"access_token": long}) == long

    def test_strips_bearer_and_accepts_wrapper_string(self):
        long = "z" * 30
        assert credentials.extract_refreshed_token({"data": {"token": "Bearer " + long}}) == long
        assert credentials.extract_refreshed_token(json.dumps({"value": long, "__version": "0"})) == long

    def test_nothing_token_shaped(self):
        assert credentials.extract_refreshed_token({"code": 0, "data": {"ok": True}}) == ""
        assert credentials.extract_refreshed_token(None) == ""
        assert credentials.extract_refreshed_token({"data": {"token": "short"}}) == ""


class TestCookieMerge:
    def test_new_values_win_and_old_names_survive(self):
        merged = credentials.merge_cookie_blobs("cf_clearance=abc; sid=1", "sid=2; tfk=3")
        pairs = dict(kv.split("=", 1) for kv in merged.split("; "))
        assert pairs == {"cf_clearance": "abc", "sid": "2", "tfk": "3"}

    def test_empty_inputs(self):
        assert credentials.merge_cookie_blobs("", "a=1") == "a=1"
        assert credentials.merge_cookie_blobs("a=1", "") == "a=1"
        assert credentials.merge_cookie_blobs("", "") == ""


class _FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers if headers is not None else {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeJar:
    def __init__(self, mapping):
        self._mapping = dict(mapping)

    def items(self):
        return list(self._mapping.items())


class _FakeClient:
    """Records POSTs and replays queued responses (or raises queued errors)."""

    def __init__(self, responses, cookies=None):
        self._responses = list(responses)
        self.calls = []
        self.cookies = _FakeJar(cookies or {})

    def post(self, url, json=None, headers=None, timeout=None):
        return self._send("POST", url, headers)

    def get(self, url, headers=None, timeout=None):
        return self._send("GET", url, headers)

    def _send(self, method, url, headers):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {})})
        item = self._responses.pop(0) if self._responses else _FakeResponse(None, 404)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def adapter():
    """A real adapter; its HTTP client is replaced per test."""
    from adapter import DeepSeekAdapter
    return DeepSeekAdapter(token="old-token", cookies="cf_clearance=abc; sid=1")


class TestCheckCredentials:
    """adapter.check_credentials(): what the panel's Refresh button really does."""

    def _with_client(self, adapter, responses):
        adapter._client = _FakeClient(responses)
        return adapter._client

    def test_defaults_match_the_measured_upstream(self):
        import os
        import adapter as adapter_module
        # Measured 2026-10-20: no refresh endpoint exists, /api/v0/users/settings
        # is the authenticated probe. Pinned unless the env says otherwise.
        if not os.environ.get("DEEPSEEK_CREDENTIAL_CHECK_PATH"):
            assert adapter_module.CREDENTIAL_CHECK_PATH == "/api/v0/users/settings"
        if not os.environ.get("DEEPSEEK_REFRESH_PATHS"):
            assert adapter_module.REFRESH_PATHS == []

    def test_live_token_is_a_get_and_reports_ok(self, adapter):
        from adapter import BASE_URL
        client = self._with_client(adapter, [_FakeResponse({"code": 0, "data": {"biz_code": 0}})])
        res = adapter.check_credentials()
        assert res["ok"] is True and res["code"] == 0 and res["detail"] == ""
        assert client.calls[0]["method"] == "GET"
        assert client.calls[0]["url"] == BASE_URL + "/api/v0/users/settings"
        assert client.calls[0]["headers"]["Authorization"] == "Bearer old-token"

    def test_rejected_token_is_negative_not_an_error(self, adapter):
        res = adapter.check_credentials() if self._with_client(
            adapter,
            [_FakeResponse({"code": 40003, "msg": "Authorization Failed (invalid token)"})],
        ) else None
        assert res["ok"] is False
        assert res["code"] == 40003
        assert res["detail"] == "token rejected by upstream"

    def test_non_json_body_reports_the_status(self, adapter):
        self._with_client(adapter, [_FakeResponse(None, 200)])
        res = adapter.check_credentials()
        assert res["ok"] is False and "non-JSON" in res["detail"]

    def test_waf_challenge_is_named(self, adapter):
        self._with_client(adapter, [_FakeResponse(None, 202, {"x-amzn-waf-action": "challenge"})])
        res = adapter.check_credentials()
        assert res["ok"] is False and "challenge" in res["detail"]

    def test_transport_error_is_captured(self, adapter):
        self._with_client(adapter, [TimeoutError("timed out")])
        res = adapter.check_credentials()
        assert res["ok"] is False and "TimeoutError" in res["detail"]

    def test_unexpected_code_is_not_ok(self, adapter):
        self._with_client(adapter, [_FakeResponse({"code": 500, "msg": "internal"})])
        res = adapter.check_credentials()
        assert res["ok"] is False and "500" in res["detail"]

    def test_blank_path_disables_the_check(self, adapter, monkeypatch):
        monkeypatch.setattr("adapter.CREDENTIAL_CHECK_PATH", "")
        res = adapter.check_credentials()
        assert res["ok"] is False and "no credential check path" in res["detail"]


class TestRefreshCredentials:
    def _with_client(self, adapter, responses, cookies=None):
        adapter._client = _FakeClient(responses, cookies)
        return adapter._client

    def test_working_path_updates_token_headers_and_cookies(self, adapter, monkeypatch):
        from adapter import BASE_URL
        # Renewal is opt-in now: the upstream has no refresh endpoint by default.
        monkeypatch.setattr("adapter.REFRESH_PATHS", ["/api/v0/auth/refresh"])
        new_tok = make_jwt(int(time.time()) + 3600)
        client = self._with_client(
            adapter,
            [_FakeResponse({"code": 0, "data": {"biz_data": {"token": new_tok}}})],
            {"cf_clearance": "abc", "sid": "2"},
        )
        info = adapter.refresh_credentials()
        assert info["token"] == new_tok
        assert adapter.token == new_tok
        assert adapter._base_headers["Authorization"] == "Bearer " + new_tok
        # rotated cookie (sid=2) merged in, untouched one kept
        assert "sid=2" in info["cookies"] and "cf_clearance=abc" in info["cookies"]
        assert info["expires_at"] > 0
        assert client.calls[0]["url"].startswith(BASE_URL)
        assert client.calls[0]["headers"]["Authorization"] == "Bearer old-token"

    def test_falls_through_to_the_next_configured_path(self, adapter, monkeypatch):
        monkeypatch.setattr("adapter.REFRESH_PATHS", ["/nope", "/api/v0/auth/refresh"])
        new_tok = make_jwt(int(time.time()) + 600)
        client = self._with_client(
            adapter, [_FakeResponse(None, 404), _FakeResponse({"token": new_tok})])
        info = adapter.refresh_credentials()
        assert info["path"] == "/api/v0/auth/refresh"
        assert len(client.calls) == 2

    def test_all_paths_missing_raises_credential_refresh_error(self, adapter, monkeypatch):
        from adapter import CredentialRefreshError
        monkeypatch.setattr("adapter.REFRESH_PATHS", ["/a", "/b"])
        self._with_client(adapter, [_FakeResponse(None, 404), _FakeResponse(None, 404)])
        with pytest.raises(CredentialRefreshError) as exc:
            adapter.refresh_credentials()
        assert "/a" in str(exc.value) and "/b" in str(exc.value)

    def test_dead_credentials_raise_auth_error_without_trying_more(self, adapter, monkeypatch):
        from adapter import UpstreamAuthError
        monkeypatch.setattr("adapter.REFRESH_PATHS", ["/a", "/b"])
        client = self._with_client(adapter, [
            _FakeResponse({"code": 40003, "msg": "Authorization Failed (invalid token)"}),
            _FakeResponse({"code": 0, "token": "unused"}),
        ])
        with pytest.raises(UpstreamAuthError):
            adapter.refresh_credentials()
        # A dead token cannot mint a new one, so the second path is pointless.
        assert len(client.calls) == 1

    def test_waf_challenge_is_reported_and_skipped(self, adapter, monkeypatch):
        from adapter import CredentialRefreshError
        monkeypatch.setattr("adapter.REFRESH_PATHS", ["/a"])
        self._with_client(adapter, [
            _FakeResponse(None, 202, {"x-amzn-waf-action": "challenge"}),
        ])
        with pytest.raises(CredentialRefreshError) as exc:
            adapter.refresh_credentials()
        assert "challenge" in str(exc.value)

    def test_token_expiry_property_reads_current_token(self, adapter):
        exp = int(time.time()) + 90
        adapter.set_token(make_jwt(exp))
        assert adapter.token_expires_at == exp
