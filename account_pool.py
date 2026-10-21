"""
Account pool — multi-account management for DeepSeek Chat proxy.

Manages multiple DeepSeek accounts, tracks their states (idle/busy/error),
provides credential health checking, session lifecycle, and persistent
panel-managed accounts.
"""
import hashlib
import json
import os
import secrets
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
import credential_store
import adapter as adapter_module
from adapter import DeepSeekAdapter
from credentials import (
    is_auth_failure,
    should_refresh,
    token_expires_at as _jwt_expires_at,
)
from crypto import (
    is_enabled as crypto_is_enabled,
    encrypt_account_dict,
    decrypt_account_dict,
    detect_store_version,
    STORE_VERSION_ENCRYPTED,
    STORE_VERSION_PLAIN,
    maybe_upgrade_store_file,
)
from logger import get_logger

load_dotenv()

log = get_logger("account_pool")

STORE_VERSION = 1
DEFAULT_STORE_PATH = Path(__file__).resolve().parent / "data" / "accounts.json"

# Auto-recovery: when an account has accumulated this many errors,
# schedule a background health check to clear the error state if the
# upstream is reachable again. Set to 0 to disable (every error is
# permanent until manual relogin).
_RECOVER_ERROR_THRESHOLD = 3
_RECOVER_FLAG_DISABLED = os.environ.get("DISABLE_AUTO_RECOVER", "").lower() in {
    "1", "true", "yes", "on",
}


def _env_seconds(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, "") or default))
    except ValueError:
        return default


# Per-account pacing (anti-mute). DeepSeek punishes bursts from one account, not
# sustained throughput, so the useful dial is the minimum gap between two
# upstream turns from the SAME account: extra requests queue instead of stacking
# up on one account while the others sit idle. 0 disables pacing (default, which
# keeps the single-account behaviour unchanged).
ACCOUNT_COOLDOWN_SECS = _env_seconds("ACCOUNT_COOLDOWN_SECS", 0.0)
# How long acquire() may wait for a cooling account to free up, so a pacing
# misconfiguration can never pin a request thread indefinitely.
ACCOUNT_COOLDOWN_MAX_WAIT_SECS = _env_seconds("ACCOUNT_COOLDOWN_MAX_WAIT_SECS", 20.0)
# Floor for a mute the upstream did not timestamp: the account stays out of
# rotation at least this long instead of being retried on the next request.
ACCOUNT_MUTE_MIN_SECS = _env_seconds("ACCOUNT_MUTE_MIN_SECS", 300.0)
# Session keep-alive & credential auto-sync (v3.3.11).
#   DeepSeek mints a bearer token only at login (adapter.CREDENTIAL_CHECK_PATH),
#   so nothing here can renew a dead token; what this CAN do is keep the STORED
#   copy of the session current:
#   * after every successful upstream turn the rotating cookies in the adapter
#     jar (ds_session_id / aws-waf-token) are mirrored back into the account
#     record, so a cold start resumes the freshest session instead of a stale
#     one that may already be invalid;
#   * a stale account is re-verified in the background the next time it is
#     picked, so a rejected token is quarantined BEFORE a user request burns on
#     it (0 disables the re-check).
ACCOUNT_KEEPALIVE_SECS = _env_seconds("ACCOUNT_KEEPALIVE_SECS", 900.0)
# How often rotated cookies may be written to the store (KV/file) per account.
ACCOUNT_COOKIE_PERSIST_SECS = _env_seconds("ACCOUNT_COOKIE_PERSIST_SECS", 300.0)

# Credential refresh (DeepSeek tokens and their cookies are short-lived).
#   proactive: when a token is within TOKEN_REFRESH_AHEAD_SECS of its JWT "exp"
#   reactive:  immediately after the upstream rejects credentials (code 40003)
# Both run on a background thread so no request waits on the refresh, and both
# respect TOKEN_REFRESH_MIN_INTERVAL_SECS so a failing endpoint is not spammed.
TOKEN_REFRESH_ENABLED = os.environ.get(
    "TOKEN_REFRESH_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
try:
    TOKEN_REFRESH_AHEAD_SECS = max(0, int(os.environ.get("TOKEN_REFRESH_AHEAD_SECS", "900") or 0))
except ValueError:
    TOKEN_REFRESH_AHEAD_SECS = 900
try:
    TOKEN_REFRESH_MIN_INTERVAL_SECS = max(0, int(os.environ.get("TOKEN_REFRESH_MIN_INTERVAL_SECS", "300") or 0))
except ValueError:
    TOKEN_REFRESH_MIN_INTERVAL_SECS = 300


def _now() -> int:
    return int(time.time())


def _account_id(prefix: str = "acc") -> str:
    return f"{prefix}_{secrets.token_urlsafe(9)}"


def _credential_fingerprint(token: str, cookies: str) -> str:
    return hashlib.sha256(f"{token}\0{cookies}".encode()).hexdigest()


def _mask_secret(value: str, start: int = 6, end: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= start + end:
        return "*" * len(value)
    return f"{value[:start]}...{value[-end:]}"


def _cookie_names(cookies: str, limit: int = 6) -> str:
    names = []
    for part in cookies.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        names.append(part.split("=", 1)[0].strip())
    if not names:
        return _mask_secret(cookies, 12, 4)
    suffix = "" if len(names) <= limit else f", +{len(names) - limit} more"
    return ", ".join(names[:limit]) + suffix


@dataclass
class Account:
    """A single DeepSeek account with credentials and runtime state."""
    token: str
    cookies: str
    email: str = ""
    id: str = field(default_factory=_account_id)
    source: str = "file"       # persisted account
    proxy: str = ""             # per-account upstream proxy (optional)
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)
    state: str = "idle"        # idle | busy | error
    error_count: int = 0
    last_error: str = ""
    last_used: float = 0.0
    # Upstream mute expiry (epoch seconds). DeepSeek answers a muted account with
    # HTTP 200 + {"biz_code": 5, "biz_msg": "user is muted"}; retrying it does
    # not shorten the penalty, it only escalates it, so the account sits out
    # until this timestamp.
    muted_until: int = 0
    # Credential lifecycle (see credentials.py / adapter.refresh_credentials()).
    token_expires_at: int = 0      # from the JWT "exp" claim; 0 = unknown
    last_refresh_at: int = 0       # last attempt (success or failure)
    last_refresh_status: str = ""  # "" | "ok" | "failed"
    last_refresh_error: str = ""
    last_ok_at: int = 0            # last upstream turn that actually succeeded
    _adapter: Optional[DeepSeekAdapter] = field(default=None, repr=False)
    _cookies_saved_at: float = field(default=0.0, repr=False)
    _refreshing: bool = field(default=False, repr=False)

    def sync_token_expiry(self) -> int:
        """Recompute `token_expires_at` from the current token."""
        self.token_expires_at = _jwt_expires_at(self.token)
        return self.token_expires_at

    @property
    def adapter(self) -> DeepSeekAdapter:
        if self._adapter is None:
            self._adapter = DeepSeekAdapter(
                token=self.token,
                cookies=self.cookies,
                proxy=self.proxy or None,
            )
        return self._adapter

    @property
    def fingerprint(self) -> str:
        return _credential_fingerprint(self.token, self.cookies)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "source": self.source,
            "state": self.state,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_used": int(self.last_used),
            "muted_until": self.muted_until,
            "muted_seconds_remaining": max(0, self.muted_until - _now()),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_preview": _mask_secret(self.token),
            "cookies_preview": _cookie_names(self.cookies),
            "credential_fingerprint": self.fingerprint[:12],
            "token_expires_at": self.token_expires_at,
            "token_expires_in": (
                self.token_expires_at - _now() if self.token_expires_at else None
            ),
            "last_refresh_at": self.last_refresh_at,
            "last_refresh_status": self.last_refresh_status,
            "last_refresh_error": self.last_refresh_error,
            "last_ok_at": self.last_ok_at,
        }

    def to_store_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "token": self.token,
            "cookies": self.cookies,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_expires_at": self.token_expires_at,
            "last_refresh_at": self.last_refresh_at,
            "last_refresh_status": self.last_refresh_status,
            "last_refresh_error": self.last_refresh_error,
            "last_ok_at": self.last_ok_at,
            "muted_until": self.muted_until,
        }


class AccountPool:
    """Thread-safe pool of DeepSeek accounts with round-robin selection."""

    def __init__(self):
        self._lock = threading.Lock()
        self._accounts: list[Account] = []
        self._next_idx = 0
        configured_store = Path(os.environ.get("ACCOUNT_STORE_PATH", str(DEFAULT_STORE_PATH)))
        if not configured_store.is_absolute():
            configured_store = Path(__file__).resolve().parent / configured_store
        self._store_path = configured_store
        self._load_persisted_accounts()

    # ── Loading / persistence ────────────────────────────────────

    def _append_loaded(self, acct: Account):
        if any(a.fingerprint == acct.fingerprint for a in self._accounts):
            log.warning("skipping_duplicate_account", extra={"email": acct.email, "id": acct.id})
            return
        self._accounts.append(acct)

    def _load_persisted_accounts(self):
        path = self._store_path
        # Through credential_store so a Vercel KV store is read the same way a
        # local file is (KV first, file mirror as fallback).
        try:
            raw = credential_store.read_store(path)
        except Exception as e:
            log.warning("failed_to_load_account_store", extra={"path": str(path), "error": str(e)})
            return
        if raw is None:
            return
        try:
            data = json.loads(raw)
        except Exception as e:
            log.warning("failed_to_parse_account_store", extra={"path": str(path), "error": str(e)})
            return

        version = detect_store_version(data)
        if version == STORE_VERSION_ENCRYPTED and not crypto_is_enabled():
            log.error("encrypted_store_without_key", extra={"path": str(path)})
            return

        # If we're encrypted, rewrite the file as v2 in place (one-shot migration).
        if (version == STORE_VERSION_PLAIN and crypto_is_enabled()
                and credential_store.backend() == "file" and path.exists()):
            try:
                maybe_upgrade_store_file(path)
                log.info("upgraded_account_store_to_encrypted", extra={"path": str(path)})
            except Exception as e:
                log.warning("account_store_upgrade_failed", extra={"path": str(path), "error": str(e)})

        for item in data.get("accounts", []):
            # Decrypt in-memory copy if v2.
            item = decrypt_account_dict(item) if version == STORE_VERSION_ENCRYPTED else item
            token = str(item.get("token") or "").strip()
            cookies = str(item.get("cookies") or "").strip()
            if not token or not cookies:
                log.warning("skipping_account_missing_credentials", extra={"id": item.get("id")})
                continue
            created_at = int(item.get("created_at") or _now())
            updated_at = int(item.get("updated_at") or created_at)
            acct = Account(
                id=str(item.get("id") or _account_id()),
                email=str(item.get("email") or ""),
                token=token,
                cookies=cookies,
                source="file",
                created_at=created_at,
                updated_at=updated_at,
                token_expires_at=int(item.get("token_expires_at") or 0),
                last_refresh_at=int(item.get("last_refresh_at") or 0),
                last_refresh_status=str(item.get("last_refresh_status") or ""),
                last_refresh_error=str(item.get("last_refresh_error") or ""),
                last_ok_at=int(item.get("last_ok_at") or 0),
                muted_until=int(item.get("muted_until") or 0),
            )
            # The live token wins over a stored expiry: credentials may have
            # been edited by hand in the store.
            acct.token_expires_at = _jwt_expires_at(token) or acct.token_expires_at
            self._append_loaded(acct)

    def _ensure_store_dir(self):
        """Prepare the file store directory, if the file store is used.

        On serverless this is the platform temp dir: it already exists and
        refuses chmod with EPERM even for the process' own files, so the
        permission tightening is best-effort. Aborting here would fail the
        save before the authoritative backend is even reached.
        """
        if not credential_store.uses_file_store():
            return
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        credential_store.quiet_chmod(str(self._store_path.parent), 0o700)

    def _save_persisted_accounts_locked(self):
        self._ensure_store_dir()
        if crypto_is_enabled():
            accounts = [encrypt_account_dict(a.to_store_dict()) for a in self._accounts]
            data = {
                "version": STORE_VERSION_ENCRYPTED,
                "encryption": "fernet",
                "accounts": accounts,
            }
        else:
            data = {
                "version": STORE_VERSION_PLAIN,
                "accounts": [a.to_store_dict() for a in self._accounts],
            }
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        # KV is authoritative when configured; the file is written as a mirror.
        credential_store.write_store(self._store_path, payload)

    # ── CRUD ───────────────────────────────────────────────────

    def add(self, token: str, cookies: str, email: str = "") -> Account:
        token = (token or "").strip()
        cookies = (cookies or "").strip()
        email = (email or "").strip()
        if not token or not cookies:
            raise ValueError("Token and cookies are required")

        with self._lock:
            fp = _credential_fingerprint(token, cookies)
            if any(a.fingerprint == fp for a in self._accounts):
                raise ValueError("Account already exists")
            if not email:
                email = f"acc-{len(self._accounts) + 1}"
            now = _now()
            acct = Account(
                id=_account_id(),
                token=token,
                cookies=cookies,
                email=email,
                source="file",
                created_at=now,
                updated_at=now,
            )
            acct.sync_token_expiry()
            self._accounts.append(acct)
            self._save_persisted_accounts_locked()
            return acct

    def get_by_id(self, account_id: str) -> Optional[Account]:
        with self._lock:
            return next((a for a in self._accounts if a.id == account_id), None)

    def update(self, account_id: str, token: str | None = None,
               cookies: str | None = None, email: str | None = None) -> Account:
        with self._lock:
            acct = next((a for a in self._accounts if a.id == account_id), None)
            if acct is None:
                raise KeyError("Account not found")
            new_token = acct.token if token is None or token == "" else token.strip()
            new_cookies = acct.cookies if cookies is None or cookies == "" else cookies.strip()
            new_email = acct.email if email is None else email.strip()
            if not new_token or not new_cookies:
                raise ValueError("Token and cookies cannot be empty")

            new_fp = _credential_fingerprint(new_token, new_cookies)
            if any(a.id != account_id and a.fingerprint == new_fp for a in self._accounts):
                raise ValueError("Account already exists")

            credentials_changed = new_token != acct.token or new_cookies != acct.cookies
            acct.token = new_token
            acct.cookies = new_cookies
            if credentials_changed:
                # A manual edit supersedes the previous refresh state.
                acct.sync_token_expiry()
                acct.last_refresh_status = ""
                acct.last_refresh_error = ""
            acct.email = new_email or acct.email
            acct.updated_at = _now()
            if credentials_changed:
                acct._adapter = None
                acct.state = "idle"
                acct.error_count = 0
                acct.last_error = ""
                # New credentials = new account as far as upstream enforcement
                # is concerned, so a previous mute no longer applies.
                acct.muted_until = 0
            self._save_persisted_accounts_locked()
            return acct

    def remove_by_id(self, account_id: str) -> bool:
        with self._lock:
            for idx, acct in enumerate(self._accounts):
                if acct.id != account_id:
                    continue
                self._accounts.pop(idx)
                if self._next_idx >= len(self._accounts):
                    self._next_idx = 0
                self._save_persisted_accounts_locked()
                return True
            return False

    # Backward-compatible index removal for callers that have not migrated.
    def remove(self, index: int) -> bool:
        with self._lock:
            if not (0 <= index < len(self._accounts)):
                return False
            account_id = self._accounts[index].id
        return self.remove_by_id(account_id)

    def get_all(self) -> list[dict]:
        with self._lock:
            return [a.to_dict() for a in self._accounts]

    def count(self) -> int:
        with self._lock:
            return len(self._accounts)

    # ── Selection ──────────────────────────────────────────────

    def _ready_at_locked(self, acct: Account, now: float) -> float:
        """Epoch time from which this IDLE account may take new work (0 = now)."""
        if acct.state != "idle":
            return float("inf")
        if acct.muted_until:
            return float(acct.muted_until)
        if ACCOUNT_COOLDOWN_SECS and acct.last_used:
            return acct.last_used + ACCOUNT_COOLDOWN_SECS
        return 0.0

    def acquire(self, wait_timeout: float = -1) -> Optional[Account]:
        """Get the next idle account (round-robin), or None if none is ready.

        Ready means idle, not muted (muted_until already passed) and - when
        ACCOUNT_COOLDOWN_SECS is set - not used more recently than that gap.
        While some accounts are cooling, the call waits for the soonest slot up
        to wait_timeout (default ACCOUNT_COOLDOWN_MAX_WAIT_SECS) instead of
        failing the request instantly; all-busy still returns None at once, as
        before. With pacing off and no mutes this is the plain round-robin of
        the original implementation.
        """
        if wait_timeout is None:
            wait_timeout = -1
        deadline = time.time() + (
            ACCOUNT_COOLDOWN_MAX_WAIT_SECS if wait_timeout < 0 else wait_timeout
        )
        while True:
            soonest = None
            with self._lock:
                if not self._accounts:
                    return None
                now = time.time()
                n = len(self._accounts)
                for _ in range(n):
                    idx = self._next_idx % n
                    self._next_idx += 1
                    acct = self._accounts[idx]
                    if now < self._ready_at_locked(acct, now):
                        continue
                    # Proactive refresh: pick the moment we are already touching
                    # this account, so the next caller finds fresh credentials.
                    acct.state = "busy"
                    acct.last_used = time.time()
                    self._schedule_refresh_locked(acct)
                    self._maybe_verify_locked(acct)
                    return acct
                for other in self._accounts:
                    if other.state != "idle":
                        continue
                    ready_at = self._ready_at_locked(other, now)
                    if ready_at and (soonest is None or ready_at < soonest):
                        soonest = ready_at
            if soonest is None or time.time() >= deadline:
                # Every account busy (nothing cooling) -> fail fast, as before.
                return None
            time.sleep(min(0.25, max(0.01, soonest - time.time())))
    def acquire_by_id(self, account_id: str) -> Optional[Account]:
        """Acquire one specific idle account.

        Used for cached upstream conversations: a DeepSeek chat session is
        owned by the credentials that created it, so round-robin selection is
        unsafe when continuing that session.
        """
        if not account_id:
            return None
        with self._lock:
            candidates = self._accounts
            now = time.time()
            for acct in candidates:
                if acct.id == account_id and acct.state == "idle":
                    if now < self._ready_at_locked(acct, now):
                        # The account we would like to continue on is muted or
                        # still cooling: start a fresh session elsewhere rather
                        # than re-entering an already-penalised account.
                        return None
                    acct.state = "busy"
                    acct.last_used = time.time()
                    self._schedule_refresh_locked(acct)
                    self._maybe_verify_locked(acct)
                    return acct
            return None
    def release(self, acct: Account):
        """Mark account back to idle unless it was already marked as error."""
        with self._lock:
            if acct.state == "busy":
                acct.state = "idle"

    def mark_error(self, acct: Account, error_msg: str = ""):
        """Mark an account as `error`. If it has failed too many times, schedule
        a background health check to see if the credentials have come back
        (e.g. WAF challenge window expired, IP block lifted). The threshold
        is intentionally low so that transient blips heal automatically.
        """
        with self._lock:
            acct.state = "error"
            acct.error_count += 1
            acct.last_error = error_msg
            should_recover = acct.error_count >= _RECOVER_ERROR_THRESHOLD
            # A rejected token never heals by waiting, so on an auth failure we
            # refresh the credentials instead of scheduling a health check.
            auth_failure = is_auth_failure(None, error_msg)
            if auth_failure:
                self._schedule_refresh_locked(acct, force=True)

        if auth_failure:
            log.info("credential_refresh_requested",
                     extra={"id": acct.id, "reason": "auth_failure"})
            return

        if should_recover and not _RECOVER_FLAG_DISABLED:
            # Run the health check on a background thread so the request
            # thread isn't blocked. We don't wait for the result; acquire()
            # will see the new state next time.
            t = threading.Thread(
                target=self._background_recover,
                args=(acct,),
                daemon=True,
            )
            t.start()

    def mark_muted(self, acct: Account, mute_until=None, reason: str = "") -> int:
        """Put an account out of rotation for the upstream mute window.

        UserMutedError reaches the client as a 429; without this the account
        stayed idle and served the very next request, which repeats the
        behaviour DeepSeek is punishing. Honors the mute_until the upstream
        reports and applies ACCOUNT_MUTE_MIN_SECS as a floor when it sends
        none. Deliberately does NOT schedule a health check: a settings probe
        can pass while chat is still muted, so early re-admission would be
        wrong.
        """
        now = time.time()
        until = int(max(float(mute_until or 0), now + ACCOUNT_MUTE_MIN_SECS))
        with self._lock:
            acct.muted_until = until
            acct.updated_at = _now()
            if reason:
                acct.last_error = reason
            self._save_persisted_accounts_locked()
        log.warning("account_muted", extra={
            "id": acct.id, "email": acct.email, "muted_until": until,
            "seconds": until - int(now), "reason": reason,
        })
        return until

    def _background_recover(self, acct: Account) -> None:
        try:
            ok = self.check_health(acct)
            log.info(
                "background_recover_finished",
                extra={"id": acct.id, "ok": ok, "error": acct.last_error},
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "background_recover_crashed",
                extra={"id": acct.id, "error": str(e)},
            )

    def mark_recovered(self, acct: Account) -> None:
        """Public hook: reset error counters and return to idle."""
        with self._lock:
            acct.state = "idle"
            acct.error_count = 0
            acct.last_error = ""
            acct._adapter = None

    # ── Credential refresh ─────────────────────────────────────

    def _schedule_refresh_locked(self, acct: Account, force: bool = False) -> bool:
        """Start a background credential refresh when one is due. Caller must
        hold `self._lock`. Returns True when a worker was started.

        Never blocks: this runs on the request path (account selection) and on
        error reporting, where waiting for a network round trip is the wrong
        trade. The cooldown keeps a moved/broken refresh endpoint from being
        hammered once per request.
        """
        if not TOKEN_REFRESH_ENABLED or acct._refreshing:
            return False
        now = _now()
        if (acct.last_refresh_at and TOKEN_REFRESH_MIN_INTERVAL_SECS
                and now - acct.last_refresh_at < TOKEN_REFRESH_MIN_INTERVAL_SECS):
            return False
        if not force and not should_refresh(acct.token_expires_at, TOKEN_REFRESH_AHEAD_SECS, now):
            return False
        acct._refreshing = True
        threading.Thread(
            target=self._refresh_worker, args=(acct, "proactive" if not force else "reactive"),
            daemon=True,
        ).start()
        return True

    def _refresh_worker(self, acct: Account, reason: str) -> None:
        try:
            ok, msg = self._do_refresh(acct, reason)
            log.info(
                "credential_refresh_finished",
                extra={"id": acct.id, "reason": reason, "ok": ok, "error": "" if ok else msg},
            )
        except Exception as e:  # noqa: BLE001 - never kill the thread silently
            with self._lock:
                acct._refreshing = False
            log.warning("credential_refresh_crashed", extra={"id": acct.id, "error": str(e)})

    def _record_check_locked(self, acct: Account, check: dict) -> tuple[bool, str]:
        """Record a credential check on the account and persist it.

        A positive check is all this deployment can do: chat.deepseek.com mints
        its token at login and offers no endpoint to renew it (see
        adapter.CREDENTIAL_CHECK_PATH), so the panel's Refresh verifies rather
        than renews. Only a success may lift a quarantined account - a failed
        check must not mark a dead account healthy again.

        A DEFINITIVE rejection (40003 / auth wording) quarantines the account
        right here. Without that, a token dead in storage stayed `idle`, so
        acquire() kept handing it to chat requests and every one of them failed
        with a 502 until the next round-robin lap marked it. Refreshing the
        panel only reported the problem; now it also takes the account out of
        rotation, which is what the message always implied.
        """
        detail = check.get("detail") or check.get("msg") or "credential check failed"
        if not check.get("ok") and is_auth_failure(check.get("code"), check.get("msg") or ""):
            detail += " - copy a fresh token and cookies from the browser into this account"
        with self._lock:
            acct.last_refresh_at = _now()
            acct.last_refresh_status = "ok" if check.get("ok") else "failed"
            acct.last_refresh_error = "" if check.get("ok") else detail
            if check.get("ok"):
                if acct.state == "error":
                    acct.state = "idle"
                acct.error_count = 0
                acct.last_error = ""
            elif is_auth_failure(check.get("code"), check.get("msg") or ""):
                # Quarantine only: the counter is left alone so a repeated
                # check cannot ladder the account into the health-check path.
                acct.state = "error"
                acct.last_error = detail
            persist_error = ""
            try:
                self._save_persisted_accounts_locked()
            except Exception as e:  # noqa: BLE001 - recorded in memory regardless
                persist_error = str(e)
                log.error("credential_check_not_persisted",
                          extra={"id": acct.id, "error": persist_error,
                                 "backend": credential_store.backend()})
            acct._refreshing = False
        if check.get("ok"):
            msg = f"credentials valid (verified via {check.get('path')})"
        else:
            msg = f"credentials not confirmed: {detail} (via {check.get('path')})"
        if persist_error:
            msg += f" (warning: not persisted: {persist_error})"
        return bool(check.get("ok")), msg

    def _maybe_verify_locked(self, acct: Account) -> None:
        """Re-verify a stale account off the request path (keep-alive).

        Called from acquire()/acquire_by_id() right after the account has been
        claimed, so the extra GET rides along with a request that was going to
        happen anyway. A rejected token is then quarantined in the background
        instead of surfacing as a 502 for whoever draws that account next.
        """
        if ACCOUNT_KEEPALIVE_SECS <= 0 or acct._refreshing:
            return
        now = _now()
        if acct.last_refresh_at and now - acct.last_refresh_at < ACCOUNT_KEEPALIVE_SECS:
            return
        acct._refreshing = True
        threading.Thread(
            target=self._refresh_worker, args=(acct, "keepalive"), daemon=True
        ).start()

    def record_upstream_ok(self, acct: Account) -> None:
        """Mirror a successful upstream turn back into the account record.

        The web session rotates its cookies continuously (`ds_session_id`,
        `aws-waf-token`); the adapter's cookie jar holds the freshest copy, so
        it is written back into the account and (throttled) into storage. This
        is the part of "auto update" that is actually possible: the stored
        session follows the live jar, while the bearer token itself is only
        ever minted by a browser login.
        """
        try:
            acct.adapter.sync_cookies()
            blob = acct.adapter.cookie_blob()
        except Exception as e:  # noqa: BLE001 - never fail a served request
            log.warning("cookie_sync_failed", extra={"id": acct.id, "error": str(e)})
            return
        with self._lock:
            acct.last_ok_at = _now()
            if not blob or blob == acct.cookies:
                return
            acct.cookies = blob
            now = time.time()
            if now - acct._cookies_saved_at < ACCOUNT_COOKIE_PERSIST_SECS:
                return
            acct._cookies_saved_at = now
            acct.updated_at = _now()
            # Storage writes (KV round-trip) never block the request thread.
            threading.Thread(
                target=self._persist_cookies_async, args=(acct,), daemon=True
            ).start()

    def _persist_cookies_async(self, acct: Account) -> None:
        with self._lock:
            try:
                self._save_persisted_accounts_locked()
            except Exception as e:  # noqa: BLE001 - the in-memory copy is fresh
                log.warning("cookie_sync_not_persisted",
                            extra={"id": acct.id, "error": str(e)})
                return
        log.info("cookies_updated", extra={"id": acct.id})


    def _do_refresh(self, acct: Account, reason: str) -> tuple[bool, str]:
        """Verify an account's credentials, renewing them only where the upstream
        offers an endpoint for it. Caller already claimed `_refreshing`.
        """
        check = acct.adapter.check_credentials()
        if not check["ok"]:
            log.warning("credential_check_failed",
                        extra={"id": acct.id, "reason": reason,
                               "code": check.get("code"),
                               "error": check.get("detail") or check.get("msg")})
            return self._record_check_locked(acct, check)
        if not adapter_module.REFRESH_PATHS:
            # Nothing to renew with; the check above is the whole answer.
            return self._record_check_locked(acct, check)
        try:
            info = acct.adapter.refresh_credentials()
        except Exception as e:  # includes UpstreamAuthError / CredentialRefreshError
            with self._lock:
                acct.last_refresh_at = _now()
                acct.last_refresh_status = "failed"
                acct.last_refresh_error = str(e)
                acct._refreshing = False
            log.warning("credential_refresh_failed",
                        extra={"id": acct.id, "reason": reason, "error": str(e)})
            return False, str(e)

        with self._lock:
            # Keep the existing adapter: refresh_credentials() swapped the new
            # bearer token into it, so the warm cookie jar and PoW solver stay
            # useful and in-flight requests are not disturbed.
            acct.token = info["token"]
            acct.cookies = info["cookies"]
            acct.token_expires_at = int(info.get("expires_at") or 0)
            acct.last_refresh_at = _now()
            acct.last_refresh_status = "ok"
            acct.last_refresh_error = ""
            acct.updated_at = _now()
            if acct.state == "error":
                acct.state = "idle"
            acct.error_count = 0
            acct.last_error = ""
            persist_error = ""
            try:
                self._save_persisted_accounts_locked()
            except Exception as e:  # noqa: BLE001 - refreshed in memory regardless
                persist_error = str(e)
                log.error("credential_refresh_not_persisted",
                          extra={"id": acct.id, "error": persist_error,
                                 "backend": credential_store.backend()})
            acct._refreshing = False

        log.info("credential_refreshed",
                 extra={"id": acct.id, "reason": reason, "path": info.get("path"),
                        "token_expires_at": acct.token_expires_at})
        msg = f"credentials refreshed via {info.get('path')}"
        if persist_error:
            msg += f" (warning: not persisted: {persist_error})"
        return True, msg

    def refresh_account(self, acct: Account) -> tuple[bool, str]:
        """Refresh one account now (synchronous). Concurrent calls collapse."""
        with self._lock:
            if acct._refreshing:
                return False, "credential refresh already in progress"
            acct._refreshing = True
        return self._do_refresh(acct, "manual")

    def refresh_by_id(self, account_id: str) -> tuple[bool, str]:
        """Manual entry point (admin API / panel button)."""
        with self._lock:
            acct = next((a for a in self._accounts if a.id == account_id), None)
        if acct is None:
            return False, "Account not found"
        return self.refresh_account(acct)

    # ── Health check / relogin ─────────────────────────────────

    def check_health(self, acct: Account) -> bool:
        """Test if account credentials are valid by creating a session."""
        try:
            adapter = DeepSeekAdapter(token=acct.token, cookies=acct.cookies)
            adapter.create_session()
            return True
        except Exception as e:
            with self._lock:
                acct.state = "error"
                acct.error_count += 1
                acct.last_error = str(e)
            return False

    def relogin_by_id(self, account_id: str) -> tuple[bool, str]:
        """Attempt to heal an error account by testing credentials."""
        with self._lock:
            acct = next((a for a in self._accounts if a.id == account_id), None)
            if acct is None:
                return False, "Account not found"
            if acct.state != "error":
                return False, f"Account is {acct.state}, not error"

        ok = self.check_health(acct)
        if ok:
            self.mark_recovered(acct)
            return True, "ok"
        return False, acct.last_error or "unknown error"

    def relogin(self, index: int) -> tuple[bool, str]:
        with self._lock:
            if not (0 <= index < len(self._accounts)):
                return False, "Account not found"
            account_id = self._accounts[index].id
        return self.relogin_by_id(account_id)

    # ── Stats ──────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            accounts = self._accounts
            total = len(accounts)
            idle = sum(1 for a in accounts if a.state == "idle")
            busy = sum(1 for a in accounts if a.state == "busy")
            error = sum(1 for a in accounts if a.state == "error")
            return {
                "total": total,
                "idle": idle,
                "busy": busy,
                "error": error,
            }
