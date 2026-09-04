from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.api.deps import get_current_user, get_db
from app.models.user import User, UserRole
from app.models.operational_error import OperationalErrorLog, ErrorSeverity, ErrorStatus
from app.services.error_service import sanitize_payload, error_service


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_admin_user():
    return User(
        id="admin-uuid-1",
        username="admin",
        email="admin@namanmarket.com",
        full_name="Nam An Admin",
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
        is_superuser=True,
    )


def test_sanitize_payload_redacts_secrets():
    raw_payload = {
        "user": "test_user",
        "password": "SuperSecretPassword123",
        "token": "bearer_secret_xyz",
        "nested": {
            "api_key": "raw_secret_key",
            "quantity": 10
        }
    }
    cleaned = sanitize_payload(raw_payload)
    assert cleaned["user"] == "test_user"
    assert "[REDACTED]" in cleaned["password"]
    assert "[REDACTED]" in cleaned["token"]
    assert "[REDACTED]" in cleaned["nested"]["api_key"]
    assert cleaned["nested"]["quantity"] == 10


def test_admin_dashboard_html_renders(client):
    """Calling /admin returns HTML with 200 OK."""
    response = client.get("/admin")
    assert response.status_code == 200
    assert "Nam An Market" in response.text
    assert "text/html" in response.headers["content-type"]


def test_system_status_html_renders(client):
    """Calling /system-status returns HTML with 200 OK."""
    response = client.get("/system-status")
    assert response.status_code == 200
    assert "System Status &amp; Nhật Ký Lỗi" in response.text or "System Status" in response.text
    assert "text/html" in response.headers["content-type"]


def test_admin_login_html_renders(client):
    """Calling /admin/login returns HTML with 200 OK."""
    response = client.get("/admin/login")
    assert response.status_code == 200
    assert "Đăng nhập Quản Trị" in response.text
    assert "text/html" in response.headers["content-type"]


def test_root_redirects_to_admin(client):
    """Calling / redirects to /admin."""
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin"


def test_get_system_health_endpoint(client):
    """GET /api/v1/system/health returns healthy status response."""
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock()
    # Mock scalars for SELECT 1 and counts
    mock_res = MagicMock()
    mock_res.scalar.return_value = 0
    mock_db.execute.return_value = mock_res

    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        response = client.get("/api/v1/system/health")
        assert response.status_code == 200
        json_data = response.json()
        assert json_data["success"] is True
        assert "status" in json_data["data"]
        assert "components" in json_data["data"]
        assert "error_summary" in json_data["data"]
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_list_operational_errors_endpoint(client, mock_admin_user):
    """GET /api/v1/system/errors returns error list."""
    mock_db = AsyncMock()
    mock_scalars = MagicMock()
    sample_error = OperationalErrorLog(
        id="err-uuid-1",
        error_code="SHOPEEFOOD_TIMEOUT",
        severity=ErrorSeverity.ERROR.value,
        module="SHOPEEFOOD",
        message="Timeout after 15s",
        resolution_status=ErrorStatus.OPEN.value,
        occurrence_count=1,
    )
    mock_scalars.all.return_value = [sample_error]
    mock_res = MagicMock()
    mock_res.scalars.return_value = mock_scalars
    mock_res.scalar.return_value = 1
    mock_db.execute.return_value = mock_res

    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        response = client.get("/api/v1/system/errors")
        assert response.status_code == 200
        json_data = response.json()
        assert json_data["success"] is True
        assert len(json_data["data"]["items"]) == 1
        assert json_data["data"]["items"][0]["error_code"] == "SHOPEEFOOD_TIMEOUT"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def test_resolve_operational_error_endpoint(client, mock_admin_user):
    """POST /api/v1/system/errors/{id}/resolve marks incident resolved."""
    mock_db = AsyncMock()
    sample_error = OperationalErrorLog(
        id="err-uuid-1",
        error_code="SHOPEEFOOD_TIMEOUT",
        severity=ErrorSeverity.ERROR.value,
        module="SHOPEEFOOD",
        message="Timeout after 15s",
        resolution_status=ErrorStatus.OPEN.value,
        occurrence_count=1,
    )
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = sample_error
    mock_db.execute.return_value = mock_res
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        response = client.post(
            "/api/v1/system/errors/err-uuid-1/resolve",
            json={
                "resolution_notes": "Restarted adapter and manually resynced menu.",
                "resolved_by": "Mai The Dong"
            }
        )
        assert response.status_code == 200
        json_data = response.json()
        assert json_data["success"] is True
        assert json_data["data"]["resolution_status"] == ErrorStatus.RESOLVED.value
        assert json_data["data"]["resolved_by"] == "Mai The Dong"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
