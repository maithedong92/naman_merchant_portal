import hashlib
import hmac
import secrets
from typing import Optional


def generate_hmac_sha256(key: str, message: str, is_hex_key: bool = True) -> str:
    """
    Generate HMAC-SHA256 signature.
    Used for ShopeeFood Foody API signature generation:
    Base string: {method}|{url}|{body_json}
    """
    if is_hex_key:
        secret_bytes = bytes.fromhex(key)
    else:
        secret_bytes = key.encode("utf-8")
    
    signature = hmac.new(
        secret_bytes,
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return signature


def verify_hmac_sha256(key: str, message: str, expected_signature: str, is_hex_key: bool = True) -> bool:
    """Verify HMAC-SHA256 signature using constant time comparison."""
    try:
        calculated = generate_hmac_sha256(key, message, is_hex_key=is_hex_key)
        return secrets.compare_digest(calculated.lower(), expected_signature.lower())
    except Exception:
        return False
