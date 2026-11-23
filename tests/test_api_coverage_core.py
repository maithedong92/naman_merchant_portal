from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import NotFoundError
from app.core.security import hash_password
from app.main import app
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.store import Store, StoreChannelMapping
from app.models.user import AuditSecurityLog, User, UserRole
from app.schemas.auth import TokenResponse
from app.schemas.user import UserResponse
from app.services.arito_service import AritoInventoryService

settings = get_settings()


# ==============================================================================
# Shared Fixtures
# ==============================================================================


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin_user():
    return User(
        id="admin-uuid-001",
        username="superadmin",
        email="superadmin@namanmarket.com",
        full_name="Nam An Super Admin",
        role=UserRole.SUPER_ADMIN.value,
        store_id=None,
        is_active=True,
        is_superuser=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def manager_user():
    return User(
        id="mgr-uuid-10001",
        username="mgr_10001",
        email="mgr10001@namanmarket.com",
        full_name="Store Manager 10001",
        role=UserRole.STORE_MANAGER.value,
        store_id="st-10001",
        is_active=True,
        is_superuser=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def staff_user():
    return User(
        id="staff-uuid-10001",
        username="staff_10001",
        email="staff10001@namanmarket.com",
        full_name="Staff 10001",
        role=UserRole.STAFF.value,
        store_id="st-10001",
        is_active=True,
        is_superuser=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _mock_db_dependency():
    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()
    mock_db.add = MagicMock()
    return mock_db


# ==============================================================================
# 1. Auth Endpoints & Failed Login Lockout
# ==============================================================================


def test_auth_login_returns_tokens_and_sets_cookie(client, admin_user):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_db] = lambda: mock_db

    token_resp = TokenResponse(
        access_token="access.jwt.token",
        refresh_token="refresh.jwt.token",
        token_type="bearer",
        expires_in=1800,
        user=UserResponse.model_validate(admin_user),
    )

    with patch(
        "app.api.v1.auth.auth_service.authenticate_user",
        new=AsyncMock(return_value=(admin_user, token_resp)),
    ) as mock_auth:
        res = client.post(
            "/api/v1/auth/login",
            json={"username": "superadmin", "password": "ValidPassword123!"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["access_token"] == "access.jwt.token"
    assert body["data"]["refresh_token"] == "refresh.jwt.token"
    assert res.cookies.get("naman_access_token") == "access.jwt.token"
    mock_auth.assert_awaited_once()


def test_auth_refresh_rotates_tokens_and_updates_cookie(client, admin_user):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_db] = lambda: mock_db

    token_resp = TokenResponse(
        access_token="new.access.token",
        refresh_token="new.refresh.token",
        token_type="bearer",
        expires_in=1800,
        user=UserResponse.model_validate(admin_user),
    )

    with patch(
        "app.api.v1.auth.auth_service.refresh_access_token",
        new=AsyncMock(return_value=token_resp),
    ) as mock_refresh:
        res = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "old.refresh.token"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["access_token"] == "new.access.token"
    assert body["data"]["refresh_token"] == "new.refresh.token"
    assert res.cookies.get("naman_access_token") == "new.access.token"
    mock_refresh.assert_awaited_once()


def test_auth_logout_revokes_token_and_clears_cookie(client, staff_user):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_current_user] = lambda: staff_user
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch(
        "app.api.v1.auth.auth_service.logout_user",
        new=AsyncMock(return_value=None),
    ) as mock_logout:
        res = client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": "refresh.to.revoke"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["revoked"] is True
    set_cookie_header = res.headers.get("set-cookie", "")
    assert "naman_access_token=" in set_cookie_header
    mock_logout.assert_awaited_once()


def test_auth_change_password_success(client, staff_user):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_current_user] = lambda: staff_user
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch(
        "app.api.v1.auth.auth_service.change_password",
        new=AsyncMock(return_value=None),
    ) as mock_change:
        res = client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "OldPassword123!",
                "new_password": "NewPassword456!",
            },
        )

    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["updated"] is True
    mock_change.assert_awaited_once()


def test_auth_failed_login_triggers_lockout_and_blocks_subsequent_login(client):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_db] = lambda: mock_db

    locked_candidate = User(
        id="user-lock-1",
        username="lock_tester",
        email="lock@namanmarket.com",
        full_name="Lock Tester",
        role=UserRole.STAFF.value,
        store_id="st-10001",
        hashed_password=hash_password("CorrectPass123!"),
        is_active=True,
        is_superuser=False,
        failed_login_attempts=settings.MAX_FAILED_LOGIN_ATTEMPTS - 1,
        locked_until=None,
    )

    mock_exec_res = MagicMock()
    mock_exec_res.scalar_one_or_none.return_value = locked_candidate
    mock_db.execute = AsyncMock(return_value=mock_exec_res)

    with (
        patch("app.services.auth_service.settings.AUTH_MONGO_ENABLED", False),
        patch("app.services.auth_service.auth_service.log_security_event", new=AsyncMock()),
    ):
        # Attempt hitting MAX_FAILED_LOGIN_ATTEMPTS locks the account
        res_fail = client.post(
            "/api/v1/auth/login",
            json={"username": "lock_tester", "password": "WrongPassword!"},
        )
        assert res_fail.status_code == 401
        assert res_fail.json()["error_code"] == "UNAUTHORIZED"
        assert locked_candidate.failed_login_attempts == settings.MAX_FAILED_LOGIN_ATTEMPTS
        assert locked_candidate.locked_until is not None
        assert locked_candidate.locked_until > datetime.now(timezone.utc)

        # Subsequent attempt even with correct password is blocked while locked
        res_blocked = client.post(
            "/api/v1/auth/login",
            json={"username": "lock_tester", "password": "CorrectPass123!"},
        )
        assert res_blocked.status_code == 401
        assert "tạm khóa" in res_blocked.json()["message"].lower()


# ==============================================================================
# 2. Users Management & RBAC
# ==============================================================================


def test_users_create_admin_success_and_staff_forbidden(client, admin_user, staff_user):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_db] = lambda: mock_db

    payload = {
        "username": "new_staff",
        "email": "new_staff@namanmarket.com",
        "full_name": "New Staff Member",
        "role": "STAFF",
        "store_id": "st-10001",
        "password": "StrongPassword123!",
    }

    # Staff -> 403
    app.dependency_overrides[get_current_user] = lambda: staff_user
    res_forbidden = client.post("/api/v1/users", json=payload)
    assert res_forbidden.status_code == 403
    assert res_forbidden.json()["error_code"] == "FORBIDDEN"

    # SuperAdmin -> 200
    created_user = User(
        id="user-created-1",
        username="new_staff",
        email="new_staff@namanmarket.com",
        full_name="New Staff Member",
        role=UserRole.STAFF.value,
        store_id="st-10001",
        is_active=True,
        is_superuser=False,
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    with patch(
        "app.api.v1.users.auth_service.create_user",
        new=AsyncMock(return_value=created_user),
    ):
        res_ok = client.post("/api/v1/users", json=payload)

    assert res_ok.status_code == 200
    body = res_ok.json()
    assert body["success"] is True
    assert body["data"]["username"] == "new_staff"


def test_users_security_logs_rbac_and_success(client, admin_user, staff_user):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_db] = lambda: mock_db

    app.dependency_overrides[get_current_user] = lambda: staff_user
    assert client.get("/api/v1/users/security-logs").status_code == 403

    log_entry = AuditSecurityLog(
        id="log-1",
        user_id=admin_user.id,
        username=admin_user.username,
        event_type="LOGIN_SUCCESS",
        ip_address="127.0.0.1",
        user_agent="pytest",
        details='{"ok": true}',
        created_at=datetime.now(timezone.utc),
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    with patch(
        "app.api.v1.users.auth_service.list_security_logs",
        new=AsyncMock(return_value=[log_entry]),
    ):
        res = client.get("/api/v1/users/security-logs")

    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert len(body["data"]) == 1
    assert body["data"][0]["event_type"] == "LOGIN_SUCCESS"


def test_users_get_update_delete_rbac_and_success(client, admin_user, staff_user):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_db] = lambda: mock_db

    target_user = User(
        id="target-user-1",
        username="target_staff",
        email="target@namanmarket.com",
        full_name="Target Staff",
        role=UserRole.STAFF.value,
        store_id="st-10001",
        is_active=True,
        is_superuser=False,
    )

    # Staff blocked on GET/PUT/DELETE
    app.dependency_overrides[get_current_user] = lambda: staff_user
    assert client.get("/api/v1/users/target-user-1").status_code == 403
    assert client.put("/api/v1/users/target-user-1", json={"full_name": "Updated"}).status_code == 403
    assert client.delete("/api/v1/users/target-user-1").status_code == 403

    # Admin GET detail (success & 404)
    app.dependency_overrides[get_current_user] = lambda: admin_user
    with patch(
        "app.api.v1.users.auth_service.get_user_by_id",
        new=AsyncMock(return_value=target_user),
    ):
        res_get = client.get("/api/v1/users/target-user-1")
    assert res_get.status_code == 200
    assert res_get.json()["data"]["id"] == "target-user-1"

    with patch(
        "app.api.v1.users.auth_service.get_user_by_id",
        new=AsyncMock(side_effect=NotFoundError("User", "missing-id")),
    ):
        res_404 = client.get("/api/v1/users/missing-id")
    assert res_404.status_code == 404

    # Admin PUT update
    updated_user = User(
        id="target-user-1",
        username="target_staff",
        email="target@namanmarket.com",
        full_name="Updated Staff Name",
        role=UserRole.STORE_MANAGER.value,
        store_id="st-10001",
        is_active=True,
        is_superuser=False,
    )
    with patch(
        "app.api.v1.users.auth_service.update_user",
        new=AsyncMock(return_value=updated_user),
    ):
        res_put = client.put(
            "/api/v1/users/target-user-1",
            json={"full_name": "Updated Staff Name", "role": "STORE_MANAGER"},
        )
    assert res_put.status_code == 200
    assert res_put.json()["data"]["role"] == "STORE_MANAGER"

    # Admin DELETE deactivate
    with patch(
        "app.api.v1.users.auth_service.update_user",
        new=AsyncMock(return_value=updated_user),
    ) as mock_deactivate:
        res_del = client.delete("/api/v1/users/target-user-1")
    assert res_del.status_code == 200
    assert res_del.json()["data"]["deactivated"] is True
    assert mock_deactivate.await_args.kwargs["user_in"].is_active is False


# ==============================================================================
# 3. Stores & Store Channel Mappings
# ==============================================================================


def test_stores_list_and_create(client, admin_user, staff_user):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_db] = lambda: mock_db

    now = datetime.now(timezone.utc)
    store_obj = Store(
        id="st-10001",
        code="10001",
        name="Nam An Thảo Điền",
        address="21 Thảo Điền",
        phone="0901000001",
        is_active=True,
        created_at=now,
        updated_at=now,
        channel_mappings=[],
    )

    # GET /api/v1/stores
    list_res = MagicMock()
    list_res.scalars.return_value.all.return_value = [store_obj]
    mock_db.execute = AsyncMock(return_value=list_res)

    app.dependency_overrides[get_current_user] = lambda: staff_user
    res_list = client.get("/api/v1/stores")
    assert res_list.status_code == 200
    assert len(res_list.json()["data"]) == 1
    assert res_list.json()["data"][0]["code"] == "10001"

    # POST /api/v1/stores -> staff forbidden
    store_payload = {"code": "10009", "name": "Nam An Mới", "is_active": True}
    assert client.post("/api/v1/stores", json=store_payload).status_code == 403

    # POST /api/v1/stores -> admin conflict when code exists
    app.dependency_overrides[get_current_user] = lambda: admin_user
    existing_res = MagicMock()
    existing_res.scalar_one_or_none.return_value = store_obj
    mock_db.execute = AsyncMock(return_value=existing_res)
    res_conflict = client.post("/api/v1/stores", json=store_payload)
    assert res_conflict.status_code == 409

    # POST /api/v1/stores -> admin creates new store
    not_found_res = MagicMock()
    not_found_res.scalar_one_or_none.return_value = None
    created_res = MagicMock()
    created_res.scalar_one.return_value = Store(
        id="st-10009",
        code="10009",
        name="Nam An Mới",
        is_active=True,
        created_at=now,
        updated_at=now,
        channel_mappings=[],
    )
    mock_db.execute = AsyncMock(side_effect=[not_found_res, created_res])

    res_create = client.post("/api/v1/stores", json=store_payload)
    assert res_create.status_code == 201
    assert res_create.json()["data"]["code"] == "10009"


def test_stores_get_detail_and_map_channel(client, admin_user, staff_user):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_db] = lambda: mock_db

    now = datetime.now(timezone.utc)
    store_10001 = Store(
        id="st-10001",
        code="10001",
        name="Nam An Thảo Điền",
        is_active=True,
        created_at=now,
        updated_at=now,
        channel_mappings=[],
    )
    store_10004 = Store(
        id="st-10004",
        code="10004",
        name="Nam An Nguyễn Thị Minh Khai",
        is_active=True,
        created_at=now,
        updated_at=now,
        channel_mappings=[],
    )

    # Staff can view own store
    res_own = MagicMock()
    res_own.scalar_one_or_none.return_value = store_10001
    mock_db.execute = AsyncMock(return_value=res_own)
    app.dependency_overrides[get_current_user] = lambda: staff_user
    assert client.get("/api/v1/stores/st-10001").status_code == 200

    # Staff cannot view other store -> 403
    res_other = MagicMock()
    res_other.scalar_one_or_none.return_value = store_10004
    mock_db.execute = AsyncMock(return_value=res_other)
    assert client.get("/api/v1/stores/st-10004").status_code == 403

    # Missing store -> 404
    res_missing = MagicMock()
    res_missing.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=res_missing)
    assert client.get("/api/v1/stores/unknown").status_code == 404

    # POST /api/v1/stores/{store_id}/channels -> staff 403
    channel_payload = {
        "channel_id": "ch-shopeefood",
        "partner_store_id": "10001",
        "is_active": True,
    }
    assert client.post("/api/v1/stores/st-10001/channels", json=channel_payload).status_code == 403

    # Admin -> 201 on new mapping
    app.dependency_overrides[get_current_user] = lambda: admin_user
    store_found = MagicMock()
    store_found.scalar_one_or_none.return_value = store_10001
    no_dup = MagicMock()
    no_dup.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(side_effect=[store_found, no_dup])

    async def fake_refresh(obj):
        obj.id = "map-1"
        obj.created_at = now
        obj.updated_at = now

    mock_db.refresh = AsyncMock(side_effect=fake_refresh)
    res_map = client.post("/api/v1/stores/st-10001/channels", json=channel_payload)
    assert res_map.status_code == 201
    assert res_map.json()["data"]["partner_store_id"] == "10001"


# ==============================================================================
# 4. Orders Status Transition & Cross-Store RBAC
# ==============================================================================


def test_orders_update_status_valid_invalid_and_cross_store(client, staff_user):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: staff_user

    now = datetime.now(timezone.utc)
    order_store_10001 = UnifiedOrder(
        id="ord-1",
        order_code="SPF-10001-01",
        channel_order_id="ext-1",
        channel_id="ch-spf",
        store_id="st-10001",
        status=UnifiedOrderStatus.ACCEPTED,
        subtotal_amount=100000,
        discount_amount=0,
        delivery_fee=0,
        total_amount=100000,
        created_at=now,
        updated_at=now,
        items=[],
        status_history=[],
    )
    order_store_10004 = UnifiedOrder(
        id="ord-2",
        order_code="SPF-10004-01",
        channel_order_id="ext-2",
        channel_id="ch-spf",
        store_id="st-10004",
        status=UnifiedOrderStatus.ACCEPTED,
        subtotal_amount=50000,
        discount_amount=0,
        delivery_fee=0,
        total_amount=50000,
        created_at=now,
        updated_at=now,
        items=[],
        status_history=[],
    )

    # Cross-store 403
    with patch(
        "app.api.v1.orders.OrderService.get_order_by_id",
        new=AsyncMock(return_value=order_store_10004),
    ):
        res_403 = client.patch(
            "/api/v1/orders/ord-2/status",
            json={"new_status": "PREPARING"},
        )
    assert res_403.status_code == 403
    assert res_403.json()["error_code"] == "FORBIDDEN"

    # Invalid transition (DELIVERED -> PREPARING) -> 422 VALIDATION_ERROR
    delivered_order = UnifiedOrder(
        id="ord-3",
        order_code="SPF-10001-03",
        channel_order_id="ext-3",
        channel_id="ch-spf",
        store_id="st-10001",
        status=UnifiedOrderStatus.DELIVERED,
        subtotal_amount=50000,
        discount_amount=0,
        delivery_fee=0,
        total_amount=50000,
        created_at=now,
        updated_at=now,
        items=[],
        status_history=[],
    )
    delivered_res = MagicMock()
    delivered_res.scalar_one_or_none.return_value = delivered_order
    mock_db.execute = AsyncMock(return_value=delivered_res)
    with patch(
        "app.api.v1.orders.OrderService.get_order_by_id",
        new=AsyncMock(return_value=delivered_order),
    ):
        res_422 = client.patch(
            "/api/v1/orders/ord-3/status",
            json={"new_status": "PREPARING"},
        )
    assert res_422.status_code == 422
    assert res_422.json()["error_code"] == "VALIDATION_ERROR"

    # Valid transition (ACCEPTED -> PREPARING) -> 200
    updated_order = UnifiedOrder(
        id="ord-1",
        order_code="SPF-10001-01",
        channel_order_id="ext-1",
        channel_id="ch-spf",
        store_id="st-10001",
        status=UnifiedOrderStatus.PREPARING,
        subtotal_amount=100000,
        discount_amount=0,
        delivery_fee=0,
        total_amount=100000,
        created_at=now,
        updated_at=now,
        items=[],
        status_history=[],
    )
    with (
        patch(
            "app.api.v1.orders.OrderService.get_order_by_id",
            new=AsyncMock(return_value=order_store_10001),
        ),
        patch(
            "app.api.v1.orders.OrderService.update_order_status",
            new=AsyncMock(return_value=updated_order),
        ) as mock_update,
    ):
        res_ok = client.patch(
            "/api/v1/orders/ord-1/status",
            json={"new_status": "PREPARING", "note": "Packing items"},
        )
    assert res_ok.status_code == 200
    assert res_ok.json()["data"]["status"] == "PREPARING"
    # Audit trail must record authenticated user's username
    passed_payload = mock_update.await_args.args[1]
    assert passed_payload.changed_by == staff_user.username


# ==============================================================================
# 5. Inventory Poppies ERP Endpoints
# ==============================================================================


def test_inventory_poppies_health_and_sync_endpoints(client, admin_user, manager_user, staff_user):
    mock_db = _mock_db_dependency()
    app.dependency_overrides[get_db] = lambda: mock_db

    # GET /api/v1/inventory/poppies/health
    app.dependency_overrides[get_current_user] = lambda: staff_user
    with patch(
        "app.api.v1.inventory.poppies_service.check_health",
        new=AsyncMock(return_value={"status": "healthy", "total_skus": 5200}),
    ):
        res_health = client.get("/api/v1/inventory/poppies/health")
    assert res_health.status_code == 200
    assert res_health.json()["data"]["status"] == "healthy"

    # POST /api/v1/inventory/poppies/sync -> staff forbidden
    assert client.post("/api/v1/inventory/poppies/sync?branch_code=10001").status_code == 403

    # Branch manager of 10001 cannot trigger chain-wide master sync (requires Head Office or SuperAdmin)
    app.dependency_overrides[get_current_user] = lambda: manager_user
    assert client.post("/api/v1/inventory/poppies/sync?branch_code=10004").status_code == 403
    assert client.post("/api/v1/inventory/poppies/sync?branch_code=10001").status_code == 403

    # Branch manager of 10001 CAN trigger branch-scoped sync-stock?store_code=10001 once Store resolves
    store_res = MagicMock()
    store_res.scalar_one_or_none.return_value = Store(id="st-10001", code="10001", name="Nam An 10001")
    mock_db.execute = AsyncMock(return_value=store_res)
    with patch(
        "app.api.v1.inventory.poppies_service.sync_stock_cttk_realtime",
        new=AsyncMock(return_value={"inventories_updated": 120}),
    ):
        res_branch_stock = client.post("/api/v1/inventory/poppies/sync-stock?store_code=10001")
    assert res_branch_stock.status_code == 200
    assert res_branch_stock.json()["data"]["inventories_updated"] == 120

    # SuperAdmin can trigger chain-wide master sync & global sync-stock
    app.dependency_overrides[get_current_user] = lambda: admin_user
    with patch(
        "app.api.v1.inventory.poppies_service.sync_master_data_from_poppies",
        new=AsyncMock(return_value={"total_fetched": 120, "branch_code": "10001"}),
    ):
        res_sync = client.post("/api/v1/inventory/poppies/sync?branch_code=10001")
    assert res_sync.status_code == 200
    assert res_sync.json()["data"]["total_fetched"] == 120

    # Branch manager cannot trigger global sync-stock (store_code=None)
    app.dependency_overrides[get_current_user] = lambda: manager_user
    res_global_denied = client.post("/api/v1/inventory/poppies/sync-stock")
    assert res_global_denied.status_code == 403

    # SuperAdmin can trigger global sync-stock
    app.dependency_overrides[get_current_user] = lambda: admin_user
    with patch(
        "app.api.v1.inventory.poppies_service.sync_stock_cttk_realtime",
        new=AsyncMock(return_value={"inventories_updated": 450}),
    ):
        res_stock = client.post("/api/v1/inventory/poppies/sync-stock")
    assert res_stock.status_code == 200
    assert res_stock.json()["data"]["inventories_updated"] == 450


# ==============================================================================
# 6. Haravan Arito Status & AritoService Error Handling
# ==============================================================================


def test_haravan_arito_status_rbac_and_success(client, admin_user, staff_user):
    app.dependency_overrides[get_current_user] = lambda: staff_user
    assert client.get("/api/v1/haravan/arito-status").status_code == 403

    app.dependency_overrides[get_current_user] = lambda: admin_user
    with patch(
        "app.api.v1.haravan.arito_service.check_connection",
        return_value={
            "connected": True,
            "database": "arito_database",
            "collection": "cttk",
            "total_records": 999,
            "message": "OK",
        },
    ):
        res = client.get("/api/v1/haravan/arito-status")
    assert res.status_code == 200
    assert res.json()["data"]["connected"] is True
    assert res.json()["data"]["total_records"] == 999


@pytest.mark.asyncio
async def test_arito_service_check_connection_and_inventory_error_handling():
    service = AritoInventoryService()

    # check_connection success
    mock_col = MagicMock()
    mock_col.estimated_document_count.return_value = 1234
    with patch.object(service, "_get_cttk_collection", return_value=mock_col):
        ok_res = service.check_connection()
    assert ok_res["connected"] is True
    assert ok_res["total_records"] == 1234

    # check_connection failure
    with patch.object(service, "_get_cttk_collection", side_effect=RuntimeError("Mongo unreachable")):
        err_res = service.check_connection()
    assert err_res["connected"] is False
    assert err_res["total_records"] == 0

    # get_inventory_by_skus empty input
    assert await service.get_inventory_by_skus([]) == {}

    # get_inventory_by_skus aggregation success (normalizes 10001A -> 10001)
    mock_col.aggregate.return_value = [
        {"_id": {"ma_vt": "SKU001", "ma_vi_tri": "10001A"}, "total_qty": 15.0}
    ]
    with patch.object(service, "_get_cttk_collection", return_value=mock_col):
        inv = await service.get_inventory_by_skus(["SKU001"])
    assert inv["SKU001"]["branches"]["10001"]["stock"] == 15.0
    assert inv["SKU001"]["total_stock_all_branches"] == 15.0

    # get_inventory_by_skus error handling returns {}
    mock_col.aggregate.side_effect = RuntimeError("Aggregation timeout")
    with patch.object(service, "_get_cttk_collection", return_value=mock_col):
        inv_err = await service.get_inventory_by_skus(["SKU001"])
    assert inv_err == {}
