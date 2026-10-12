from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from httpx import Response
from starlette.testclient import TestClient

from app.api.deps import get_current_user, require_store_manager, require_super_admin
from app.main import app
from app.models.user import User, UserRole
from app.modules.haravan.client import HaravanClient
from app.modules.haravan.sync_worker import HaravanSyncWorker
from app.services.arito_service import AritoInventoryService


@pytest.fixture
def mock_admin_user():
    return User(
        id="user-superadmin",
        username="admin",
        email="admin@naman.com",
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
        is_superuser=True,
    )


@pytest.mark.asyncio
async def test_haravan_client_check_connection():
    """Verify HaravanClient checks connection and handles responses."""
    client = HaravanClient(base_url="https://test-apis.haravan.com", token="test-token")
    mock_response = Response(200, json={"orders": [{"id": 123, "order_number": "#NA100"}]})

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        result = await client.check_connection()
        assert result["connected"] is True
        assert result["status_code"] == 200
        assert result["sample_order_count"] == 1


@pytest.mark.asyncio
async def test_haravan_client_fetch_latest_orders():
    """Verify HaravanClient fetches orders with pagination."""
    client = HaravanClient(base_url="https://test-apis.haravan.com", token="test-token")
    mock_orders = [{"id": i, "order_number": f"#NA{i}"} for i in range(1, 26)]
    mock_response = Response(200, json={"orders": mock_orders})

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        orders = await client.fetch_latest_orders(limit=25)
        assert len(orders) == 25
        assert orders[0]["order_number"] == "#NA1"


def test_haravan_sync_worker_normalize_order_preserves_internal():
    """Verify that internal field is removed from set payload to prevent overwriting dispatch data."""
    raw_doc = {
        "id": 987654321,
        "order_number": "#NA229000",
        "total_price": 450000.0,
        "internal": {
            "assigned_store": "10001",
            "order_processing_status": "Đã phân tuyến",
        },
    }
    clean = HaravanSyncWorker.normalize_order_doc(raw_doc)
    assert clean["id"] == "987654321"
    assert "internal" not in clean
    assert clean["total_price"] == 450000.0


def test_haravan_sync_worker_upsert_order():
    """Verify upsert_order uses $set and $setOnInsert to protect internal field."""
    worker = HaravanSyncWorker()
    mock_col = MagicMock()
    mock_col.update_one.return_value = MagicMock(matched_count=1)

    order_doc = {
        "id": "112233",
        "order_number": "#NA1122",
        "source": "shopee",
    }
    success = worker.upsert_order(mock_col, order_doc)
    assert success is True

    # Assert update_one arguments
    mock_col.update_one.assert_called_once()
    args, kwargs = mock_col.update_one.call_args
    query = args[0]
    update = args[1]
    assert query == {"id": "112233"}
    assert "$set" in update
    assert update["$set"]["order_number"] == "#NA1122"
    assert "$setOnInsert" in update
    assert "internal" in update["$setOnInsert"]
    assert kwargs.get("upsert") is True


@pytest.mark.asyncio
async def test_haravan_sync_once():
    """Verify sync_once fetches orders and writes safely to MongoDB."""
    mock_client = MagicMock()
    mock_client.fetch_latest_orders = AsyncMock(
        return_value=[
            {"id": "1", "order_number": "#NA1"},
            {"id": "2", "order_number": "#NA2"},
        ]
    )
    worker = HaravanSyncWorker(client=mock_client)

    with patch.object(worker, "get_mongo_collection") as mock_get_col, \
         patch.object(worker, "upsert_order", return_value=True) as mock_upsert:
        mock_get_col.return_value = MagicMock()
        res = await worker.sync_once(limit=10)
        assert res["success"] is True
        assert res["processed"] == 2
        assert worker.last_sync_count == 2


def test_arito_inventory_service_sync_and_ranking():
    """Verify AritoInventoryService aggregates inventory correctly across the 4 stores."""
    service = AritoInventoryService(mongo_uri="mongodb://localhost:27017")
    mock_col = MagicMock()
    # Mock aggregation results from cttk
    mock_col.aggregate.return_value = [
        {"_id": {"ma_vt": "SKU_BEEF", "ma_vi_tri": "10001"}, "total_qty": 15.0},
        {"_id": {"ma_vt": "SKU_BEEF", "ma_vi_tri": "10004"}, "total_qty": 3.0},
    ]

    with patch.object(service, "_get_cttk_collection", return_value=mock_col):
        stock = service.get_inventory_by_skus_sync(["SKU_BEEF"])
        assert "SKU_BEEF" in stock
        assert stock["SKU_BEEF"]["branches"]["10001"]["stock"] == 15.0
        assert stock["SKU_BEEF"]["branches"]["10004"]["stock"] == 3.0
        assert stock["SKU_BEEF"]["branches"]["10005"]["stock"] == 0.0


@pytest.mark.asyncio
async def test_arito_recommend_best_store():
    """Verify recommendation algorithm picks the branch with highest stock fulfillment."""
    service = AritoInventoryService(mongo_uri="mongodb://localhost:27017")
    mock_stock = {
        "SKU_MILK": {
            "sku": "SKU_MILK",
            "branches": {
                "10001": {"branch_name": "Thao Dien", "stock": 10.0},
                "10004": {"branch_name": "Hung Phuc", "stock": 0.0},
                "10005": {"branch_name": "Mai Chi Tho", "stock": 5.0},
                "10006": {"branch_name": "NVT", "stock": 2.0},
            },
        }
    }

    with patch.object(service, "get_inventory_by_skus", new_callable=AsyncMock) as mock_get_inv:
        mock_get_inv.return_value = mock_stock
        rec = await service.recommend_best_store_for_order([{"sku": "SKU_MILK", "quantity": 8}])
        assert rec["recommended_store"] == "10001"
        assert rec["fulfilled_items_count"] == 1


@pytest.mark.asyncio
async def test_haravan_api_endpoints(mock_admin_user):
    """Verify FastAPI router endpoints for Haravan status and inventory check."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[require_store_manager] = lambda: mock_admin_user
    app.dependency_overrides[require_super_admin] = lambda: mock_admin_user

    client = TestClient(app)

    # 1. Status endpoint
    with patch("app.modules.haravan.sync_worker.haravan_sync_worker.client.check_connection", new_callable=AsyncMock) as mock_conn:
        mock_conn.return_value = {"connected": True, "message": "OK"}
        res_status = client.get("/api/v1/haravan/status")
        assert res_status.status_code == 200
        assert res_status.json()["success"] is True

    # 2. Inventory check endpoint
    with patch("app.services.arito_service.arito_service.get_inventory_by_skus", new_callable=AsyncMock) as mock_inv:
        mock_inv.return_value = {
            "SKU_APPLE": {
                "sku": "SKU_APPLE",
                "branches": {"10001": {"stock": 12.0}},
            }
        }
        res_inv = client.get("/api/v1/haravan/inventory-check?skus=SKU_APPLE")
        assert res_inv.status_code == 200
        assert res_inv.json()["data"]["SKU_APPLE"]["branches"]["10001"]["stock"] == 12.0

    app.dependency_overrides.clear()
