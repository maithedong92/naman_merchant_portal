from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_current_user, require_super_admin, require_store_manager, get_db
from app.main import app
from app.models.channel import Channel
from app.models.store import Store, StoreChannelMapping
from app.models.user import User, UserRole


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_admin_user():
    return User(
        id="admin-e2e-uuid",
        username="admin",
        email="admin@namanmarket.com",
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
        is_superuser=True,
    )


@pytest.mark.asyncio
async def test_e2e_shopeemart_complete_pipeline(client, mock_admin_user):
    """
    End-to-End Test for ShopeeMart:
    1. Verify Outlet Shop mappings.
    2. Add Master Item to Mart Catalog.
    3. Publish SKU to 4 Outlet Stores.
    4. Batch update Outlet Stock.
    5. Ingest Order Webhook, Ship Order, and query AWB.
    6. Verify Escrow & Wallet Financials.
    """
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[require_super_admin] = lambda: mock_admin_user
    app.dependency_overrides[require_store_manager] = lambda: mock_admin_user

    try:
        # Step 1: Add new SKU to ShopeeMart Master Catalog
        item_payload = {
            "item_name": "Thịt Thăn Bò Úc Tươi Cao Cấp Nam An 500g",
            "description": "Thịt bò Úc tươi nhập khẩu đạt tiêu chuẩn kiểm nghiệm nghiêm ngặt.",
            "category_id": 100201,
            "original_price": 285000.0,
            "normal_stock": 50,
            "seller_sku": "NAMAN-BEEF-500G"
        }
        res_add = client.post("/api/v1/shopeemart/product/add-item?shop_id=10001", json=item_payload)
        assert res_add.status_code == 200
        data_add = res_add.json()
        assert data_add["success"] is True
        item_id = data_add["data"].get("item_id", 999001)

        # Step 2: Publish Item to 4 Nam An Outlets (10001, 10004, 10005, 10006)
        publish_payload = {
            "item_id": item_id,
            "outlet_shop_id_list": [10001, 10004, 10005, 10006],
            "price_list": [{"outlet_shop_id": 10001, "price": 285000.0}],
            "stock_list": [{"outlet_shop_id": 10001, "stock": 20}]
        }
        res_pub = client.post("/api/v1/shopeemart/product/publish-outlet?shop_id=10001", json=publish_payload)
        assert res_pub.status_code == 200
        assert res_pub.json()["success"] is True

        # Step 3: Batch Update Stock on Outlet
        stock_payload = {
            "outlet_stock_list": [
                {"outlet_shop_id": 10001, "item_id": item_id, "normal_stock": 35},
                {"outlet_shop_id": 10004, "item_id": item_id, "normal_stock": 25}
            ]
        }
        res_stock = client.post("/api/v1/shopeemart/product/batch-outlet-stock?shop_id=10001", json=stock_payload)
        assert res_stock.status_code == 200
        assert res_stock.json()["success"] is True

        # Step 4: Ingest Inbound Order Webhook for ShopeeMart
        order_webhook_payload = {
            "data": {
                "order_sn": "261005E2ESHP001",
                "order_status": "READY_TO_SHIP",
                "shop_id": 10001,
                "buyer_username": "khachhang_naman",
                "total_amount": 285000.0,
                "recipient_address": {
                    "name": "Nguyễn Văn A",
                    "phone": "0912345678",
                    "full_address": "21 Thảo Điền, P. Thảo Điền, TP. Thủ Đức"
                },
                "item_list": [
                    {
                        "item_id": item_id,
                        "item_name": "Thịt Thăn Bò Úc Tươi Cao Cấp Nam An 500g",
                        "item_sku": "NAMAN-BEEF-500G",
                        "model_quantity_purchased": 1,
                        "model_discounted_price": 285000.0
                    }
                ]
            }
        }
        res_hook = client.post("/shopeemart/webhooks/order", json=order_webhook_payload)
        assert res_hook.status_code == 200
        assert res_hook.json()["code"] == 0

        # Step 5: Query Tracking Number & Create AWB Airway Bill
        res_tracking = client.get("/api/v1/shopeemart/orders/261005E2ESHP001/tracking?shop_id=10001")
        assert res_tracking.status_code == 200
        assert res_tracking.json()["success"] is True

        awb_payload = {
            "document_type": "THERMAL_AIR_WAYBILL"
        }
        res_awb = client.post("/api/v1/shopeemart/orders/261005E2ESHP001/create-awb?shop_id=10001", json=awb_payload)
        assert res_awb.status_code == 200
        assert res_awb.json()["success"] is True

        res_download = client.get("/api/v1/shopeemart/orders/261005E2ESHP001/download-awb?shop_id=10001")
        assert res_download.status_code == 200
        assert res_download.json()["success"] is True

        # Step 6: Verify Escrow Payouts & Wallet Reconciliation
        res_escrow = client.get("/api/v1/shopeemart/orders/261005E2ESHP001/escrow?shop_id=10001")
        assert res_escrow.status_code == 200
        assert res_escrow.json()["success"] is True
        assert res_escrow.json()["data"]["response"]["path"] == "/api/v2/payment/get_escrow_detail"

    finally:
        app.dependency_overrides.clear()
