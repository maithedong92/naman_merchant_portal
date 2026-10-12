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


@pytest.mark.asyncio
async def test_haravan_order_dispatch_workflow(mock_admin_user):
    """
    Test End-to-End quy trình phân tuyến & vòng đời đơn hàng theo 100% logic pmdonhang:
    1. SuperAdmin thấy đơn chưa phân tuyến.
    2. Điều phối viên phân tuyến về chi nhánh 10001 (chụp snapshot Arito).
    3. Cửa hàng 10001 yêu cầu phân tuyến lại (gỡ bỏ assigned_store = None).
    4. Phân tuyến lại -> Đóng gói -> Gửi vận đơn -> Hoàn thành đơn hàng.
    """
    from app.models.store import Store
    store_10001 = Store(id="store-10001", code="10001", name="21 Thảo Điền")
    staff_10001 = User(
        id="user-staff-10001",
        username="staff_10001",
        email="staff10001@naman.com",
        role=UserRole.STAFF.value,
        store_id="store-10001",
        store=store_10001,
        is_active=True,
    )
    mock_order_doc = {
        "_id": "670102030405060708090a0b",
        "id": "HV_TEST_999",
        "order_number": "#NA999",
        "source": "checkout_next",
        "total_price": 500000.0,
        "line_items": [{"sku": "SKU_001", "name": "Bò Úc", "quantity": 1}],
        "internal": {
            "order_processing_status": "Đơn hàng mới",
            "assigned_store": None,
            "history": [],
        },
    }

    from app.services.haravan_order_service import haravan_order_service

    # Mock MongoDB collection
    mock_col = MagicMock()
    mock_col.find_one.return_value = mock_order_doc
    mock_col.count_documents.return_value = 1
    mock_col.find.return_value.sort.return_value.skip.return_value.limit.return_value = [mock_order_doc]

    with patch.object(haravan_order_service, "_get_orders_collection", return_value=mock_col), \
         patch("app.services.arito_service.arito_service.get_inventory_by_skus", new_callable=AsyncMock) as mock_inv:
        mock_inv.return_value = {"SKU_001": {"branches": {"10001": {"stock": 10.0}}}}

        # 1. SuperAdmin list unassigned orders
        orders, total = await haravan_order_service.list_orders(
            current_user=mock_admin_user,
            unassigned_only=True,
        )
        assert total == 1
        assert orders[0]["order_number"] == "#NA999"

        # 2. Phân tuyến đơn cho chi nhánh 10001
        assigned = await haravan_order_service.assign_order(
            order_id_or_sn="HV_TEST_999",
            store_code="10001",
            reason="Gần cửa hàng",
            note="Khách cần gấp trước 12h",
            current_user=mock_admin_user,
        )
        mock_col.update_one.assert_called()
        # Verify snapshot and fields were updated
        update_args = mock_col.update_one.call_args[0][1]
        assert update_args["$set"]["internal.assigned_store"] == "10001"
        assert update_args["$set"]["internal.order_processing_status"] == "Đã phân tuyến"
        assert "internal.assignment_stock" in update_args["$set"]

        # 3. Nhân viên 10001 yêu cầu phân tuyến lại (thiếu hàng)
        mock_order_doc["internal"]["assigned_store"] = "10001"
        reassigned = await haravan_order_service.request_reassignment(
            order_id_or_sn="HV_TEST_999",
            reason="Cửa hàng không có hàng",
            note="Hết thăn bò Úc tại kệ",
            current_user=staff_10001,
        )
        reassign_update = mock_col.update_one.call_args[0][1]
        assert reassign_update["$set"]["internal.assigned_store"] is None
        assert reassign_update["$set"]["internal.order_processing_status"] == "Yêu cầu phân tuyến lại"

        # 4. Đóng gói đơn hàng
        await haravan_order_service.pack_order(
            order_id_or_sn="HV_TEST_999",
            current_user=staff_10001,
        )
        pack_update = mock_col.update_one.call_args[0][1]
        assert pack_update["$set"]["internal.order_processing_status"] == "Đã đóng gói"

        # 5. Gửi vận đơn
        await haravan_order_service.ship_order(
            order_id_or_sn="HV_TEST_999",
            tracking_code="AHA12345678",
            carrier="Ahamove",
            current_user=staff_10001,
        )
        ship_update = mock_col.update_one.call_args[0][1]
        assert ship_update["$set"]["internal.order_processing_status"] == "Đã gửi vận đơn"
        assert ship_update["$set"]["internal.tracking_code"] == "AHA12345678"

        # 6. Hoàn thành đơn hàng
        await haravan_order_service.complete_order(
            order_id_or_sn="HV_TEST_999",
            actual_total=505000.0,
            actual_shipping=25000.0,
            actual_carrier="Ahamove",
            tracking_code="AHA12345678",
            note="Giao thành công, khách đã trả tiền mặt",
            current_user=staff_10001,
        )
        complete_update = mock_col.update_one.call_args[0][1]
        assert complete_update["$set"]["internal.order_processing_status"] == "Hoàn thành"
        assert complete_update["$set"]["internal.completion_info"]["actual_total"] == "505000.0"
        assert complete_update["$set"]["internal.completion_info"]["actual_carrier"] == "Ahamove"


def test_haravan_orders_html_ui_components():
    """Verify that orders.html contains Haravan Omnichannel filter pills and dispatch modals matching pmdonhang."""
    client = TestClient(app)
    response = client.get("/admin/orders")
    assert response.status_code == 200
    html = response.text

    # 1. Header controls
    assert "btn-sync-haravan" in html
    assert "Đồng bộ Haravan" in html

    # 2. Channel Pills & Unassigned quick filter
    assert "Haravan Omnichannel" in html
    assert "btn-unassigned-filter" in html
    assert "Cần Phân Tuyến (Haravan)" in html

    # 3. Haravan Modals (Follow 100% pmdonhang)
    assert "haravan-assign-modal" in html
    assert "Tồn Kho Thực Tế Arito ERP (4 Siêu Thị)" in html
    assert "10001 (21TD)" in html
    assert "10004 (46NLB)" in html
    assert "10005 (17MCT)" in html
    assert "10006 (303NVT)" in html

    assert "haravan-reassign-modal" in html
    assert "Cửa hàng không có hàng" in html
    assert "Cửa hàng quá tải" in html

    assert "haravan-ship-modal" in html
    assert "Mã vận đơn (Tracking Code)" in html

    assert "haravan-complete-modal" in html
    assert "Tổng tiền thực tế" in html

    # 4. Incomplete Orders Filter (pmdonhang parity)
    assert "btn-incomplete-filter" in html
    assert "Chưa Hoàn Thành" in html
    assert "incomplete-counter-badge" in html


@pytest.mark.asyncio
async def test_haravan_incomplete_orders_filter_query(mock_admin_user):
    """Verify that incomplete orders filter uses $nin: ['Hoàn thành', 'Đã hủy'] matching pmdonhang."""
    from app.services.haravan_order_service import HaravanOrderService
    svc = HaravanOrderService()

    with patch.object(svc, "_get_orders_collection") as mock_get_col:
        mock_col = MagicMock()
        mock_col.count_documents.return_value = 10
        mock_col.find.return_value.sort.return_value.skip.return_value.limit.return_value = []
        mock_get_col.return_value = mock_col

        # Test incomplete_only=True
        items, total = await svc.list_orders(current_user=mock_admin_user, incomplete_only=True)
        query = mock_col.find.call_args[0][0]
        assert query["internal.order_processing_status"] == {"$nin": ["Hoàn thành", "Đã hủy"]}

        # Test status="Chưa hoàn thành"
        items, total = await svc.list_orders(current_user=mock_admin_user, status="Chưa hoàn thành")
        query2 = mock_col.find.call_args[0][0]
        assert query2["internal.order_processing_status"] == {"$nin": ["Hoàn thành", "Đã hủy"]}


@pytest.mark.asyncio
async def test_haravan_count_incomplete_orders(mock_admin_user):
    """Verify counting incomplete orders."""
    from app.services.haravan_order_service import HaravanOrderService
    svc = HaravanOrderService()

    with patch.object(svc, "_get_orders_collection") as mock_get_col:
        mock_col = MagicMock()
        mock_col.count_documents.return_value = 28
        mock_get_col.return_value = mock_col

        cnt = await svc.count_incomplete_orders(current_user=mock_admin_user)
        assert cnt == 28
        query = mock_col.count_documents.call_args[0][0]
        assert query["internal.order_processing_status"] == {"$nin": ["Hoàn thành", "Đã hủy"]}


def test_haravan_incomplete_count_api_endpoint(mock_admin_user):
    """Verify GET /api/v1/haravan/incomplete-count endpoint."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    with patch("app.api.v1.haravan.haravan_order_service.count_incomplete_orders", new_callable=AsyncMock) as mock_count:
        mock_count.return_value = 28
        res = client.get("/api/v1/haravan/incomplete-count")
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["data"]["count"] == 28

    app.dependency_overrides.clear()


def test_haravan_update_admin_note_endpoint(mock_admin_user):
    """Verify POST /api/v1/haravan/orders/{order_id}/admin-note endpoint."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    with patch("app.api.v1.haravan.haravan_order_service.update_admin_note", new_callable=AsyncMock) as mock_update:
        mock_update.return_value = {
            "id": 12345,
            "order_number": "#NA12345",
            "internal": {"admin_notes": "Khách VIP, giao buổi sáng"},
        }
        res = client.post(
            "/api/v1/haravan/orders/12345/admin-note",
            json={"note": "Khách VIP, giao buổi sáng"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["data"]["internal"]["admin_notes"] == "Khách VIP, giao buổi sáng"

    app.dependency_overrides.clear()


