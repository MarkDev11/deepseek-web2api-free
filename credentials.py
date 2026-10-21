"""Credential lifecycle helpers: JWT expiry, auth-failure detection, refresh payloads.

DeepSeek web credentials are short-lived. Two facts drive this module:

* The access token is a JWT, so its own `exp` claim tells us how much life is
  left - no extra request needed to know when we are about to expire.
* An expired/rejected token does NOT arrive as HTTP 401. The upstream answers
  HTTP 200 with {"code": 40003, "msg": "Authorization Failed (invalid token)"}
  (observed on /api/v0/chat/create_pow_challenge). Detecting that code is what
  lets us refresh reactively instead of failing the request.

Everything here is pure and offline: no network, no globals. The wire calls
live in `adapter.DeepSeekAdapter.refresh_credentials()`.
"""
from __future__ import annotations

import base64
import json
import re
import time

# Upstream business codes / message markers that mean "these credentials are
# no longer usable" (as opposed to rate limiting or a WAF challenge).
AUTH_ERROR_CODES = frozenset({40003})
AUTH_ERROR_MARKERS = (
    "authorization failed",
    "invalid token",
    "token expired",
    "unauthorized",
    "not logged in",
    "credentials rejected",
)

# Key names a refreshed token may hide behind. DeepSeek has wrapped payloads in
# several shapes over time, so extraction is deliberately tolerant.
_TOKEN_KEYS = ("token", "access_token", "accessToken", "jwt")
_CONTAINER_KEYS = ("data", "biz_data", "bizData", "result")
# A token stored bare in localStorage is wrapped as {"value": ..., "__version": ...}.
_WRAPPER_KEYS = ("value",)


def _strip_bearer(value: str) -> str:
    s = (value or "").strip()
    if s.lower().startswith("bearer "):
        s = s[len("bearer "):].strip()
    return s


def decode_jwt_claims(token: str) -> dict | None:
    """Return a JWT's claims without verifying the signature.

    Returns None when the value is not a JWT: the adapter also accepts the
    localStorage wrapper form, and users sometimes paste a bare opaque token.
    """
    s = _strip_bearer(token)
    if not s or s.count(".") != 2:
        return None
    header, payload, _sig = s.split(".")
    if not payload:
        return None
    try:
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:
        return None
    return claims if isinstance(claims, dict) else None


def token_expires_at(token: str) -> int:
    """Unix timestamp when the token expires, or 0 when unknown."""
    claims = decode_jwt_claims(token)
    if not claims:
        return 0
    exp = claims.get("exp")
    try:
        return int(exp) if exp is not None else 0
    except (TypeError, ValueError):
        return 0


def token_expires_in(token: str, now: int | None = None) -> int | None:
    """Seconds of remaining validity; negative = expired, None = unknown."""
    exp = token_expires_at(token)
    if not exp:
        return None
    return exp - int(now if now is not None else time.time())


def should_refresh(expires_at: int, ahead_secs: int,
                   now: int | None = None) -> bool:
    """True when the token is within `ahead_secs` of expiring (or already expired).

    An unknown expiry (expires_at == 0) never triggers a proactive refresh; we
    only react to an explicit auth failure in that case.
    """
    if not expires_at or expires_at <= 0 or ahead_secs <= 0:
        return False
    now = int(now if now is not None else time.time())
    return now >= expires_at - ahead_secs


def is_auth_failure(code, msg: str = "") -> bool:
    """True when an upstream business code/message means bad credentials."""
    try:
        if code is not None and int(code) in AUTH_ERROR_CODES:
            return True
    except (TypeError, ValueError):
        pass
    low = (msg or "").lower()
    return any(marker in low for marker in AUTH_ERROR_MARKERS)


def _unwrap_value(value):
    """DeepSeek stores its token as {"value": "...", "__version": "0"}."""
    if isinstance(value, dict):
        inner = value.get("value")
        if isinstance(inner, str) and inner:
            return inner
    return value


def _looks_like_token(value) -> bool:
    if not isinstance(value, str):
        return False
    s = _strip_bearer(value)
    return bool(s) and " " not in s and len(s) >= 20


def _search_keys(node, depth: int = 0) -> str:
    """Search for a token stored under one of _TOKEN_KEYS (containers first)."""
    if depth > 5:
        return ""
    if isinstance(node, dict):
        for key in _TOKEN_KEYS:
            if key in node:
                cand = _unwrap_value(node[key])
                if _looks_like_token(cand):
                    return cand
        for key in _CONTAINER_KEYS:
            if key in node:
                found = _search_keys(node[key], depth + 1)
                if found:
                    return found
        for key in _WRAPPER_KEYS:
            if key in node and isinstance(node[key], str):
                cand = _strip_bearer(node[key])
                if _looks_like_token(cand):
                    return cand
        for key, val in node.items():
            if key in _CONTAINER_KEYS or key in _TOKEN_KEYS:
                continue
            found = _search_keys(val, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _search_keys(item, depth + 1)
            if found:
                return found
    return ""


def extract_refreshed_token(payload) -> str:
    """Pull the new token out of a refresh response, tolerating several shapes.

    Handles {"data": {"biz_data": {"token": {"value": ...}}}}, {"data":
    {"token": "..."}}, {"access_token": "..."}, a bare localStorage wrapper and
    a plain string. Returns "" when nothing token-shaped is present.
    """
    if isinstance(payload, str):
        s = payload.strip()
        if s.startswith("{"):
            # localStorage-style wrapper handed over as raw JSON text
            try:
                return extract_refreshed_token(json.loads(s))
            except ValueError:
                return ""
        return _strip_bearer(s) if _looks_like_token(s) else ""
    if not isinstance(payload, (dict, list)):
        return ""
    found = _search_keys(payload)
    return _strip_bearer(found) if found else ""


def merge_cookie_blobs(old: str, new: str) -> str:
    """Merge two `name=value; name2=value2` blobs; `new` wins per name.

    A refresh response usually rotates only a couple of cookies. Keeping the
    names we already had avoids discarding a still-valid cf_clearance just
    because it was absent from one response.
    """
    def parse(blob: str) -> dict:
        out = {}
        for part in (blob or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if name:
                out[name] = value.strip()
        return out

    merged = parse(old)
    merged.update(parse(new))
    return "; ".join(f"{k}={v}" for k, v in merged.items())


_COOKIE_PREFIX_RE = re.compile(r"^\s*cookie\s*:\s*", re.IGNORECASE)


def _strip_cookie_prefix(value: str) -> str:
    text = _COOKIE_PREFIX_RE.sub("", (value or "").strip())
    return text.strip().rstrip(";").strip()


def _looks_like_cookie_header(value: str) -> bool:
    text = (value or "").strip()
    if not text or "=" not in text:
        return False
    if _COOKIE_PREFIX_RE.match(text):
        return True
    return ";" in text


def parse_credential_blob(raw: str, current_token: str = "",
                          current_cookies: str = "") -> tuple[str, str]:
    """Turn one pasted blob into (token, cookies).

    A pasted blob may be a bare token, a localStorage wrapper
    ({"value": ...}), a raw "Cookie:" header, or any combination joined with
    "||" or newlines. Whatever is absent falls back to what the account already
    has, and a cookie header is merged name-by-name so a still-valid
    ds_session_id (HttpOnly, invisible to document.cookie) survives a partial
    paste.

    Returns the inputs untouched when the blob yields nothing recognisable, so
    a bad paste can never blank out working credentials.
    """
    text = (raw or "").strip().strip("'\"").strip()
    if not text:
        return current_token, current_cookies

    token = ""
    cookies = ""
    for chunk in re.split(r"\|\||\n", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.startswith("{") or chunk.startswith("["):
            try:
                found = extract_refreshed_token(json.loads(chunk))
            except ValueError:
                found = ""
            if found:
                token = found
            continue
        if _looks_like_cookie_header(chunk):
            cookies = _strip_cookie_prefix(chunk)
            continue
        candidate = _strip_bearer(chunk)
        if _looks_like_token(candidate):
            token = candidate

    final_token = token or current_token
    if cookies:
        final_cookies = merge_cookie_blobs(current_cookies, cookies)
    else:
        final_cookies = current_cookies
    return final_token, final_cookies

