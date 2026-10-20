"""Backend selection and read/write behavior of credential_store (no network).

The Upstash REST call is faked at `_command`, so KV behavior is tested without
a Vercel KV store.
"""
import base64
import json
import os
import stat

import pytest

import credential_store as cs


@pytest.fixture
def no_kv(monkeypatch):
    for name in ("KV_REST_API_URL", "KV_REST_API_TOKEN",
                 "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
                 "KV_ACCOUNTS_KEY"):
        monkeypatch.delenv(name, raising=False)


class _FakeKV:
    """Minimal stand-in for the Upstash REST endpoint."""

    def __init__(self, set_result="OK", fail_on=None):
        self.data = {}
        self.calls = []
        self.set_result = set_result
        self.fail_on = fail_on

    def command(self, cfg, args):
        self.calls.append(list(args))
        cmd = args[0]
        if self.fail_on == cmd:
            raise RuntimeError("kv down")
        if cmd == "get":
            return self.data.get(args[1])
        if cmd == "set":
            self.data[args[1]] = args[2] if self.set_result == "OK" else None
            return self.set_result
        raise AssertionError("unexpected command " + cmd)


@pytest.fixture
def kv(monkeypatch, tmp_path):
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.com")
    monkeypatch.setenv("KV_REST_API_TOKEN", "secret-token")
    monkeypatch.delenv("KV_ACCOUNTS_KEY", raising=False)
    fake = _FakeKV()
    monkeypatch.setattr(cs, "_command", fake.command)
    return fake, tmp_path


class TestBackendSelection:
    def test_file_backend_without_kv_env(self, no_kv, tmp_path):
        assert cs.backend() == "file"
        assert cs.describe() == {"backend": "file", "key": ""}

    def test_partial_kv_config_stays_on_file(self, monkeypatch, no_kv):
        monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.com")
        assert cs.backend() == "file"

    def test_kv_backend_when_configured(self, kv):
        assert cs.backend() == "vercel_kv"
        assert cs.describe()["key"] == cs.DEFAULT_KV_KEY

    def test_upstash_console_names_also_select_kv(self, no_kv, monkeypatch):
        # The Upstash console prints UPSTASH_REDIS_REST_*; pasting those as is
        # must work too, and the trailing slash must not end up in the URL.
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://db.example.com/")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
        assert cs.backend() == "vercel_kv"
        assert cs._kv_config() == ("https://db.example.com", "tok")

    def test_kv_names_take_precedence(self, no_kv, monkeypatch):
        monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.com")
        monkeypatch.setenv("KV_REST_API_TOKEN", "a")
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://other.example.com")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "b")
        assert cs._kv_config() == ("https://kv.example.com", "a")


class TestFileBackend:
    def test_roundtrip(self, no_kv, tmp_path):
        path = tmp_path / "accounts.json"
        payload = json.dumps({"version": 3, "accounts": []}, indent=2) + "\n"
        cs.write_store(path, payload)
        assert cs.read_store(path) == payload

    def test_file_is_owner_only(self, no_kv, tmp_path):
        path = tmp_path / "accounts.json"
        cs.write_store(path, "{}")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & stat.S_IWRITE  # still writable by us
        if os.name != "nt":
            # Windows reports synthetic 0o666 for any writable file, so the
            # owner-only intent is only assertable on POSIX.
            assert mode & 0o077 == 0

    def test_missing_store_reads_as_none(self, no_kv, tmp_path):
        assert cs.read_store(tmp_path / "absent.json") is None

    def test_creates_missing_parent_directory(self, no_kv, tmp_path):
        path = tmp_path / "nested" / "deeper" / "accounts.json"
        cs.write_store(path, "{}")
        assert path.exists()


class TestKvBackend:
    def test_write_sets_kv_and_mirrors_the_file(self, kv):
        fake, tmp = kv
        path = tmp / "accounts.json"
        payload = json.dumps({"source": "kv"}, indent=2) + "\n"
        cs.write_store(path, payload)
        assert fake.data[cs.kv_key()] == base64.b64encode(payload.encode()).decode()
        assert path.read_text(encoding="utf-8") == payload  # mirror for cold starts
        assert cs.read_store(path) == payload

    def test_value_is_single_line_base64(self, kv):
        fake, tmp = kv
        cs.write_store(tmp / "accounts.json", json.dumps({"a": "b" * 200}, indent=2))
        stored = fake.data[cs.kv_key()]
        assert "\n" not in stored and "\r" not in stored

    def test_read_prefers_kv_over_a_stale_mirror(self, kv):
        fake, tmp = kv
        path = tmp / "accounts.json"
        path.write_text(json.dumps({"source": "file"}), encoding="utf-8")
        fake.data[cs.kv_key()] = base64.b64encode(
            json.dumps({"source": "kv"}).encode()).decode()
        assert json.loads(cs.read_store(path))["source"] == "kv"

    def test_plain_json_value_is_accepted(self, kv):
        # A key written by another tool (redis-cli, dashboard) is not base64.
        fake, tmp = kv
        fake.data[cs.kv_key()] = json.dumps({"source": "raw"})
        assert json.loads(cs.read_store(tmp / "accounts.json"))["source"] == "raw"

    def test_empty_kv_falls_back_to_the_mirror(self, kv):
        _fake, tmp = kv
        path = tmp / "accounts.json"
        path.write_text(json.dumps({"source": "file"}), encoding="utf-8")
        assert json.loads(cs.read_store(path))["source"] == "file"

    def test_read_failure_falls_back_to_the_mirror(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.com")
        monkeypatch.setenv("KV_REST_API_TOKEN", "secret-token")
        monkeypatch.setattr(cs, "_command", _FakeKV(fail_on="get").command)
        path = tmp_path / "accounts.json"
        path.write_text(json.dumps({"source": "file"}), encoding="utf-8")
        assert json.loads(cs.read_store(path))["source"] == "file"

    def test_write_failure_raises_after_writing_the_mirror(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.com")
        monkeypatch.setenv("KV_REST_API_TOKEN", "secret-token")
        monkeypatch.setattr(cs, "_command", _FakeKV(set_result=None).command)
        path = tmp_path / "accounts.json"
        with pytest.raises(cs.CredentialStoreError):
            cs.write_store(path, json.dumps({"source": "kv"}))
        # The mirror still holds the data, and the caller learns KV did not take it.
        assert json.loads(path.read_text(encoding="utf-8"))["source"] == "kv"

class TestUpstashWireFormat:
    """`_command` is the only code that talks to the database. A real Upstash
    endpoint answers a newline-separated body with "ERR wrong number of
    arguments" for two-argument commands, so the exact request shape is pinned
    here instead of being faked away.
    """

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _capture(self, monkeypatch, payload):
        seen = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            seen["url"] = url
            seen["json"] = json
            seen["headers"] = headers or {}
            seen["timeout"] = timeout
            return self._Response(payload)

        monkeypatch.setattr(cs.httpx, "post", fake_post)
        return seen

    def test_two_argument_command_is_one_json_array_at_the_root(self, monkeypatch):
        seen = self._capture(monkeypatch, {"result": "OK"})
        assert cs._command(("https://db.example.com", "tok"), ["set", "k", "v"]) == "OK"
        assert seen["url"] == "https://db.example.com/"
        assert seen["json"] == ["set", "k", "v"]
        assert seen["headers"]["Authorization"] == "Bearer tok"
        assert seen["headers"]["Content-Type"] == "application/json"
        assert seen["timeout"]

    def test_one_argument_command_uses_the_same_shape(self, monkeypatch):
        seen = self._capture(monkeypatch, {"result": "value"})
        assert cs._command(("https://db.example.com/", "tok"), ["get", "k"]) == "value"
        assert seen["url"] == "https://db.example.com/"
        assert seen["json"] == ["get", "k"]

    def test_error_payload_raises(self, monkeypatch):
        self._capture(monkeypatch, {"error": "ERR wrong number of arguments"})
        with pytest.raises(cs.CredentialStoreError, match="wrong number"):
            cs._command(("https://db.example.com", "tok"), ["set", "k", "v"])


    def test_custom_key_is_used(self, kv, monkeypatch):
        fake, tmp = kv
        monkeypatch.setenv("KV_ACCOUNTS_KEY", "mine:accounts")
        cs.write_store(tmp / "accounts.json", "{}")
        assert "mine:accounts" in fake.data


class TestFileMirrorPermissions:
    """Vercel's /tmp rejects chmod with EPERM; that must not lose accounts."""

    def test_chmod_eperm_does_not_fail_the_save(self, tmp_path, monkeypatch, no_kv):
        target = str(tmp_path)

        def refusing_chmod(path, mode):
            if str(path).startswith(target):
                raise PermissionError(1, "Operation not permitted", str(path))

        monkeypatch.setattr(cs.os, "chmod", refusing_chmod)
        path = tmp_path / "nested" / "accounts.json"
        cs.write_store(path, json.dumps({"accounts": []}))
        assert json.loads(path.read_text(encoding="utf-8")) == {"accounts": []}
