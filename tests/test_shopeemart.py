import json
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_current_active_user, get_db
from app.main import app
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.product import Product
from app.models.store import Store, StoreChannelMapping
from app.models.user import User, UserRole
from app.modules.shopeemart.adapter import ShopeeMartChannelAdapter
from app.modules.shopeemart.service import ShopeeMartClient, shopeemart_client


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_admin_user():
    return User(
        id="admin-uuid-shopee",
        username="admin",
        email="admin@namanmarket.com",
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
        is_superuser=True,
    )


# ==============================================================================
# 1. HMAC-SHA256 Signing & Verification Tests
# ==============================================================================

def test_shopeemart_signature_generation():
    """Verify HMAC-SHA256 generation complies with Shopee Open Platform API v2."""
    sm_client = ShopeeMartClient()
    sm_client.partner_id = "123456"
    sm_client.partner_key = "test_partner_secret_key"

    # Public API sign: partner_id + path + timestamp
    pub_sign = sm_client.generate_public_sign("/api/v2/auth/token/get", 1600000000)
    assert isinstance(pub_sign, str)
    assert len(pub_sign) == 64  # SHA256 hex length

    # Shop API sign: partner_id + path + timestamp + access_token + shop_id
    shop_sign = sm_client.generate_shop_sign(
        "/api/v2/order/get_order_detail",
        1600000000,
        access_token="tok_123",
        shop_id="10001"
    )
    assert isinstance(shop_sign, str)
    assert len(shop_sign) == 64


def test_shopeemart_webhook_signature_verification():
    """Verify webhook signature validation logic."""
    sm_client = ShopeeMartClient()
    sm_client.partner_id = "123456"
    sm_client.partner_key = "secret_key_abc"

    url = "https://ump.namanmarket.com/shopeemart/webhooks/order"
    raw_body = b'{"order_sn": "260904XYZ123", "status": "READY_TO_SHIP"}'

    # Calculate expected signature
    import hashlib
    import hmac
    base_str = f"{url}|{raw_body.decode('utf-8')}"
    expected_sign = hmac.new(
        sm_client.partner_key.encode("utf-8"),
        base_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    # Valid signature
    assert sm_client.verify_webhook_signature(url, raw_body, expected_sign) is True

    # Invalid signature
    assert sm_client.verify_webhook_signature(url, raw_body, "tampered_signature_123") is False


# ==============================================================================
# 2. OAuth 2.0 Auth URL & Token Lifecycle Tests
# ==============================================================================

def test_shopeemart_auth_url_endpoint(client, mock_admin_user):
    """Test generating Shopee authorization URL for shop owner."""
    app.dependency_overrides[get_current_active_user] = lambda: mock_admin_user

    try:
        response = client.get("/api/v1/shopeemart/auth/url?redirect_url=https://ump.namanmarket.com/callback")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "auth_url" in data["data"]
        assert "auth_partner" in data["data"]["auth_url"]
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.asyncio
async def test_shopeemart_token_exchange_simulation():
    """Verify code exchange and token caching."""
    sm_client = ShopeeMartClient()
    token_resp = await sm_client.exchange_code_for_token(code="test_auth_code_123", shop_id="10001")
    assert token_resp.access_token is not None
    assert token_resp.refresh_token is not None
    assert token_resp.shop_id == 10001

    # Verify cached token retrieval
    valid_token = await sm_client.get_valid_access_token("10001")
    assert valid_token == token_resp.access_token


def test_shopeemart_connection_status_endpoint(client):
    """Test public connection status endpoint."""
    response = client.get("/shopeemart/connection-status?shop_id=10001")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["channel_code"] == "SHOPEEMART"
    assert "has_valid_token" in data["data"]


# ==============================================================================
# 3. Inbound Order Webhook Processing Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_shopeemart_handle_order_webhook_creation():
    """Verify parsing and ingesting of incoming ShopeeMart order."""
    adapter = ShopeeMartChannelAdapter()

    shopee_payload = {
        "order_sn": "260905SHP998877",
        "shop_id": 10001,
        "status": "READY_TO_SHIP",
        "buyer_username": "nguyenvana_shopee",
        "total_amount": 250000.0,
        "actual_shipping_fee": 25000.0,
        "seller_discount": 10000.0,
        "recipient_address": {
            "name": "Nguyễn Văn A",
            "phone": "0987654321",
            "full_address": "46 Hưng Phúc, Phường Tân Phong, Quận 7, TP.HCM"
        },
        "item_list": [
            {
                "item_id": 112233,
                "item_name": "Táo Envy New Zealand Size 30",
                "item_sku": "SKU-APPLE-ENVY",
                "model_quantity_purchased": 2,
                "model_discounted_price": 120000.0
            }
        ]
    }

    raw_body = json.dumps(shopee_payload).encode("utf-8")
    mock_db = AsyncMock()

    mock_order = UnifiedOrder(
        id="ord-shopee-uuid-1",
        order_code="NAM-SHP-20260905-998877",
        channel_order_id="260905SHP998877",
        display_order_id="998877",
        status=UnifiedOrderStatus.ACCEPTED,
    )

    with patch("app.services.order_service.OrderService.create_or_get_inbound_order", return_value=(mock_order, True)) as mock_create:
        resp = await adapter.handle_order_webhook(headers={}, raw_body=raw_body, db=mock_db)

        assert resp["code"] == 0
        assert resp["message"] == "success"
        assert resp["order_sn"] == "260905SHP998877"
        assert resp["internal_order_code"] == "NAM-SHP-20260905-998877"
        assert resp["created"] is True

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["channel_code"] == "SHOPEEMART"
        assert call_kwargs["partner_store_id"] == "10001"
        assert call_kwargs["channel_order_id"] == "260905SHP998877"
        assert call_kwargs["order_data"]["total_amount"] == 250000.0
        assert call_kwargs["order_data"]["customer_name"] == "Nguyễn Văn A"
        assert call_kwargs["items_data"][0]["sku"] == "SKU-APPLE-ENVY"
        assert call_kwargs["items_data"][0]["quantity"] == 2


@pytest.mark.asyncio
async def test_shopeemart_handle_order_webhook_status_update():
    """Verify webhook handles existing order with status change to CANCELLED."""
    adapter = ShopeeMartChannelAdapter()

    shopee_payload = {
        "order_sn": "260905SHP998877",
        "shop_id": 10001,
        "status": "CANCELLED",
        "total_amount": 250000.0,
    }

    raw_body = json.dumps(shopee_payload).encode("utf-8")
    mock_db = AsyncMock()

    mock_existing_order = UnifiedOrder(
        id="ord-shopee-uuid-1",
        order_code="NAM-SHP-20260905-998877",
        channel_order_id="260905SHP998877",
        display_order_id="998877",
        status=UnifiedOrderStatus.ACCEPTED,
    )

    with patch("app.services.order_service.OrderService.create_or_get_inbound_order", return_value=(mock_existing_order, False)), \
         patch("app.services.order_service.OrderService.update_order_status") as mock_update_status:

        resp = await adapter.handle_order_webhook(headers={}, raw_body=raw_body, db=mock_db)

        assert resp["code"] == 0
        assert resp["created"] is False
        mock_update_status.assert_called_once()
        update_args = mock_update_status.call_args.kwargs
        assert update_args["order_id"] == "ord-shopee-uuid-1"
        assert update_args["payload"].new_status == UnifiedOrderStatus.CANCELLED


# ==============================================================================
# 4. Inventory Batch Update & Outbound Status Operations
# ==============================================================================

@pytest.mark.asyncio
async def test_shopeemart_inventory_sync():
    """Verify batch inventory sync formats properly for v2.product.update_stock."""
    adapter = ShopeeMartChannelAdapter()

    stock_items = [
        {"sku": "SKU-SALMON-200G", "is_out_of_stock": False, "available_stock": 18, "price": 180000},
        {"sku": "SKU-MILK-MEIJI", "is_out_of_stock": True, "available_stock": 0, "price": 85000},
    ]

    mock_db = AsyncMock()

    with patch.object(shopeemart_client, "update_stock", return_value={"error": "", "message": "success"}) as mock_stock:
        result = await adapter.sync_inventory(
            store_id="store-1",
            partner_store_id="10001",
            stock_items=stock_items,
            db=mock_db,
        )

        assert result.success is True
        assert result.total_synced == 2
        mock_stock.assert_called_once()

        call_args = mock_stock.call_args.kwargs
        assert call_args["shop_id"] == "10001"
        assert len(call_args["stock_list"]) == 2
        assert call_args["stock_list"][0]["seller_sku"] == "SKU-SALMON-200G"
        assert call_args["stock_list"][0]["normal_stock"] == 18
        assert call_args["stock_list"][1]["seller_sku"] == "SKU-MILK-MEIJI"
        assert call_args["stock_list"][1]["normal_stock"] == 0


@pytest.mark.asyncio
async def test_shopeemart_outbound_order_transitions():
    """Verify outbound state updates trigger ship_order and cancel_order APIs."""
    adapter = ShopeeMartChannelAdapter()
    mock_db = AsyncMock()

    with patch.object(shopeemart_client, "ship_order") as mock_ship, \
         patch.object(shopeemart_client, "cancel_order") as mock_cancel:

        # 1. READY (Ready to Ship)
        success_ready = await adapter.update_order_status("260905SHP111", "10001", UnifiedOrderStatus.READY, mock_db)
        assert success_ready is True
        mock_ship.assert_called_once_with(order_sn="260905SHP111", shop_id="10001")

        # 2. CANCELLED
        success_cancel = await adapter.update_order_status(
            "260905SHP111", "10001", UnifiedOrderStatus.CANCELLED, mock_db, reason="OUT_OF_STOCK"
        )
        assert success_cancel is True
        mock_cancel.assert_called_once_with(order_sn="260905SHP111", shop_id="10001", cancel_reason="OUT_OF_STOCK")


# ==============================================================================
# 5. Product Listing & Outlet Publish Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_shopeemart_add_item_and_publish_outlet():
    """Verify adding item to Mart and publishing to Outlet shops."""
    sm_client = ShopeeMartClient()

    # 1. Add item simulation
    item_payload = {
        "item_name": "Sữa Chua Hy Lạp Lên Men Tự Nhiên 200g",
        "description": "Sữa chua hữu cơ nguyên chất",
        "original_price": 55000.0,
        "normal_stock": 50,
        "category_id": 10020,
        "seller_sku": "SKU-YOGURT-GREEK"
    }
    add_res = await sm_client.add_item(shop_id="10001", item_data=item_payload)
    assert add_res["message"] == "sandbox_mode_success"

    # 2. Publish to Outlet shops
    publish_res = await sm_client.publish_item_to_outlet_shop(
        shop_id="10001",
        item_id=889900,
        outlet_shop_id_list=[10001, 10004, 10005, 10006]
    )
    assert publish_res["message"] == "sandbox_mode_success"


# ==============================================================================
# 6. Logistics, Tracking & Airway Bill (AWB) Document Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_shopeemart_logistics_and_awb():
    """Verify tracking number retrieval and AWB document creation."""
    sm_client = ShopeeMartClient()

    # 1. Get tracking number
    track_res = await sm_client.get_tracking_number("260905SHP111", shop_id="10001")
    assert track_res["message"] == "sandbox_mode_success"

    # 2. Create shipping document
    create_doc_res = await sm_client.create_shipping_document("260905SHP111", shop_id="10001")
    assert create_doc_res["message"] == "sandbox_mode_success"

    # 3. Download shipping document
    download_res = await sm_client.download_shipping_document("260905SHP111", shop_id="10001")
    assert download_res["message"] == "sandbox_mode_success"


# ==============================================================================
# 7. Buyer Cancellation & Return/Refund Management Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_shopeemart_buyer_cancellation_and_returns():
    """Verify buyer cancellation response and return dispute lifecycle."""
    sm_client = ShopeeMartClient()

    # 1. Handle buyer cancellation
    buyer_cancel_res = await sm_client.handle_buyer_cancellation("260905SHP111", operation="ACCEPT")
    assert buyer_cancel_res["message"] == "sandbox_mode_success"

    # 2. Return list & detail
    returns_res = await sm_client.get_return_list(shop_id="10001")
    assert returns_res["message"] == "sandbox_mode_success"

    ret_detail = await sm_client.get_return_detail("RET-260905-001", shop_id="10001")
    assert ret_detail["message"] == "sandbox_mode_success"

    # 3. Confirm return
    confirm_res = await sm_client.confirm_return("RET-260905-001", shop_id="10001")
    assert confirm_res["message"] == "sandbox_mode_success"

    # 4. Dispute return
    dispute_res = await sm_client.dispute_return(
        return_sn="RET-260905-001",
        dispute_reason="SELLER_DISPUTE_DAMAGED_ITEM",
        dispute_text_reason="Hàng giao còn nguyên vẹn tem bảo hành",
        shop_id="10001"
    )
    assert dispute_res["message"] == "sandbox_mode_success"


# ==============================================================================
# 8. Escrow Settlement & Financials Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_shopeemart_escrow_financials():
    """Verify escrow settlement query."""
    sm_client = ShopeeMartClient()
    escrow_res = await sm_client.get_escrow_detail("260905SHP111", shop_id="10001")
    assert escrow_res["message"] == "sandbox_mode_success"

