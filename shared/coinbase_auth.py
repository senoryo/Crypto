"""
JWT authentication helper for Coinbase Advanced Trade CDP API keys.

Supports both legacy EC/PEM keys (ES256) and newer Ed25519 CDP keys (EdDSA).
Automatically detects key format and uses the correct algorithm.
"""

import base64
import time
import secrets

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _load_key(secret: str):
    """Load a private key, auto-detecting PEM (EC) vs base64 (Ed25519).

    Returns (private_key, algorithm) tuple.
    """
    if secret.strip().startswith("-----"):
        # PEM-encoded EC key
        return load_pem_private_key(secret.encode("utf-8"), password=None), "ES256"

    # Base64-encoded Ed25519 key from CDP portal
    raw = base64.b64decode(secret)
    # Ed25519 private key is 32 bytes; CDP gives 64 (private + public)
    priv_key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    return priv_key, "EdDSA"


def build_jwt(key_name: str, secret: str, method: str, path: str) -> str:
    """Build a signed JWT for Coinbase REST API Bearer authentication.

    Args:
        key_name: CDP API key name or ID
        secret: PEM-encoded EC key or base64-encoded Ed25519 key
        method: HTTP method (GET, POST, etc.)
        path: API path (e.g. "/api/v3/brokerage/orders")

    Returns:
        Signed JWT string suitable for Authorization: Bearer header
    """
    private_key, algorithm = _load_key(secret)

    now = int(time.time())
    uri = f"{method.upper()} {path}"

    payload = {
        "sub": key_name,
        "iss": "cdp",
        "aud": ["cdp_service"],
        "nbf": now,
        "exp": now + 120,
        "uris": [uri],
    }

    headers = {
        "kid": key_name,
        "nonce": secrets.token_hex(16),
        "typ": "JWT",
    }

    return jwt.encode(payload, private_key, algorithm=algorithm, headers=headers)


def build_ws_subscribe_message(
    key_name: str,
    secret: str,
    channel: str,
    product_ids: list[str],
) -> dict:
    """Build an authenticated WebSocket subscribe message for Coinbase.

    Args:
        key_name: CDP API key name or ID
        secret: PEM-encoded EC key or base64-encoded Ed25519 key
        channel: Channel name (e.g. "user")
        product_ids: List of product IDs (e.g. ["BTC-USD", "ETH-USD"])

    Returns:
        Dict ready to be JSON-serialized and sent over WebSocket
    """
    private_key, algorithm = _load_key(secret)

    now = int(time.time())

    payload = {
        "sub": key_name,
        "iss": "cdp",
        "aud": ["cdp_service"],
        "nbf": now,
        "exp": now + 120,
    }

    headers = {
        "kid": key_name,
        "nonce": secrets.token_hex(16),
        "typ": "JWT",
    }

    token = jwt.encode(payload, private_key, algorithm=algorithm, headers=headers)

    return {
        "type": "subscribe",
        "product_ids": product_ids,
        "channel": channel,
        "jwt": token,
    }
