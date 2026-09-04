import pytest
from datetime import datetime, timedelta, timezone
from app.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_token,
    generate_hmac_sha256,
    verify_hmac_sha256,
)
from app.core.exceptions import UnauthorizedError
from app.models.user import UserRole


def test_password_hashing():
    raw_password = "NamAnSecure@2024!"
    hashed = hash_password(raw_password)
    
    assert hashed != raw_password
    assert verify_password(raw_password, hashed) is True
    assert verify_password("WrongPassword123", hashed) is False


def test_access_token_lifecycle():
    data = {
        "sub": "usr-uuid-12345",
        "username": "admin",
        "role": UserRole.SUPER_ADMIN.value,
        "is_superuser": True
    }
    token, exp = create_access_token(data, expires_delta=timedelta(minutes=30))
    assert isinstance(token, str)
    assert exp > datetime.now(timezone.utc)

    # Decode and verify claims
    payload = decode_token(token)
    assert payload["sub"] == "usr-uuid-12345"
    assert payload["username"] == "admin"
    assert payload["role"] == UserRole.SUPER_ADMIN.value
    assert payload["type"] == "access"


def test_refresh_token_lifecycle():
    data = {"sub": "usr-uuid-12345"}
    raw_token, token_hash, exp = create_refresh_token(data, expires_delta=timedelta(days=7))
    
    assert isinstance(raw_token, str)
    assert hash_token(raw_token) == token_hash
    
    payload = decode_token(raw_token)
    assert payload["sub"] == "usr-uuid-12345"
    assert payload["type"] == "refresh"
    assert "jti" in payload


def test_expired_token():
    data = {"sub": "expired-user"}
    # Create an expired token (expired 10 minutes ago)
    token, _ = create_access_token(data, expires_delta=timedelta(minutes=-10))
    
    with pytest.raises(UnauthorizedError) as exc_info:
        decode_token(token)
    assert exc_info.value.status_code == 401
    assert "hết hạn" in exc_info.value.message


def test_invalid_token():
    with pytest.raises(UnauthorizedError):
        decode_token("invalid.jwt.token")


def test_hmac_sha256_webhook_verification():
    key = "a9756768d72268a6d66ea886031988f0638f03b0036fc63fbe0b86a6aef18546"
    message = "POST|https://portal.namanmarket.com/api/v1/shopeefood/webhook|{}"
    
    sig = generate_hmac_sha256(key, message, is_hex_key=True)
    assert verify_hmac_sha256(key, message, sig, is_hex_key=True) is True
    assert verify_hmac_sha256(key, message + "_tampered", sig, is_hex_key=True) is False
