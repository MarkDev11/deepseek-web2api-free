"""KV-backed persistence for client API keys (v3.3.17).

The sqlite store in /tmp died with every Vercel cold start, so hosted clients
holding a WebUI-issued key intermittently got 401 ("API key is invalid") after
each deploy. These tests fake the Upstash layer at the seam the module uses
(_kv_config / _kv_command) and pin the KV-authoritative behaviour: hydrate on
init, write-through on mutation, seed-when-absent, background refresh.
"""
import hashlib
import json

import pytest

import api_key_store
from api_key_store import ApiKeyStore
from credential_store import _decode, _encode


class FakeKV:
    def __init__(self, initial=None):
        self.value = initial
        self.sets = 0

    def config(self):
        return ("https://fake.redis.example", "token")

    def command(self, cfg, args):
        if args[0] == "get":
            return self.value
        if args[0] == "set":
            self.value = args[2]
            self.sets += 1
            return "OK"
        raise AssertionError("unexpected command " + repr(args))


def record_for(key, key_id="rec-1", name="cli"):
    return {
        "id": key_id,
        "name": name,
        "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        "key_prefix": key[:8],
        "created_at": 1,
    }


def kv_payload(records):
    return _encode(json.dumps({"v": 1, "keys": records}))


def read_keys(fake):
    obj = json.loads(_decode(fake.value))
    return obj["keys"]


@pytest.fixture
def kv(monkeypatch):
    def install(initial=None):
        fake = FakeKV(initial)
        monkeypatch.setattr(api_key_store, "_kv_config", fake.config)
        monkeypatch.setattr(api_key_store, "_kv_command", fake.command)
        monkeypatch.setenv("API_KEY_KV_SYNC_SECS", "0")
        return fake
    return install


def test_hydrates_from_kv_on_init(tmp_path, kv):
    kv(kv_payload([record_for("sk-everywhere")]))
    store = ApiKeyStore(tmp_path / "keys.sqlite3")
    assert store.verify("sk-everywhere") is True
    assert [k["name"] for k in store.list_keys()] == ["cli"]


def test_create_and_revoke_push_to_kv(tmp_path, kv):
    fake = kv()
    store = ApiKeyStore(tmp_path / "keys.sqlite3")
    assert fake.sets == 1  # init seeds the absent KV key with the (empty) local list

    created = store.create_key("harness")
    keys = read_keys(fake)
    assert len(keys) == 1
    assert keys[0]["key_hash"] == hashlib.sha256(created["key"].encode()).hexdigest()
    assert fake.sets == 2

    assert store.revoke_key(created["api_key"]["id"]) is True
    assert read_keys(fake) == []
    assert fake.sets == 3


def test_seed_when_absent_survives_a_new_instance(tmp_path, kv):
    fake = kv()
    first = ApiKeyStore(tmp_path / "first.sqlite3", legacy_keys=["legacy-key"])
    assert any(r["key_hash"] == hashlib.sha256(b"legacy-key").hexdigest()
               for r in read_keys(fake))

    # Fresh instance: brand-new /tmp sqlite, same KV -> key still verifies.
    second = ApiKeyStore(tmp_path / "second.sqlite3")
    assert second.verify("legacy-key") is True


def test_kv_network_error_keeps_local_store_usable(tmp_path, monkeypatch):
    monkeypatch.setattr(api_key_store, "_kv_config", lambda: ("https://x", "t"))

    def boom(cfg, args):
        raise RuntimeError("network down")

    monkeypatch.setattr(api_key_store, "_kv_command", boom)
    monkeypatch.setenv("API_KEY_KV_SYNC_SECS", "0")
    store = ApiKeyStore(tmp_path / "keys.sqlite3")
    created = store.create_key("offline")
    assert store.verify(created["key"]) is True


def test_background_refresh_sees_another_instance(tmp_path, kv):
    fake = kv()
    instance_a = ApiKeyStore(tmp_path / "a.sqlite3")
    instance_b = ApiKeyStore(tmp_path / "b.sqlite3")

    created = instance_b.create_key("from-b")
    assert instance_a.verify(created["key"]) is False  # not yet

    instance_a._kv_refresh_tick()
    assert instance_a.verify(created["key"]) is True


def test_without_kv_behaves_as_plain_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(api_key_store, "_kv_config", lambda: None)
    monkeypatch.setenv("API_KEY_KV_SYNC_SECS", "0")
    store = ApiKeyStore(tmp_path / "keys.sqlite3")
    created = store.create_key("local")
    assert store.verify(created["key"]) is True
    assert api_key_store.kv_status() == {"enabled": False, "key": None}


def test_kv_status_shape(tmp_path, kv):
    kv()
    status = api_key_store.kv_status()
    assert status["enabled"] is True
    assert status["key"] == "ds2api:api_keys.json"

def test_reveal_material_survives_kv_round_trip(tmp_path, kv):
    kv()
    instance_a = ApiKeyStore(tmp_path / "a.sqlite3")
    created = instance_a.create_key("shared")

    # Fresh instance hydrates purely from KV - material included.
    instance_b = ApiKeyStore(tmp_path / "b.sqlite3")
    record = next(r for r in instance_b.list_keys() if r["name"] == "shared")
    assert record["revealable"] is True
    assert instance_b.reveal_key(record["id"]) == created["key"]

def test_reveal_self_heals_across_instances(tmp_path, kv):
    kv()
    instance_a = ApiKeyStore(tmp_path / "a.sqlite3")
    instance_b = ApiKeyStore(tmp_path / "b.sqlite3")  # booted earlier, stale

    created = instance_a.create_key("late-arrival")
    record = next(r for r in instance_a.list_keys() if r["name"] == "late-arrival")

    # No manual tick: the reveal itself pulls once and finds the key.
    assert instance_b.reveal_key(record["id"]) == created["key"]


def test_create_on_stale_instance_does_not_clobber(tmp_path, kv):
    kv()
    instance_a = ApiKeyStore(tmp_path / "a.sqlite3")
    instance_b = ApiKeyStore(tmp_path / "b.sqlite3")

    key_a = instance_a.create_key("made-on-a")
    # b's mirror still predates key A; creating through b must not erase it.
    key_b = instance_b.create_key("made-on-b")

    fresh = ApiKeyStore(tmp_path / "fresh.sqlite3")
    assert fresh.verify(key_a["key"]) is True
    assert fresh.verify(key_b["key"]) is True
