from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_current_user, get_db, require_staff
from app.main import app
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.user import User, UserRole
from app.services.order_service import OrderService


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_staff_user():
    return User(
        id="staff-uuid-1",
        username="staff_thao_dien",
        email="thao_dien@namanmarket.com",
        role=UserRole.STAFF.value,
        store_id="store-10001",
        is_active=True,
        is_superuser=False,
    )


@pytest.fixture
def mock_admin_user():
    return User(
        id="admin-uuid-1",
        username="admin",
        email="admin@namanmarket.com",
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
        is_superuser=True,
    )


def test_admin_orders_html_route(client):
    """GET /admin/orders renders HTML template."""
    response = client.get("/admin/orders")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Điều Phối & Xử Lý Đơn Hàng Hợp Nhất" in response.text
    assert "80mm" in response.text


def test_get_order_summary_endpoint(client, mock_admin_user):
    """GET /api/v1/orders/summary returns counter dictionary."""
    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db

    fake_summary = {
        "total_orders": 25,
        "pending_count": 3,
        "preparing_count": 5,
        "ready_count": 2,
        "delivering_count": 4,
        "delivered_today_count": 10,
        "cancelled_today_count": 1,
        "revenue_today": 3500000.0,
    }

    try:
        with patch.object(OrderService, "get_order_summary", AsyncMock(return_value=fake_summary)):
            res = client.get("/api/v1/orders/summary")
            assert res.status_code == 200
            data = res.json()
            assert data["success"] is True
            assert data["data"]["pending_count"] == 3
            assert data["data"]["revenue_today"] == 3500000.0
    finally:
        app.dependency_overrides.clear()


def test_list_orders_endpoint(client, mock_admin_user):
    """GET /api/v1/orders returns paginated orders with channel and store details."""
    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_db] = lambda: mock_db

    fake_order = UnifiedOrder(
        id="ord-1",
        order_code="NAM-202609-001",
        channel_id="ch-spf",
        store_id="store-10001",
        channel_order_id="SPF-999111",
        display_order_id="999111",
        status=UnifiedOrderStatus.PENDING,
        subtotal_amount=150000.0,
        discount_amount=0.0,
        delivery_fee=20000.0,
        total_amount=170000.0,
        customer_name="Nguyễn Văn B",
        customer_phone="0911222333",
        delivery_address="46 Hưng Phúc, Q7, TP.HCM",
        items=[],
        status_history=[],
    )

    try:
        with patch.object(OrderService, "list_orders", AsyncMock(return_value=([fake_order], 1))):
            res = client.get("/api/v1/orders?channel_code=SHOPEEFOOD&status=PENDING")
            assert res.status_code == 200
            data = res.json()
            assert data["success"] is True
            assert len(data["data"]["items"]) == 1
            o = data["data"]["items"][0]
            assert o["order_code"] == "NAM-202609-001"
            assert o["status"] == "PENDING"
            assert o["total_amount"] == 170000.0
    finally:
        app.dependency_overrides.clear()
