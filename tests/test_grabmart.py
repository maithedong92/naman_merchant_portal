import json
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_current_active_user, get_db
from app.main import app
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.order import OrderItem, OrderStatusHistory, UnifiedOrder, UnifiedOrderStatus
from app.models.product import Category, Product
from app.models.store import Store, StoreChannelMapping
from app.models.user import User, UserRole
from app.modules.grabmart.adapter import GrabMartChannelAdapter
from app.modules.grabmart.schemas import (
    GrabMartMenuPayload,
    GrabPushOrderStateWebhook,
    GrabSubmitOrderWebhook,
)
from app.modules.grabmart.service import GrabMartClient, grabmart_client


@pytest.fixture
def client():
    return TestClient(app)


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


# ==============================================================================
# 1. OAuth & Authentication Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_grabmart_client_token_simulation():
    """Client in sandbox/unconfigured mode safely returns a valid token."""
    client = GrabMartClient()
    token = await client.get_access_token()
    assert token is not None
    assert len(token) > 0


def test_grabmart_partner_oauth_webhook_endpoint(client):
    """GrabMart calls POST /api/v1/grabmart/oauth/token to authenticate."""
    payload = {
        "client_id": "grabmart_pos_system",
        "client_secret": "grabmart_secret_123",
        "grant_type": "client_credentials",
        "scope": "grabmart.partner.pos"
    }
    response = client.post("/api/v1/grabmart/oauth/token", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["token_type"] == "Bearer"
    assert "access_token" in data
    assert data["expires_in"] == 86400


# ==============================================================================
# 2. Menu Sync & Catalog Tests (GrabMart v1.1.3)
# ==============================================================================

@pytest.mark.asyncio
async def test_grabmart_catalog_menu_structure():
    """Verify built GrabMart menu conforms strictly to v1.1.3 specification."""
    adapter = GrabMartChannelAdapter()

    # Mock products and inventory
    cat = Category(id="cat-1", code="fresh_produce", name="Rau Củ Tươi", sequence=1)
    prod1 = Product(
        id="prod-1",
        sku="SKU-TOMATO",
        name="Cà Chua Beef Hữu Cơ",
        base_price=45000.0,
        category=cat,
        image_url="https://namanmarket.com/images/tomato.jpg",
        barcode="8931234567890",
        is_active=True,
    )
    inv1 = StoreInventory(
        store_id="store-1",
        product_id="prod-1",
        available_stock=25,
        is_out_of_stock=False,
    )

    prod2 = Product(
        id="prod-2",
        sku="SKU-AVOCADO",
        name="Bơ 034 Lâm Đồng",
        base_price=80000.0,
        category=cat,
        is_active=True,
    )
    inv2 = StoreInventory(
        store_id="store-1",
        product_id="prod-2",
        available_stock=0,
        is_out_of_stock=True,
    )

    mock_db = AsyncMock()
    mock_res = MagicMock()
    mock_res.all.return_value = [(prod1, inv1), (prod2, inv2)]
    mock_db.execute = AsyncMock(return_value=mock_res)

    menu = await adapter.build_catalog_menu(
        store_id="store-1",
        partner_store_id="GM-THAO-DIEN",
        db=mock_db,
    )

    # 1. Currency validation (Vietnam: code VND, symbol ₫, exponent 0)
    assert menu["currency"]["code"] == "VND"
    assert menu["currency"]["symbol"] == "₫"
    assert menu["currency"]["exponent"] == 0

    # 2. Selling Times validation (v1.1.3 requirement: 1..20 schedules)
    assert len(menu["sellingTimes"]) >= 1
    schedule = menu["sellingTimes"][0]
    assert schedule["id"] == "standard_schedule"
    assert "serviceHours" in schedule
    assert schedule["serviceHours"]["mon"]["openPeriodType"] == "OpenPeriod"

    # 3. Categories and Subcategories validation
    assert len(menu["categories"]) == 1
    category = menu["categories"][0]
    assert category["id"] == "fresh_produce"
    assert len(category["subcategories"]) == 1

    items = category["subcategories"][0]["items"]
    assert len(items) == 2

    # In-stock item
    tomato = next(i for i in items if i["id"] == "SKU-TOMATO")
    assert tomato["availableStatus"] == "AVAILABLE"
    assert tomato["price"] == 45000
    assert tomato["maxStock"] == 25
    assert tomato["barcodes"] == ["8931234567890"]

    # Out-of-stock item: maxStock MUST be 0 when UNAVAILABLE
    avocado = next(i for i in items if i["id"] == "SKU-AVOCADO")
    assert avocado["availableStatus"] == "UNAVAILABLE"
    assert avocado["maxStock"] == 0


def test_grabmart_get_menu_webhook_pull(client):
    """GrabMart pulls store menu via GET /api/v1/grabmart/menu."""
    adapter = GrabMartChannelAdapter()
    adapter._store_menu_cache = getattr(adapter, "_store_menu_cache", {})
    
    # Seed cache
    from app.modules.grabmart.adapter import _store_menu_cache
    _store_menu_cache["GM-TEST-STORE"] = {
        "merchantID": "GM-TEST-STORE",
        "currency": {"code": "VND", "symbol": "₫", "exponent": 0},
        "sellingTimes": [],
        "categories": [],
    }

    response = client.get("/api/v1/grabmart/menu?merchantID=GM-TEST-STORE")
    assert response.status_code == 200
    data = response.json()
    assert data["merchantID"] == "GM-TEST-STORE"
    assert data["currency"]["code"] == "VND"


# ==============================================================================
# 3. Inbound Submit Order Webhook Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_grabmart_handle_submit_order_webhook():
    """Verify parsing and storing of GrabMart Submit Order Webhook payload."""
    adapter = GrabMartChannelAdapter()

    grab_order_payload = {
        "orderID": "GM-ORD-998877",
        "shortOrderNumber": "GM-102",
        "merchantID": "1-CYNGRUNGSBCCC",
        "partnerMerchantID": "10001",
        "paymentType": "CASHLESS",
        "orderTime": "2026-09-04T10:00:00Z",
        "currency": {"code": "VND", "symbol": "₫", "exponent": 0},
        "featureFlags": {
            "orderAcceptedType": "AUTO",
            "orderType": "DeliveredByGrab",
            "isMexEditOrder": False,
        },
        "items": [
            {
                "id": "SKU-MILK-1L",
                "grabItemID": "GRAB-ITM-1",
                "name": "Sữa Tươi Thanh Trùng 1L",
                "quantity": 2,
                "price": 38000,
                "specifications": "Hạn sử dụng xa nhất",
            }
        ],
        "price": {
            "subtotal": 76000,
            "tax": 0,
            "deliveryFee": 15000,
            "merchantFundPromo": 5000,
            "grabFundPromo": 0,
            "total": 86000,
        },
        "receiver": {
            "name": "Nguyễn Văn A",
            "phones": "0901234567",
            "address": {
                "address": "21 Thảo Điền, P. Thảo Điền, TP. Thủ Đức",
                "deliveryInstruction": "Giao cổng bảo vệ",
            },
        },
    }

    raw_body = json.dumps(grab_order_payload).encode("utf-8")
    mock_db = AsyncMock()

    # Mock OrderService return
    mock_order = UnifiedOrder(
        id="ord-uuid-1",
        order_code="NAM-20260904-GM102",
        channel_order_id="GM-ORD-998877",
        display_order_id="GM-102",
        status=UnifiedOrderStatus.ACCEPTED,
    )

    with patch("app.services.order_service.OrderService.create_or_get_inbound_order", return_value=(mock_order, True)) as mock_create:
        resp = await adapter.handle_order_webhook(headers={}, raw_body=raw_body, db=mock_db)

        assert resp["status"] == "ACCEPTED"
        assert resp["orderID"] == "GM-ORD-998877"
        assert resp["shortOrderNumber"] == "GM-102"
        assert resp["internal_order_code"] == "NAM-20260904-GM102"
        assert resp["created"] is True

        # Verify args passed to OrderService
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["channel_code"] == "GRABMART"
        assert call_kwargs["partner_store_id"] == "10001"
        assert call_kwargs["channel_order_id"] == "GM-ORD-998877"
        assert call_kwargs["order_data"]["subtotal_amount"] == 76000.0
        assert call_kwargs["order_data"]["total_amount"] == 86000.0
        assert "Ghi chú giao: Giao cổng bảo vệ" in call_kwargs["order_data"]["delivery_address"]
        assert call_kwargs["items_data"][0]["sku"] == "SKU-MILK-1L"
        assert call_kwargs["items_data"][0]["quantity"] == 2


# ==============================================================================
# 4. Push Order State Webhook Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_grabmart_push_order_state_transitions():
    """Verify Push Order State Webhook transitions order states correctly."""
    adapter = GrabMartChannelAdapter()

    mock_db = AsyncMock()

    # Mock Channel
    mock_channel = Channel(id="chan-gm-id", code="GRABMART", name="GrabMart VN")
    
    # Mock Order
    mock_order = UnifiedOrder(
        id="ord-uuid-2",
        order_code="NAM-GM-2",
        channel_id="chan-gm-id",
        channel_order_id="GM-ORD-555",
        status=UnifiedOrderStatus.ACCEPTED,
    )

    mock_db.execute.side_effect = [
        MagicMock(scalar_one_or_none=MagicMock(return_value=mock_channel)),
        MagicMock(scalar_one_or_none=MagicMock(return_value=mock_order)),
    ]

    state_payload = {
        "merchantID": "1-CYNGRUNGSBCCC",
        "partnerMerchantID": "10001",
        "orderID": "GM-ORD-555",
        "state": "COLLECTED",
        "driverETA": None,
        "message": "Driver picked up package",
    }

    raw_body = json.dumps(state_payload).encode("utf-8")

    with patch("app.services.order_service.OrderService.update_order_status") as mock_update:
        resp = await adapter.handle_order_state_webhook(headers={}, raw_body=raw_body, db=mock_db)

        assert resp["status"] == "OK"
        assert resp["state"] == "COLLECTED"

        # Verify OrderService updated status to PICKED_UP
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["order_id"] == "ord-uuid-2"
        assert call_kwargs["payload"].new_status == UnifiedOrderStatus.PICKED_UP
        assert call_kwargs["payload"].changed_by == "GRABMART_WEBHOOK"


# ==============================================================================
# 5. Real-Time Stock Sync Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_grabmart_inventory_sync():
    """Verify batch inventory sync formats correctly for GrabMart."""
    adapter = GrabMartChannelAdapter()

    stock_items = [
        {"sku": "SKU-001", "is_out_of_stock": False, "available_stock": 15, "price": 50000},
        {"sku": "SKU-002", "is_out_of_stock": True, "available_stock": 0, "price": 30000},
    ]

    mock_db = AsyncMock()

    with patch.object(grabmart_client, "batch_update_items", return_value={"total_updated": 2}) as mock_batch:
        result = await adapter.sync_inventory(
            store_id="store-1",
            partner_store_id="GM-THAO-DIEN",
            stock_items=stock_items,
            db=mock_db,
        )

        assert result.success is True
        assert result.total_synced == 2
        mock_batch.assert_called_once()
        
        call_items = mock_batch.call_args.kwargs["items"]
        assert len(call_items) == 2
        assert call_items[0]["availableStatus"] == "AVAILABLE"
        assert call_items[0]["maxStock"] == 15
        assert call_items[1]["availableStatus"] == "UNAVAILABLE"
        assert call_items[1]["maxStock"] == 0


# ==============================================================================
# 6. Outbound Order Lifecycle Operations
# ==============================================================================

@pytest.mark.asyncio
async def test_grabmart_outbound_order_status_transitions():
    """Verify outbound state updates call appropriate GrabMart APIs."""
    adapter = GrabMartChannelAdapter()
    mock_db = AsyncMock()

    with patch.object(grabmart_client, "accept_or_reject_order") as mock_accept, \
         patch.object(grabmart_client, "mark_order_ready") as mock_ready, \
         patch.object(grabmart_client, "cancel_order") as mock_cancel:

        # 1. ACCEPTED
        await adapter.update_order_status("GM-ORD-1", "GM-OUTLET", UnifiedOrderStatus.ACCEPTED, mock_db)
        mock_accept.assert_called_once_with("GM-ORD-1", to_state="Accepted")

        # 2. READY
        await adapter.update_order_status("GM-ORD-1", "GM-OUTLET", UnifiedOrderStatus.READY, mock_db)
        mock_ready.assert_called_once_with("GM-ORD-1")

        # 3. CANCELLED
        await adapter.update_order_status("GM-ORD-1", "GM-OUTLET", UnifiedOrderStatus.CANCELLED, mock_db, reason="Shop closed")
        mock_cancel.assert_called_once_with(order_id="GM-ORD-1", merchant_id="GM-OUTLET", cancel_code=1001)
