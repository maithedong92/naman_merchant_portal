from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from app.api.deps import (
    get_current_active_user,
    get_current_user,
    get_db,
    require_staff,
    require_store_manager,
)
from app.core.database import get_optional_db
from app.core.exceptions import BadRequestError, ForbiddenError
from app.main import app
from app.models.channel import Channel
from app.models.inventory import InventoryStockLog, StoreInventory
from app.models.order import OrderItem, UnifiedOrder, UnifiedOrderStatus
from app.models.store import Store
from app.models.user import User, UserRole
from app.modules.grabmart.adapter import GrabMartChannelAdapter
from app.modules.grabmart.router import settings as grab_router_settings
from app.modules.shopeefood.adapter import ShopeeFoodChannelAdapter
from app.modules.shopeemart.adapter import ShopeeMartChannelAdapter, normalize_shopee_cancel_reason
from app.modules.shopeemart.service import shopeemart_client
from app.schemas.order import OrderStatusUpdateSchema
from app.services.arito_service import AritoInventoryService
from app.services.file_service import file_service
from app.services.haravan_order_service import HaravanOrderService
from app.services.order_service import OrderService
from app.services.report_service import ReportService


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def read_only_user():
    return User(
        id="ro-uuid-1",
        username="auditor_ro",
        email="ro@namanmarket.com",
        role=UserRole.READ_ONLY.value,
        store_id=None,
        is_active=True,
        is_superuser=False,
    )


@pytest.fixture
def unassigned_staff_user():
    return User(
        id="staff-no-store",
        username="staff_hq",
        email="staff_hq@namanmarket.com",
        role=UserRole.STAFF.value,
        store_id=None,
        is_active=True,
        is_superuser=False,
    )


@pytest.fixture
def branch_10001_staff():
    u = User(
        id="staff-10001",
        username="staff_10001",
        email="s10001@namanmarket.com",
        role=UserRole.STAFF.value,
        store_id="st-10001",
        is_active=True,
        is_superuser=False,
    )
    u.store = Store(id="st-10001", code="10001", name="Thảo Điền", is_active=True)
    return u


@pytest.fixture
def branch_10001_manager():
    u = User(
        id="mgr-10001",
        username="manager_10001",
        email="m10001@namanmarket.com",
        role=UserRole.STORE_MANAGER.value,
        store_id="st-10001",
        is_active=True,
        is_superuser=False,
    )
    u.store = Store(id="st-10001", code="10001", name="Thảo Điền", is_active=True)
    return u


# ==============================================================================
# 1. WEBHOOK SIGNATURE VERIFICATION, PROXY URL RECONSTRUCTION & FAIL-CLOSED 500
# ==============================================================================

def test_shopeefood_webhook_hmac_verification_and_proxy_missing_header_block(client):
    """Verify ShopeeFood webhook rejects invalid signatures (401) and missing Authorization from proxy (401)."""
    payload = b'{"id":"SF-999","order_code":"SF999","status":"NEW"}'

    # 1. Forged signature -> 401
    r_bad = client.post(
        "/api/v1/shopeefood/webhooks/order",
        content=payload,
        headers={"Authorization": "Signature deadbeef0000", "Content-Type": "application/json"},
    )
    assert r_bad.status_code == 401

    # 2. Missing Authorization header when arriving through reverse proxy (X-Forwarded-Proto) -> 401
    r_missing = client.post(
        "/api/v1/shopeefood/webhooks/order",
        content=payload,
        headers={"X-Forwarded-Proto": "https", "Content-Type": "application/json"},
    )
    assert r_missing.status_code == 401


def test_shopeemart_webhook_proxy_url_signature_and_missing_header_block(client):
    """Verify ShopeeMart webhook verifies signature against reconstructed public https:// URL and blocks forged/missing headers."""
    body = b'{"code":3,"shop_id":12345,"data":{"ordersn":"261109PROXY1","order_status":"READY_TO_SHIP"}}'
    public_url = "https://portal.namanmarket.com/api/v1/shopeemart/webhooks/order"

    with patch.object(shopeemart_client, "partner_id", "200999"), \
         patch.object(shopeemart_client, "partner_key", "secret_shopee_key_2026"), \
         patch("app.modules.shopeemart.router.adapter.handle_order_webhook", new_callable=AsyncMock, return_value={"code": 0, "message": "success"}):
        # 1. Missing Authorization header with X-Forwarded-Proto -> 401
        r_no_auth = client.post(
            "/api/v1/shopeemart/webhooks/order",
            content=body,
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "portal.namanmarket.com"},
        )
        assert r_no_auth.status_code == 401

        # 2. Invalid signature -> 401
        r_invalid = client.post(
            "/api/v1/shopeemart/webhooks/order",
            content=body,
            headers={
                "Authorization": "invalid_hmac_hex",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "portal.namanmarket.com",
            },
        )
        assert r_invalid.status_code == 401

        # 3. Valid signature computed against public HTTPS URL (behind Nginx TLS termination) -> 200
        base_str = f"{public_url}|{body.decode('utf-8')}"
        valid_sig = hmac.new(
            b"secret_shopee_key_2026", base_str.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        r_valid = client.post(
            "/api/v1/shopeemart/webhooks/order",
            content=body,
            headers={
                "Authorization": valid_sig,
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "portal.namanmarket.com",
            },
        )
        assert r_valid.status_code == 200
        assert r_valid.json()["code"] == 0


def test_grabmart_oauth_secret_check_and_webhook_500_on_db_crash(client):
    """Verify GrabMart rejects invalid client_secret when configured and returns HTTP 500 (not 200 ACCEPTED) on DB failure."""
    with patch.object(grab_router_settings, "GRABMART_CLIENT_SECRET", "real_prod_grab_secret_999"):
        r_bad_oauth = client.post(
            "/api/v1/grabmart/oauth/token",
            json={"client_id": "grab", "client_secret": "wrong_attacker_secret"},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert r_bad_oauth.status_code == 401

        r_good_oauth = client.post(
            "/api/v1/grabmart/oauth/token",
            json={"client_id": "grab", "client_secret": "real_prod_grab_secret_999"},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert r_good_oauth.status_code == 200
        assert "access_token" in r_good_oauth.json()

    # Verify DB crash during GrabMart order ingestion rolls back and returns 500 FAILED (so Grab retries!)
    mock_db = AsyncMock()
    app.dependency_overrides[get_optional_db] = lambda: mock_db
    try:
        with patch("app.modules.grabmart.router.adapter.handle_order_webhook", new_callable=AsyncMock, side_effect=RuntimeError("PG deadlock")), \
             patch("app.modules.grabmart.router.error_service.log_error", new_callable=AsyncMock):
            r_crash = client.post(
                "/api/v1/grabmart/webhooks/order",
                json={"orderID": "GM-CRASH-1", "merchantID": "10001"},
            )
            assert r_crash.status_code == 500
            assert r_crash.json()["status"] == "FAILED"
            mock_db.rollback.assert_awaited()
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 2. RBAC, CROSS-BRANCH ISOLATION, PII PROTECTION & AUDIT SPOOFING
# ==============================================================================

def test_unauthenticated_shopee_awb_download_blocked(client):
    """Verify GET /api/v1/shopeemart/orders/{order_sn}/download-awb requires authentication (401)."""
    app.dependency_overrides.clear()
    res = client.get("/api/v1/shopeemart/orders/261109PII/download-awb")
    assert res.status_code == 401


def test_read_only_user_blocked_from_all_order_and_inventory_mutations(client, read_only_user):
    """Verify READ_ONLY user receives 403 on Haravan, GrabMart, ShopeeMart, UnifiedOrder, and Inventory mutations."""
    app.dependency_overrides[get_current_user] = lambda: read_only_user
    app.dependency_overrides[get_current_active_user] = lambda: read_only_user
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    try:
        for path, body in [
            ("/api/v1/haravan/orders/12345/pack", None),
            ("/api/v1/haravan/orders/12345/reassign", {"reason": "Hết hàng", "note": "Chuyển kho"}),
            ("/api/v1/haravan/orders/12345/ship", {"carrier": "GrabExpress", "tracking_code": "TRK1"}),
            ("/api/v1/haravan/orders/12345/complete", {"actual_total": 100000, "actual_shipping": 15000}),
            ("/api/v1/haravan/orders/12345/cancel", {"reason": "Khách hủy", "note": "Note"}),
            ("/api/v1/haravan/orders/12345/admin-note", {"note": "Ghi chú"}),
            ("/api/v1/grabmart/orders/ORD-1/prepare", {"orderID": "ORD-1", "toState": "Accepted"}),
            ("/api/v1/grabmart/orders/ORD-1/ready", {"orderID": "ORD-1"}),
            ("/api/v1/grabmart/orders/ORD-1/cancel", {"orderID": "ORD-1", "merchantID": "10001", "cancelCode": 1001}),
            ("/api/v1/shopeemart/orders/SN123/ready", {"order_sn": "SN123"}),
            ("/api/v1/shopeemart/orders/SN123/cancel", {"order_sn": "SN123", "cancel_reason": "OUT_OF_STOCK"}),
            ("/api/v1/shopeemart/orders/SN123/buyer-cancellation", {"order_sn": "SN123", "operation": "ACCEPT"}),
        ]:
            resp = client.post(path, json=body) if body is not None else client.post(path)
            assert resp.status_code == 403, f"Expected 403 for READ_ONLY on POST {path}, got {resp.status_code}"

        r_del_file = client.delete("/api/v1/haravan/orders/12345/files/order")
        assert r_del_file.status_code == 403

        r_patch_ord = client.patch("/api/v1/orders/ord-1/status", json={"new_status": "ACCEPTED"})
        assert r_patch_ord.status_code == 403

        r_inv_toggle = client.patch(
            "/api/v1/inventory/toggle",
            json={"store_code": "10001", "sku": "SKU-1", "is_out_of_stock": True},
        )
        assert r_inv_toggle.status_code == 403

        r_inv_quick = client.patch(
            "/api/v1/inventory/quick-update",
            json={"store_code": "10001", "sku": "SKU-1", "available_stock": 10},
        )
        assert r_inv_quick.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_cross_branch_staff_blocked_from_mutating_other_store_orders(client, branch_10001_staff):
    """Verify STAFF assigned to 10001 (st-10001) gets 403 when trying to cancel/mutate Store 10004 (st-10004) orders or inventory."""
    app.dependency_overrides[get_current_user] = lambda: branch_10001_staff
    app.dependency_overrides[get_current_active_user] = lambda: branch_10001_staff
    app.dependency_overrides[require_staff] = lambda: branch_10001_staff

    other_store_order = UnifiedOrder(
        id="ord-10004",
        order_code="NAM-GM-20261109-999",
        channel_id="ch-grab",
        store_id="st-10004",
        channel_order_id="GM-10004-ORD",
        status=UnifiedOrderStatus.ACCEPTED,
    )
    mock_db = AsyncMock()
    res_ord = MagicMock()
    res_ord.scalar_one_or_none.return_value = other_store_order
    res_ord.scalars.return_value.first.return_value = other_store_order
    mock_db.execute.return_value = res_ord
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        with patch.object(OrderService, "get_order_by_id", new_callable=AsyncMock, return_value=other_store_order):
            # 1. PATCH /api/v1/orders/{id}/status on st-10004 order -> 403
            r_u = client.patch("/api/v1/orders/ord-10004/status", json={"new_status": "READY"})
            assert r_u.status_code == 403

            # 2. POST /api/v1/grabmart/orders/{id}/cancel on st-10004 order -> 403
            r_g = client.post(
                "/api/v1/grabmart/orders/GM-10004-ORD/cancel",
                json={"orderID": "GM-10004-ORD", "merchantID": "10004", "cancelCode": 1001},
            )
            assert r_g.status_code == 403

            # 3. POST /api/v1/shopeemart/orders/{id}/cancel on st-10004 order -> 403
            r_s = client.post(
                "/api/v1/shopeemart/orders/GM-10004-ORD/cancel",
                json={"order_sn": "GM-10004-ORD", "cancel_reason": "OUT_OF_STOCK"},
            )
            assert r_s.status_code == 403

        # 4. PATCH /api/v1/inventory/toggle on store_code="10004" -> 403
        r_inv = client.patch(
            "/api/v1/inventory/toggle",
            json={"store_code": "10004", "sku": "SKU-1", "is_out_of_stock": True},
        )
        assert r_inv.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_patch_order_status_overrides_spoofed_changed_by(client, branch_10001_staff):
    """Verify PATCH /api/v1/orders/{id}/status overwrites payload.changed_by with current_user.username."""
    app.dependency_overrides[get_current_user] = lambda: branch_10001_staff
    app.dependency_overrides[require_staff] = lambda: branch_10001_staff
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    own_order = UnifiedOrder(
        id="ord-10001",
        order_code="NAM-GM-20261109-111",
        channel_id="ch-grab",
        store_id="st-10001",
        channel_order_id="GM-111",
        status=UnifiedOrderStatus.READY,
        subtotal_amount=50000.0,
        total_amount=50000.0,
        order_time=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    own_order.items = []
    own_order.status_history = []

    try:
        with patch.object(OrderService, "get_order_by_id", new_callable=AsyncMock, return_value=own_order), \
             patch.object(OrderService, "update_order_status", new_callable=AsyncMock, return_value=own_order) as mock_upd:
            res = client.patch(
                "/api/v1/orders/ord-10001/status",
                json={"new_status": "READY", "changed_by": "SPOOFED_SUPER_ADMIN"},
            )
            assert res.status_code == 200
            passed_payload = mock_upd.call_args.kwargs.get("payload") or mock_upd.call_args.args[1]
            assert passed_payload.changed_by == "staff_10001"
    finally:
        app.dependency_overrides.clear()


def test_unassigned_staff_reports_analytics_returns_valid_empty_report(client, unassigned_staff_user):
    """Verify unassigned STAFF calling GET /api/v1/reports/analytics gets a valid 200 empty report instead of 500 ValidationError."""
    app.dependency_overrides[get_current_user] = lambda: unassigned_staff_user
    app.dependency_overrides[get_current_active_user] = lambda: unassigned_staff_user
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    try:
        res = client.get("/api/v1/reports/analytics")
        assert res.status_code == 200
        body = res.json()
        assert body["success"] is True
        assert body["data"]["summary"]["total_orders"] == 0
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 3. HARAVAN APIResponse.fail, FILE SERVICE SSRF/XSS, STATE MACHINE & ARITO
# ==============================================================================

def test_haravan_error_paths_return_clean_apiresponse_fail_without_500(client, branch_10001_manager):
    """Verify all 5 error paths in app/api/v1/haravan.py use APIResponse.fail instead of crashing with AttributeError: APIResponse.error."""
    app.dependency_overrides[get_current_user] = lambda: branch_10001_manager
    app.dependency_overrides[require_staff] = lambda: branch_10001_manager
    app.dependency_overrides[require_store_manager] = lambda: branch_10001_manager

    try:
        # 1. /sync error path
        with patch("app.api.v1.haravan.haravan_sync_worker.sync_once", new_callable=AsyncMock, return_value={"success": False, "error": "Haravan timeout"}):
            r_sync = client.post("/api/v1/haravan/sync")
            assert r_sync.status_code == 200
            assert r_sync.json()["success"] is False
            assert r_sync.json()["error_code"] == "HARAVAN_SYNC_ERROR"

        # 2. /files/upload invalid file_type
        r_bad_type = client.post(
            "/api/v1/haravan/orders/123/files/upload",
            data={"file_type": "invalid_type"},
            files={"file": ("bill.pdf", b"%PDF-1.4 test", "application/pdf")},
        )
        assert r_bad_type.status_code == 200
        assert r_bad_type.json()["success"] is False
        assert r_bad_type.json()["error_code"] == "INVALID_FILE_TYPE"

        # 3. /files/upload empty 0-byte file
        r_empty = client.post(
            "/api/v1/haravan/orders/123/files/upload",
            data={"file_type": "order"},
            files={"file": ("empty.pdf", b"", "application/pdf")},
        )
        assert r_empty.status_code == 200
        assert r_empty.json()["success"] is False
        assert r_empty.json()["error_code"] == "EMPTY_FILE"

        # 4. /files/upload disallowed .exe extension
        r_exe = client.post(
            "/api/v1/haravan/orders/123/files/upload",
            data={"file_type": "order"},
            files={"file": ("malware.exe", b"MZ9000", "application/octet-stream")},
        )
        assert r_exe.status_code == 200
        assert r_exe.json()["success"] is False
        assert r_exe.json()["error_code"] == "FILE_UPLOAD_FAILED"

        # 5. DELETE /files/{file_type} invalid file_type
        r_del_bad = client.delete("/api/v1/haravan/orders/123/files/hacked_field")
        assert r_del_bad.status_code == 200
        assert r_del_bad.json()["success"] is False
        assert r_del_bad.json()["error_code"] == "INVALID_FILE_TYPE"

        # 6. GET /files/{file_type}/download security headers (nosniff + sandbox CSP)
        with patch("app.api.v1.haravan.haravan_order_service.get_order_detail", new_callable=AsyncMock, return_value={"id": "123", "internal": {"order_file": "https://namanapi.namanmarket.com/uploads/bill.pdf"}}), \
             patch("app.api.v1.haravan.file_service.download_file_bytes", new_callable=AsyncMock, return_value=(True, b"%PDF-1.4 content", "application/pdf", "bill.pdf")):
            r_dl = client.get("/api/v1/haravan/orders/123/files/order/download")
            assert r_dl.status_code == 200
            assert r_dl.headers.get("x-content-type-options") == "nosniff"
            assert "sandbox" in (r_dl.headers.get("content-security-policy") or "")
    finally:
        app.dependency_overrides.clear()


def test_file_service_blocks_ssrf_urls_and_allows_trusted_host():
    """Verify FileService.clean_file_url blocks internal/metadata SSRF URLs."""
    assert file_service.clean_file_url("https://namanapi.namanmarket.com/uploads/bill_1.pdf") == "https://namanapi.namanmarket.com/uploads/bill_1.pdf"
    assert file_service.clean_file_url("http://169.254.169.254/latest/meta-data/") == ""
    assert file_service.clean_file_url("http://host.docker.internal:6515/api/v1/pos/master-data/health") == ""
    assert file_service.clean_file_url("http://127.0.0.1:3002/api/v1/health") == ""


@pytest.mark.asyncio
async def test_haravan_rbac_regex_escape_and_state_machine(unassigned_staff_user, branch_10001_manager):
    """Verify Haravan unassigned STAFF isolation, branch manager cross-store assign block, regex escaping, and terminal state guards."""
    svc = HaravanOrderService(mongo_uri="mongodb://localhost:27017")
    mock_col = MagicMock()
    mock_col.count_documents.return_value = 0
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value.skip.return_value.limit.return_value = []
    mock_col.find.return_value = mock_cursor
    svc._get_orders_collection = MagicMock(return_value=mock_col)

    # 1. Unassigned STAFF (store_id=None) gets 0 orders and cannot view branch orders
    u_items, u_total = svc.list_orders_sync(current_user=unassigned_staff_user)
    assert u_total == 0
    assert u_items == []
    assert svc.count_incomplete_orders_sync(current_user=unassigned_staff_user) == 0

    # 2. Search with +84 and parentheses is escaped and combined with unassigned_only via $and
    admin = User(id="u-adm", username="admin", role=UserRole.SUPER_ADMIN.value, is_active=True, is_superuser=True)
    items, total = svc.list_orders_sync(
        current_user=admin,
        unassigned_only=True,
        search="+84901234567 (VIP)",
    )
    assert total == 0
    built_query = mock_col.count_documents.call_args[0][0]
    assert "$and" in built_query
    assert len(built_query["$and"]) == 2

    # 3. Branch manager at 10001 cannot assign an order to rival branch 10005
    svc.get_order_detail = AsyncMock(
        return_value={"id": "888", "order_number": "#NA888", "line_items": [], "internal": {"order_processing_status": "Đơn hàng mới", "assigned_store": None}}
    )
    with pytest.raises(ForbiddenError):
        await svc.assign_order(
            order_id_or_sn="888",
            store_code="10005",
            reason="Đẩy đơn sang chi nhánh khác",
            note="",
            current_user=branch_10001_manager,
        )

    # 4. Cancelled order cannot be completed, and Completed order cannot be cancelled
    svc.get_order_detail = AsyncMock(
        return_value={"id": "999", "order_number": "#NA999", "internal": {"order_processing_status": "Đã hủy"}}
    )
    with pytest.raises(BadRequestError):
        await svc.complete_order("999", 100000.0, 15000.0, "Grab", "TRK", "Note", admin)

    svc.get_order_detail = AsyncMock(
        return_value={"id": "1000", "order_number": "#NA1000", "internal": {"order_processing_status": "Hoàn thành"}}
    )
    with pytest.raises(BadRequestError):
        await svc.cancel_order("1000", "Đổi ý", "Note", admin)


@pytest.mark.asyncio
async def test_arito_recommend_aggregates_duplicate_skus_and_returns_none_when_zero_stock():
    """Verify AritoInventoryService sums duplicate SKU lines and returns recommended_store=None when all retail branches have 0 stock."""
    svc = AritoInventoryService(mongo_uri="mongodb://localhost:27017")
    # Branch 10001 has 4 units, Branch 10004 has 6 units; order has two lines of SKU_A (3 + 2 = 5 units total)
    mock_stock = {
        "SKU_A": {
            "sku": "SKU_A",
            "branches": {
                "10001": {"branch_name": "Thao Dien", "stock": 4.0},
                "10004": {"branch_name": "Nguyen Luong Bang", "stock": 6.0},
                "10005": {"branch_name": "Mai Chi Tho", "stock": 0.0},
                "10006": {"branch_name": "NVT", "stock": 0.0},
            },
        }
    }
    with patch.object(svc, "get_inventory_by_skus", new_callable=AsyncMock, return_value=mock_stock):
        rec = await svc.recommend_best_store_for_order(
            [{"sku": "SKU_A", "quantity": 3}, {"sku": "SKU_A", "quantity": 2}]
        )
        # 10004 has 6 >= 5 so it fulfills the aggregated SKU, whereas 10001 (4 < 5) does not!
        assert rec["recommended_store"] == "10004"

    # When all branches have 0 stock, recommended_store must be None (not 10001!)
    empty_stock = {
        "SKU_A": {
            "sku": "SKU_A",
            "branches": {code: {"branch_name": code, "stock": 0.0} for code in ("10001", "10004", "10005", "10006")},
        }
    }
    with patch.object(svc, "get_inventory_by_skus", new_callable=AsyncMock, return_value=empty_stock):
        rec_zero = await svc.recommend_best_store_for_order([{"sku": "SKU_A", "quantity": 1}])
        assert rec_zero["recommended_store"] is None


# ==============================================================================
# 4. ORDERSERVICE LIFECYCLE, INVENTORY RESERVATION/ROLLBACK & PARTNER ADAPTERS
# ==============================================================================

@pytest.mark.asyncio
async def test_order_service_lifecycle_reserve_complete_and_cancel_before_vs_after_pickup():
    """Verify OrderService handles ACCEPTED -> DELIVERED stock deduction, PREPARING -> CANCELLED stock restoration, and PICKED_UP -> CANCELLED non-restoration."""
    # Case A: ACCEPTED -> DELIVERED releases reserved_stock and deducts total_stock
    order_a = UnifiedOrder(
        id="ord-a",
        order_code="NAM-SM-20261109-001",
        channel_id="ch-sm",
        store_id="st-10001",
        channel_order_id="261109ABC",
        status=UnifiedOrderStatus.ACCEPTED,
        subtotal_amount=100000.0,
        total_amount=100000.0,
    )
    order_a.items = [OrderItem(id="it-a", order_id="ord-a", sku="SKU-1", item_name="Item 1", quantity=2, unit_price=50000.0, total_price=100000.0)]
    inv_a = StoreInventory(id="inv-a", store_id="st-10001", product_id="p-1", sku="SKU-1", total_stock=10.0, reserved_stock=2.0, available_stock=6.0, safety_stock=2.0, is_out_of_stock=False)

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    res_ord_a = MagicMock()
    res_ord_a.scalar_one_or_none.return_value = order_a
    res_inv_a = MagicMock()
    res_inv_a.scalars.return_value.first.return_value = inv_a
    res_inv_a.scalar_one_or_none.return_value = inv_a
    mock_db.execute.side_effect = [res_ord_a, res_inv_a, res_ord_a]

    updated_a = await OrderService.update_order_status(
        db=mock_db,
        order_id="ord-a",
        payload=OrderStatusUpdateSchema(new_status=UnifiedOrderStatus.DELIVERED, note="Delivered", changed_by="SYSTEM"),
        sync_to_channel=False,
    )
    assert updated_a.status == UnifiedOrderStatus.DELIVERED
    assert updated_a.completed_at is not None
    assert float(inv_a.reserved_stock) == 0.0
    assert float(inv_a.total_stock) == 8.0
    assert float(inv_a.available_stock) == 6.0
    assert any(isinstance(c.args[0], InventoryStockLog) and c.args[0].change_type == "ORDER_COMPLETE" for c in mock_db.add.call_args_list)

    # Case B: PICKED_UP -> CANCELLED must NOT auto-restore shelf stock because driver already took the goods
    order_b = UnifiedOrder(
        id="ord-b",
        order_code="NAM-GM-20261109-002",
        channel_id="ch-gm",
        store_id="st-10001",
        channel_order_id="GM-002",
        status=UnifiedOrderStatus.PICKED_UP,
        subtotal_amount=50000.0,
        total_amount=50000.0,
    )
    order_b.items = [OrderItem(id="it-b", order_id="ord-b", sku="SKU-1", item_name="Item 1", quantity=2, unit_price=25000.0, total_price=50000.0)]
    inv_b = StoreInventory(id="inv-b", store_id="st-10001", product_id="p-1", sku="SKU-1", total_stock=8.0, reserved_stock=0.0, available_stock=6.0, safety_stock=2.0, is_out_of_stock=False)

    mock_db_b = AsyncMock()
    mock_db_b.add = MagicMock()
    res_ord_b = MagicMock()
    res_ord_b.scalar_one_or_none.return_value = order_b
    mock_db_b.execute.side_effect = [res_ord_b, res_ord_b]

    updated_b = await OrderService.update_order_status(
        db=mock_db_b,
        order_id="ord-b",
        payload=OrderStatusUpdateSchema(new_status=UnifiedOrderStatus.CANCELLED, note="Cancelled after pickup", changed_by="SYSTEM"),
        sync_to_channel=False,
    )
    assert updated_b.status == UnifiedOrderStatus.CANCELLED
    assert float(inv_b.total_stock) == 8.0
    assert float(inv_b.available_stock) == 6.0


@pytest.mark.asyncio
async def test_shopeefood_update_order_webhook_transitions_existing_order():
    """Verify ShopeeFood /webhooks/update_order updates driver info and transitions status when created=False."""
    sf_adapter = ShopeeFoodChannelAdapter()
    existing = UnifiedOrder(
        id="ord-sf-1",
        order_code="NAM-SF-20261109-101",
        channel_id="ch-sf",
        store_id="st-10001",
        channel_order_id="SF-101",
        status=UnifiedOrderStatus.ACCEPTED,
    )
    mock_db = AsyncMock()
    webhook_body = json.dumps({
        "order_code": "SF-101",
        "restaurant_id": "10001",
        "order_status": "CANCELLED",
        "cancel_reason": "Khách hàng đổi ý trên ShopeeFood",
        "driver_name": "Nguyễn Tài Xế",
        "driver_phone": "0909111222",
    }).encode("utf-8")

    with patch("app.modules.shopeefood.adapter.channel_service.is_feature_enabled", new_callable=AsyncMock, return_value=True), \
         patch("app.modules.shopeefood.adapter.OrderService.create_or_get_inbound_order", new_callable=AsyncMock, return_value=(existing, False)), \
         patch("app.modules.shopeefood.adapter.OrderService.update_order_status", new_callable=AsyncMock, return_value=existing) as mock_upd:
        res = await sf_adapter.handle_order_webhook({}, webhook_body, mock_db)
        assert res["result"] == "success"
        assert existing.driver_name == "Nguyễn Tài Xế"
        assert existing.driver_phone == "0909111222"
        mock_upd.assert_awaited_once()
        assert mock_upd.call_args.kwargs["payload"].new_status == UnifiedOrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_shopeemart_ship_only_on_ready_and_out_of_stock_item_list_extraction():
    """Verify ShopeeMart calls ship_order only on READY (not PREPARING) and extracts item_list on OUT_OF_STOCK cancel."""
    sm_adapter = ShopeeMartChannelAdapter()
    ord_obj = UnifiedOrder(
        id="ord-sm-1",
        order_code="NAM-SM-20261109-555",
        channel_id="ch-sm",
        store_id="st-10001",
        channel_order_id="261109SM555",
        status=UnifiedOrderStatus.ACCEPTED,
        raw_payload={"item_list": [{"item_id": 112233, "model_id": 4455}]},
    )
    mock_db = AsyncMock()

    with patch.object(shopeemart_client, "ship_order", new_callable=AsyncMock, return_value={"response": {}}) as mock_ship, \
         patch.object(shopeemart_client, "cancel_order", new_callable=AsyncMock, return_value={"response": {}}) as mock_cancel:
        # 1. PREPARING -> must NOT call ship_order
        ok_prep = await sm_adapter.update_order_status(
            "261109SM555", "10001", UnifiedOrderStatus.PREPARING, mock_db, order=ord_obj
        )
        assert ok_prep is True
        mock_ship.assert_not_awaited()

        # 2. READY -> calls ship_order once
        ok_ready = await sm_adapter.update_order_status(
            "261109SM555", "10001", UnifiedOrderStatus.READY, mock_db, order=ord_obj
        )
        assert ok_ready is True
        mock_ship.assert_awaited_once_with(order_sn="261109SM555", shop_id="10001")

        # 3. CANCELLED with Vietnamese reason -> maps to OUT_OF_STOCK and passes extracted item_list
        assert normalize_shopee_cancel_reason("Hết hàng tại siêu thị") == "OUT_OF_STOCK"
        ok_cancel = await sm_adapter.update_order_status(
            "261109SM555",
            "10001",
            UnifiedOrderStatus.CANCELLED,
            mock_db,
            reason="Hết hàng tại siêu thị",
            order=ord_obj,
        )
        assert ok_cancel is True
        mock_cancel.assert_awaited_once_with(
            order_sn="261109SM555",
            shop_id="10001",
            cancel_reason="OUT_OF_STOCK",
            item_list=[{"item_id": 112233, "model_id": 4455}],
        )


# ==============================================================================
# 5. OMNICHANNEL REPORTS (CANCELLED 0 VND, CSV FORMULA SANITIZER) & UI TEMPLATES
# ==============================================================================

@pytest.mark.asyncio
async def test_report_service_cancelled_order_zero_payout_and_csv_formula_escaping():
    """Verify ReportService sets commission=0 and net_amount=0 on CANCELLED orders and escapes formula triggers in CSV export."""
    ch = Channel(id="ch-gm", code="GRAB", name="GrabMart", is_active=True)
    st = Store(id="st-1", code="10001", name="Thảo Điền", is_active=True)
    ord_cancelled = UnifiedOrder(
        id="ord-c1",
        order_code="NAM-GM-20261109-C1",
        channel_id="ch-gm",
        store_id="st-1",
        channel_order_id="=HYPERLINK(\"http://evil\")",
        status=UnifiedOrderStatus.CANCELLED,
        customer_name="+84909999999",
        subtotal_amount=200000.0,
        discount_amount=0.0,
        delivery_fee=15000.0,
        total_amount=215000.0,
        cancellation_reason="@SUM(1+1)",
        order_time=datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc),
    )
    ord_cancelled.channel = ch
    ord_cancelled.store = st

    mock_db = MagicMock()  # Non-AsyncSession MagicMock skips live Mongo fetch deterministically
    res_count = MagicMock()
    res_count.scalar.return_value = 1
    res_count.scalar_one.return_value = 1
    res_list = MagicMock()
    res_list.scalars.return_value.all.return_value = [ord_cancelled]
    mock_db.execute = AsyncMock(side_effect=[res_count, res_list])

    rec_items, rec_total = await ReportService.get_reconciliation_orders(db=mock_db, page=1, page_size=20)
    assert rec_total == 1
    assert len(rec_items) == 1
    item = rec_items[0]
    assert item.status == "CANCELLED"
    assert item.estimated_commission == 0.0
    assert item.estimated_net_amount == 0.0

    # Verify CSV export also sets 0.0 commission/net on CANCELLED and prefixes formula cells with "'"
    mock_db.execute = AsyncMock(return_value=res_list)
    csv_text = await ReportService.export_reconciliation_csv(db=mock_db)
    assert "'=HYPERLINK" in csv_text
    assert "'+84909999999" in csv_text
    assert "'@SUM(1+1)" in csv_text


def test_frontend_templates_enforce_security_and_urgent_order_monitoring():
    """Verify base.html, login.html, orders.html, and reports.html contain all required security & operational guards."""
    templates_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"

    base_html = (templates_dir / "base.html").read_text(encoding="utf-8")
    assert "_refreshTokenPromise" in base_html, "Missing singleton refresh token mutex in base.html"
    assert "msgSpan.textContent" in base_html, "showToast must assign message via textContent to prevent DOM XSS"

    login_html = (templates_dir / "login.html").read_text(encoding="utf-8")
    assert "phhthtnkhk" not in login_html, "Plaintext SuperAdmin password must not appear in login.html"
    assert "NamAn@2026" not in login_html, "Plaintext branch password must not appear in login.html"
    assert "startsWith('/')" in login_html and "!redirect.startsWith('//')" in login_html, "Open redirect guard missing in login.html"

    orders_html = (templates_dir / "orders.html").read_text(encoding="utf-8")
    assert "checkUrgentPlatformOrders" in orders_html, "Urgent platform order background monitor missing in orders.html"
    assert "downloadHaravanFile" in orders_html, "Authenticated blob download helper missing in orders.html"
    assert "@media print" in orders_html and ".no-print" in orders_html, "Thermal AWB @media print CSS missing in orders.html"
    assert "isReadOnly()" in orders_html, "READ_ONLY UI gating helper missing in orders.html"

    reports_html = (templates_dir / "reports.html").read_text(encoding="utf-8")
    assert "!res.ok" in reports_html, "exportReconciliationCsv must check !res.ok before downloading blob"
