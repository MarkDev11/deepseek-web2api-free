"""Where the account store lives: a local JSON file, or Vercel KV.

Backend is chosen by environment: when both `KV_REST_API_URL` and
`KV_REST_API_TOKEN` are present (Vercel KV injects them into the runtime),
KV is authoritative and uses the Upstash-compatible REST protocol. A JSON file
sits alongside it as a mirror, which

* keeps local development working with no extra services, and
* gives a serverless instance something to fall back to if KV is briefly
  unreachable.

The mirror is optional (`ACCOUNT_STORE_MIRROR=0`): on serverless the only
writable directory is the platform's temp dir, which never survives a cold
start, so there the mirror is an extra fsync per save that is never read back.

This exists because a Vercel container's filesystem is ephemeral: /tmp dies
with the instance, so a refreshed token written there disappears on the next
cold start and the account goes back to its expired credentials.

The stored value is the same JSON document the file backend writes, base64
encoded so it survives the REST call as a single opaque JSON string.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path

import httpx

from logger import get_logger

log = get_logger("credential_store")

DEFAULT_KV_KEY = "ds2api:accounts.json"
_KV_TIMEOUT = float(os.environ.get("KV_REST_TIMEOUT_SECS", "8"))


class CredentialStoreError(RuntimeError):
    """Raised when the authoritative store backend cannot be written."""


def _first_env(*names: str) -> str:
    """First non-empty value among the given names. Upstash's own console
    calls the pair UPSTASH_REDIS_REST_*, Vercel KV called it KV_REST_API_*;
    both are accepted so either can be pasted as is."""
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _kv_config() -> tuple | None:
    url = _first_env("KV_REST_API_URL", "UPSTASH_REDIS_REST_URL").rstrip("/")
    token = _first_env("KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_TOKEN")
    if not url or not token:
        return None
    return (url, token)


def kv_key() -> str:
    return (os.environ.get("KV_ACCOUNTS_KEY") or DEFAULT_KV_KEY).strip() or DEFAULT_KV_KEY


def backend() -> str:
    """"vercel_kv" when KV is configured, otherwise "file"."""
    return "vercel_kv" if _kv_config() else "file"


def _mirror_enabled() -> bool:
    """Whether the JSON file mirrors the KV document."""
    return (os.environ.get("ACCOUNT_STORE_MIRROR", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def uses_file_store() -> bool:
    """Whether `path` participates in reads/writes.

    True when the file is the store (no KV) or when mirroring is enabled;
    False means the store is KV-only and the local path is never touched.
    """
    return _kv_config() is None or _mirror_enabled()


def _command(cfg: tuple, args: list) -> object:
    """Run one Upstash-REST command; returns the parsed `result`."""
    url, token = cfg
    cmd = args[0]
    # One JSON array POSTed to the root path. The "arguments separated by
    # newlines in the body" form that circulates in examples delivers only one
    # argument in practice: a real database answers SET key value with
    # "ERR wrong number of arguments", which would fail every write.
    resp = httpx.post(
        f"{url.rstrip('/')}/",
        json=list(args),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=_KV_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise CredentialStoreError(f"kv {cmd} failed: {payload['error']}")
    return payload.get("result") if isinstance(payload, dict) else payload


def _encode(payload: str) -> str:
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _decode(value: str) -> str | None:
    """Undo `_encode`; tolerate a plain (unencoded) JSON value too."""
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return value if value.lstrip().startswith("{") else None
    try:
        obj = json.loads(decoded)
    except ValueError:
        return value if value.lstrip().startswith("{") else None
    return decoded if isinstance(obj, dict) else (value if value.lstrip().startswith("{") else None)


def _read_file(path: Path) -> str | None:
    try:
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")
    except Exception as e:  # pragma: no cover - filesystem dependent
        log.warning("account_store_file_read_failed", extra={"path": str(path), "error": str(e)})
        return None


def read_store(path: Path) -> str | None:
    """Return the stored JSON document, or None when nothing is stored yet."""
    cfg = _kv_config()
    if cfg:
        try:
            result = _command(cfg, ["get", kv_key()])
        except Exception as e:
            log.warning("kv_read_failed_falling_back_to_file",
                        extra={"key": kv_key(), "error": str(e)})
        else:
            if isinstance(result, str) and result:
                decoded = _decode(result)
                if decoded:
                    return decoded
                log.warning("kv_value_unparseable", extra={"key": kv_key()})
            # Empty KV: fall through to the file mirror so a store created
            # before KV was configured is not silently lost.
        if not _mirror_enabled():
            return None
    return _read_file(path)


def write_store(path: Path, payload: str) -> None:
    """Persist the JSON document. KV is authoritative; the file is a mirror."""
    cfg = _kv_config()
    error: Exception | None = None
    if cfg:
        try:
            result = _command(cfg, ["set", kv_key(), _encode(payload)])
            if not (isinstance(result, str) and result.upper() == "OK"):
                # Anything other than OK (None, an error string, a rejected
                # write) must not look like success: the caller needs to know
                # the refreshed credentials did not reach the authoritative store.
                raise CredentialStoreError(f"kv set returned {result!r}")
        except Exception as e:
            error = e
    if not cfg or _mirror_enabled():
        try:
            _write_file(path, payload)
        except Exception as e:
            if cfg and error is None:
                # KV took it; the mirror is only a convenience.
                log.warning("account_store_mirror_write_failed",
                            extra={"path": str(path), "error": str(e)})
            elif not cfg:
                raise CredentialStoreError(f"file store write failed: {e}") from e
    if error is not None:
        raise CredentialStoreError(f"kv write failed: {error}") from error


def quiet_chmod(target: str, mode: int) -> None:
    """Tighten permissions when the filesystem allows it.

    Serverless filesystems may reject chmod on their temp directory with
    EPERM ("Operation not permitted", seen on Vercel's /tmp) even for a file
    the process created itself. Losing the owner-only mode bits is far
    cheaper than failing the save, so the error is swallowed here instead of
    being allowed to abort the write.
    """
    try:
        os.chmod(target, mode)
    except OSError:
        pass


def _write_file(path: Path, payload: str) -> None:
    """Atomic replace (tmp + os.replace) with owner-only permissions."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    quiet_chmod(str(path.parent), 0o700)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        quiet_chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        quiet_chmod(str(path), 0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def describe() -> dict:
    """Small, secret-free summary for the admin panel."""
    return {
        "backend": backend(),
        "key": kv_key() if _kv_config() else "",
        # "store" = the file is the store, "mirror" = copy of KV, "off" = KV only.
        "file_store": "store" if not _kv_config()
        else ("mirror" if _mirror_enabled() else "off"),
    }
