from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_current_active_user, get_current_user, get_db
from app.core.exceptions import BadRequestError
from app.main import app
from app.models.inventory import InventoryStockLog, StoreInventory
from app.models.order import OrderItem, UnifiedOrder, UnifiedOrderStatus
from app.models.user import User, UserRole
from app.schemas.order import OrderStatusUpdateSchema
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


def test_read_only_user_blocked_from_order_and_inventory_mutations(client, read_only_user):
    """Verify READ_ONLY user receives 403 on Haravan, GrabMart, ShopeeMart, and Inventory mutations."""
    app.dependency_overrides[get_current_user] = lambda: read_only_user
    app.dependency_overrides[get_current_active_user] = lambda: read_only_user
    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        r_hrv_pack = client.post("/api/v1/haravan/orders/12345/pack")
        assert r_hrv_pack.status_code == 403

        r_hrv_cancel = client.post(
            "/api/v1/haravan/orders/12345/cancel",
            json={"reason": "Test", "note": "Note"},
        )
        assert r_hrv_cancel.status_code == 403

        r_grab_cancel = client.post(
            "/api/v1/grabmart/orders/ORD-1/cancel",
            json={"orderID": "ORD-1", "merchantID": "10001", "cancelCode": 1001},
        )
        assert r_grab_cancel.status_code == 403

        r_sm_cancel = client.post(
            "/api/v1/shopeemart/orders/SN123/cancel",
            json={"order_sn": "SN123", "cancel_reason": "OUT_OF_STOCK"},
        )
        assert r_sm_cancel.status_code == 403

        r_inv_toggle = client.patch(
            "/api/v1/inventory/toggle",
            json={"store_code": "10001", "sku": "SKU-1", "is_out_of_stock": True},
        )
        assert r_inv_toggle.status_code == 403
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


@pytest.mark.asyncio
async def test_order_service_catchup_accepted_to_delivered_releases_reserved_stock():
    """Verify ACCEPTED -> DELIVERED catch-up transition is allowed, releases reserved_stock, deducts total_stock, and logs ORDER_COMPLETE."""
    order = UnifiedOrder(
        id="ord-1",
        order_code="NAM-SM-20261109-001",
        channel_id="ch-sm",
        store_id="st-10001",
        channel_order_id="261109ABC",
        status=UnifiedOrderStatus.ACCEPTED,
        subtotal_amount=100000.0,
        total_amount=100000.0,
    )
    order.items = [
        OrderItem(
            id="it-1",
            order_id="ord-1",
            sku="290031441000",
            item_name="Trứng gà Kata",
            quantity=2,
            unit_price=50000.0,
            total_price=100000.0,
        )
    ]
    inv = StoreInventory(
        id="inv-1",
        store_id="st-10001",
        product_id="p-1",
        sku="290031441000",
        total_stock=10.0,
        reserved_stock=2.0,
        available_stock=6.0,
        safety_stock=2.0,
        is_out_of_stock=False,
    )

    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    res_ord = MagicMock()
    res_ord.scalar_one_or_none.return_value = order
    res_inv = MagicMock()
    res_inv.scalars.return_value.first.return_value = inv
    res_inv.scalar_one_or_none.return_value = inv
    mock_db.execute.side_effect = [res_ord, res_inv, res_ord]

    updated = await OrderService.update_order_status(
        db=mock_db,
        order_id="ord-1",
        payload=OrderStatusUpdateSchema(
            new_status=UnifiedOrderStatus.DELIVERED,
            note="ShopeeMart webhook COMPLETED",
            changed_by="SHOPEEMART_WEBHOOK",
        ),
        sync_to_channel=False,
    )

    assert updated.status == UnifiedOrderStatus.DELIVERED
    assert updated.completed_at is not None
    assert float(inv.reserved_stock) == 0.0
    assert float(inv.total_stock) == 8.0
    assert float(inv.available_stock) == 6.0  # 8 total - 0 reserved - 2 safety
    added_objs = [c.args[0] for c in mock_db.add.call_args_list]
    assert any(isinstance(o, InventoryStockLog) and o.change_type == "ORDER_COMPLETE" for o in added_objs)


def test_file_service_blocks_ssrf_urls_and_allows_trusted_host():
    """Verify FileService.clean_file_url blocks internal/metadata SSRF URLs."""
    assert file_service.clean_file_url("https://namanapi.namanmarket.com/uploads/bill_1.pdf") == "https://namanapi.namanmarket.com/uploads/bill_1.pdf"
    assert file_service.clean_file_url("http://169.254.169.254/latest/meta-data/") == ""
    assert file_service.clean_file_url("http://host.docker.internal:6515/api/v1/pos/master-data/health") == ""


@pytest.mark.asyncio
async def test_haravan_regex_escape_and_cancelled_state_guard(read_only_user):
    """Verify Haravan search escapes regex metacharacters (+84) and blocks completing a cancelled order."""
    svc = HaravanOrderService(mongo_uri="mongodb://localhost:27017")
    mock_col = MagicMock()
    mock_col.count_documents.return_value = 0
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value.skip.return_value.limit.return_value = []
    mock_col.find.return_value = mock_cursor

    admin = User(id="u-adm", username="admin", role=UserRole.SUPER_ADMIN.value, is_active=True, is_superuser=True)
    svc._get_orders_collection = MagicMock(return_value=mock_col)

    items, total = svc.list_orders_sync(
        current_user=admin,
        unassigned_only=True,
        search="+84901234567 (VIP)",
    )
    assert total == 0
    assert items == []
    built_query = mock_col.count_documents.call_args[0][0]
    assert "$and" in built_query
    assert len(built_query["$and"]) == 2

    # Completing an already-cancelled order must raise BadRequestError
    svc.get_order_detail = AsyncMock(
        return_value={"id": "999", "order_number": "#NA999", "internal": {"order_processing_status": "Đã hủy"}}
    )
    with pytest.raises(BadRequestError):
        await svc.complete_order(
            order_id_or_sn="999",
            actual_total=100000.0,
            actual_shipping=15000.0,
            actual_carrier="GrabExpress",
            tracking_code="TRK1",
            note="Test",
            current_user=admin,
        )


def test_csv_formula_injection_sanitizer():
    """Verify CSV cells starting with =, +, -, @ are prefixed with a single quote."""
    assert ReportService._sanitize_csv_cell("=1+1") == "'=1+1"
    assert ReportService._sanitize_csv_cell("+84901234567") == "'+84901234567"
    assert ReportService._sanitize_csv_cell("@SUM(A1:A2)") == "'@SUM(A1:A2)"
    assert ReportService._sanitize_csv_cell("Nguyễn Văn A") == "Nguyễn Văn A"
