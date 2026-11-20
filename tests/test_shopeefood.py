import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_db
from app.core.security import generate_hmac_sha256
from app.main import app
from app.models.channel import Channel
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.product import Category, Product
from app.models.store import Store, StoreChannelMapping
from app.modules.shopeefood.adapter import ShopeeFoodChannelAdapter, _shopeefood_menu_cache
from app.modules.shopeefood.service import ShopeeFoodClient, shopeefood_client
from app.services.channel_service import channel_service
from app.services.order_service import OrderService


@pytest.fixture
def client():
    return TestClient(app)


# ==============================================================================
# 1. HMAC-SHA256 & Foody Header Tests
# ==============================================================================

def test_shopeefood_hmac_signature_vector():
    """Verify HMAC-SHA256 matches Foody External API Integration v7.0.2 specification test vector."""
    # From Page 6 of the official document:
    key = "5feceb66ffc86f38d952786c6d696c79"
    base_string = "GET|http://testexternalapi.deliverynow.vn/s2s/order/get_list?from_item_id=0&request_count=30&sort=1&status=1&from_date=2018-08-17&last_request=2017-08-22+11%3A00%3A33|"
    expected = "c514bd078e8ff4f1c9e2c0c296f99f98ce20aaa1e1dc37e2f82415758fe0affb"

    calculated = generate_hmac_sha256(key, base_string, is_hex_key=True)
    assert calculated == expected


def test_shopeefood_client_build_headers():
    """ShopeeFoodClient builds valid Authorization and X-Foody headers."""
    spf = ShopeeFoodClient()
    headers = spf.build_headers("POST", "https://gstageexternalapi.deliverynow.vn/s2s/menu/sync", '{"test":1}')
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Signature ")
    assert headers["X-Foody-App-Id"] == "10045"
    assert headers["X-Foody-Country"] == "VN"
    assert headers["Content-Type"] == "application/json"


# ==============================================================================
# 2. Public Store Menu Basic Auth Endpoint Tests
# ==============================================================================

def test_shopeefood_store_menu_basic_auth(client):
    """GET /shopeefoodapi/{store_code}.json enforces Basic Auth: namanmarket / shopeefoodapi."""
    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        # 1. Without credentials -> 401
        res_no_auth = client.get("/shopeefoodapi/10001.json")
        assert res_no_auth.status_code == 401

        # 2. Invalid credentials -> 401
        bad_auth = base64.b64encode(b"wrong:pass").decode("ascii")
        res_bad_auth = client.get(
            "/shopeefoodapi/10001.json",
            headers={"Authorization": f"Basic {bad_auth}"}
        )
        assert res_bad_auth.status_code == 401

        # 3. Valid credentials -> 200 JSON catalog
        good_auth = base64.b64encode(b"namanmarket:shopeefoodapi").decode("ascii")

        mock_store = Store(id="store-1", code="10001", name="Nam An Thảo Điền")
        mock_res = MagicMock()
        mock_res.scalar_one_or_none.return_value = mock_store
        mock_res.all.return_value = []
        mock_db.execute.return_value = mock_res

        res_ok = client.get(
            "/shopeefoodapi/10001.json",
            headers={"Authorization": f"Basic {good_auth}"}
        )
        assert res_ok.status_code == 200
        catalog = res_ok.json()
        assert "sections" in catalog
        assert catalog["partnerMerchantID"] == "10001"
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 3. Inbound Order Webhook & Feature Toggle Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_shopeefood_order_webhook_ingest():
    """Inbound webhook parses ShopeeFood order and stores in UnifiedOrder."""
    adapter = ShopeeFoodChannelAdapter()
    mock_db = AsyncMock()

    payload = {
        "partner_restaurant_id": "10001",
        "order_id": "SPF-889900",
        "display_order_id": "889900",
        "subtotal": 150000.0,
        "discount": 15000.0,
        "shipping_fee": 20000.0,
        "total_amount": 155000.0,
        "customer": {
            "name": "Nguyễn Văn A",
            "phone": "0901234567",
            "address": "21 Thảo Điền, Q2, TP.HCM"
        },
        "items": [
            {
                "partner_dish_id": "SKU-SALAD-01",
                "name": "Salad Rau Mầm Hữu Cơ",
                "quantity": 2,
                "price": 75000.0,
                "notes": "Ít sốt"
            }
        ]
    }
    raw_body = json.dumps(payload).encode("utf-8")

    fake_order = UnifiedOrder(
        id="order-spf-1",
        order_code="NA-202609-001",
        channel_order_id="SPF-889900",
        status=UnifiedOrderStatus.PENDING,
    )

    with patch.object(channel_service, "is_feature_enabled", AsyncMock(side_effect=lambda ch, feat, db: feat == "order_webhook_enabled")):
        with patch.object(OrderService, "create_or_get_inbound_order", AsyncMock(return_value=(fake_order, True))):
            result = await adapter.handle_order_webhook({}, raw_body, mock_db)

            assert result["result"] == "success"
            assert result["reply"]["order_id"] == "SPF-889900"
            assert result["reply"]["auto_confirmed"] is False


@pytest.mark.asyncio
async def test_shopeefood_order_webhook_disabled_flag():
    """When order_webhook_enabled is False, webhook is rejected gracefully."""
    adapter = ShopeeFoodChannelAdapter()
    mock_db = AsyncMock()

    raw_body = json.dumps({"order_id": "SPF-1122"}).encode("utf-8")

    with patch.object(channel_service, "is_feature_enabled", AsyncMock(return_value=False)):
        result = await adapter.handle_order_webhook({}, raw_body, mock_db)

        assert result["result"] == "failed"
        assert "tạm dừng" in result["error"]


@pytest.mark.asyncio
async def test_shopeefood_auto_confirm_feature():
    """When auto_confirm_enabled is True, order status is ACCEPTED and Foody update is triggered."""
    adapter = ShopeeFoodChannelAdapter()
    mock_db = AsyncMock()

    payload = {
        "partner_restaurant_id": "10001",
        "order_id": "SPF-AUTO-01",
        "subtotal": 100000.0,
        "total_amount": 100000.0,
        "items": []
    }
    raw_body = json.dumps(payload).encode("utf-8")

    fake_order = UnifiedOrder(
        id="order-auto-1",
        order_code="NA-AUTO-001",
        channel_order_id="SPF-AUTO-01",
        status=UnifiedOrderStatus.ACCEPTED,
    )

    with patch.object(channel_service, "is_feature_enabled", AsyncMock(return_value=True)):
        with patch.object(OrderService, "create_or_get_inbound_order", AsyncMock(return_value=(fake_order, True))):
            with patch.object(shopeefood_client, "update_order_status", AsyncMock(return_value={"result": "success"})) as mock_confirm:
                result = await adapter.handle_order_webhook({}, raw_body, mock_db)

                assert result["result"] == "success"
                assert result["reply"]["auto_confirmed"] is True
                mock_confirm.assert_called_once_with(
                    order_code="SPF-AUTO-01",
                    status=0,
                    partner_restaurant_id="10001"
                )


# ==============================================================================
# 4. Menu & Inventory Sync Feature Toggles Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_shopeefood_menu_sync_feature_toggle():
    """sync_menu skips when menu_sync_enabled is False."""
    adapter = ShopeeFoodChannelAdapter()
    mock_db = AsyncMock()

    with patch.object(channel_service, "is_feature_enabled", AsyncMock(return_value=False)):
        result = await adapter.sync_menu("store-1", "10001", mock_db)
        assert result.success is False
        assert "TẮT" in result.message


@pytest.mark.asyncio
async def test_shopeefood_stock_sync_feature_toggle():
    """sync_inventory skips when stock_sync_enabled is False."""
    adapter = ShopeeFoodChannelAdapter()
    mock_db = AsyncMock()

    with patch.object(channel_service, "is_feature_enabled", AsyncMock(return_value=False)):
        result = await adapter.sync_inventory("store-1", "10001", [{"sku": "SKU-1", "available_stock": 5}], mock_db)
        assert result.success is False
        assert "TẮT" in result.message


@pytest.mark.asyncio
async def test_shopeefood_outbound_status_update():
    """Adapter calls update_order_status for ACCEPTED and CANCELLED."""
    adapter = ShopeeFoodChannelAdapter()
    mock_db = AsyncMock()

    with patch.object(shopeefood_client, "update_order_status", AsyncMock(return_value={"result": "success"})) as mock_upd:
        # ACCEPTED -> status 0
        ok = await adapter.update_order_status("SPF-123", "10001", UnifiedOrderStatus.ACCEPTED, mock_db)
        assert ok is True
        mock_upd.assert_called_with(order_code="SPF-123", status=0, partner_restaurant_id="10001")

        # CANCELLED -> status 2 (OUT_OF_SERVICE)
        ok_cancel = await adapter.update_order_status("SPF-123", "10001", UnifiedOrderStatus.CANCELLED, mock_db, reason="Hết hàng")
        assert ok_cancel is True
        mock_upd.assert_called_with(
            order_code="SPF-123",
            status=2,
            partner_restaurant_id="10001",
            reason_ids=[79],
            merchant_note="Hết hàng"
        )


@pytest.mark.asyncio
async def test_shopeefood_sync_inventory_pushes_prices_via_bulk_update_and_menu_fallback():
    """
    Case 1: stock_items with explicit 'price' pushes status + bulk_update_dishes and clears menu cache.
    Case 2: If bulk_update_dishes returns non-success and menu_sync_enabled=True, calls notify_menu_sync.
    Case 3: Pure stock sync without 'price' does NOT call bulk_update_dishes or notify_menu_sync.
    """
    adapter = ShopeeFoodChannelAdapter()
    mock_db = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_res

    # Case 1: explicit price triggers bulk_update_dishes and cache invalidation
    _shopeefood_menu_cache["10001"] = {"stale": True}
    with patch.object(channel_service, "is_feature_enabled", AsyncMock(return_value=True)), \
         patch.object(shopeefood_client, "update_dish_status", AsyncMock(return_value={"result": "success"})) as mock_status, \
         patch.object(shopeefood_client, "bulk_update_dishes", AsyncMock(return_value={"result": "success"})) as mock_bulk, \
         patch.object(shopeefood_client, "notify_menu_sync", AsyncMock(return_value={"result": "success"})) as mock_menu:
        res1 = await adapter.sync_inventory(
            "store-1",
            "10001",
            [{"sku": "290011724000", "available_stock": 5, "is_out_of_stock": False, "price": 929907.4}],
            mock_db,
        )
    assert res1.success is True
    assert "10001" not in _shopeefood_menu_cache
    mock_status.assert_awaited_once()
    mock_bulk.assert_awaited_once_with(
        partner_restaurant_id="10001",
        dishes=[{"partner_dish_id": "290011724000", "price": 929907}],
    )
    mock_menu.assert_not_awaited()

    # Case 2: bulk_update_dishes error_dish_not_found falls back to notify_menu_sync
    with patch.object(channel_service, "is_feature_enabled", AsyncMock(return_value=True)), \
         patch.object(shopeefood_client, "update_dish_status", AsyncMock(return_value={"result": "error_dish_not_found"})), \
         patch.object(shopeefood_client, "bulk_update_dishes", AsyncMock(return_value={"result": "error_dish_not_found"})), \
         patch.object(shopeefood_client, "notify_menu_sync", AsyncMock(return_value={"result": "success"})) as mock_menu2:
        res2 = await adapter.sync_inventory(
            "store-1",
            "10001",
            [{"sku": "290011724000", "available_stock": 5, "is_out_of_stock": False, "price": 929907}],
            mock_db,
        )
    assert res2.success is True
    mock_menu2.assert_awaited_once_with(partner_restaurant_id="10001")

    # Case 3: pure stock sync without 'price' skips bulk_update_dishes and notify_menu_sync
    with patch.object(channel_service, "is_feature_enabled", AsyncMock(return_value=True)), \
         patch.object(shopeefood_client, "update_dish_status", AsyncMock(return_value={"result": "success"})), \
         patch.object(shopeefood_client, "bulk_update_dishes", AsyncMock(return_value={"result": "success"})) as mock_bulk3, \
         patch.object(shopeefood_client, "notify_menu_sync", AsyncMock(return_value={"result": "success"})) as mock_menu3:
        res3 = await adapter.sync_inventory(
            "store-1",
            "10001",
            [{"sku": "290011724000", "available_stock": 0, "is_out_of_stock": True}],
            mock_db,
        )
    assert res3.success is True
    mock_bulk3.assert_not_awaited()
    mock_menu3.assert_not_awaited()


def test_shopeefood_official_numeric_status_mapping():
    """Foody v7.0.2 OrderMerchantStatus numeric codes map cleanly to UnifiedOrderStatus."""
    assert ShopeeFoodChannelAdapter._map_webhook_status(5) == UnifiedOrderStatus.PENDING    # M_ASSIGNED
    assert ShopeeFoodChannelAdapter._map_webhook_status(6) == UnifiedOrderStatus.PENDING    # M_RECEIVED
    assert ShopeeFoodChannelAdapter._map_webhook_status(3) == UnifiedOrderStatus.ACCEPTED   # CONFIRMED
    assert ShopeeFoodChannelAdapter._map_webhook_status(0) == UnifiedOrderStatus.ACCEPTED   # CONFIRM
    assert ShopeeFoodChannelAdapter._map_webhook_status(1) == UnifiedOrderStatus.PICKED_UP  # PICKED
    assert ShopeeFoodChannelAdapter._map_webhook_status(2) == UnifiedOrderStatus.DELIVERED  # DELIVERED
    assert ShopeeFoodChannelAdapter._map_webhook_status(7) == UnifiedOrderStatus.CANCELLED  # M_OUT_OF_SERVICE
    assert ShopeeFoodChannelAdapter._map_webhook_status(8) == UnifiedOrderStatus.CANCELLED  # CANCELLED
    assert ShopeeFoodChannelAdapter._map_webhook_status("CANCELLED") == UnifiedOrderStatus.CANCELLED
    assert ShopeeFoodChannelAdapter._map_webhook_status("DELIVERED") == UnifiedOrderStatus.DELIVERED


def test_shopeefood_webhook_s2s_proof_of_order_when_no_auth_header(client):
    """
    When Authorization header is omitted by Foody webhook caller and S2S verification is enabled:
    - Valid get_order_details matching order_code and active store -> 200 OK & hydrates items/totals.
    - Failed get_order_details or mismatched code or unmapped store -> 401 Unauthorized.
    - Explicit forged Authorization signature NEVER falls back to S2S verification -> 401 Unauthorized.
    """
    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    fake_settings = SimpleNamespace(
        SHOPEEFOOD_WEBHOOK_S2S_VERIFY_FALLBACK=True,
        SHOPEEFOOD_APP_KEY="test_app_key_123",
    )

    s2s_reply = {
        "result": "success",
        "reply": {
            "code": "20116-422986729",
            "serial": "0x01",
            "status": 5,
            "restaurant": {"id": 632674, "partner_restaurant_id": "10001", "name": "Nam An Thảo Điền"},
            "order_value": {"value": 259900.0},
            "total_merchant_discount": {"value": 20000.0},
            "total_value": {"value": 239900.0},
            "customer_bill": {"shipping_fee": {"value": 15000.0}, "total_amount": {"value": 254900.0}},
            "customer": {"name": "Chị Lan", "phone": "0909000111"},
            "deliver_address": {"address": "21 Thảo Điền", "name": "Chị Lan", "phone": "0909000111"},
            "assignee": {"name": "Tài xế Minh", "phone": "0908000222"},
            "dish_groups": [
                {
                    "group_name": "CÀ PHÊ",
                    "dishes": [
                        {
                            "dish_id": 279582287,
                            "partner_dish_id": "1016035751",
                            "dish_name": "Cafe Davidoff 90G",
                            "quantity": 1,
                            "price": {"value": 259900.0},
                            "total": {"value": 259900.0},
                        }
                    ],
                }
            ],
        },
    }

    fake_mapping = StoreChannelMapping(
        store_id="st-10001",
        channel_id="ch-sf",
        partner_store_id="10001",
        is_active=True,
    )
    fake_order = UnifiedOrder(
        id="ord-sf-live-1",
        order_code="NA-SF-001",
        channel_order_id="20116-422986729",
        status=UnifiedOrderStatus.PENDING,
    )

    try:
        # 1. Valid S2S verification -> 200 OK and hydrates items/totals from dish_groups
        scm_res = MagicMock()
        scm_res.scalars.return_value.first.return_value = fake_mapping
        mock_db.execute.return_value = scm_res

        with patch("app.modules.shopeefood.router.get_settings", return_value=fake_settings), \
             patch.object(shopeefood_client, "get_order_details", AsyncMock(return_value=s2s_reply)) as mock_det, \
             patch("app.modules.shopeefood.adapter.channel_service.is_feature_enabled", AsyncMock(side_effect=lambda ch, feat, db: feat == "order_webhook_enabled")), \
             patch("app.modules.shopeefood.adapter.OrderService.create_or_get_inbound_order", AsyncMock(return_value=(fake_order, True))) as mock_create:
            resp_ok = client.post(
                "/shopeefoodapi/order/",
                json={"order_code": "20116-422986729", "partner_restaurant_id": "10001", "update_type": 1},
            )
            assert resp_ok.status_code == 200
            assert resp_ok.json()["result"] == "success"
            mock_det.assert_awaited_once_with("20116-422986729", mark_as_received=0)
            kwargs = mock_create.await_args.kwargs
            assert kwargs["order_data"]["subtotal_amount"] == 259900.0
            assert kwargs["order_data"]["total_amount"] == 239900.0
            assert kwargs["order_data"]["customer_name"] == "Chị Lan"
            assert kwargs["order_data"]["driver_name"] == "Tài xế Minh"
            assert len(kwargs["items_data"]) == 1
            assert kwargs["items_data"][0]["sku"] == "1016035751"

        # 2. Failed get_order_details -> 401 Fail-Closed
        with patch("app.modules.shopeefood.router.get_settings", return_value=fake_settings), \
             patch.object(shopeefood_client, "get_order_details", AsyncMock(return_value={"result": "error_params"})):
            resp_fail = client.post(
                "/shopeefoodapi/order/",
                json={"order_code": "FAKE-999", "partner_restaurant_id": "10001"},
            )
            assert resp_fail.status_code == 401

        # 3. Unknown store (not mapped and not in Store table) -> 401 Fail-Closed
        none_res = MagicMock()
        none_res.scalars.return_value.first.return_value = None
        mock_db.execute.return_value = none_res
        with patch("app.modules.shopeefood.router.get_settings", return_value=fake_settings), \
             patch.object(shopeefood_client, "get_order_details", AsyncMock(return_value=s2s_reply)):
            resp_nostore = client.post(
                "/shopeefoodapi/order/",
                json={"order_code": "20116-422986729", "partner_restaurant_id": "10001"},
            )
            assert resp_nostore.status_code == 401

        # 4. Bad explicit signature NEVER falls back to S2S verification -> 401 immediately
        with patch("app.modules.shopeefood.router.get_settings", return_value=fake_settings), \
             patch.object(shopeefood_client, "get_order_details", AsyncMock(return_value=s2s_reply)) as mock_det_never:
            resp_forged = client.post(
                "/shopeefoodapi/order/",
                json={"order_code": "20116-422986729", "partner_restaurant_id": "10001"},
                headers={"Authorization": "Signature deadbeef"},
            )
            assert resp_forged.status_code == 401
            mock_det_never.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()

