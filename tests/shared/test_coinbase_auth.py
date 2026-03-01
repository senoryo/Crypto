"""Tests for shared/coinbase_auth.py — JWT authentication helpers."""

import base64
import time
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from shared.coinbase_auth import _load_key, build_jwt, build_ws_subscribe_message


# ---------------------------------------------------------------------------
# Helpers to generate test keys
# ---------------------------------------------------------------------------

def _make_ed25519_secret() -> str:
    """Return a base64-encoded Ed25519 private key (64 bytes: priv + pub)."""
    priv = Ed25519PrivateKey.generate()
    raw_priv = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_pub = priv.public_key().public_bytes(
        Encoding.Raw,
        format=PublicFormat.Raw,
    )
    return base64.b64encode(raw_priv + raw_pub).decode()


def _make_ec_pem_secret() -> str:
    """Return a PEM-encoded EC P-256 private key string."""
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
    return pem.decode()


# ---------------------------------------------------------------------------
# Tests for _load_key
# ---------------------------------------------------------------------------

class TestLoadKey:

    def test_detects_ed25519_from_base64(self):
        secret = _make_ed25519_secret()
        key, algo = _load_key(secret)
        assert algo == "EdDSA"
        assert isinstance(key, Ed25519PrivateKey)

    def test_detects_ec_from_pem(self):
        secret = _make_ec_pem_secret()
        key, algo = _load_key(secret)
        assert algo == "ES256"

    def test_invalid_base64_raises(self):
        with pytest.raises(Exception):
            _load_key("not-valid-base64!!!")


# ---------------------------------------------------------------------------
# Tests for build_jwt
# ---------------------------------------------------------------------------

class TestBuildJwt:

    def test_jwt_decodes_successfully_ed25519(self):
        secret = _make_ed25519_secret()
        key_name = "test-key-name"
        token = build_jwt(key_name, secret, "GET", "/api/v3/brokerage/orders")
        # Decode without verification (we don't have the public key readily in PEM)
        payload = pyjwt.decode(token, options={"verify_signature": False})
        assert payload["sub"] == key_name
        assert payload["iss"] == "cdp"
        assert "/api/v3/brokerage/orders" in payload["uris"][0]

    def test_jwt_contains_correct_uri(self):
        secret = _make_ec_pem_secret()
        token = build_jwt("k", secret, "POST", "/api/v3/brokerage/orders")
        payload = pyjwt.decode(token, options={"verify_signature": False})
        assert payload["uris"] == ["POST /api/v3/brokerage/orders"]

    def test_jwt_expiry_is_near_future(self):
        secret = _make_ed25519_secret()
        token = build_jwt("k", secret, "GET", "/path")
        payload = pyjwt.decode(token, options={"verify_signature": False})
        now = int(time.time())
        # exp should be within ~120s of now
        assert payload["exp"] - now <= 125
        assert payload["exp"] - now >= 115


# ---------------------------------------------------------------------------
# Tests for build_ws_subscribe_message
# ---------------------------------------------------------------------------

class TestBuildWsSubscribeMessage:

    def test_structure(self):
        secret = _make_ed25519_secret()
        msg = build_ws_subscribe_message("k", secret, "user", ["BTC-USD", "ETH-USD"])
        assert msg["type"] == "subscribe"
        assert msg["channel"] == "user"
        assert msg["product_ids"] == ["BTC-USD", "ETH-USD"]
        assert "jwt" in msg

    def test_jwt_is_valid_token(self):
        secret = _make_ed25519_secret()
        msg = build_ws_subscribe_message("mykey", secret, "user", ["BTC-USD"])
        payload = pyjwt.decode(msg["jwt"], options={"verify_signature": False})
        assert payload["sub"] == "mykey"
        assert payload["iss"] == "cdp"
