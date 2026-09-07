import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from app.main import app
from app.api.deps import get_current_user
from app.models.user import User, UserRole
from app.models.inventory import StoreInventory
from app.schemas.inventory import (
    InventoryItemOverview,
    InventoryOverviewSummary,
)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_admin_user():
    return User(
        id="admin-inventory-uuid",
        username="admin",
        email="admin@namanmarket.com",
        full_name="Nam An Admin",
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
        is_superuser=True,
    )


def test_admin_inventory_html_renders(client):
    """Calling /admin/inventory returns HTML 200 OK with correct headings and elements."""
    response = client.get("/admin/inventory")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Quản Lý Thực Đơn &amp; Tồn Kho Đa Kênh" in response.text or "Quản Lý Thực Đơn & Tồn Kho Đa Kênh" in response.text
    assert "Đồng Bộ Tồn Kho Đa Sàn" in response.text
    assert "21 Thảo Điền" in response.text


def test_inventory_alias_html_renders(client):
    """Calling /inventory also renders the inventory management page."""
    response = client.get("/inventory")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "21 Thảo Điền" in response.text


def test_get_inventory_overview_api(client, mock_admin_user):
    """Calling GET /api/v1/inventory/overview returns paginated items and summary."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user

    mock_summary = InventoryOverviewSummary(
        total_skus=16,
        in_stock_count=14,
        out_of_stock_count=2,
        low_stock_count=1,
    )
    mock_items = [
        InventoryItemOverview(
            product_id="prod-1",
            sku="SKU-SALMON-01",
            barcode="8935001001",
            name="Cá Hồi Tươi Na Uy Fillet (500g)",
            unit="Khay",
            base_price=320000.0,
            image_url=None,
            category_id="cat-1",
            category_name="Thịt & Thủy Hải Sản Tươi Sống",
            store_id="store-1",
            store_code="10001",
            store_name="21 Thảo Điền",
            total_stock=25.0,
            reserved_stock=0.0,
            safety_stock=2.0,
            available_stock=23.0,
            is_out_of_stock=False,
            last_synced_at=None,
        )
    ]

    with patch(
        "app.services.inventory_service.InventoryService.get_store_inventory_overview",
        new=AsyncMock(return_value=(mock_items, 1, mock_summary))
    ):
        response = client.get("/api/v1/inventory/overview?store_code=10001")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        items_payload = data["data"]["paginated"]
        summary_payload = data["data"]["summary"]
        assert items_payload["meta"]["total_items"] == 1
        assert len(items_payload["items"]) == 1
        assert items_payload["items"][0]["sku"] == "SKU-SALMON-01"
        assert summary_payload["total_skus"] == 16
        assert summary_payload["in_stock_count"] == 14

    app.dependency_overrides.clear()


def test_toggle_item_status_api(client, mock_admin_user):
    """Calling PATCH /api/v1/inventory/toggle updates stock and dispatches sync."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user

    mock_inv = StoreInventory(
        id="inv-1",
        store_id="store-1",
        product_id="prod-1",
        sku="SKU-SALMON-01",
        total_stock=0.0,
        reserved_stock=0.0,
        safety_stock=2.0,
        available_stock=0.0,
        is_out_of_stock=True,
        last_synced_at=None,
    )

    with patch(
        "app.services.inventory_service.InventoryService.toggle_item_status",
        new=AsyncMock(return_value=(mock_inv, []))
    ):
        payload = {
            "store_code": "10001",
            "sku": "SKU-SALMON-01",
            "is_out_of_stock": True,
            "reason": "Hết cá tươi trong ngày",
        }
        response = client.patch("/api/v1/inventory/toggle", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["inventory"]["sku"] == "SKU-SALMON-01"
        assert data["data"]["inventory"]["is_out_of_stock"] is True

    app.dependency_overrides.clear()


def test_quick_update_stock_api(client, mock_admin_user):
    """Calling PATCH /api/v1/inventory/quick-update updates quantities correctly."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user

    mock_inv = StoreInventory(
        id="inv-1",
        store_id="store-1",
        product_id="prod-1",
        sku="SKU-SALMON-01",
        total_stock=50.0,
        reserved_stock=0.0,
        safety_stock=5.0,
        available_stock=45.0,
        is_out_of_stock=False,
        last_synced_at=None,
    )

    with patch(
        "app.services.inventory_service.InventoryService.quick_update_stock",
        new=AsyncMock(return_value=(mock_inv, []))
    ):
        payload = {
            "store_code": "10001",
            "sku": "SKU-SALMON-01",
            "new_stock": 50.0,
            "reason": "Nhập thêm lô cá hồi mới",
        }
        response = client.patch("/api/v1/inventory/quick-update", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["inventory"]["total_stock"] == 50.0
        assert data["data"]["inventory"]["available_stock"] == 45.0

    app.dependency_overrides.clear()


def test_sync_all_store_inventory_api(client, mock_admin_user):
    """Calling POST /api/v1/inventory/sync-all triggers bulk channel sync."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user

    with patch(
        "app.services.inventory_service.InventoryService.sync_all_store_inventory",
        new=AsyncMock(return_value={"total_synced": 16, "channels": ["SHOPEEFOOD", "GRABMART"]})
    ):
        response = client.post("/api/v1/inventory/sync-all?store_code=10001")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["sync_results"]["total_synced"] == 16

    app.dependency_overrides.clear()
