import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch, MagicMock

from app.main import app
from app.models.user import User, UserRole
from app.models.store import Store
from app.core.security import create_access_token


@pytest.mark.asyncio
async def test_admin_users_html_route():
    """Verify that /admin/users and /users web routes render successfully."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res1 = await ac.get("/admin/users")
        assert res1.status_code == 200
        assert "Quản Lý Người Dùng & Phân Quyền Chi Nhánh" in res1.text

        res2 = await ac.get("/users")
        assert res2.status_code == 200
        assert "Quản Lý Người Dùng & Phân Quyền Chi Nhánh" in res2.text


@pytest.mark.asyncio
async def test_user_response_has_store_properties():
    """Verify that User model exposes store_code and store_name properties."""
    store = Store(id="store-10001", code="10001", name="Nam An Thảo Điền")
    user = User(
        id="user-staff-1",
        username="staff_td",
        email="staff_td@naman.com",
        full_name="Nguyễn Văn Staff",
        role=UserRole.STAFF.value,
        store_id=store.id,
        is_active=True,
    )
    user.store = store

    assert user.store_code == "10001"
    assert user.store_name == "Nam An Thảo Điền"


@pytest.mark.asyncio
async def test_staff_without_store_gets_empty_orders():
    """Staff without an assigned store must strictly receive empty orders and 0 summary."""
    unassigned_staff = User(
        id="user-no-store",
        username="no_store_user",
        email="nostore@naman.com",
        full_name="No Store Staff",
        role=UserRole.STAFF.value,
        store_id=None,
        is_active=True,
        is_superuser=False,
    )

    from app.api.deps import get_current_user
    app.dependency_overrides[get_current_user] = lambda: unassigned_staff

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Orders list
        res_orders = await ac.get("/api/v1/orders")
        assert res_orders.status_code == 200
        data_orders = res_orders.json()
        assert data_orders["success"] is True
        assert data_orders["data"]["items"] == []
        assert data_orders["data"]["meta"]["total_items"] == 0

        # Orders summary
        res_summary = await ac.get("/api/v1/orders/summary")
        assert res_summary.status_code == 200
        data_summary = res_summary.json()
        assert data_summary["success"] is True
        assert data_summary["data"]["pending_count"] == 0
        assert data_summary["data"]["revenue_today"] == 0.0

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_staff_store_isolation_enforced_in_order_list():
    """Staff with store_id A must only query store_id A, ignoring client attempts to override store_code."""
    store_a = Store(id="uuid-store-10001", code="10001", name="Nam An Thảo Điền")
    staff_store_a = User(
        id="user-staff-10001",
        username="staff_10001",
        email="staff10001@naman.com",
        full_name="Staff Thảo Điền",
        role=UserRole.STAFF.value,
        store_id=store_a.id,
        is_active=True,
        is_superuser=False,
    )
    staff_store_a.store = store_a

    from app.api.deps import get_current_user
    app.dependency_overrides[get_current_user] = lambda: staff_store_a

    with patch("app.services.order_service.OrderService.list_orders", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = ([], 0)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Client attempts to query orders of store 10004
            res = await ac.get("/api/v1/orders?store_code=10004")
            assert res.status_code == 200

            # Verify that OrderService received effective_store_id == uuid-store-10001 and store_code == None
            assert mock_list.called
            params_called = mock_list.call_args[0][0]
            assert params_called.store_id == "uuid-store-10001"
            assert params_called.store_code is None

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_staff_cannot_access_order_of_another_store():
    """Staff from store A must receive 403 Forbidden when trying to view an order from store B."""
    store_a = Store(id="uuid-store-10001", code="10001", name="Nam An Thảo Điền")
    staff_store_a = User(
        id="user-staff-10001",
        username="staff_10001",
        email="staff10001@naman.com",
        full_name="Staff Thảo Điền",
        role=UserRole.STAFF.value,
        store_id=store_a.id,
        is_active=True,
        is_superuser=False,
    )
    staff_store_a.store = store_a

    # Mock order belonging to store B (uuid-store-10004)
    order_store_b = MagicMock()
    order_store_b.id = "order-other-branch"
    order_store_b.store_id = "uuid-store-10004"

    from app.api.deps import get_current_user
    app.dependency_overrides[get_current_user] = lambda: staff_store_a

    with patch("app.services.order_service.OrderService.get_order_by_id", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = order_store_b

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/api/v1/orders/order-other-branch")
            assert res.status_code == 403
            assert "không có quyền" in (res.json().get("message") or "")

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_staff_cannot_toggle_inventory_of_another_store():
    """Staff from store 10001 must receive 403 Forbidden when toggling an item in store 10004."""
    store_a = Store(id="uuid-store-10001", code="10001", name="Nam An Thảo Điền")
    staff_store_a = User(
        id="user-staff-10001",
        username="staff_10001",
        email="staff10001@naman.com",
        full_name="Staff Thảo Điền",
        role=UserRole.STAFF.value,
        store_id=store_a.id,
        is_active=True,
        is_superuser=False,
    )
    staff_store_a.store = store_a

    from app.api.deps import get_current_user
    app.dependency_overrides[get_current_user] = lambda: staff_store_a

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {
            "store_code": "10004",  # Different store
            "sku": "BEEF-WAGYU-A5",
            "is_out_of_stock": True
        }
        res = await ac.patch("/api/v1/inventory/toggle", json=payload)
        assert res.status_code == 403
        assert "không có quyền thao tác trên chi nhánh '10004'" in (res.json().get("message") or "")

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_login_page_renders_clean_without_hardcoded_values():
    """Verify login page has no hardcoded values in input elements and includes demo account switcher."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/admin/login")
        assert res.status_code == 200
        # Inputs must be clean (no hardcoded prefilled values)
        assert 'value="admin"' not in res.text
        assert 'value="phhthtnkhk"' not in res.text
        assert 'Mặc định SuperAdmin: admin' not in res.text
        # Must have demo switcher helper
        assert "selectDemoAccount" in res.text
        assert "manager_10001" in res.text


@pytest.mark.asyncio
async def test_seed_sample_staff_endpoint():
    """Verify superadmin can trigger seed-sample-staff endpoint while regular staff gets 403."""
    admin_user = User(
        id="user-superadmin",
        username="admin",
        email="admin@naman.com",
        full_name="Super Administrator",
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
        is_superuser=True,
    )
    staff_user = User(
        id="user-staff",
        username="staff_only",
        email="staff@naman.com",
        full_name="Staff Regular",
        role=UserRole.STAFF.value,
        is_active=True,
        is_superuser=False,
    )

    from app.api.deps import get_current_user

    # 1. Staff is rejected with 403
    app.dependency_overrides[get_current_user] = lambda: staff_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res_denied = await ac.post("/api/v1/users/seed-sample-staff")
        # RoleChecker raises ForbiddenError (403)
        assert res_denied.status_code == 403

    # 2. SuperAdmin succeeds
    app.dependency_overrides[get_current_user] = lambda: admin_user
    with patch("app.services.auth_service.auth_service.seed_branch_staff_users", new_callable=AsyncMock) as mock_seed:
        mock_seed.return_value = [
            {"username": "manager_10001", "role": "STORE_MANAGER", "store_code": "10001"}
        ]
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res_ok = await ac.post("/api/v1/users/seed-sample-staff")
            assert res_ok.status_code == 200
            data = res_ok.json()
            assert data["success"] is True
            assert data["data"]["count"] == 1

    app.dependency_overrides.clear()

