"""Persistent client API-key management.

Authentication checks the SHA-256 hash. Since v3.3.18 the full key material
is stored alongside (Fernet-encrypted when DEEPSEEK_ENCRYPTION_KEY is set,
plain with a marker otherwise) so the WebUI can offer a copy button at any
time, not just once at creation; the material leaves the server only through
the admin-authenticated reveal endpoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import credential_store
import crypto
from logger import get_logger

log = get_logger("api_key_store")


_DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "api_keys.sqlite3"
_STORE: "ApiKeyStore | None" = None
_STORE_LOCK = threading.Lock()


def _legacy_keys_from_env() -> list[str]:
    """Read old env-based keys for a one-time import into the key store."""
    candidates = [
        part.strip()
        for part in os.environ.get("API_KEYS", "").split(",")
        if part.strip()
    ]
    single = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if single:
        candidates.append(single)

    keys: list[str] = []
    seen: set[str] = set()
    for key in candidates:
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _ensure_writable(path: Path) -> Path:
    """Keep the key store on a filesystem the process can actually write to.

    `server.py` builds this store at import time, so an unwritable path is not a
    degraded feature but a dead process: on serverless hosts (Vercel) the project
    directory is read-only, `data/` is not even part of the bundle, and `mkdir`
    there raises before a single request is served - every route then answers 500.
    Falling back to the temp directory keeps the instance alive; the warning says
    where the keys actually live and that they only last as long as the instance.
    """
    probe = path.parent
    try:
        probe.mkdir(parents=True, exist_ok=True)
        marker = probe / ".api-key-store-write-test"
        marker.touch()
        marker.unlink()
        return path
    except OSError as exc:
        fallback = Path(tempfile.gettempdir()) / path.name
        log.warning("api_key_store_path_unwritable", extra={
            "path": str(path), "fallback": str(fallback), "error": str(exc)})
        return fallback

# ── KV persistence (serverless-safe, v3.3.17) ────────────────────────
# The sqlite file lives in /tmp on Vercel, so it only ever survived as long
# as its instance: after every cold start or deploy every WebUI-issued key
# was gone and clients holding that key got intermittent 401s (requests
# landing on a fresh instance failed while a warm one still had the row).
# When the same Upstash REST pair that backs the account store is present,
# KV is authoritative: pull at instance start, push after every mutation,
# and re-pull every API_KEY_KV_SYNC_SECS (default 60, 0 disables the timer)
# so a key created or revoked on another instance becomes visible without
# a restart. sqlite stays as the local mirror that answers every verify().
# The KV key name is KV_API_KEYS_KEY.

_KV_PAYLOAD_VERSION = 1


def _kv_config():
    return credential_store._kv_config()


def _kv_command(cfg, args):
    return credential_store._command(cfg, args)


def _kv_key() -> str:
    return (os.environ.get("KV_API_KEYS_KEY", "").strip() or "ds2api:api_keys.json")


def kv_status() -> dict:
    """Shape for /admin/api/env: is the KV backend active and under which key."""
    cfg = _kv_config()
    return {"enabled": cfg is not None, "key": _kv_key() if cfg else None}


class ApiKeyStore:
    """SQLite-backed API key metadata and hash store (optional KV mirror)."""

    def __init__(self, path: Path, legacy_keys: list[str] | None = None):
        self.path = Path(path)
        self._legacy_keys = legacy_keys or []
        self._kv_timer: threading.Timer | None = None
        self._initialize()
        self._schedule_kv_refresh()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @staticmethod
    def _key_hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @classmethod
    def _prefix(cls, key: str) -> str:
        # Never expose an entire short legacy key in the UI.
        if len(key) >= 12:
            return key[:8]
        return f"legacy-{cls._key_hash(key)[:8]}"

    @staticmethod
    def _protect_material(key: str) -> str:
        """At-rest form of the full key. Fernet when configured; otherwise a
        plain marker - the material is only ever returned by the admin-only
        reveal endpoint, the same trust level as the admin password."""
        try:
            if crypto.is_enabled():
                return "enc:" + crypto.encrypt_str(key)
        except Exception as exc:
            log.warning("api_key_material_encrypt_failed", extra={"error": str(exc)})
        return "raw:" + key

    @staticmethod
    def _reveal_material(material: str | None) -> str | None:
        if not material:
            return None
        if material.startswith("enc:"):
            try:
                return crypto.decrypt_str(material[4:])
            except Exception as exc:
                # Wrong or rotated DEEPSEEK_ENCRYPTION_KEY: the hash still
                # authenticates the key, only the copy feature degrades.
                log.warning("api_key_material_decrypt_failed", extra={"error": str(exc)})
                return None
        if material.startswith("raw:"):
            return material[4:]
        return material

    @classmethod
    def _insert_key(
        cls,
        conn: sqlite3.Connection,
        name: str,
        key: str,
        created_at: int,
    ) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO api_keys
               (id, name, key_hash, key_prefix, created_at, key_material)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                secrets.token_hex(8),
                name,
                cls._key_hash(key),
                cls._prefix(key),
                created_at,
                cls._protect_material(key),
            ),
        )

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    key_material TEXT
                )"""
            )
            # Pre-v3.3.18 databases lack the material column; old rows stay
            # NULL (not revealable) - everything created from now on is.
            try:
                conn.execute("ALTER TABLE api_keys ADD COLUMN key_material TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute(
                """CREATE TABLE IF NOT EXISTS api_key_store_meta (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )
            conn.execute("BEGIN IMMEDIATE")
            imported = conn.execute(
                "SELECT value FROM api_key_store_meta WHERE name = ?",
                ("legacy_env_imported",),
            ).fetchone()
            if imported is None:
                now = int(time.time())
                for index, key in enumerate(self._legacy_keys, start=1):
                    self._insert_key(conn, f"Imported key {index}", key, now)
                conn.execute(
                    "INSERT INTO api_key_store_meta (name, value) VALUES (?, ?)",
                    ("legacy_env_imported", "1"),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

        self._kv_sync_on_init()

    # ── KV mirror helpers ─────────────────────────────────────────────

    def _snapshot(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, key_hash, key_prefix, created_at, key_material FROM api_keys"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _replace_all(self, records: list[dict]) -> int:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM api_keys")
            for rec in records:
                conn.execute(
                    """INSERT OR REPLACE INTO api_keys
                       (id, name, key_hash, key_prefix, created_at, key_material)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (rec["id"], rec["name"], rec["key_hash"],
                     rec.get("key_prefix") or rec["key_hash"][:16], int(rec.get("created_at") or 0),
                     rec.get("key_material")),
                )
            conn.commit()
            return len(records)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _kv_pull(self) -> list[dict] | None:
        """Records held in KV, or None when KV holds nothing usable
        (not configured, key absent, network error, malformed payload)."""
        cfg = _kv_config()
        if cfg is None:
            return None
        try:
            raw = _kv_command(cfg, ["get", _kv_key()])
        except Exception as exc:
            log.warning("api_keys_kv_read_failed", extra={"error": str(exc)})
            return None
        if raw is None:
            return None
        payload = credential_store._decode(str(raw))
        if not payload:
            return None
        try:
            obj = json.loads(payload)
            records = obj.get("keys") if isinstance(obj, dict) else None
        except ValueError:
            log.warning("api_keys_kv_bad_payload")
            return None
        if not isinstance(records, list):
            return None
        return [r for r in records
                if isinstance(r, dict) and r.get("id") and r.get("key_hash")]

    def _kv_push(self) -> None:
        """Write the local snapshot to KV; a failure is logged, not fatal
        (this instance keeps working from sqlite, durability across
        instances is what degrades)."""
        cfg = _kv_config()
        if cfg is None:
            return
        payload = json.dumps({"v": _KV_PAYLOAD_VERSION, "keys": self._snapshot()})
        try:
            _kv_command(cfg, ["set", _kv_key(), credential_store._encode(payload)])
        except Exception as exc:
            log.warning("api_keys_kv_write_failed", extra={"error": str(exc)})

    def _kv_sync_on_init(self) -> None:
        if _kv_config() is None:
            return
        records = self._kv_pull()
        if records is None:
            # KV holds nothing yet: seed it with the local content
            # (e.g. the one-time legacy env import) instead of wiping it.
            self._kv_push()
            log.info("api_keys_kv_seeded")
            return
        count = self._replace_all(records)
        log.info("api_keys_kv_hydrated", extra={"count": count})

    def _kv_refresh_secs(self) -> int:
        try:
            return int(os.environ.get("API_KEY_KV_SYNC_SECS") or 60)
        except ValueError:
            return 60

    def _schedule_kv_refresh(self) -> None:
        if _kv_config() is None or self._kv_refresh_secs() <= 0:
            return
        timer = threading.Timer(self._kv_refresh_secs(), self._kv_refresh_tick)
        timer.daemon = True
        timer.start()
        self._kv_timer = timer

    def _kv_refresh_tick(self) -> None:
        try:
            records = self._kv_pull()
            if records is not None and records != self._snapshot():
                self._replace_all(records)
        except Exception as exc:
            log.warning("api_keys_kv_refresh_failed", extra={"error": str(exc)})
        finally:
            try:
                self._schedule_kv_refresh()
            except Exception:
                pass

    @staticmethod
    def _public_record(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "prefix": row["key_prefix"],
            "created_at": row["created_at"],
            "revealable": ("key_material" in row.keys() and bool(row["key_material"])),
        }

    def list_keys(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, key_prefix, created_at, key_material FROM api_keys "
                "ORDER BY created_at DESC, id DESC"
            ).fetchall()
            return [self._public_record(row) for row in rows]
        finally:
            conn.close()

    def has_keys(self) -> bool:
        conn = self._connect()
        try:
            return conn.execute("SELECT 1 FROM api_keys LIMIT 1").fetchone() is not None
        finally:
            conn.close()

    def verify(self, candidate: str) -> bool:
        if not candidate:
            return False
        digest = self._key_hash(candidate)
        conn = self._connect()
        try:
            rows = conn.execute("SELECT key_hash FROM api_keys").fetchall()
            matched = False
            for row in rows:
                matched = secrets.compare_digest(digest, row["key_hash"]) or matched
            return matched
        finally:
            conn.close()

    def create_key(self, name: str) -> dict:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Key name is required")
        if len(normalized_name) > 64:
            raise ValueError("Key name must be 64 characters or fewer")

        key = f"sk-{secrets.token_urlsafe(32)}"
        created_at = int(time.time())
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO api_keys
                   (id, name, key_hash, key_prefix, created_at, key_material)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    secrets.token_hex(8),
                    normalized_name,
                    self._key_hash(key),
                    self._prefix(key),
                    created_at,
                    self._protect_material(key),
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id, name, key_prefix, created_at, key_material FROM api_keys WHERE key_hash = ?",
                (self._key_hash(key),),
            ).fetchone()
            result = {"key": key, "api_key": self._public_record(row)}
            self._kv_push()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reveal_key(self, key_id: str) -> str | None:
        """Full key for the WebUI copy button. None when the row predates
        key-material storage or decryption fails (rotated encryption key)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT key_material FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
            return self._reveal_material(row["key_material"]) if row else None
        finally:
            conn.close()

    def revoke_key(self, key_id: str) -> bool:
        conn = self._connect()
        try:
            cursor = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
            conn.commit()
            removed = cursor.rowcount > 0
            if removed:
                self._kv_push()
            return removed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_api_key_store() -> ApiKeyStore:
    """Return the process-wide store, importing legacy env keys only once."""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                configured_path = os.environ.get("API_KEY_STORE_PATH", "").strip()
                path = Path(configured_path) if configured_path else _DEFAULT_PATH
                if not path.is_absolute():
                    path = Path(__file__).resolve().parent / path
                _STORE = ApiKeyStore(_ensure_writable(path), _legacy_keys_from_env())
    return _STORE
