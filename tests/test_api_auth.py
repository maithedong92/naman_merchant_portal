from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.api.deps import get_current_user, get_db
from app.models.user import User, UserRole
from app.core.security import create_access_token


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


@pytest.fixture
def mock_staff_user():
    return User(
        id="staff-uuid-1",
        username="staff_thaodien",
        email="staff@namanmarket.com",
        full_name="Staff Thảo Điền",
        role=UserRole.STAFF.value,
        store_id="store-thaodien-id",
        is_active=True,
        is_superuser=False,
    )


def test_unauthenticated_access_me_fails(client):
    """Calling /api/v1/auth/me without token should return 401 Unauthorized."""
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    json_data = response.json()
    assert json_data["success"] is False
    assert json_data["error_code"] == "UNAUTHORIZED"


def test_invalid_token_fails(client):
    """Calling with bogus Bearer token should return 401 Unauthorized."""
    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer totally_fake_token_here"}
    )
    assert response.status_code == 401
    json_data = response.json()
    assert json_data["success"] is False


def test_authenticated_me_succeeds(client, mock_staff_user):
    """With valid current_user injected, /api/v1/auth/me returns user profile."""
    app.dependency_overrides[get_current_user] = lambda: mock_staff_user
    try:
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 200
        json_data = response.json()
        assert json_data["success"] is True
        assert json_data["data"]["username"] == "staff_thaodien"
        assert json_data["data"]["role"] == UserRole.STAFF.value
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_rbac_staff_cannot_access_user_management(client, mock_staff_user):
    """Staff role cannot access /api/v1/users (SUPER_ADMIN only) -> returns 403 Forbidden."""
    app.dependency_overrides[get_current_user] = lambda: mock_staff_user
    try:
        response = client.get("/api/v1/users")
        assert response.status_code == 403
        json_data = response.json()
        assert json_data["success"] is False
        assert json_data["error_code"] == "FORBIDDEN"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_rbac_admin_can_access_user_management(client, mock_admin_user):
    """SuperAdmin role can access /api/v1/users."""
    # Mock DB query to avoid database dependency
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [mock_admin_user]
    mock_res = MagicMock()
    mock_res.scalars.return_value = mock_scalars
    mock_res.scalar.return_value = 1
    mock_db.execute.return_value = mock_res

    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        response = client.get("/api/v1/users")
        assert response.status_code == 200
        json_data = response.json()
        assert json_data["success"] is True
        assert len(json_data["data"]["items"]) == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def test_store_access_guard_denies_other_store(client, mock_staff_user):
    """Staff belonging to store-thaodien-id cannot access inventory of store-anphu-id."""
    mock_db = AsyncMock()
    mock_store = MagicMock()
    mock_store.id = "store-anphu-id"
    mock_store.code = "10002"
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = mock_store
    mock_db.execute.return_value = mock_res

    app.dependency_overrides[get_current_user] = lambda: mock_staff_user
    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        response = client.get("/api/v1/inventory/stores/10002")
        assert response.status_code == 403
        json_data = response.json()
        assert json_data["success"] is False
        assert json_data["error_code"] == "FORBIDDEN"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def test_store_access_guard_allows_own_store(client, mock_staff_user):
    """Staff belonging to store-thaodien-id can access inventory of store-thaodien-id."""
    mock_db = AsyncMock()
    mock_store = MagicMock()
    mock_store.id = "store-thaodien-id"
    mock_store.code = "10001"
    
    mock_res_store = MagicMock()
    mock_res_store.scalar_one_or_none.return_value = mock_store
    
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_res_inv = MagicMock()
    mock_res_inv.scalars.return_value = mock_scalars

    mock_db.execute.side_effect = [mock_res_store, mock_res_inv]

    app.dependency_overrides[get_current_user] = lambda: mock_staff_user
    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        response = client.get("/api/v1/inventory/stores/10001")
        assert response.status_code == 200
        json_data = response.json()
        assert json_data["success"] is True
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def test_login_endpoint(client, mock_admin_user, monkeypatch):
    """Calling /api/v1/auth/login returns token response."""
    from app.schemas.auth import TokenResponse
    from app.schemas.user import UserResponse
    from app.services.auth_service import auth_service

    token_mock = TokenResponse(
        access_token="mock_access_token_xyz",
        refresh_token="mock_refresh_token_xyz",
        token_type="bearer",
        expires_in=3600,
        user=UserResponse.model_validate(mock_admin_user)
    )

    async def mock_authenticate(*args, **kwargs):
        return mock_admin_user, token_mock

    monkeypatch.setattr(auth_service, "authenticate_user", mock_authenticate)

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "NamAn@2024Admin!"}
    )
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["success"] is True
    assert json_data["data"]["access_token"] == "mock_access_token_xyz"
    assert json_data["data"]["user"]["username"] == "admin"

