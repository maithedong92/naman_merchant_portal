import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.exceptions import AppException
from app.main import (
    RequestBodySizeLimitMiddleware,
    _SecretRedactingFilter,
    app,
    app_exception_handler,
    lifespan,
    unhandled_exception_handler,
)

ROOT_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Middleware: HTTP Security Headers & Minimized /api/info & Error Scrubbing
# ---------------------------------------------------------------------------


def test_security_headers_applied_on_api_and_static_routes(client):
    """Responses must carry defensive security headers and no-store on /api/v1/."""
    with patch("app.main.settings.DEBUG", False):
        api_res = client.get("/api/v1/health")
        assert api_res.status_code == 200
        assert api_res.headers.get("x-content-type-options") == "nosniff"
        assert api_res.headers.get("x-frame-options") == "DENY"
        assert api_res.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
        assert (
            api_res.headers.get("permissions-policy")
            == "camera=(), microphone=(), geolocation=(), payment=()"
        )
        assert (
            api_res.headers.get("strict-transport-security")
            == "max-age=31536000; includeSubDomains"
        )
        assert api_res.headers.get("cache-control") == "no-store"

    with patch("app.main.settings.DEBUG", True):
        img_res = client.get("/static/images/placeholder.png")
        assert img_res.status_code == 200
        assert img_res.headers.get("x-content-type-options") == "nosniff"
        assert img_res.headers.get("x-frame-options") == "DENY"
        assert "strict-transport-security" not in img_res.headers


def test_api_info_minimized_when_not_debug(client):
    """GET /api/info must omit version, channels, and docs when DEBUG=False."""
    with patch("app.main.settings.DEBUG", False):
        res = client.get("/api/info")
        assert res.status_code == 200
        data = res.json()["data"]
        assert "portal" in data
        assert "version" not in data
        assert "channels" not in data
        assert "docs" not in data


def test_static_css_and_vendor_mounts_do_not_shadow_static_images(client):
    """Mounting /static/css and /static/vendor must not break /static/images/*."""
    img_res = client.get("/static/images/test.png")
    assert img_res.status_code == 200
    assert img_res.headers.get("content-type", "").startswith("image/png")

    css_res = client.get("/static/css/does-not-exist.css")
    assert css_res.status_code == 404


@pytest.mark.asyncio
async def test_app_exception_handler_hides_5xx_details_in_non_debug():
    """5xx AppException responses must return data=None when DEBUG=False."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/test",
        "headers": [],
        "query_string": b"",
    }
    req = Request(scope)
    exc = AppException(
        message="Upstream error",
        error_code="UPSTREAM_ERR",
        status_code=502,
        details={"raw_response": "sensitive internal trace"},
    )

    with (
        patch("app.main.settings.DEBUG", False),
        patch("app.main.error_service.log_error", new=AsyncMock()),
    ):
        resp = await app_exception_handler(req, exc)

    assert resp.status_code == 502
    assert b'"data":null' in resp.body.replace(b" ", b"")
    assert b"sensitive internal trace" not in resp.body


@pytest.mark.asyncio
async def test_unhandled_exception_handler_logs_path_without_query_string(caplog):
    """unhandled_exception_handler must log request.url.path without sensitive query params."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/shopeemart/auth/callback",
        "query_string": b"code=secret_auth_code_123&sign=secret_hmac_456",
        "headers": [],
    }
    req = Request(scope)

    with (
        patch("app.main.error_service.log_error", new=AsyncMock()),
        caplog.at_level(logging.ERROR, logger="naman_portal"),
    ):
        resp = await unhandled_exception_handler(req, RuntimeError("boom"))

    assert resp.status_code == 500
    assert "/api/v1/shopeemart/auth/callback" in caplog.text
    assert "secret_auth_code_123" not in caplog.text
    assert "secret_hmac_456" not in caplog.text


# ---------------------------------------------------------------------------
# 2. Request Body Size Limit Middleware (2 MB default, 20 MB upload)
# ---------------------------------------------------------------------------


def test_request_body_limit_rejects_over_2mb_content_length(client):
    """Standard endpoint must return 413 JSON when Content-Length exceeds 2 MB."""
    oversized = b"x" * (2 * 1024 * 1024 + 1024)
    res = client.post(
        "/api/v1/auth/login",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 413
    body = res.json()
    assert body["success"] is False
    assert body["error_code"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_request_body_limit_rejects_streamed_body_without_content_length():
    """Chunked/streamed request exceeding 2 MB without Content-Length must return 413 JSON."""

    async def chunk_stream():
        chunk = b"a" * (512 * 1024)
        for _ in range(5):  # 2.5 MB total without Content-Length
            yield chunk

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.post(
            "/api/v1/auth/login",
            content=chunk_stream(),
            headers={"Content-Type": "application/json"},
        )

    assert res.status_code == 413
    assert res.json()["error_code"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_request_body_limit_allows_up_to_20mb_on_files_upload_and_rejects_above():
    """Upload endpoint allows 3 MB (> 2 MB default) and rejects > 20 MB with 413."""
    dummy_app = FastAPI()

    @dummy_app.post("/api/v1/haravan/orders/{order_id}/files/upload")
    async def upload_ok(request: Request):
        body = await request.body()
        return {"size": len(body)}

    wrapped = RequestBodySizeLimitMiddleware(dummy_app)
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        res_ok = await ac.post(
            "/api/v1/haravan/orders/123/files/upload",
            content=b"b" * (3 * 1024 * 1024),
        )
        assert res_ok.status_code == 200
        assert res_ok.json()["size"] == 3 * 1024 * 1024

        res_too_large = await ac.post(
            "/api/v1/haravan/orders/123/files/upload",
            content=b"",
            headers={"Content-Length": str(20 * 1024 * 1024 + 1)},
        )
        assert res_too_large.status_code == 413
        assert res_too_large.json()["error_code"] == "PAYLOAD_TOO_LARGE"


# ---------------------------------------------------------------------------
# 3. Logging Redaction Filter
# ---------------------------------------------------------------------------


def test_secret_redacting_filter_masks_tokens_and_credentials(caplog):
    """_SecretRedactingFilter must redact Bearer tokens and sensitive key=value params."""
    portal_logger = logging.getLogger("naman_portal")
    assert any(isinstance(f, _SecretRedactingFilter) for f in portal_logger.filters)
    assert any(isinstance(f, _SecretRedactingFilter) for f in logging.getLogger().filters)

    with caplog.at_level(logging.INFO, logger="naman_portal"):
        portal_logger.info(
            "Outbound call Authorization: Bearer jwt.secret.tok "
            "https://api.example.com?access_token=tok_123&refresh_token=ref_456"
            "&partner_key=pk_789&password=pw_000&sign=sig_abc"
        )
        portal_logger.info("Arg format Authorization: Bearer %s", "arg_secret_jwt")

    text = caplog.text
    for leaked in (
        "jwt.secret.tok",
        "tok_123",
        "ref_456",
        "pk_789",
        "pw_000",
        "sig_abc",
        "arg_secret_jwt",
    ):
        assert leaked not in text
    assert "[REDACTED]" in text


# ---------------------------------------------------------------------------
# 4. Config Defaults, Production Fail-Fast Validation & Database Timeouts
# ---------------------------------------------------------------------------


def test_settings_hardened_defaults():
    """Verify secure defaults for ALLOWED_ORIGINS, TRUSTED_PROXY_CIDRS, and new settings."""
    clean = Settings(_env_file=None)
    assert clean.ALLOWED_ORIGINS == ["https://ump.namanmarket.com"]
    assert clean.TRUSTED_PROXY_CIDRS == ["127.0.0.1/32", "::1/128", "172.16.0.0/12"]
    assert clean.PUBLIC_BASE_URL == ""
    assert clean.SECRETS_ENCRYPTION_KEY == ""


@pytest.mark.parametrize(
    "overrides",
    [
        {"JWT_SECRET_KEY": ""},
        {"JWT_SECRET_KEY": "short_secret"},
        {"JWT_SECRET_KEY": "change_in_production_secret_key_that_is_long_enough"},
        {"JWT_SECRET_KEY": "CHANGE_ME_super_long_secret_value_here_123456"},
        {"JWT_SECRET_KEY": "your_jwt_secret_key_value_exceeding_32_chars"},
        {"INTERNAL_API_SECRET": ""},
        {"INTERNAL_API_SECRET": "CHANGE_ME_RANDOM_32_BYTES"},
        {"INTERNAL_API_SECRET": "your_internal_secret"},
        {"ALLOWED_ORIGINS": []},
        {"ALLOWED_ORIGINS": ["*"]},
        {"ALLOWED_ORIGINS": ["http://localhost:3001"]},
        {"ALLOWED_ORIGINS": ["http://127.0.0.1:3002"]},
    ],
)
def test_validate_production_security_fails_fast(overrides):
    """validate_production_security must raise RuntimeError in production when secrets/origins are weak."""
    base_kwargs = {
        "_env_file": None,
        "ENVIRONMENT": "production",
        "DEBUG": False,
        "JWT_SECRET_KEY": "A" * 40,
        "INTERNAL_API_SECRET": "B" * 32,
        "ALLOWED_ORIGINS": ["https://ump.namanmarket.com"],
    }
    base_kwargs.update(overrides)
    cfg = Settings(**base_kwargs)
    with pytest.raises(RuntimeError):
        cfg.validate_production_security()


def test_validate_production_security_passes_with_strong_prod_config():
    """validate_production_security succeeds when all production secrets and origins are valid."""
    cfg = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        DEBUG=False,
        JWT_SECRET_KEY="X" * 48,
        INTERNAL_API_SECRET="Y" * 32,
        ALLOWED_ORIGINS=["https://ump.namanmarket.com"],
    )
    cfg.validate_production_security()


@pytest.mark.asyncio
async def test_lifespan_fails_fast_in_production_with_weak_secrets(monkeypatch):
    """App lifespan must fail fast when ENVIRONMENT=production, DEBUG=False, and secrets are weak."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    weak_settings = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        DEBUG=False,
        JWT_SECRET_KEY="short",
        INTERNAL_API_SECRET="",
        ALLOWED_ORIGINS=["https://ump.namanmarket.com"],
    )
    with patch("app.main.settings", weak_settings):
        with pytest.raises(RuntimeError, match="Production security validation failed"):
            async with lifespan(app):
                pass


def test_database_postgres_engine_sets_asyncpg_timeouts():
    """PostgreSQL branch in app.core.database must configure command_timeout and statement_timeout."""
    probe = """
import json
from unittest.mock import MagicMock, patch
from app.core.config import Settings

fake_settings = Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/ump")
captured = {}
def fake_create(url, **kwargs):
    captured["url"] = url
    captured["connect_args"] = kwargs.get("connect_args", {})
    return MagicMock()

with patch("app.core.config.get_settings", return_value=fake_settings), \
     patch("sqlalchemy.ext.asyncio.create_async_engine", side_effect=fake_create):
    import app.core.database
print(json.dumps(captured))
"""
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    import json
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["connect_args"].get("command_timeout") == 30
    assert data["connect_args"].get("server_settings", {}).get("statement_timeout") == "30000"


# ---------------------------------------------------------------------------
# 5. Live Scripts Hardening & Supply Chain Pinning & Infra Config Checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script_rel",
    ["scripts/check_live_menu_sync.py", "scripts/test_live_system.py"],
)
def test_live_scripts_have_no_hardcoded_secrets_or_insecure_tls(script_rel):
    """Live scripts must not disable TLS verification or embed hardcoded passwords."""
    content = (ROOT_DIR / script_rel).read_text(encoding="utf-8")
    assert "verify=False" not in content
    assert not re.search(r"\bADMIN_PASS\s*=", content)
    assert "phhthtnkhk" not in content
    assert 'auth=("namanmarket", "shopeefoodapi")' not in content
    assert "UMP_ADMIN_PASSWORD" in content
    assert "UMP_BASE_URL" in content
    assert "UMP_ADMIN_USER" in content

    if script_rel.endswith("check_live_menu_sync.py"):
        assert "SHOPEEFOOD_MENU_PASSWORD" in content
        assert "print(f\"  Config: {ch.get('config')}\")" not in content
        assert "masked" in content


@pytest.mark.parametrize(
    "script_rel",
    ["scripts/check_live_menu_sync.py", "scripts/test_live_system.py"],
)
def test_live_scripts_fail_fast_when_required_env_missing(script_rel):
    """Live scripts must exit non-zero with clear error when required env vars are absent."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"UMP_ADMIN_PASSWORD", "SHOPEEFOOD_MENU_PASSWORD"}
    }
    proc = subprocess.run(
        [sys.executable, str(ROOT_DIR / script_rel)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode != 0
    assert "UMP_ADMIN_PASSWORD" in (proc.stderr + proc.stdout)


def test_python_dependencies_are_strictly_pinned():
    """requirements.txt and pyproject.toml must pin exact == versions including security packages."""
    req_lines = [
        line.strip()
        for line in (ROOT_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(req_lines) >= 18
    for line in req_lines:
        assert "==" in line, f"Unpinned requirement in requirements.txt: {line}"
        assert not any(op in line for op in (">=", "<=", "~=", "!="))

    req_names = {re.split(r"[\[=]", line)[0].lower().replace("-", "_") for line in req_lines}
    for required in ("pymongo", "typing_extensions", "cryptography", "email_validator"):
        assert required in req_names

    pyproject_text = (ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8")
    for pkg in ("pymongo==", "typing_extensions==", "cryptography=="):
        assert pkg in pyproject_text


def test_docker_nginx_alembic_and_hygiene_files():
    """Verify Docker, Compose, Nginx, Alembic, .gitignore, .dockerignore, and .env.example hardening."""
    dockerignore = (ROOT_DIR / ".dockerignore").read_text(encoding="utf-8")
    for entry in (
        ".env",
        ".env.*",
        "data/",
        ".git/",
        ".ag-kit-backups/",
        ".pytest_cache/",
        "__pycache__/",
        "*.rar",
        "*.zip",
        ".claude/",
        ".cursor/",
        ".opencode/",
        "tests/",
    ):
        assert entry in dockerignore

    gitignore = (ROOT_DIR / ".gitignore").read_text(encoding="utf-8")
    assert ".ag-kit-backups/" in gitignore

    env_example = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
    assert "DEBUG=False" in env_example
    assert "PUBLIC_BASE_URL=" in env_example
    assert "SECRETS_ENCRYPTION_KEY=" in env_example
    assert "ssl=require" in env_example
    assert "tls=true" in env_example

    alembic_env = (ROOT_DIR / "alembic" / "env.py").read_text(encoding="utf-8")
    assert '.replace("%", "%%")' in alembic_env

    dockerfile = (ROOT_DIR / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY alembic ./alembic" in dockerfile
    assert "COPY alembic.ini ." in dockerfile
    assert "chmod -R a-w /app/app" in dockerfile
    assert "--proxy-headers" in dockerfile
    assert "--forwarded-allow-ips=127.0.0.1,172.16.0.0/12" in dockerfile

    compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    assert "cap_drop:" in compose
    assert "no-new-privileges:true" in compose
    assert "max-size:" in compose
    assert "healthcheck:" in compose
    assert "mem_limit:" in compose
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        parsed = yaml.safe_load(compose)
        for svc_name in ("app", "nginx"):
            svc = parsed["services"][svc_name]
            assert "ALL" in svc["cap_drop"]
            assert "no-new-privileges:true" in svc["security_opt"]
            assert svc["logging"]["driver"] == "json-file"
        assert "healthcheck" in parsed["services"]["nginx"]

    default_conf = (ROOT_DIR / "docker" / "nginx" / "conf.d" / "default.conf").read_text(encoding="utf-8")
    assert "https://cdn.tailwindcss.com" not in default_conf
    assert "https://cdn.jsdelivr.net" not in default_conf
    assert "'unsafe-eval'" not in default_conf
    assert "connect-src 'self';" in default_conf
    assert "frame-ancestors 'none';" in default_conf
    assert 'X-Frame-Options "DENY"' in default_conf
    assert "set_real_ip_from 127.0.0.1;" in default_conf
    assert "set_real_ip_from 172.16.0.0/12;" in default_conf
    assert "real_ip_header X-Real-IP;" in default_conf
    assert "client_max_body_size 2M;" in default_conf
    assert "location ~ ^/api/v1/haravan/orders/.+/files/upload$" in default_conf
    assert "client_max_body_size 20M;" in default_conf

    host_conf = (ROOT_DIR / "docker" / "nginx" / "host-nginx.conf.example").read_text(encoding="utf-8")
    assert "server_tokens off;" in host_conf
    assert "location ~ /\\. {" in host_conf
    assert "deny all;" in host_conf
    assert "client_max_body_size 2M;" in host_conf
    assert "client_max_body_size 20M;" in host_conf
    assert "add_header X-Frame-Options" not in host_conf
    assert "add_header X-Content-Type-Options" not in host_conf
    for route in (
        "/shopeefoodapi/",
        "/grabmart/",
        "/shopeemart/",
        "/oauth/token",
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/admin/login",
    ):
        assert route in host_conf
