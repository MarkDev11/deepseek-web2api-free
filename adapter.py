"""
DeepSeek Chat API Adapter - WASM-based PoW solving, session management, streaming
Supports expert mode (thinking_enabled, search_enabled).

Anti-detection upgrades (2026-06):
- TLS/JA3 impersonation via curl_cffi (chrome150)
- Header set & values captured from a real Chrome 149 + chat.deepseek.com session
- Cookie jar auto-rotates Set-Cookie (cf_clearance / awswaf token refresh)
- Detects 405 x-amzn-waf-action=captcha and 403/429 cf-mitigated=challenge
"""
import json
import os
import re
import time
import struct
import base64
import random
import threading
import tempfile
import datetime
from pathlib import Path
from dotenv import load_dotenv
from wasmtime import Store, Module, Instance

try:
    from curl_cffi import requests as cffi_requests
except ImportError as e:
    raise SystemExit(
        "curl_cffi is required for TLS fingerprint impersonation.\n"
        "Run: pip install -r requirements.txt"
    ) from e

from credentials import (
    extract_refreshed_token,
    is_auth_failure,
    merge_cookie_blobs,
    token_expires_at as _jwt_expires_at,
)
from logger import get_logger

load_dotenv()
log = get_logger("adapter")

BASE_URL = "https://chat.deepseek.com"
IMPERSONATE = os.environ.get("DEEPSEEK_IMPERSONATE", "chrome150")

# Client fingerprint defaults, measured from a live browser request on
# 2026-10-20 (see DeepSeekAdapter._base_headers). The upstream reads these as
# part of "is this the real web client", and the credentials in the pool belong
# to one specific browser, so the header set should describe that browser
# rather than a stale capture. Each value is overridable per deployment.
CLIENT_VERSION = "2.5.0"
CLIENT_LOCALE = "en_US"
CLIENT_TZ_OFFSET = "25200"          # UTC+7
ACCEPT_LANGUAGE = "en-US,en;q=0.8"
CLIENT_BUNDLE_ID = os.environ.get("DEEPSEEK_CLIENT_BUNDLE_ID", "com.deepseek.chat")
CLIENT_PLATFORM = os.environ.get("DEEPSEEK_CLIENT_PLATFORM", "web")

# The web client reads its own version out of its bundle, so a pinned copy goes
# stale the moment DeepSeek ships a new one (it already did: 2.3.0 -> 2.5.0).
# With DEEPSEEK_CLIENT_SYNC on, one background thread per process reads the live
# bundle, extracts the version string and hands it to every adapter created
# afterwards. Best-effort by construction: an unreachable bundle, a WAF
# challenge or an unparsable bundle all leave the pinned CLIENT_VERSION in
# place, and the fetch never sits on the request path (a cold start pays
# nothing). The result is cached on disk so warm instances do not refetch.
_BUNDLE_URL_RE = re.compile(
    r"https://fe-static\.deepseek\.com/chat/static/main\.[0-9a-f]+\.js"
)
_BUNDLE_VERSION_RE = re.compile(r'version:"(\d+\.\d+\.\d+)"')
_SYNC_CACHE_PATH = Path(
    os.environ.get("DEEPSEEK_CLIENT_SYNC_CACHE")
    or str(Path(tempfile.gettempdir()) / "ds_client_fingerprint.json")
)
try:
    _SYNC_TTL_SECS = max(60, int(os.environ.get("DEEPSEEK_CLIENT_SYNC_TTL_SECS") or 86400))
except ValueError:
    _SYNC_TTL_SECS = 86400
_client_sync = {"version": "", "bundle_url": "", "checked_at": 0.0}
_client_sync_lock = threading.Lock()
_client_sync_started = False


def parse_client_version(bundle_text: str) -> str:
    """Return the client version the web bundle reports, or "" if absent."""
    m = _BUNDLE_VERSION_RE.search(bundle_text or "")
    return m.group(1) if m else ""


def current_client_version() -> str:
    """Explicit env override > version learned from the live bundle > pinned."""
    env = (os.environ.get("DEEPSEEK_CLIENT_VERSION") or "").strip()
    if env:
        return env
    with _client_sync_lock:
        learned = _client_sync["version"]
    return learned or CLIENT_VERSION


def client_sync_state() -> dict:
    with _client_sync_lock:
        return dict(_client_sync)


def _load_sync_cache() -> None:
    try:
        data = json.loads(_SYNC_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    with _client_sync_lock:
        if data.get("version") and not _client_sync["version"]:
            _client_sync["version"] = str(data["version"])
        if data.get("bundle_url") and not _client_sync["bundle_url"]:
            _client_sync["bundle_url"] = str(data["bundle_url"])
        try:
            if data.get("checked_at"):
                _client_sync["checked_at"] = float(data["checked_at"])
        except (TypeError, ValueError):
            pass


def _save_sync_cache() -> None:
    try:
        _SYNC_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _client_sync_lock:
            data = dict(_client_sync)
        _SYNC_CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log.debug("client_sync_cache_write_failed", extra={"error": str(e)})


def sync_client_fingerprint(timeout: float = 10.0, cookies: str = "") -> dict:
    """Read the live SPA bundle and adopt the client version it reports.

    Returns the state dict. Never raises: this is fingerprint freshness, not a
    request prerequisite.
    """
    with _client_sync_lock:
        fresh = (
            _client_sync["checked_at"]
            and time.time() - _client_sync["checked_at"] < _SYNC_TTL_SECS
        )
        if fresh:
            return dict(_client_sync)
    bundle_url = ""
    # The index page sits behind the WAF challenge (202 without a token) while
    # the static bundle does not, so the discovery hop reuses the account's own
    # cookies -- the same request a browser makes when it opens the app.
    headers = {"Cookie": cookies} if cookies else None
    try:
        home = cffi_requests.get(
            BASE_URL + "/",
            impersonate=IMPERSONATE,
            timeout=timeout,
            headers=headers,
        )
        m = _BUNDLE_URL_RE.search(home.text or "")
        bundle_url = m.group(0) if m else ""
        if home.status_code != 200:
            log.debug("client_sync_home_status", extra={"status": home.status_code})
    except Exception as e:
        log.debug("client_sync_home_failed", extra={"error": str(e)})
    if not bundle_url:
        with _client_sync_lock:
            bundle_url = _client_sync["bundle_url"]
    if not bundle_url:
        return client_sync_state()
    version = ""
    try:
        resp = cffi_requests.get(bundle_url, impersonate=IMPERSONATE, timeout=timeout)
        if resp.status_code == 200:
            version = parse_client_version(resp.text or "")
    except Exception as e:
        log.debug("client_sync_bundle_failed", extra={"error": str(e)})
    with _client_sync_lock:
        previous = _client_sync["version"]
        _client_sync["bundle_url"] = bundle_url
        _client_sync["checked_at"] = time.time()
        if version:
            _client_sync["version"] = version
        state = dict(_client_sync)
    if version and version != previous:
        log.info(
            "client_fingerprint_synced",
            extra={"version": version, "previous": previous, "bundle_url": bundle_url},
        )
    elif not version:
        log.debug("client_sync_no_version", extra={"bundle_url": bundle_url})
    _save_sync_cache()
    return state


def _start_client_sync(cookies: str = "") -> None:
    """Kick the one-per-process bundle sync (enabled by DEEPSEEK_CLIENT_SYNC)."""
    global _client_sync_started
    flag = (os.environ.get("DEEPSEEK_CLIENT_SYNC") or "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return
    with _client_sync_lock:
        if _client_sync_started:
            return
        _client_sync_started = True
    threading.Thread(
        target=sync_client_fingerprint,
        kwargs={"cookies": cookies},
        name="client-fingerprint-sync",
        daemon=True,
    ).start()


_load_sync_cache()
try:
    JITTER_SECS = max(0.0, float(os.environ.get("DEEPSEEK_JITTER_SECS", "0.4") or 0))
except ValueError:
    JITTER_SECS = 0.0

# Backoff delays (seconds) before retrying after an upstream rate limit
# (RateLimitError). Each entry consumes one retry; after the last entry
# the error is surfaced to the caller. Disable by setting to empty.
try:
    _raw_delays = os.environ.get("DEEPSEEK_RATE_LIMIT_RETRY_DELAYS", "5,15")
    RATE_LIMIT_RETRY_DELAYS = [float(x) for x in _raw_delays.split(",") if x.strip()]
except ValueError:
    RATE_LIMIT_RETRY_DELAYS = []

# Credential renewal endpoints, tried in order (see refresh_credentials()).
# Empty by default, measured 2026-10-20: chat.deepseek.com has no token-refresh
# endpoint. Reading the app bundle (fe-static.deepseek.com/chat/static/main.*.js)
# yields 46 /api/v0/ paths and none of them mints a token - the bearer token is
# issued at login, kept in localStorage.userToken, and only a browser login
# replaces it. The two paths this list used to try by default answer 200 with the
# SPA's index.html (as does any unknown /api/ path), which surfaced as
# "HTTP 200, non-JSON body". Set DEEPSEEK_REFRESH_PATHS if that ever changes.
REFRESH_PATHS = [
    x.strip() for x in os.environ.get("DEEPSEEK_REFRESH_PATHS", "").split(",") if x.strip()
]

# What the panel's Refresh button can actually do: ask whether the credentials it
# already has still work. GET /api/v0/users/settings is the lightest
# authenticated endpoint - a live token gets {"code":0}, a dead one
# {"code":40003,"msg":"Authorization Failed (invalid token)"}, verified against a
# real account and a deliberately broken token.
CREDENTIAL_CHECK_PATH = os.environ.get(
    "DEEPSEEK_CREDENTIAL_CHECK_PATH", "/api/v0/users/settings",
).strip()
try:
    REFRESH_TIMEOUT_SECS = max(1.0, float(os.environ.get("DEEPSEEK_REFRESH_TIMEOUT_SECS", "20") or 20))
except ValueError:
    REFRESH_TIMEOUT_SECS = 20.0

_WASM_PATH = Path(__file__).resolve().parent / "sha3_wasm_bg.wasm"
with open(_WASM_PATH, "rb") as f:
    _WASM_BYTES = f.read()


class WASMError(Exception):
    pass


class PoWError(Exception):
    pass


class WAFChallengeError(Exception):
    """Raised when AWS WAF or Cloudflare returns a challenge response."""
    def __init__(self, kind: str, status: int, body: str = ""):
        super().__init__(f"{kind} challenge ({status}): {body[:200]}")
        self.kind = kind
        self.status = status
        self.body = body


class UpstreamAuthError(RuntimeError):
    """The upstream rejected the credentials (HTTP 200 + code 40003,
    "Authorization Failed (invalid token)"). Unlike a rate limit, waiting does
    not help — the token has to be refreshed, and if that fails the account
    needs a fresh token/cookies from the browser.
    """


class CredentialRefreshError(RuntimeError):
    """No configured refresh endpoint yielded a new token."""


class UpstreamEmptyError(RuntimeError):
    """Raised when the upstream returned a 200 with an empty body (no SSE
    data lines at all). This is typically a transient throttle/WAF state;
    the adapter retries once with a fresh session before giving up.
    """


class UpstreamHintError(RuntimeError):
    """Raised when the upstream signals an error via an SSE ``hint`` event
    (``type=error``) instead of an HTTP error — e.g. rate limiting. The
    adapter previously ignored these, surfacing them as empty content.
    """


class RateLimitError(UpstreamHintError):
    """Upstream rate limiting (finish_reason ``rate_limit_reached``:
    '消息发送过于频繁，请稍后重试'). Maps to HTTP 429 on the server side."""


class UpstreamParentError(UpstreamEmptyError):
    """Upstream rejected the parent message id (biz_code 26, "invalid
    message id"): the cached session thread tip is gone. Handled like an
    empty response - retry once on a fresh session without a parent -
    mirroring the dsh-plugin WebClient, which drops the poisoned session
    before its retry (UPSTREAM_PARENT).
    """


class UserMutedError(RateLimitError):
    """The upstream answered with ``biz_msg: "user is muted"`` (biz_code 5)
    — an account-level penalty, not a protocol issue. The body is a plain
    JSON 200 (no SSE channel), so the parser previously saw zero tokens and
    misreported it as an empty response.
    """

    def __init__(self, message: str, mute_until: float | None = None):
        super().__init__(message, "user_muted")
        self.mute_until = mute_until


def _mute_msg(raw) -> str | None:
    """Return a human-readable mute message if ``raw`` is an upstream mute /
    enforcement body, else None. Detects the shape::

        {"code": 0, "data": {"biz_code": 5, "biz_msg": "user is muted",
                             "biz_data": {"is_muted": 1, "mute_until": ...}}}
    """
    if not isinstance(raw, dict):
        return None
    d = raw.get("data")
    if not isinstance(d, dict):
        return None
    biz_msg = str(d.get("biz_msg") or "")
    biz_code = d.get("biz_code")
    if biz_code is None:
        return None
    if int(biz_code) == 5 or "mute" in biz_msg.lower():
        until = ""
        bd = d.get("biz_data")
        if isinstance(bd, dict) and bd.get("mute_until"):
            try:
                until = "，解封时间 " + datetime.datetime.fromtimestamp(
                    float(bd["mute_until"])
                ).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError, OSError, OverflowError):
                until = ""
        return f"上游账号已静音：{biz_msg or 'user is muted'}{until}"
    return None


def _biz_envelope(raw) -> tuple[int | None, str]:
    """Return ``(biz_code, biz_msg)`` from a plain-JSON upstream body.

    Mirrors the dsh-plugin WebClient, which inspects the non-SSE 200
    envelope for biz_code 4 (rate limit), 5 (mute, see _mute_msg) and 26
    (invalid parent message id) before treating the body as content.
    """
    if not isinstance(raw, dict):
        return None, ""
    d = raw.get("data")
    if not isinstance(d, dict):
        return None, ""
    code = d.get("biz_code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return code, str(d.get("biz_msg") or "")


def _is_parent_rejection(code: int | None, msg: str) -> bool:
    return code == 26 or bool(re.search(r"invalid message id", msg, re.IGNORECASE))


def _is_json_rate_limit(code: int | None, msg: str) -> bool:
    return code == 4 or bool(re.search(r"频繁|frequent|too many", msg, re.IGNORECASE))


# Upstream hif signature headers: the browser fetches these from dedicated
# endpoints and attaches them to chat/completion requests. Values are
# cached for the TTL announced via the x-hif-ttl response header (600s).
HIF_LEIM_URL = "https://hif-leim.deepseek.com/query"
HIF_DLIQ_URL = "https://hif-dliq.deepseek.com/query"


class _HifProvider:
    """Best-effort fetcher/cache for the ``x-hif-leim`` / ``x-hif-dliq``
    signature headers.

    Any failure (network, non-200, bad payload) degrades to no hif headers
    — the upstream currently accepts requests without them, so the main
    request must never fail because of hif. A stale cached value is reused
    if a refresh fails (better than dropping the header mid-session).
    """

    def __init__(self, client=None):
        self._client = client
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _fetch(self, url: str) -> tuple[str, float] | None:
        if self._client is None:
            return None
        try:
            resp = self._client.get(url, timeout=10)
            if resp.status_code != 200:
                return None
            try:
                ttl = float(resp.headers.get("x-hif-ttl", "600") or 600)
            except (TypeError, ValueError):
                ttl = 600.0
            value = resp.json().get("data", {}).get("biz_data", {}).get("value")
            if not value:
                return None
            return str(value), max(ttl, 1.0)
        except Exception as e:
            log.warning("hif_fetch_failed", extra={"url": url, "error": str(e)[:120]})
            return None

    def _get(self, key: str, url: str) -> str | None:
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[1] > now:
                return hit[0]
        fetched = self._fetch(url)
        if fetched is None:
            with self._lock:
                hit = self._cache.get(key)
                return hit[0] if hit else None
        value, ttl = fetched
        with self._lock:
            self._cache[key] = (value, now + ttl)
        return value

    def headers(self) -> dict:
        """Return ``{x-hif-leim: ..., x-hif-dliq: ...}`` or ``{}`` on failure."""
        out = {}
        leim = self._get("leim", HIF_LEIM_URL)
        if leim:
            out["X-Hif-Leim"] = leim
        dliq = self._get("dliq", HIF_DLIQ_URL)
        if dliq:
            out["X-Hif-Dliq"] = dliq
        return out


class _WASMSolver:
    """WASM-based PoW solver (reused across calls) — thread-safe via lock."""

    def __init__(self):
        self._lock = threading.Lock()
        self.store = Store()
        module = Module(self.store.engine, _WASM_BYTES)
        instance = Instance(self.store, module, [])
        exports = instance.exports(self.store)
        self.memory = exports["memory"]
        self.wasm_solve = exports["wasm_solve"]
        self.add_to_stack = exports["__wbindgen_add_to_stack_pointer"]
        self.malloc = exports["__wbindgen_export_0"]
        self._wbindgen_free = exports["__wbindgen_export_2"]
        self._allocations: list[tuple[int, int]] = []

    def _encode(self, s: str):
        data = s.encode("utf-8")
        ptr = self.malloc(self.store, len(data), 1)
        mem = self.memory.data_ptr(self.store)
        for i, b in enumerate(data):
            mem[ptr + i] = b
        self._allocations.append((ptr, len(data)))
        return ptr, len(data)

    def _free_allocations(self):
        for ptr, length in self._allocations:
            try:
                self._wbindgen_free(self.store, ptr, length, 1)
            except Exception as e:
                log.debug("wasm_free_failed", extra={"ptr": ptr, "length": length, "error": str(e)})
        self._allocations.clear()

    def solve(self, challenge: str, salt: str, expire_at: int, difficulty: int) -> int:
        with self._lock:
            try:
                prefix = f"{salt}_{expire_at}_"
                stack_ptr = self.add_to_stack(self.store, -16)
                chal_ptr, chal_len = self._encode(challenge)
                prefix_ptr, prefix_len = self._encode(prefix)
                self.wasm_solve(self.store, stack_ptr, chal_ptr, chal_len,
                                prefix_ptr, prefix_len, float(difficulty))
                mem = self.memory.data_ptr(self.store)
                ret = int.from_bytes(bytes(mem[stack_ptr:stack_ptr + 4]),
                                     byteorder='little', signed=True)
                if ret == 0:
                    raise PoWError("WASM solver found no solution")
                result = struct.unpack('<d', bytes(mem[stack_ptr + 8:stack_ptr + 16]))[0]
                self.add_to_stack(self.store, 16)
                return int(result)
            finally:
                self._free_allocations()


class DeepSeekAdapter:
    """Adapter for DeepSeek Chat API"""

    # The UA and Sec-Ch-Ua must describe the ClientHello curl_cffi actually
    # sends (DEEPSEEK_IMPERSONATE), never the browser a capture happened to
    # use: a newer UA on an older TLS profile is exactly the kind of mismatch
    # these headers are supposed to avoid. chrome150 is the newest profile this
    # curl_cffi build knows (0.16.3), so the defaults move together with it --
    # bump all three (impersonate, UA, brand) in one commit, never one alone.
    _DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    )
    _DEFAULT_SEC_CH_UA = (
        '"Google Chrome";v="150", "Chromium";v="150", "Not_A Brand";v="24"'
    )

    def __init__(self, token: str, cookies: str,
                 impersonate: str = IMPERSONATE, proxy: str | None = None):
        self.token = self._normalize_token(token)
        self.cookies = cookies
        self.impersonate = impersonate
        self.proxy = proxy
        self._solver = None
        # curl_cffi.Session keeps a cookie jar that auto-merges Set-Cookie,
        # so cf_clearance / AWS WAF tokens stay fresh across calls.
        self._client = cffi_requests.Session(
            impersonate=impersonate,
            timeout=600,
            proxies={"all": proxy} if proxy else None,
        )
        # Best-effort hif signature headers (see _HifProvider); never
        # fails the main request.
        self._hif = _HifProvider(client=self._client)
        # Seed jar from the user-supplied cookie blob (one-shot import only;
        # afterwards the jar is the source of truth).
        if cookies:
            for part in cookies.split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                name, value = part.split("=", 1)
                try:
                    self._client.cookies.set(
                        name.strip(), value.strip(), domain=".deepseek.com"
                    )
                except Exception as e:
                    log.debug(
                        "cookie_seed_failed",
                        extra={"name": name, "error": str(e)},
                    )

        self._msg_counters: dict[str, int] = {}
        version = current_client_version()
        # Read at construction time (not import time) so a deployment can
        # override them without touching the module constants.
        bundle_id = (os.environ.get("DEEPSEEK_CLIENT_BUNDLE_ID") or CLIENT_BUNDLE_ID).strip()
        client_platform = (
            os.environ.get("DEEPSEEK_CLIENT_PLATFORM") or CLIENT_PLATFORM
        ).strip()
        client_locale = (os.environ.get("DEEPSEEK_CLIENT_LOCALE") or CLIENT_LOCALE).strip()
        tz_offset = (os.environ.get("DEEPSEEK_CLIENT_TZ_OFFSET") or CLIENT_TZ_OFFSET).strip()
        accept_language = (
            os.environ.get("DEEPSEEK_ACCEPT_LANGUAGE") or ACCEPT_LANGUAGE
        ).strip()
        # The real frontend identifies the browser with x-device-id (stored in
        # localStorage as "deepseek-device-id:chat"). It is per browser, not per
        # deployment, so it stays unset unless configured.
        device_id = (os.environ.get("DEEPSEEK_DEVICE_ID") or "").strip()
        # Header set captured from a live browser fetch().
        # Names use the same casing the real browser sends.
        self._base_headers = {
            "User-Agent": self._DEFAULT_UA,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": accept_language,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "Priority": "u=1, i",
            "Sec-Ch-Ua": self._DEFAULT_SEC_CH_UA,
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            # DeepSeek-specific application headers (live capture 2026-10-20).
            # X-App-Version is no longer sent by the real frontend and was
            # dropped. User-Agent and Sec-Ch-Ua stay pinned to the curl_cffi
            # impersonation target: they must agree with the TLS fingerprint of
            # the session, so they are deliberately not taken from this capture.
            "X-Client-Version": version,
            "X-Client-Platform": client_platform,
            "X-Client-Locale": client_locale,
            "X-Client-Timezone-Offset": tz_offset,
            "x-client-bundle-id": bundle_id,
        }
        if device_id:
            self._base_headers["X-Device-Id"] = device_id
        # Best-effort, off the request path: learns the current client version
        # from the live bundle (DEEPSEEK_CLIENT_SYNC=1).
        _start_client_sync(self.cookies)

    @staticmethod
    def _normalize_token(token: str) -> str:
        """Accept either a bare token or DeepSeek's localStorage JSON wrapper.

        DeepSeek stores its token in localStorage as
            {"value":"<bare-token>","__version":"0"}
        but the network layer sends only the bare value as `Authorization:
        Bearer <bare-token>`. Users sometimes copy the localStorage form by
        mistake. Auto-unwrap it so the adapter accepts either form.
        """
        if not token:
            return token
        s = token.strip()
        if s.startswith("Bearer "):
            s = s[len("Bearer "):].strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict) and "value" in obj and isinstance(obj["value"], str):
                    return obj["value"]
            except (ValueError, TypeError):
                pass
        return s

    @staticmethod
    def _detect_waf_challenge(status: int, headers) -> str | None:
        """Return the challenge kind if the response is a WAF/CDN challenge."""
        get = headers.get if hasattr(headers, "get") else lambda k, d=None: dict(headers).get(k, d)
        waf_action = (get("x-amzn-waf-action") or "").lower()
        cf_mitigated = (get("cf-mitigated") or "").lower()
        if status == 405 and waf_action == "captcha":
            return "aws-waf-captcha"
        if status == 202 and waf_action == "challenge":
            return "aws-waf-challenge"
        if status in (403, 429) and cf_mitigated == "challenge":
            return "cloudflare-challenge"
        return None

    @property
    def solver(self):
        if self._solver is None:
            self._solver = _WASMSolver()
        return self._solver

    def _get_challenge(self, target_path: str = "/api/v0/chat/completion"):
        resp = self._client.post(
            f"{BASE_URL}/api/v0/chat/create_pow_challenge",
            json={"target_path": target_path},
            headers=self._base_headers,
        )
        kind = self._detect_waf_challenge(resp.status_code, resp.headers)
        if kind:
            raise WAFChallengeError(kind, resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()
        # Expired/rejected credentials arrive as HTTP 200 with
        # {"code":40003,"msg":"Authorization Failed (invalid token)","data":null}.
        # Guard every level: data itself (or data.data) may be None, and the
        # old code crashed with AttributeError while building the error
        # message, masking the real 40003 cause.
        if not isinstance(data, dict):
            raise UpstreamEmptyError(
                "challenge endpoint returned a non-JSON body")
        inner = data.get("data")
        biz = inner.get("biz_data") if isinstance(inner, dict) else None
        challenge = biz.get("challenge") if isinstance(biz, dict) else None
        if not challenge:
            code, msg = data.get("code"), str(data.get("msg") or "")
            if is_auth_failure(code, msg):
                raise UpstreamAuthError(
                    f"credentials rejected: {code} - {msg}")
            raise RuntimeError(
                f"challenge rejected: {code if code is not None else 'unknown'} - "
                f"{msg or 'refresh DEEPSEEK_TOKEN/COOKIES'}"
            )
        return challenge

    def _solve(self, challenge_data: dict) -> str:
        nonce = self.solver.solve(
            challenge=challenge_data["challenge"],
            salt=challenge_data["salt"],
            expire_at=challenge_data["expire_at"],
            difficulty=challenge_data["difficulty"],
        )
        raw = json.dumps({
            "algorithm": "DeepSeekHashV1",
            "challenge": challenge_data["challenge"],
            "salt": challenge_data["salt"],
            "answer": nonce,
            "signature": challenge_data["signature"],
            "target_path": challenge_data["target_path"],
        }, separators=(",", ":"))
        return base64.b64encode(raw.encode()).decode()

    def _pow_headers(self, target_path: str = "/api/v0/chat/completion",
                     include_hif: bool = True):
        if JITTER_SECS > 0:
            time.sleep(random.uniform(0, JITTER_SECS))
        c = self._get_challenge(target_path)
        pow_h = self._solve(c)
        headers = {**self._base_headers, "X-DS-PoW-Response": pow_h}
        if include_hif:
            headers.update(self._hif.headers())
        return headers

    def create_session(self) -> str:
        """Create a new chat session, returns session_id"""
        headers = self._pow_headers("/api/v0/chat/completion", include_hif=False)
        resp = self._client.post(
            f"{BASE_URL}/api/v0/chat_session/create",
            json={"target_path": "/api/v0/chat/completion"},
            headers=headers,
        )
        kind = self._detect_waf_challenge(resp.status_code, resp.headers)
        if kind:
            raise WAFChallengeError(kind, resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            if is_auth_failure(data.get("code"), str(data.get("msg") or "")):
                raise UpstreamAuthError(f"credentials rejected: {data}")
            raise RuntimeError(f"Session creation failed: {data}")
        biz = data["data"]["biz_data"]
        # Handle both formats: direct id vs nested chat_session.id
        if "id" in biz:
            return biz["id"]
        return biz["chat_session"]["id"]

    # ── Credential refresh ───────────────────────────────────────

    @property
    def token_expires_at(self) -> int:
        """Unix expiry from the token's JWT `exp` claim (0 = unknown)."""
        return _jwt_expires_at(self.token)

    def set_token(self, token: str) -> None:
        """Swap the bearer token in place (accepts the localStorage form too)."""
        self.token = self._normalize_token(token)
        self._base_headers["Authorization"] = f"Bearer {self.token}"

    def cookie_blob(self) -> str:
        """The live cookie jar as a `name=value; ...` blob.

        The jar is the source of truth after construction (Set-Cookie rotations
        land there), so exporting it is how a refreshed WAF/session cookie makes
        it back into the persisted account.
        """
        parts = []
        jar = getattr(self._client, "cookies", None)
        getter = getattr(jar, "items", None)
        if callable(getter):
            try:
                for name, value in getter():
                    if name:
                        parts.append(f"{name}={value if value is not None else ''}")
            except Exception as e:  # pragma: no cover - jar implementation detail
                log.debug("cookie_blob_failed", extra={"error": str(e)})
        return "; ".join(parts)

    def sync_cookies(self) -> bool:
        """Adopt the live jar into `self.cookies`; True when it changed.

        Callers persist the returned blob so the account record carries the
        rotating WAF/session cookies instead of the values typed in when the
        account was added.
        """
        blob = self.cookie_blob()
        if not blob or blob == self.cookies:
            return False
        self.cookies = blob
        return True

    def refresh_credentials(self) -> dict:
        """Mint fresh credentials using the current ones.

        Posts an empty JSON body to each path in REFRESH_PATHS until one returns
        a token, then swaps token + cookies into this adapter and returns
        `{token, cookies, path, expires_at, raw}`.

        Two failure modes are deliberately distinguished:
        * UpstreamAuthError - the current credentials are already dead, and a
          refresh needs a live token. Only a browser login can recover.
        * CredentialRefreshError - the endpoint is missing/moved or the payload
          shape changed; DEEPSEEK_REFRESH_PATHS is the escape hatch.
        """
        notes = []
        for path in REFRESH_PATHS:
            url = f"{BASE_URL}{path}"
            try:
                resp = self._client.post(
                    url, json={}, headers=self._base_headers,
                    timeout=REFRESH_TIMEOUT_SECS,
                )
            except Exception as e:
                notes.append(f"{path}: {e}")
                continue

            kind = self._detect_waf_challenge(resp.status_code, resp.headers)
            if kind:
                notes.append(f"{path}: {kind} ({resp.status_code})")
                continue

            try:
                payload = resp.json()
            except Exception:
                notes.append(f"{path}: HTTP {resp.status_code}, non-JSON body")
                continue

            code = payload.get("code") if isinstance(payload, dict) else None
            msg = str(payload.get("msg") or "") if isinstance(payload, dict) else ""
            token = extract_refreshed_token(payload)
            if not token:
                if is_auth_failure(code, msg):
                    raise UpstreamAuthError(
                        f"credentials rejected by {path}: {code} - {msg}")
                notes.append(f"{path}: code={code} msg={msg or 'no token in response'}")
                continue

            self.set_token(token)
            cookies = merge_cookie_blobs(self.cookies, self.cookie_blob())
            self.cookies = cookies
            expires_at = _jwt_expires_at(token)
            log.info("credentials_refreshed",
                     extra={"path": path, "token_expires_at": expires_at})
            return {
                "token": token,
                "cookies": cookies,
                "path": path,
                "expires_at": expires_at,
                "raw": payload,
            }

        raise CredentialRefreshError(
            "credential refresh failed: " + (" | ".join(notes) or "no refresh paths configured"))

    def check_credentials(self) -> dict:
        """Ask the upstream whether this account's token is still accepted.

        Returns {"ok", "code", "msg", "path", "detail"}. A rejected credential is
        the normal negative answer, not an exception: the caller has to record it
        on the account and tell the user what to do instead.
        """
        path = CREDENTIAL_CHECK_PATH
        if not path:
            return {"ok": False, "code": None, "msg": "", "path": "",
                    "detail": "no credential check path configured"}
        try:
            resp = self._client.get(
                f"{BASE_URL}{path}", headers=self._base_headers,
                timeout=REFRESH_TIMEOUT_SECS,
            )
        except Exception as e:
            return {"ok": False, "code": None, "msg": "", "path": path,
                    "detail": f"{type(e).__name__}: {e}"}

        kind = self._detect_waf_challenge(resp.status_code, resp.headers)
        if kind:
            return {"ok": False, "code": resp.status_code, "msg": "", "path": path,
                    "detail": f"{kind} ({resp.status_code})"}
        try:
            payload = resp.json()
        except Exception:
            return {"ok": False, "code": resp.status_code, "msg": "", "path": path,
                    "detail": f"HTTP {resp.status_code}, non-JSON body"}

        code = payload.get("code") if isinstance(payload, dict) else None
        msg = str(payload.get("msg") or "") if isinstance(payload, dict) else ""
        if code == 0:
            return {"ok": True, "code": 0, "msg": msg, "path": path, "detail": ""}
        if is_auth_failure(code, msg):
            return {"ok": False, "code": code, "msg": msg, "path": path,
                    "detail": "token rejected by upstream"}
        if code is not None:
            return {"ok": False, "code": code, "msg": msg, "path": path,
                    "detail": f"unexpected code {code}"}
        return {"ok": False, "code": None, "msg": msg, "path": path,
                "detail": "no code in response"}

    def _parse_sse(self, text: str):
        """Parse SSE text into a list of events"""
        events = []
        current_event = ""
        for line in text.split("\n"):
            if line.startswith("event: "):
                current_event = line[7:]
            elif line.startswith("data: "):
                data_str = line[6:]
                if data_str:
                    try:
                        events.append((current_event, json.loads(data_str)))
                    except json.JSONDecodeError:
                        events.append((current_event, data_str))
                current_event = ""
        return events

    @staticmethod
    def _scan_toast_errors(events):
        """Return the first upstream toast error in ``events`` as
        ``(message, finish_reason)``, or ``None`` if there is none.

        DeepSeek's web backend signals "this client is too old to use
        Expert / your account hit a content policy / etc." via an SSE
        ``toast`` event with ``type=error`` instead of a non-2xx HTTP
        status. Without this scan we silently turn that into an empty
        completion (issue #8) and the user has no idea why.
        """
        for event_type, data in events:
            if event_type != "toast" or not isinstance(data, dict):
                continue
            if str(data.get("type", "")).lower() != "error":
                continue
            content = data.get("content") or data.get("msg") or ""
            finish_reason = data.get("finish_reason") or "upstream_toast_error"
            return content, finish_reason
        return None

    @staticmethod
    def _scan_hint_errors(events):
        """Return the first upstream ``hint`` error in ``events`` as
        ``(message, finish_reason)`` or ``None``.

        Rate limiting arrives as an SSE ``event: hint`` frame with
        ``type=error`` and ``finish_reason=rate_limit_reached``
        ('消息发送过于频繁，请稍后重试'). Previously unparsed, so a
        rate-limited request looked like a successful empty completion.
        """
        for event_type, data in events:
            if event_type != "hint" or not isinstance(data, dict):
                continue
            if str(data.get("type", "")).lower() != "error":
                continue
            content = data.get("content") or data.get("msg") or ""
            finish_reason = data.get("finish_reason") or "upstream_hint_error"
            return content, finish_reason
        return None

    @staticmethod
    def _raise_hint_error(content: str, finish_reason: str):
        if finish_reason == "rate_limit_reached" or "频繁" in (content or ""):
            raise RateLimitError(content or "rate limited", finish_reason)
        raise UpstreamHintError(
            f"upstream_hint_error: {content or ''} ({finish_reason})"
        )

    def _send_completion(self, session_id: str, prompt: str, stream: bool = False,
                         model_type: str | None = None,
                         thinking_enabled: bool = False, search_enabled: bool = False,
                         parent_message_id: int | None = None):
        """Send a completion request, returns raw response.

        ``parent_message_id`` is the message id from the previous turn in
        this chat_session. Pass ``None`` to start a fresh thread. When the
        caller doesn't provide one, the per-session counter is used as a
        monotonically increasing fallback (legacy behavior).
        """
        headers = self._pow_headers("/api/v0/chat/completion")
        if parent_message_id is None:
            mid = self._msg_counters.get(session_id, 0) + 1
            self._msg_counters[session_id] = mid
            parent_message_id = mid - 1 if mid > 1 else None
        body = {
            "chat_session_id": session_id,
            "parent_message_id": parent_message_id,
            "model_type": model_type,
            "prompt": prompt,
            "ref_file_ids": [],
            "stream": stream,
            "thinking_enabled": thinking_enabled,
            "search_enabled": search_enabled,
            "preempt": False,
        }
        # Non-streaming calls use a fresh Session: a pooled connection left
        # dirty by a previously streamed call (iter_lines abandoned on the
        # FINISHED status) could otherwise be reused here and return an
        # empty body. The extra TLS handshake is negligible for non-stream.
        if stream:
            client = self._client
        else:
            client = cffi_requests.Session(
                impersonate=self.impersonate,
                timeout=600,
                proxies={"all": self.proxy} if self.proxy else None,
            )
        resp = client.post(
            f"{BASE_URL}/api/v0/chat/completion",
            json=body,
            headers=headers,
        )
        kind = self._detect_waf_challenge(resp.status_code, resp.headers)
        if kind:
            raise WAFChallengeError(kind, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp

    def chat(self, session_id: str, prompt: str, model_type: str | None = None,
             thinking_enabled: bool = False, search_enabled: bool = False,
             parent_message_id: int | None = None,
             ready_out: dict | None = None) -> tuple[str, str]:
        """Send a non-streaming chat message.

        Returns ``(content, thinking)``:
          * ``content`` — the user-facing answer (concatenated text tokens).
          * ``thinking`` — the expert-mode reasoning chain (empty string in
            quick mode or when ``thinking_enabled`` is False).

        Previously this method silently discarded ``thinking_parts``,
        which meant the Anthropic ``/v1/messages`` non-streaming endpoint
        could never expose the ``thinking`` content block. Callers that
        only care about the visible text should unpack with
        ``content, _ = adapter.chat(...)``.

        ``ready_out`` is an optional dict the caller provides; when the
        upstream ``ready`` event is seen the adapter fills
        ``ready_out["response_message_id"]`` (and keys it by the session
        actually used). This maintains the multi-turn parent chain — the
        server caches this id and re-sends it as ``parent_message_id`` on
        the next turn.
        """
        for attempt in range(1, max(len(RATE_LIMIT_RETRY_DELAYS), 1) + 2):
            try:
                ret = self._chat_once(session_id, prompt, model_type=model_type,
                                      thinking_enabled=thinking_enabled,
                                      search_enabled=search_enabled,
                                      parent_message_id=parent_message_id,
                                      ready_out=ready_out)
                if ready_out is not None:
                    ready_out["session_id"] = session_id
                return ret
            except UserMutedError:
                # Account-level penalty: persists until mute_until, retrying
                # with backoff would only waste time. Surface immediately.
                raise
            except RateLimitError:
                idx = attempt - 1
                if idx >= len(RATE_LIMIT_RETRY_DELAYS):
                    raise
                delay = RATE_LIMIT_RETRY_DELAYS[idx]
                log.warning("upstream_rate_limit_retry_nonstream",
                            extra={"attempt": attempt, "delay": delay})
                time.sleep(delay)
                session_id = self.create_session()
                # Fresh session: no parent id from the stale session may be
                # re-sent, or the upstream replies empty again.
                parent_message_id = None
            except UpstreamEmptyError:
                if attempt > 1:
                    raise
                log.warning("upstream_empty_retry_nonstream")
                session_id = self.create_session()
                # Fresh session: never re-send the stale parent id.
                parent_message_id = None
        raise UpstreamEmptyError("upstream returned empty response")  # pragma: no cover

    def _chat_once(self, session_id: str, prompt: str, model_type: str | None = None,
                   thinking_enabled: bool = False, search_enabled: bool = False,
                   parent_message_id: int | None = None,
                   ready_out: dict | None = None) -> tuple[str, str]:
        resp = self._send_completion(session_id, prompt, stream=False,
                                     model_type=model_type,
                                     thinking_enabled=thinking_enabled,
                                     search_enabled=search_enabled,
                                     parent_message_id=parent_message_id)
        if not resp.text or not resp.text.strip():
            raise UpstreamEmptyError("upstream returned empty response body")

        # A mute/enforcement body is a plain JSON 200 (not SSE) — check it
        # before the SSE parse (which would silently drop it).
        try:
            raw = json.loads(resp.text)
        except json.JSONDecodeError:
            raw = None
        mute = _mute_msg(raw) if isinstance(raw, dict) else None
        if mute:
            raise UserMutedError(mute)
        # Parity with the plugin non-SSE branch: a rejected parent id or a
        # plain-JSON rate limit must not masquerade as an empty response.
        code, msg = _biz_envelope(raw)
        if _is_parent_rejection(code, msg):
            raise UpstreamParentError(f"上游拒绝了 parent message id：{msg or 'invalid message id'}")
        if _is_json_rate_limit(code, msg):
            raise RateLimitError(f"上游限流：{msg or 'biz_code 4'}", "rate_limit")

        events = self._parse_sse(resp.text)

        # Issue #8: upstream may reject with a `toast` event of type=error
        # instead of a non-2xx HTTP status. Surface that to the caller as
        # a real exception instead of silently returning empty content.
        toast = self._scan_toast_errors(events)
        if toast is not None:
            raise RuntimeError(f"upstream_toast_error: {toast[0]} ({toast[1]})")

        # Multi-turn parent chain: capture the upstream response message id
        # from the `event: ready` frame (or the response message_id field)
        # so the caller can cache it and send it as parent_message_id on
        # the next turn. Without this, reusing a cached session for a
        # second turn fails with an empty upstream response.
        if ready_out is not None:
            for event_type, data in events:
                if not isinstance(data, dict):
                    continue
                mid = None
                if event_type == "ready" and isinstance(data.get("response_message_id"), int):
                    mid = data["response_message_id"]
                elif isinstance(data.get("v"), dict) and 'response' in data["v"]:
                    r = data["v"]["response"]
                    if isinstance(r.get("message_id"), int):
                        mid = r["message_id"]
                if mid is not None:
                    ready_out["response_message_id"] = mid
                    ready_out["session_id"] = session_id
                    break

        # Rate limiting arrives as an `event: hint` with type=error
        # (finish_reason=rate_limit_reached). Surface it instead of
        # returning an empty completion.
        hint = self._scan_hint_errors(events)
        if hint is not None:
            self._raise_hint_error(hint[0], hint[1])

        # Collect all content from both normal mode and expert fragment mode
        content_parts = []
        thinking_parts = []
        frag_type = None  # None, 'thinking', 'content'

        for event_type, data in events:
            if not isinstance(data, dict):
                continue
            p = data.get("p", "")
            o = data.get("o", "")
            v = data.get("v", "")

            # Expert mode: initial response with fragments
            if isinstance(v, dict) and 'response' in v:
                resp_data = v['response']
                fragments = resp_data.get('fragments', [])
                if fragments:
                    ft = fragments[0].get('type', '')
                    frag_type = 'thinking' if ft == 'THINK' else 'content'
                    fc = fragments[0].get('content', '')
                    if fc:
                        (thinking_parts if frag_type == 'thinking' else content_parts).append(fc)
                continue

            # Expert mode: fragment content append
            if p == "response/fragments/-1/content" and o == "APPEND":
                if frag_type == 'thinking':
                    thinking_parts.append(v)
                else:
                    content_parts.append(v)
                continue
            if p == "response/fragments/-1/content" and not o:
                # Frag content without o (happens after fragment switch)
                if frag_type == 'thinking':
                    thinking_parts.append(v)
                else:
                    content_parts.append(v)
                continue

            # Expert mode: fragment switch. The switch frame may already
            # carry initial content for the new fragment (v[0].content) —
            # dropping it truncates the head of the answer (e.g. quick +
            # thinking replies arriving as "arta" instead of "Jakarta").
            if p == "response/fragments" and o == "APPEND":
                if isinstance(v, list) and v:
                    frag = v[0]
                    new_type = frag.get('type', '')
                    if new_type == 'RESPONSE':
                        frag_type = 'content'
                    elif new_type == 'THINK':
                        frag_type = 'thinking'
                    # A short answer can arrive entirely inside the APPEND that
                    # introduces the RESPONSE fragment (observed for
                    # "1+1=? 只回答数字" with thinking on: the frame carries
                    # content="2" and there is no follow-up content token).
                    # Without this the non-stream path returned an empty answer.
                    seed = frag.get('content', '')
                    if seed:
                        (thinking_parts if frag_type == 'thinking'
                         else content_parts).append(seed)
                continue

            # Normal mode
            if p == "response/content" and o == "APPEND":
                content_parts.append(v)
                continue

            # Plain token event — belongs to current fragment or normal mode
            if "v" in data and "p" not in data and "o" not in data:
                token = data["v"]
                if isinstance(token, str) and token:
                    if frag_type == 'thinking':
                        thinking_parts.append(token)
                    else:
                        content_parts.append(token)
                continue

        content = "".join(content_parts)
        thinking = "".join(thinking_parts)
        # Defense-in-depth: a response with neither visible content nor
        # reasoning is almost never legitimate (upstream occasionally
        # returns an empty SSE after a streamed call). Surface it as an
        # UpstreamEmptyError so the caller retries once with a fresh
        # session instead of silently returning empty content.
        if not content and not thinking:
            raise UpstreamEmptyError("upstream returned empty content")
        return content, thinking

    def chat_stream(self, session_id: str, prompt: str,
                    model_type: str | None = None,
                    thinking_enabled: bool = False, search_enabled: bool = False,
                    parent_message_id: int | None = None,
                    ready_out: dict | None = None):
        """Stream a chat message, yields content tokens.

        In expert mode (model_type='expert'), yields dicts with
        __type='thinking' for reasoning tokens and strings for final content.

        ``parent_message_id`` is the message id from the previous turn; pass
        ``None`` to start a fresh thread.

        ``ready_out`` is an optional dict filled with the upstream
        ``response_message_id`` (and the session id actually used) when the
        ``event: ready`` frame arrives. The caller caches it and re-sends it
        as ``parent_message_id`` on the next turn to keep multi-turn sessions
        working (a stale parent id makes the upstream reply empty).

        A completely empty upstream stream (no tokens at all) is retried
        once with a fresh session; if it is still empty an
        ``UpstreamEmptyError`` is raised.
        """
        for attempt in range(1, max(len(RATE_LIMIT_RETRY_DELAYS), 1) + 2):
            yielded = False
            try:
                for token in self._chat_stream_once(
                        session_id, prompt, model_type=model_type,
                        thinking_enabled=thinking_enabled,
                        search_enabled=search_enabled,
                        parent_message_id=parent_message_id,
                        ready_out=ready_out):
                    yielded = True
                    yield token
            except UserMutedError:
                # Account-level penalty: persists until mute_until — surface
                # immediately, no backoff retry.
                raise
            except RateLimitError:
                idx = attempt - 1
                if idx >= len(RATE_LIMIT_RETRY_DELAYS):
                    raise
                delay = RATE_LIMIT_RETRY_DELAYS[idx]
                log.warning("upstream_rate_limit_retry_stream",
                            extra={"attempt": attempt, "delay": delay})
                time.sleep(delay)
            except UpstreamEmptyError:
                if attempt > 1:
                    raise
            else:
                if yielded:
                    return
                log.warning("upstream_empty_retry_stream")
            if attempt > max(len(RATE_LIMIT_RETRY_DELAYS), 1):
                break
            session_id = self.create_session()
            # The retry uses a brand-new session — a parent message id from
            # the stale session is invalid there and makes the upstream
            # reply empty again. Start a fresh thread.
            parent_message_id = None
        raise UpstreamEmptyError("upstream returned empty response")  # pragma: no cover

    def _chat_stream_once(self, session_id: str, prompt: str,
                          model_type: str | None = None,
                          thinking_enabled: bool = False, search_enabled: bool = False,
                          parent_message_id: int | None = None,
                          ready_out: dict | None = None):
        headers = self._pow_headers("/api/v0/chat/completion")
        if parent_message_id is None:
            mid = self._msg_counters.get(session_id, 0) + 1
            self._msg_counters[session_id] = mid
            parent_message_id = mid - 1 if mid > 1 else None
        body = {
            "chat_session_id": session_id,
            "parent_message_id": parent_message_id,
            "model_type": model_type,
            "prompt": prompt,
            "ref_file_ids": [],
            "stream": True,
            "thinking_enabled": thinking_enabled,
            "search_enabled": search_enabled,
            "preempt": False,
        }
        resp = self._client.post(
            f"{BASE_URL}/api/v0/chat/completion",
            json=body, headers=headers, stream=True,
        )
        try:
            kind = self._detect_waf_challenge(resp.status_code, resp.headers)
            if kind:
                # Drain so the connection can be reused.
                try:
                    body_text = resp.text
                except Exception as e:
                    log.debug("waf_body_read_failed", extra={"error": str(e)})
                    body_text = ""
                raise WAFChallengeError(kind, resp.status_code, body_text)
            resp.raise_for_status()
            frag_type = None  # None, 'thinking', 'content'
            current_event = ""  # tracks the most recent `event:` SSE field

            for line in resp.iter_lines():
                # curl_cffi yields bytes from iter_lines.
                if isinstance(line, (bytes, bytearray)):
                    try:
                        line = line.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                line = line.rstrip()
                if not line:
                    continue
                if line.startswith("event: "):
                    current_event = line[7:]
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                    if not data_str:
                        continue
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                else:
                    # Plain JSON body (non-SSE) — e.g. the account-mute
                    # enforcement payload. Try to parse it; if it doesn't
                    # look like our envelope, skip.
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                if not isinstance(data, dict):
                    continue

                p = data.get("p", "")
                o = data.get("o", "")
                v = data.get("v", "")

                # Account-level mute enforcement arrives as a plain JSON
                # 200 body (no SSE channel) — surface it as a real error
                # instead of an empty response.
                mute = _mute_msg(data)
                if mute:
                    raise UserMutedError(mute)
                code, msg = _biz_envelope(data)
                if _is_parent_rejection(code, msg):
                    raise UpstreamParentError(f"上游拒绝了 parent message id：{msg or 'invalid message id'}")
                if _is_json_rate_limit(code, msg):
                    raise RateLimitError(f"上游限流：{msg or 'biz_code 4'}", "rate_limit")

                # Upstream may reject with a `toast` event of type=error
                # (issue #8). Surface it as a real error so the SSE
                # consumer sees the upstream message instead of empty
                # content. The `event: toast` line and the `toast`
                # payload both arrive as separate SSE frames, so we have
                # to check both — sometimes the server only sends the
                # data frame with the toast field inline.
                # Upstream error events (toast / hint). The payload may be the
                # top-level data dict itself ({type, content, finish_reason})
                # or wrapped as {"v": {...}} — check both.
                err = (data if isinstance(data, dict)
                       and str(data.get("type", "")).lower() == "error" else None)
                if err is None and isinstance(v, dict) \
                        and str(v.get("type", "")).lower() == "error":
                    err = v
                if current_event == "toast" and err is not None:
                    raise RuntimeError(
                        f"upstream_toast_error: {err.get('content') or err.get('msg') or ''} "
                        f"({err.get('finish_reason') or 'upstream_toast_error'})"
                    )
                if current_event == "hint" and err is not None:
                    self._raise_hint_error(
                        err.get("content") or err.get("msg") or "",
                        err.get("finish_reason") or "upstream_hint_error",
                    )

                # Multi-turn parent chain: record the upstream response
                # message id from the `event: ready` frame so the caller can
                # cache and re-send it as parent_message_id next turn.
                if ready_out is not None and current_event == "ready":
                    if isinstance(data.get("response_message_id"), int):
                        ready_out["response_message_id"] = data["response_message_id"]
                        ready_out["session_id"] = session_id
                if isinstance(data.get("toast"), dict) and \
                        str(data["toast"].get("type", "")).lower() == "error":
                    t = data["toast"]
                    raise RuntimeError(
                        f"upstream_toast_error: {t.get('content') or t.get('msg') or ''} "
                        f"({t.get('finish_reason') or 'upstream_toast_error'})"
                    )

                # Initial response with fragments (expert mode)
                if isinstance(v, dict) and 'response' in v:
                    resp_data = v['response']
                    fragments = resp_data.get('fragments', [])
                    if fragments:
                        ft = fragments[0].get('type', '')
                        frag_type = 'thinking' if ft == 'THINK' else 'content'
                        fc = fragments[0].get('content', '')
                        if fc:
                            if frag_type == 'thinking':
                                yield {"__type": "thinking", "content": fc}
                            else:
                                yield fc
                    else:
                        frag_type = 'content'
                        content = resp_data.get('content', '')
                        if content:
                            yield content
                    continue

                # Fragment content append (expert mode)
                if p == "response/fragments/-1/content" and o == "APPEND":
                    if frag_type == 'thinking':
                        if v:
                            yield {"__type": "thinking", "content": v}
                    else:
                        if v:
                            yield v
                    continue

                # Fragment content without o (after fragment switch in batched responses)
                if p == "response/fragments/-1/content" and not o:
                    if frag_type == 'thinking':
                        if v:
                            yield {"__type": "thinking", "content": v}
                    else:
                        if v:
                            yield v
                    continue

                # Fragment switch (expert mode). Same head-truncation
                # hazard as the non-streaming parser: emit v[0].content
                # instead of dropping it.
                if p == "response/fragments" and o == "APPEND":
                    if isinstance(v, list) and v:
                        new_type = v[0].get('type', '')
                        if new_type == 'RESPONSE':
                            frag_type = 'content'
                        elif new_type == 'THINK':
                            frag_type = 'thinking'
                        fc = v[0].get('content', '')
                        if fc:
                            if frag_type == 'thinking':
                                yield {"__type": "thinking", "content": fc}
                            else:
                                yield fc
                    continue

                # Normal mode content
                if p == "response/content" and o == "APPEND":
                    yield v
                    continue

                # Plain token event
                if "v" in data and "p" not in data and "o" not in data:
                    token = data["v"]
                    if isinstance(token, str) and token:
                        if frag_type == 'thinking':
                            yield {"__type": "thinking", "content": token}
                        else:
                            yield token
                    continue

                # Status
                if p == "response/status":
                    yield {"__type": "status", "status": v}
                    continue
        finally:
            try:
                resp.close()
            except Exception as e:
                log.debug("response_close_failed", extra={"error": str(e)})
