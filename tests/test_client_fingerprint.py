"""Application headers the upstream reads as "is this the real web client".

Two things went stale once already, so both are pinned here:

* the app headers (a 2026-08 capture was still shipping `X-Client-Version: 2.3.0`
  while the live frontend sent `2.5.0`, and the bundle-id/platform headers were
  missing entirely), and
* the UA/brand pair, which must describe the ClientHello curl_cffi actually
  presents rather than whatever browser a capture used.

`DEEPSEEK_CLIENT_SYNC` additionally lets a deployment adopt the version the live
bundle reports; those tests drive the fetcher with a stub, never the network.
"""

import re

import pytest

import adapter
from adapter import DeepSeekAdapter


@pytest.fixture(autouse=True)
def _clean_fingerprint_env(monkeypatch):
    for name in ("DEEPSEEK_CLIENT_VERSION", "DEEPSEEK_CLIENT_LOCALE",
                 "DEEPSEEK_CLIENT_TZ_OFFSET", "DEEPSEEK_ACCEPT_LANGUAGE",
                 "DEEPSEEK_DEVICE_ID", "DEEPSEEK_CLIENT_BUNDLE_ID",
                 "DEEPSEEK_CLIENT_PLATFORM", "DEEPSEEK_CLIENT_SYNC",
                 "DEEPSEEK_CLIENT_SYNC_CACHE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolated_sync_state(monkeypatch, tmp_path):
    """Keep the module-level sync state (and its cache file) per-test."""
    monkeypatch.setattr(adapter, "_client_sync",
                        {"version": "", "bundle_url": "", "checked_at": 0.0})
    monkeypatch.setattr(adapter, "_SYNC_CACHE_PATH", tmp_path / "fp.json")
    monkeypatch.setattr(adapter, "_client_sync_started", False)


def _headers():
    return DeepSeekAdapter(token="t", cookies="c")._base_headers


def test_defaults_match_the_live_capture():
    h = _headers()
    assert h["X-Client-Version"] == "2.5.0"
    assert h["X-Client-Locale"] == "en_US"
    assert h["X-Client-Timezone-Offset"] == "25200"
    assert h["Accept-Language"] == "en-US,en;q=0.8"


def test_app_identity_headers_match_the_bundle():
    """The real client derives these from its bundle (see the minified
    export that maps brand -> bundle id and `platform:"web"`)."""
    h = _headers()
    assert h["x-client-bundle-id"] == "com.deepseek.chat"
    assert h["X-Client-Platform"] == "web"


def test_app_identity_headers_are_overridable(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_CLIENT_BUNDLE_ID", "com.deepseek.chat.nov")
    monkeypatch.setenv("DEEPSEEK_CLIENT_PLATFORM", "pc")
    h = _headers()
    assert h["x-client-bundle-id"] == "com.deepseek.chat.nov"
    assert h["X-Client-Platform"] == "pc"


def test_device_id_is_absent_unless_configured():
    assert "X-Device-Id" not in _headers()


def test_device_id_is_sent_when_configured(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_DEVICE_ID", " fa44965a-126f-42c6-b8fa-bec7e03cbd95 ")
    assert _headers()["X-Device-Id"] == "fa44965a-126f-42c6-b8fa-bec7e03cbd95"


def test_every_value_is_overridable(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_CLIENT_VERSION", "9.9.9")
    monkeypatch.setenv("DEEPSEEK_CLIENT_LOCALE", "zh_CN")
    monkeypatch.setenv("DEEPSEEK_CLIENT_TZ_OFFSET", "28800")
    monkeypatch.setenv("DEEPSEEK_ACCEPT_LANGUAGE", "zh-CN,zh;q=0.9")
    h = _headers()
    assert h["X-Client-Version"] == "9.9.9"
    assert h["X-Client-Locale"] == "zh_CN"
    assert h["X-Client-Timezone-Offset"] == "28800"
    assert h["Accept-Language"] == "zh-CN,zh;q=0.9"


def test_ua_and_brand_headers_agree_with_each_other():
    """User-Agent and Sec-Ch-Ua are deliberately *not* copied from a browser
    capture: they must describe the TLS fingerprint curl_cffi actually presents,
    so the two versions have to keep agreeing with each other."""
    h = _headers()
    ua = re.search(r"Chrome/(\d+)", h["User-Agent"]).group(1)
    brand = re.search(r'Chrom(?:e|ium)";v="(\d+)"', h["Sec-Ch-Ua"]).group(1)
    assert ua == brand


def test_ua_and_brand_describe_the_impersonation_profile():
    """The trio (impersonate, UA, brand) is a single decision -- pin it so a
    half-done bump cannot ship a Chrome N UA on an older ClientHello."""
    h = _headers()
    version = adapter.IMPERSONATE.replace("chrome", "")
    assert version.isdigit()
    assert f"Chrome/{version}" in h["User-Agent"]
    assert f'"{version}"' in h["Sec-Ch-Ua"]


def test_parse_client_version_reads_the_bundle_constant():
    bundle = 'x(q_)({brand:eh.getBrand(),platform:"web",version:"2.5.0",locale:ep})'
    assert adapter.parse_client_version(bundle) == "2.5.0"
    assert adapter.parse_client_version("") == ""
    assert adapter.parse_client_version("version: not-a-version") == ""


def test_current_client_version_precedence(monkeypatch):
    assert adapter.current_client_version() == adapter.CLIENT_VERSION
    adapter._client_sync["version"] = "2.6.0"
    assert adapter.current_client_version() == "2.6.0"
    monkeypatch.setenv("DEEPSEEK_CLIENT_VERSION", "9.9.9")
    assert adapter.current_client_version() == "9.9.9"


class _StubResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


def _stub_fetcher(pages, calls=None):
    def fake_get(url, **kwargs):
        if calls is not None:
            calls.append({"url": url, **kwargs})
        for prefix, response in pages.items():
            if url.startswith(prefix):
                return response
        raise AssertionError(f"unexpected fetch: {url}")
    return fake_get


def test_sync_is_off_by_default(monkeypatch):
    DeepSeekAdapter(token="t", cookies="c")
    assert adapter._client_sync_started is False


def test_sync_enabled_starts_one_background_thread(monkeypatch):
    started = []
    monkeypatch.setenv("DEEPSEEK_CLIENT_SYNC", "1")
    monkeypatch.setattr(adapter, "sync_client_fingerprint",
                        lambda *a, **k: started.append(1))
    DeepSeekAdapter(token="t", cookies="c")
    DeepSeekAdapter(token="t", cookies="c")
    assert adapter._client_sync_started is True
    for thread in __import__("threading").enumerate():
        if thread.name == "client-fingerprint-sync":
            thread.join(timeout=5)
    assert started == [1]


def test_sync_adopts_the_version_the_bundle_reports(monkeypatch):
    bundle_url = ("https://fe-static.deepseek.com/chat/static/"
                  "main.0123456789abcdef.js")
    calls = []
    monkeypatch.setattr(
        adapter.cffi_requests, "get",
        _stub_fetcher({
            "https://chat.deepseek.com/": _StubResponse(
                f'<html><script src="{bundle_url}"></script></html>'),
            bundle_url: _StubResponse(
                'a=e=>{...};version:"2.7.1",locale:"en_US"'),
        }, calls),
    )
    state = adapter.sync_client_fingerprint(cookies="aws-waf-token=abc")
    assert state["version"] == "2.7.1"
    assert state["bundle_url"] == bundle_url
    assert state["checked_at"] > 0
    assert adapter.current_client_version() == "2.7.1"
    assert DeepSeekAdapter(token="t", cookies="c")._base_headers[
        "X-Client-Version"] == "2.7.1"
    # the discovery hop carries the account cookies (the index page is behind
    # the WAF challenge), the static bundle does not need them
    assert len(calls) == 2
    assert calls[0]["headers"] == {"Cookie": "aws-waf-token=abc"}
    assert "headers" not in calls[1]


def test_sync_keeps_the_pinned_version_when_the_fetch_fails(monkeypatch):
    def boom(url, **kwargs):
        raise ConnectionError("network down")

    monkeypatch.setattr(adapter.cffi_requests, "get", boom)
    state = adapter.sync_client_fingerprint()
    assert state["version"] == ""
    assert adapter.current_client_version() == adapter.CLIENT_VERSION
    assert DeepSeekAdapter(token="t", cookies="c")._base_headers[
        "X-Client-Version"] == adapter.CLIENT_VERSION


def test_sync_keeps_the_pinned_version_when_the_bundle_is_unparsable(monkeypatch):
    homepage = '<script src="https://fe-static.deepseek.com/chat/static/main.aa00bb11.js">'
    monkeypatch.setattr(
        adapter.cffi_requests, "get",
        _stub_fetcher({
            "https://chat.deepseek.com/": _StubResponse(homepage),
            "https://fe-static.deepseek.com/": _StubResponse("<html>nothing</html>"),
        }),
    )
    state = adapter.sync_client_fingerprint()
    assert state["version"] == ""
    assert state["checked_at"] > 0
    assert adapter.current_client_version() == adapter.CLIENT_VERSION


def test_sync_is_skipped_while_the_cache_is_fresh(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter.cffi_requests, "get",
                        _stub_fetcher({}, calls))
    adapter._client_sync["checked_at"] = adapter.time.time()
    adapter._client_sync["version"] = "2.5.0"
    state = adapter.sync_client_fingerprint()
    assert state["version"] == "2.5.0"
    assert calls == []
