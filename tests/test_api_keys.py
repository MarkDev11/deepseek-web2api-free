"""Tests for persistent API-key storage, management routes, and client auth."""
from __future__ import annotations

import secrets
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import admin
import server
from api_key_store import ApiKeyStore, _legacy_keys_from_env


@pytest.fixture
def api_key_store(tmp_path):
    return ApiKeyStore(tmp_path / "api_keys.sqlite3")


@pytest.fixture
def admin_client(monkeypatch, tmp_path):
    store = ApiKeyStore(tmp_path / "admin_api_keys.sqlite3")
    monkeypatch.setattr(admin, "get_api_key_store", lambda: store)

    token = secrets.token_urlsafe(24)
    admin._tokens.add(token)
    app = FastAPI()
    app.include_router(admin.router)
    try:
        with TestClient(app) as client:
            yield client, store, token
    finally:
        admin._tokens.discard(token)


@pytest.fixture
def server_client(monkeypatch, tmp_path):
    store = ApiKeyStore(tmp_path / "server_api_keys.sqlite3")
    monkeypatch.setattr(server, "API_KEY_STORE", store)
    with TestClient(server.app) as client:
        yield client, store


def test_legacy_environment_keys_are_deduplicated(monkeypatch):
    monkeypatch.setenv("API_KEYS", "first-key, second-key, first-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "second-key")

    assert _legacy_keys_from_env() == ["first-key", "second-key"]


def test_legacy_keys_import_only_once_and_store_hashes(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    legacy_keys = ["old-client-key-one", "old-client-key-two"]

    store = ApiKeyStore(db_path, legacy_keys)
    assert all(store.verify(key) for key in legacy_keys)
    assert len(store.list_keys()) == 2

    # Restarting with different environment values must not silently add or
    # replace credentials after the one-time migration marker is set.
    reopened = ApiKeyStore(db_path, ["late-environment-key"])
    assert not reopened.verify("late-environment-key")
    assert all(reopened.verify(key) for key in legacy_keys)

    rows = sqlite3.connect(db_path).execute(
        "SELECT name FROM pragma_table_info('api_keys') ORDER BY cid"
    ).fetchall()
    assert [row[0] for row in rows] == [
        "id", "name", "key_hash", "key_prefix", "created_at", "key_material"
    ]
    # v3.3.18: full key material is deliberately stored (marker-prefixed) so
    # the WebUI copy button keeps working; the reveal path returns it verbatim.
    for rec in store.list_keys():
        assert rec["revealable"] is True
    assert sorted(
        store.reveal_key(rec["id"]) for rec in store.list_keys()
    ) == sorted(legacy_keys)


def test_generated_key_is_verifiable_and_only_returned_at_creation(api_key_store):
    result = api_key_store.create_key("  local client  ")
    key = result["key"]
    record = result["api_key"]

    assert key.startswith("sk-")
    assert len(key) > 40
    assert record["name"] == "local client"
    assert set(record) == {"id", "name", "prefix", "created_at", "revealable"}
    assert record["prefix"] == key[:8]
    assert api_key_store.verify(key)
    assert not api_key_store.verify("sk-invalid")
    assert key not in repr(api_key_store.list_keys())


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
def test_create_rejects_empty_or_overlong_names(api_key_store, name):
    with pytest.raises(ValueError):
        api_key_store.create_key(name)


def test_multiple_keys_can_be_revoked_independently(api_key_store):
    first = api_key_store.create_key("first")
    second = api_key_store.create_key("second")

    assert api_key_store.has_keys()
    assert api_key_store.verify(first["key"])
    assert api_key_store.verify(second["key"])
    assert api_key_store.revoke_key(first["api_key"]["id"])
    assert not api_key_store.verify(first["key"])
    assert api_key_store.verify(second["key"])
    assert not api_key_store.revoke_key("missing-id")


def test_admin_api_key_routes_require_admin_auth(admin_client):
    client, _, _ = admin_client

    assert client.get("/admin/api/api-keys").status_code == 401
    assert client.post(
        "/admin/api/api-keys", json={"name": "unauthorized"}
    ).status_code == 401
    assert client.delete("/admin/api/api-keys/missing").status_code == 401


def test_admin_api_key_routes_create_list_and_revoke(admin_client):
    client, store, token = admin_client
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/admin/api/api-keys", json={"name": "desktop app"}, headers=headers
    )
    assert created.status_code == 200
    assert created.headers["cache-control"] == "no-store"
    body = created.json()
    key = body["key"]
    record = body["api_key"]
    assert store.verify(key)

    listed = client.get("/admin/api/api-keys", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == {"keys": [record]}
    assert key not in listed.text

    invalid_name = client.post(
        "/admin/api/api-keys", json={"name": " "}, headers=headers
    )
    assert invalid_name.status_code == 400

    revoked = client.delete(
        f"/admin/api/api-keys/{record['id']}", headers=headers
    )
    assert revoked.status_code == 200
    assert not store.verify(key)
    assert client.get("/admin/api/api-keys", headers=headers).json() == {"keys": []}
    assert client.delete(
        f"/admin/api/api-keys/{record['id']}", headers=headers
    ).status_code == 404


def test_generated_keys_authenticate_v1_and_revocation_takes_effect(server_client):
    client, store = server_client

    assert client.get("/v1/models").status_code == 503
    key_record = store.create_key("auth integration")
    key = key_record["key"]

    bearer = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {key}"}
    )
    assert bearer.status_code == 200

    x_api_key = client.get("/v1/models", headers={"x-api-key": key})
    assert x_api_key.status_code == 200
    assert client.get(
        "/v1/models", headers={"Authorization": "Bearer wrong-key"}
    ).status_code == 401

    assert store.revoke_key(key_record["api_key"]["id"])
    assert client.get(
        "/v1/models", headers={"Authorization": f"Bearer {key}"}
    ).status_code == 503



class TestStorePathFallback:
    """`server.py` opens the key store while importing, so an unwritable path
    cannot be allowed to raise: on a read-only deployment filesystem that single
    mkdir aborts the import and every route answers 500.
    """

    def test_unwritable_path_falls_back_to_the_temp_directory(self, monkeypatch, tmp_path):
        import api_key_store

        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(api_key_store, "_STORE", None)
        monkeypatch.setenv(
            "API_KEY_STORE_PATH", str(blocker / "nested" / "api_keys.sqlite3"))

        store = api_key_store.get_api_key_store()

        assert store.path.parent == Path(tempfile.gettempdir())
        assert store.list_keys() == []  # usable, just not durable

    def test_writable_path_is_used_as_configured(self, monkeypatch, tmp_path):
        import api_key_store

        target = tmp_path / "keys" / "api_keys.sqlite3"
        monkeypatch.setattr(api_key_store, "_STORE", None)
        monkeypatch.setenv("API_KEY_STORE_PATH", str(target))

        store = api_key_store.get_api_key_store()

        assert store.path == target
        assert target.exists()

    def test_fallback_creates_the_directory_it_owns(self, monkeypatch, tmp_path):
        import api_key_store

        target = tmp_path / "deep" / "deeper" / "api_keys.sqlite3"
        monkeypatch.setattr(api_key_store, "_STORE", None)
        monkeypatch.setenv("API_KEY_STORE_PATH", str(target))

        store = api_key_store.get_api_key_store()

        assert store.path == target and target.exists()


class TestRevealKey:
    """v3.3.18: the full key material is stored (encrypted when possible)
    so the WebUI can offer a permanent copy button."""

    def test_created_key_is_revealable(self, api_key_store):
        created = api_key_store.create_key("copy-me")
        record = api_key_store.list_keys()[0]
        assert record["revealable"] is True
        assert api_key_store.reveal_key(record["id"]) == created["key"]

    def test_unknown_id_reveals_nothing(self, api_key_store):
        assert api_key_store.reveal_key("missing") is None

    def test_fernet_round_trip_hides_plaintext_on_disk(self, tmp_path, monkeypatch):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("DEEPSEEK_ENCRYPTION_KEY", Fernet.generate_key().decode())
        db = tmp_path / "enc.sqlite3"
        store = ApiKeyStore(db)
        created = store.create_key("encrypted")

        record = store.list_keys()[0]
        assert store.reveal_key(record["id"]) == created["key"]

        material = sqlite3.connect(db).execute(
            "SELECT key_material FROM api_keys"
        ).fetchone()[0]
        assert material.startswith("enc:")
        assert created["key"] not in material

    def test_rotated_encryption_key_degrades_to_none(self, tmp_path, monkeypatch):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("DEEPSEEK_ENCRYPTION_KEY", Fernet.generate_key().decode())
        store = ApiKeyStore(tmp_path / "rot.sqlite3")
        created = store.create_key("rotated")
        monkeypatch.setenv("DEEPSEEK_ENCRYPTION_KEY", Fernet.generate_key().decode())

        record = store.list_keys()[0]
        assert store.verify(created["key"]) is True  # auth unaffected
        assert store.reveal_key(record["id"]) is None  # only copy degrades

    def test_pre_v3318_schema_migrates_and_stays_unrevealable(self, tmp_path):
        db = tmp_path / "old.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        conn.execute(
            "INSERT INTO api_keys VALUES ('old-1', 'veteran', 'hash-1', 'sk-old', 1)"
        )
        conn.commit()
        conn.close()

        store = ApiKeyStore(db)
        veteran = store.list_keys()[0]
        assert veteran["revealable"] is False
        assert store.reveal_key("old-1") is None

        created = store.create_key("post-migration")
        new = next(r for r in store.list_keys() if r["name"] == "post-migration")
        assert new["revealable"] is True
        assert store.reveal_key(new["id"]) == created["key"]

    def test_reveal_route_returns_key_and_requires_auth(self, admin_client):
        client, store, token = admin_client
        headers = {"Authorization": f"Bearer {token}"}

        created = client.post(
            "/admin/api/api-keys", json={"name": "reveal-route"}, headers=headers
        ).json()
        key_id = created["api_key"]["id"]

        revealed = client.get(f"/admin/api/api-keys/{key_id}/reveal", headers=headers)
        assert revealed.status_code == 200
        assert revealed.json() == {"key": created["key"]}
        assert revealed.headers["cache-control"] == "no-store"

        anonymous = client.get(f"/admin/api/api-keys/{key_id}/reveal")
        assert anonymous.status_code in (401, 403)

        missing = client.get("/admin/api/api-keys/nope/reveal", headers=headers)
        assert missing.status_code == 404

    def test_reveal_route_reports_legacy_as_not_recoverable(self, admin_client):
        client, store, token = admin_client
        headers = {"Authorization": f"Bearer {token}"}

        conn = sqlite3.connect(store.path)
        conn.execute(
            "INSERT INTO api_keys (id, name, key_hash, key_prefix, created_at, key_material)"
            " VALUES ('legacy-row', 'veteran', 'h', 'sk-v', 1, NULL)"
        )
        conn.commit()
        conn.close()

        response = client.get("/admin/api/api-keys/legacy-row/reveal", headers=headers)
        assert response.status_code == 404
        assert "recreate" in response.json()["detail"].lower()
