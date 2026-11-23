from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user, get_db, require_staff
from app.core.database import Base
from app.main import app
from app.models.channel import Channel
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.store import Store
from app.models.user import User, UserRole
from app.schemas.order import OrderFilterParams
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


def test_admin_orders_html_route(client, mock_admin_user):
    """GET /admin/orders redirects unauthenticated users and renders HTML when authenticated."""
    unauth = client.get("/admin/orders", follow_redirects=False)
    assert unauth.status_code == 302
    assert "/admin/login" in unauth.headers["location"]

    from unittest.mock import AsyncMock, patch
    with patch("app.web.router._get_authenticated_web_user", new_callable=AsyncMock, return_value=mock_admin_user):
        response = client.get("/admin/orders")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Điều Phối & Xử Lý Đơn Hàng Hợp Nhất" in response.text
    assert "pipeline-stage-tabs" in response.text
    assert "btn-view-table" in response.text
    assert "order-details-modal" in response.text
    assert "detail-modal-status-pipeline" in response.text
    assert "detail-tab-btn-basic" in response.text
    assert "detail-tab-btn-products" in response.text
    assert "detail-tab-btn-history" in response.text
    assert "10001A" in response.text
    assert "10008A" in response.text
    assert "Lưu Ghi Chú Nội Bộ" in response.text


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


async def test_list_orders_grabmart_filter_matches_canonical_grab_channel(client, mock_admin_user):
    """Filtering orders by GRABMART, GRAB, or GRAB_MART matches orders stored on canonical GRAB channel."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            store = Store(id="st-10001", code="10001", name="Nam An Market - 21 Thảo Điền", is_active=True)
            ch_grab = Channel(id="ch-grab", code="GRAB", name="GrabMart Express", is_active=True)
            ch_spf = Channel(id="ch-spf", code="SHOPEEFOOD", name="ShopeeFood VN", is_active=True)
            db.add_all([store, ch_grab, ch_spf])
            await db.flush()

            grab_order = UnifiedOrder(
                id="ord-grab-1",
                order_code="NAM-GM-20261123-0001",
                channel_id=ch_grab.id,
                store_id=store.id,
                channel_order_id="GRAB-A100",
                display_order_id="A100",
                status=UnifiedOrderStatus.PENDING,
                subtotal_amount=200000.0,
                discount_amount=0.0,
                delivery_fee=15000.0,
                total_amount=215000.0,
            )
            spf_order = UnifiedOrder(
                id="ord-spf-1",
                order_code="NAM-SF-20261123-0002",
                channel_id=ch_spf.id,
                store_id=store.id,
                channel_order_id="SPF-B200",
                display_order_id="B200",
                status=UnifiedOrderStatus.PENDING,
                subtotal_amount=100000.0,
                discount_amount=0.0,
                delivery_fee=10000.0,
                total_amount=110000.0,
            )
            db.add_all([grab_order, spf_order])
            await db.commit()

            for code_filter in ("GRABMART", "GRAB", "GRAB_MART", "grabmart"):
                items, total = await OrderService.list_orders(
                    OrderFilterParams(channel_code=code_filter),
                    db,
                )
                assert total == 1
                assert len(items) == 1
                assert items[0].order_code == "NAM-GM-20261123-0001"
                assert items[0].channel.code == "GRAB"

            spf_items, spf_total = await OrderService.list_orders(
                OrderFilterParams(channel_code="SHOPEEFOOD"),
                db,
            )
            assert spf_total == 1
            assert spf_items[0].order_code == "NAM-SF-20261123-0002"
    finally:
        await engine.dispose()

    with patch("app.web.router._get_authenticated_web_user", new_callable=AsyncMock, return_value=mock_admin_user):
        html_res = client.get("/admin/orders")
    assert html_res.status_code == 200
    assert "c === 'GRABMART' || c === 'GRAB' || c === 'GRAB_MART'" in html_res.text
