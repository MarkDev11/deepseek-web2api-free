"""Unit tests for stateless admin tokens (serverless-safe sessions)."""
import admin


class TestSignedTokens:
    def test_disabled_without_secret(self, monkeypatch):
        monkeypatch.setattr(admin, "_TOKEN_SECRET", b"")
        assert admin._verify_signed_token("abc.def") is False
        assert admin._verify_token("abc.def") is False

    def test_sign_verify_roundtrip(self, monkeypatch):
        monkeypatch.setattr(admin, "_TOKEN_SECRET", b"test-secret")
        rand = admin._generate_token()
        signed = admin._sign_token(rand)
        assert "." in signed
        assert admin._verify_signed_token(signed) is True
        # Verifies without any server-side state (cross-instance case).
        assert admin._verify_token(signed) is True

    def test_tampered_signature_rejected(self, monkeypatch):
        monkeypatch.setattr(admin, "_TOKEN_SECRET", b"test-secret")
        signed = admin._sign_token(admin._generate_token())
        bad = signed[:-1] + ("0" if signed[-1] != "0" else "1")
        assert admin._verify_signed_token(bad) is False

    def test_wrong_secret_rejected(self, monkeypatch):
        monkeypatch.setattr(admin, "_TOKEN_SECRET", b"secret-a")
        signed = admin._sign_token(admin._generate_token())
        monkeypatch.setattr(admin, "_TOKEN_SECRET", b"secret-b")
        assert admin._verify_signed_token(signed) is False

    def test_legacy_unsigned_token_still_works_in_memory(self, monkeypatch):
        monkeypatch.setattr(admin, "_TOKEN_SECRET", b"test-secret")
        rand = admin._generate_token()
        admin._tokens.add(rand)
        try:
            assert admin._verify_token(rand) is True
        finally:
            admin._tokens.discard(rand)
