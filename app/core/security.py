import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import bcrypt
import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError

settings = get_settings()


# ==============================================================================
# Password Hashing & Verification (bcrypt)
# ==============================================================================

def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt with automatic salt generation."""
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8")
        )
    except Exception:
        return False


# ==============================================================================
# Token Hashing (SHA-256 for Refresh Token storage)
# ==============================================================================

def hash_token(token: str) -> str:
    """Generate SHA-256 hex digest of a token string for safe database lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ==============================================================================
# JWT Access & Refresh Token Utilities
# ==============================================================================

def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> Tuple[str, datetime]:
    """
    Create a signed JWT access token.
    Returns (token_string, expires_at_datetime).
    """
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({
        "exp": expire,
        "iat": now,
        "type": "access"
    })
    
    encoded_jwt = jwt.encode(
        to_encode,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM
    )
    return encoded_jwt, expire


def create_refresh_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> Tuple[str, str, datetime]:
    """
    Create a signed JWT refresh token.
    Returns (raw_token, token_hash, expires_at_datetime).
    """
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    
    # Generate random jti (JWT ID) to ensure uniqueness even for identical payload
    jti = secrets.token_hex(16)
    to_encode.update({
        "exp": expire,
        "iat": now,
        "jti": jti,
        "type": "refresh"
    })
    
    encoded_jwt = jwt.encode(
        to_encode,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM
    )
    token_hash = hash_token(encoded_jwt)
    return encoded_jwt, token_hash, expire


def decode_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate a JWT token.
    Raises UnauthorizedError if the token is expired, invalid, or malformed.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM]
        )
        return payload
    except ExpiredSignatureError:
        raise UnauthorizedError(
            message="Phiên đăng nhập hoặc token đã hết hạn.",
            details={"reason": "TOKEN_EXPIRED"}
        )
    except InvalidTokenError as e:
        raise UnauthorizedError(
            message="Mã xác thực không hợp lệ.",
            details={"reason": "TOKEN_INVALID", "detail": str(e)}
        )


# ==============================================================================
# Channel Webhook & Partner Signature Utilities
# ==============================================================================

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

