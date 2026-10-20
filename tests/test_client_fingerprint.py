"""Application headers the upstream reads as "is this the real web client".

They went stale once already (a capture from 2026-08 was still shipping
`X-Client-Version: 2.3.0` while the live frontend sent `2.5.0`), so both the
defaults and the per-deployment overrides are pinned here.
"""

import re

import pytest

from adapter import DeepSeekAdapter


@pytest.fixture(autouse=True)
def _clean_fingerprint_env(monkeypatch):
    for name in ("DEEPSEEK_CLIENT_VERSION", "DEEPSEEK_CLIENT_LOCALE",
                 "DEEPSEEK_CLIENT_TZ_OFFSET", "DEEPSEEK_ACCEPT_LANGUAGE",
                 "DEEPSEEK_DEVICE_ID"):
        monkeypatch.delenv(name, raising=False)


def _headers():
    return DeepSeekAdapter(token="t", cookies="c")._base_headers


def test_defaults_match_the_live_capture():
    h = _headers()
    assert h["X-Client-Version"] == "2.5.0"
    assert h["X-Client-Locale"] == "en_US"
    assert h["X-Client-Timezone-Offset"] == "25200"
    assert h["Accept-Language"] == "en-US,en;q=0.8"


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
