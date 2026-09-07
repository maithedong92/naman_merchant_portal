import base64
import json
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
from app.modules.shopeefood.adapter import ShopeeFoodChannelAdapter
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
