"""
Symmetric encryption helpers for protecting channel partner credentials and OAuth tokens at rest.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Optional, Union

from cryptography.fernet import Fernet

from app.core.config import get_settings
from app.core.security import get_jwt_secret

ENCRYPTED_PREFIX = "enc:v1:"


def _derive_fernet_key() -> bytes:
    """Resolve Fernet key from SECRETS_ENCRYPTION_KEY or derive deterministically from JWT secret."""
    settings = get_settings()
    raw_key = (
        getattr(settings, "SECRETS_ENCRYPTION_KEY", "")
        or os.environ.get("SECRETS_ENCRYPTION_KEY", "")
        or ""
    ).strip()

    if raw_key:
        candidate = raw_key.encode("utf-8")
        try:
            Fernet(candidate)
            return candidate
        except Exception:
            digest = hashlib.sha256(candidate).digest()
            return base64.urlsafe_b64encode(digest)

    jwt_secret = get_jwt_secret()
    digest = hashlib.sha256(jwt_secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet() -> Fernet:
    return Fernet(_derive_fernet_key())


def encrypt_secret(val: Optional[str]) -> str:
    """Encrypt a plaintext secret with Fernet and tag with the `enc:v1:` prefix."""
    if val is None:
        return ""
    s = str(val)
    if not s:
        return ""
    fernet = _get_fernet()
    if s.startswith(ENCRYPTED_PREFIX):
        token = s[len(ENCRYPTED_PREFIX):]
        try:
            fernet.decrypt(token.encode("utf-8"))
            return s
        except Exception:
            pass
    encrypted = fernet.encrypt(s.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_PREFIX}{encrypted}"


def decrypt_secret(val: Optional[str]) -> str:
    """
    Decrypt an `enc:v1:` secret; returns legacy plaintext values unchanged for zero-downtime migration.
    """
    if val is None:
        return ""
    s = str(val)
    if not s:
        return ""
    if not s.startswith(ENCRYPTED_PREFIX):
        return s
    token = s[len(ENCRYPTED_PREFIX):]
    fernet = _get_fernet()
    return fernet.decrypt(token.encode("utf-8")).decode("utf-8")


def write_restricted_json(path: Union[str, Path], payload: Any) -> None:
    """Atomically write JSON to disk with owner-only permissions (0o600 where supported)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(serialized)
            tmp_file.flush()
            try:
                os.fsync(tmp_file.fileno())
            except OSError:
                pass

        try:
            os.chmod(tmp_path, 0o600)
        except (OSError, NotImplementedError):
            pass

        os.replace(tmp_path, target)
        tmp_path = None

        try:
            os.chmod(target, 0o600)
        except (OSError, NotImplementedError):
            pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
