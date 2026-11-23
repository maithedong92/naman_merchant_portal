import json
import pathlib
import re
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_current_user, get_db
from app.core.database import Base
from app.core.exceptions import ExternalChannelError, ValidationError
from app.main import app
from app.models.channel import Channel
from app.models.inventory import InventoryStockLog, StoreInventory
from app.models.order import OrderItem, OrderStatusHistory, UnifiedOrder, UnifiedOrderStatus
from app.models.product import Product
from app.models.store import Store, StoreChannelMapping
from app.models.user import User, UserRole
from app.modules.grabmart.adapter import GrabMartChannelAdapter
from app.modules.grabmart.service import GrabMartClient
from app.modules.shopeefood.adapter import ShopeeFoodChannelAdapter
from app.schemas.order import OrderEditItemInput, OrderEditRequest
from app.services.channel_registry import channel_registry
from app.services.channel_service import channel_service
from app.services.inventory_service import InventoryService
from app.services.order_service import OrderService

grabmart_adapter = GrabMartChannelAdapter()
shopeefood_adapter = ShopeeFoodChannelAdapter()


@pytest.fixture
async def edit_db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        store_10001 = Store(id="store-10001", code="10001", name="Thảo Điền")
        ch_grab = Channel(id="ch-grab", code="GRABMART", name="GrabMart", is_active=True)
        ch_foody = Channel(id="ch-foody", code="SHOPEEFOOD", name="ShopeeFood", is_active=True)
        session.add_all([store_10001, ch_grab, ch_foody])
        await session.flush()

        session.add_all([
            StoreChannelMapping(
                store_id="store-10001",
                channel_id="ch-grab",
                partner_store_id="grab-mer-10001",
                is_active=True,
            ),
            StoreChannelMapping(
                store_id="store-10001",
                channel_id="ch-foody",
                partner_store_id="foody-rest-10001",
                is_active=True,
            ),
        ])

        p1 = Product(id="prod-1", sku="SKU-A", name="Sữa Tươi A", base_price=50000.0, is_active=True)
        p2 = Product(id="prod-2", sku="SKU-B", name="Bánh Mì B", base_price=30000.0, is_active=True)
        p3 = Product(id="prod-3", sku="SKU-C", name="Trái Cây C (Thay thế)", base_price=40000.0, is_active=True)
        p4 = Product(id="prod-4", sku="SKU-W", name="Cá Hồi Ký", base_price=200000.0, is_active=True)
        session.add_all([p1, p2, p3, p4])
        await session.flush()

        # Pre-reserved for Grab order (2x SKU-A, 1x SKU-B)
        inv_a = StoreInventory(
            store_id="store-10001",
            product_id="prod-1",
            sku="SKU-A",
            total_stock=10.0,
            reserved_stock=2.0,
            available_stock=8.0,
            safety_stock=0.0,
            is_out_of_stock=False,
        )
        inv_b = StoreInventory(
            store_id="store-10001",
            product_id="prod-2",
            sku="SKU-B",
            total_stock=5.0,
            reserved_stock=1.0,
            available_stock=4.0,
            safety_stock=0.0,
            is_out_of_stock=False,
        )
        inv_c = StoreInventory(
            store_id="store-10001",
            product_id="prod-3",
            sku="SKU-C",
            total_stock=6.0,
            reserved_stock=0.0,
            available_stock=6.0,
            safety_stock=0.0,
            is_out_of_stock=False,
        )
        inv_w = StoreInventory(
            store_id="store-10001",
            product_id="prod-4",
            sku="SKU-W",
            total_stock=5.0,
            reserved_stock=1.0,
            available_stock=4.0,
            safety_stock=0.0,
            is_out_of_stock=False,
        )
        session.add_all([inv_a, inv_b, inv_c, inv_w])

        grab_raw_payload = {
            "orderID": "GM-2026-001",
            "shortOrderNumber": "GF-001",
            "merchantID": "grab-mer-10001",
            "currency": {"code": "VND", "symbol": "đ", "exponent": 0},
            "receiver": {
                "name": "Nguyen Van A",
                "virtualContact": {
                    "phoneNumber": "+842899990001",
                    "PIN": "1234",
                    "expiredAt": "2026-11-23T18:00:00Z",
                    "status": "ACTIVE",
                },
            },
            "items": [
                {
                    "id": "SKU-A",
                    "grabItemID": "VNITE-A",
                    "name": "Sữa Tươi A",
                    "quantity": 2,
                    "price": 50000,
                    "soldByWeight": False,
                    "outOfStockInstruction": {
                        "title": "Đổi sang Trái Cây C",
                        "instructionType": "SPECIFY_REPLACEMENT",
                        "replacementItemID": "SKU-C",
                    },
                },
                {
                    "id": "SKU-B",
                    "grabItemID": "VNITE-B",
                    "name": "Bánh Mì B",
                    "quantity": 1,
                    "price": 30000,
                    "soldByWeight": False,
                },
            ],
        }

        grab_order = UnifiedOrder(
            id="ord-grab-1",
            order_code="GRAB-GM-001",
            channel_id="ch-grab",
            store_id="store-10001",
            channel_order_id="GM-2026-001",
            display_order_id="GF-001",
            status=UnifiedOrderStatus.ACCEPTED,
            subtotal_amount=130000.0,
            discount_amount=0.0,
            delivery_fee=15000.0,
            total_amount=145000.0,
            customer_name="Nguyen Van A",
            stock_reservation_status="RESERVED",
            raw_payload=grab_raw_payload,
        )
        session.add(grab_order)
        await session.flush()

        session.add_all([
            OrderItem(
                order_id="ord-grab-1",
                product_id="prod-1",
                sku="SKU-A",
                item_name="Sữa Tươi A",
                quantity=2,
                unit_price=50000.0,
                total_price=100000.0,
            ),
            OrderItem(
                order_id="ord-grab-1",
                product_id="prod-2",
                sku="SKU-B",
                item_name="Bánh Mì B",
                quantity=1,
                unit_price=30000.0,
                total_price=30000.0,
            ),
        ])

        foody_order = UnifiedOrder(
            id="ord-foody-1",
            order_code="FOODY-1001",
            channel_id="ch-foody",
            store_id="store-10001",
            channel_order_id="SF-CODE-99",
            display_order_id="SF99",
            status=UnifiedOrderStatus.PREPARING,
            subtotal_amount=100000.0,
            discount_amount=0.0,
            delivery_fee=10000.0,
            total_amount=110000.0,
            customer_name="Tran Thi B",
            stock_reservation_status="RESERVED",
            raw_payload={"code": "SF-CODE-99"},
        )
        session.add(foody_order)
        await session.flush()
        session.add(
            OrderItem(
                order_id="ord-foody-1",
                product_id="prod-1",
                sku="SKU-A",
                item_name="Sữa Tươi A",
                quantity=2,
                unit_price=50000.0,
                total_price=100000.0,
            )
        )
        await session.commit()
        yield session

    await engine.dispose()


async def test_grabmart_client_edit_order_request_and_error_translation():
    client = GrabMartClient()
    items_payload = [{"itemID": "SKU-A", "status": "UPDATED", "quantity": 1}]

    with patch.object(client, "call_api", new=AsyncMock(return_value={})) as mock_call:
        resp = await client.edit_order("GM 123/4", items=items_payload, only_recalculate=True)
        assert resp == {}
        mock_call.assert_awaited_once()
        method, endpoint = mock_call.await_args.args[:2]
        assert method == "PUT"
        assert endpoint == "/partner/v1/orders/GM%20123%2F4"
        assert mock_call.await_args.kwargs["body"] == {
            "orderID": "GM 123/4",
            "items": items_payload,
            "onlyRecalculate": True,
        }

    # 4xx translation in Vietnamese
    req = httpx.Request("PUT", "https://partner-api.grab.com/partner/v1/orders/GM-1")
    err_400 = httpx.Response(400, request=req, json={"reason": "not editable", "message": "not editable"})
    with patch.object(client, "call_api", new=AsyncMock(side_effect=httpx.HTTPStatusError("bad", request=req, response=err_400))):
        with pytest.raises(ValidationError) as exc_info:
            await client.edit_order("GM-1", items=items_payload)
        assert "không ở trạng thái cho phép chỉnh sửa" in str(exc_info.value)

    # 5xx raises ExternalChannelError
    err_502 = httpx.Response(502, request=req, json={"message": "upstream failure"})
    with patch.object(client, "call_api", new=AsyncMock(side_effect=httpx.HTTPStatusError("gw", request=req, response=err_502))):
        with pytest.raises(ExternalChannelError):
            await client.edit_order("GM-1", items=items_payload)


async def test_order_service_edit_validation_guards(edit_db_session):
    db = edit_db_session
    channel_registry.register(grabmart_adapter)

    # 1. Requires customer_confirmed=True
    with pytest.raises(ValidationError, match="khách hàng đồng ý"):
        await OrderService.edit_order(
            "ord-grab-1",
            OrderEditRequest(
                items=[OrderEditItemInput(sku="SKU-A", status="UPDATED", quantity=1)],
                customer_confirmed=False,
                edit_reason="Hết hàng",
            ),
            changed_by="staff1",
            db=db,
        )

    # 2. Cannot delete all active items
    with pytest.raises(ValidationError, match="ít nhất 1 món"):
        await OrderService.edit_order(
            "ord-grab-1",
            OrderEditRequest(
                items=[
                    OrderEditItemInput(sku="SKU-A", status="DELETED", quantity=0),
                    OrderEditItemInput(sku="SKU-B", status="DELETED", quantity=0),
                ],
                customer_confirmed=True,
                edit_reason="Khách hủy hết",
            ),
            changed_by="staff1",
            db=db,
        )

    # 3. Disallow price/weight adjustment when soldByWeight=False
    with pytest.raises(ValidationError, match="soldByWeight=true"):
        await OrderService.edit_order(
            "ord-grab-1",
            OrderEditRequest(
                items=[
                    OrderEditItemInput(sku="SKU-A", status="UPDATED", quantity=2, adjusted_weight=1.5),
                    OrderEditItemInput(sku="SKU-B", status="UNCHANGED", quantity=1),
                ],
                customer_confirmed=True,
                edit_reason="Sửa cân nặng",
            ),
            changed_by="staff1",
            db=db,
        )

    # 4. Disallow edit in READY status
    ord_obj = await OrderService.get_order_by_id("ord-grab-1", db)
    ord_obj.status = UnifiedOrderStatus.READY
    await db.commit()

    with pytest.raises(ValidationError, match="PENDING / ACCEPTED / PREPARING"):
        await OrderService.edit_order(
            "ord-grab-1",
            OrderEditRequest(
                items=[OrderEditItemInput(sku="SKU-A", status="UPDATED", quantity=1)],
                customer_confirmed=True,
                edit_reason="Đổi muộn",
            ),
            changed_by="staff1",
            db=db,
        )


async def test_order_service_edit_success_stock_delta_and_only_recalculate(edit_db_session):
    db = edit_db_session
    channel_registry.register(grabmart_adapter)

    with patch.object(grabmart_adapter, "sync_inventory", new=AsyncMock()):
        with patch("app.modules.grabmart.adapter.grabmart_client.edit_order", new=AsyncMock(return_value={})) as mock_edit:
            # First: only_recalculate=True calls Grab without mutating local order or stock
            recalc_order = await OrderService.edit_order(
                "ord-grab-1",
                OrderEditRequest(
                    items=[
                        OrderEditItemInput(sku="SKU-A", grab_item_id="VNITE-A", status="UPDATED", quantity=1),
                        OrderEditItemInput(sku="SKU-B", grab_item_id="VNITE-B", status="DELETED", quantity=0),
                        OrderEditItemInput(sku="SKU-C", status="ADDED", quantity=2),
                    ],
                    customer_confirmed=True,
                    edit_reason="Kiểm tra hợp lệ",
                    only_recalculate=True,
                ),
                changed_by="staff1",
                db=db,
            )
            assert mock_edit.await_count == 1
            assert mock_edit.await_args.kwargs["only_recalculate"] is True
            assert len(recalc_order.items) == 2

            # Now: actual edit
            # Original order had SKU-A:2, SKU-B:1 -> new order: SKU-A:1, SKU-B:DELETED(0), SKU-C:ADDED(2),
            # while testing UNCHANGED item too on second edit or keeping one UNCHANGED
            mock_edit.reset_mock()

    with patch.object(grabmart_adapter, "sync_inventory", new=AsyncMock()):
        with patch("app.modules.grabmart.adapter.grabmart_client.edit_order", new=AsyncMock(return_value={})) as mock_edit:
            updated = await OrderService.edit_order(
                "ord-grab-1",
                OrderEditRequest(
                    items=[
                        OrderEditItemInput(sku="SKU-B", grab_item_id="VNITE-B", status="UNCHANGED", quantity=1),
                        OrderEditItemInput(sku="SKU-C", status="ADDED", quantity=2),
                        OrderEditItemInput(sku="SKU-A", grab_item_id="VNITE-A", status="DELETED", quantity=0),
                    ],
                    customer_confirmed=True,
                    edit_reason="Hết sữa A, khách đồng ý đổi sang C",
                    only_recalculate=False,
                ),
                changed_by="staff_td",
                db=db,
            )

    assert mock_edit.await_count == 1
    sent_items = mock_edit.await_args.kwargs["items"]
    # GrabMart spec: original items first, ADDED appended at end; UNCHANGED mapped to UPDATED
    assert sent_items[-1]["itemID"] == "SKU-C"
    assert sent_items[-1]["status"] == "ADDED"
    assert sent_items[-1]["isExternalItemID"] is True
    assert sent_items[0]["itemID"] == "VNITE-B"
    assert sent_items[0]["status"] == "UPDATED"
    assert sent_items[1]["itemID"] == "VNITE-A"
    assert sent_items[1]["status"] == "DELETED"

    # Verify updated order state & properties
    assert updated.is_edited is True
    assert updated.virtual_contact is not None
    assert updated.virtual_contact["PIN"] == "1234"
    assert updated.grab_edit_context["canEditByStatus"] is True
    item_map = {it.sku: it for it in updated.items}
    assert set(item_map.keys()) == {"SKU-B", "SKU-C"}
    assert item_map["SKU-C"].quantity == 2
    assert updated.subtotal_amount == 110000.0  # 1*30000 + 2*40000
    assert updated.total_amount == 125000.0     # + 15000 delivery fee

    # Verify virtual stock delta: SKU-A released -2 (reserved 2 -> 0, avail 8 -> 10),
    # SKU-B delta 0 (reserved stays 1, avail 4), SKU-C +2 (reserved 0 -> 2, avail 6 -> 4)
    invs = {
        row.sku: row
        for row in (await db.execute(select(StoreInventory).where(StoreInventory.store_id == "store-10001"))).scalars().all()
    }
    assert invs["SKU-A"].reserved_stock == 0.0
    assert invs["SKU-A"].available_stock == 10.0
    assert invs["SKU-B"].reserved_stock == 1.0
    assert invs["SKU-C"].reserved_stock == 2.0
    assert invs["SKU-C"].available_stock == 4.0

    logs = (
        await db.execute(select(InventoryStockLog).where(InventoryStockLog.change_type == "ORDER_EDIT"))
    ).scalars().all()
    assert {l.sku: l.quantity_delta for l in logs} == {"SKU-A": 2.0, "SKU-C": -2.0}

    histories = (
        await db.execute(select(OrderStatusHistory).where(OrderStatusHistory.order_id == "ord-grab-1"))
    ).scalars().all()
    assert any("staff_td" == h.changed_by and "Hết sữa A" in (h.note or "") for h in histories)


async def test_grabmart_is_mex_edit_order_webhook_idempotent_and_external(edit_db_session):
    db = edit_db_session

    # Scenario 1: External edit from Grab Merchant App arrives via isMexEditOrder webhook
    # Original order: SKU-A:2, SKU-B:1. Grab sends: SKU-A:1, SKU-C:1 (B deleted).
    webhook_payload = {
        "orderID": "GM-2026-001",
        "shortOrderNumber": "GF-001",
        "merchantID": "grab-mer-10001",
        "partnerMerchantID": "10001",
        "orderState": "ACCEPTED",
        "featureFlags": {"orderType": "DELIVERY", "isMexEditOrder": True},
        "price": {"subtotal": 90000, "merchantChargeFee": 0, "deliveryFee": 15000, "eaterPayment": 105000},
        "items": [
            {"id": "SKU-A", "grabItemID": "VNITE-A", "name": "Sữa Tươi A", "quantity": 1, "price": 50000},
            {"id": "SKU-C", "grabItemID": "VNITE-C", "name": "Trái Cây C", "quantity": 1, "price": 40000},
        ],
    }

    with patch.object(channel_service, "is_feature_enabled", new=AsyncMock(return_value=True)):
        with patch.object(grabmart_adapter, "sync_inventory", new=AsyncMock()):
            res1 = await grabmart_adapter.handle_order_webhook(
                headers={},
                raw_body=json.dumps(webhook_payload).encode("utf-8"),
                db=db,
            )
            assert res1["created"] is False

    invs = {
        row.sku: row
        for row in (await db.execute(select(StoreInventory).where(StoreInventory.store_id == "store-10001"))).scalars().all()
    }
    # SKU-A: 2 -> 1 (reserved 1, avail 9)
    # SKU-B: 1 -> 0 (reserved 0, avail 5)
    # SKU-C: 0 -> 1 (reserved 1, avail 5)
    assert invs["SKU-A"].reserved_stock == 1.0
    assert invs["SKU-B"].reserved_stock == 0.0
    assert invs["SKU-C"].reserved_stock == 1.0

    # Scenario 2: Replaying identical webhook (or echo after Portal edit) does NOT double-deduct
    with patch.object(channel_service, "is_feature_enabled", new=AsyncMock(return_value=True)):
        with patch.object(grabmart_adapter, "sync_inventory", new=AsyncMock()):
            await grabmart_adapter.handle_order_webhook(
                headers={},
                raw_body=json.dumps(webhook_payload).encode("utf-8"),
                db=db,
            )

    invs_after = {
        row.sku: row
        for row in (await db.execute(select(StoreInventory).where(StoreInventory.store_id == "store-10001"))).scalars().all()
    }
    assert invs_after["SKU-A"].reserved_stock == 1.0
    assert invs_after["SKU-B"].reserved_stock == 0.0
    assert invs_after["SKU-C"].reserved_stock == 1.0


async def test_shopeefood_update_type_1_and_manual_sync(edit_db_session):
    db = edit_db_session
    channel_registry.register(shopeefood_adapter)

    s2s_details_reply = {
        "reply": {
            "code": "SF-CODE-99",
            "status": 4,
            "restaurant": {"partner_restaurant_id": "foody-rest-10001"},
            "order_value": {"value": 80000},
            "total_merchant_discount": {"value": 0},
            "customer_bill": {"shipping_fee": {"value": 10000}},
            "total_value": {"value": 90000},
            "dish_groups": [
                {
                    "dishes": [
                        {"partner_dish_id": "SKU-C", "dish_name": "Trái Cây C", "quantity": 2, "original_price": {"value": 40000}}
                    ]
                }
            ],
        }
    }
    webhook_body = {
        "restaurant_id": 888,
        "partner_restaurant_id": "foody-rest-10001",
        "order_code": "SF-CODE-99",
        "status": 4,
        "update_type": 1,
    }

    with patch.object(channel_service, "is_feature_enabled", new=AsyncMock(return_value=True)):
        with patch.object(shopeefood_adapter, "sync_inventory", new=AsyncMock()):
            with patch("app.modules.shopeefood.adapter.shopeefood_client.get_order_details", new=AsyncMock(return_value=s2s_details_reply)):
                await shopeefood_adapter.handle_order_webhook(
                    headers={},
                    raw_body=json.dumps(webhook_body).encode("utf-8"),
                    db=db,
                    verified_order_details=s2s_details_reply["reply"],
                )

    foody_ord = await OrderService.get_order_by_id("ord-foody-1", db)
    assert [it.sku for it in foody_ord.items] == ["SKU-C"]
    assert foody_ord.total_amount == 90000.0

    # Manual sync-detail endpoint test via refresh_order_from_channel
    s2s_details_reply["reply"]["dish_groups"][0]["dishes"][0]["quantity"] = 3
    s2s_details_reply["reply"]["order_value"]["value"] = 120000
    s2s_details_reply["reply"]["total_value"]["value"] = 130000
    with patch.object(shopeefood_adapter, "sync_inventory", new=AsyncMock()):
        with patch("app.modules.shopeefood.adapter.shopeefood_client.get_order_details", new=AsyncMock(return_value=s2s_details_reply)):
            refreshed = await shopeefood_adapter.refresh_order_from_channel(foody_ord, db)
    assert refreshed.items[0].quantity == 3
    assert refreshed.total_amount == 130000.0


def test_edit_order_and_sync_detail_api_rbac(edit_db_session):
    client = TestClient(app)

    async def override_db():
        yield edit_db_session

    ro_user = User(id="u-ro", username="viewer", email="v@naman.vn", role=UserRole.READ_ONLY.value, store_id="store-10001", is_active=True)
    cs_user = User(id="u-cs", username="cs1", email="cs@naman.vn", role=UserRole.CS.value, store_id="store-10001", is_active=True)
    other_staff = User(id="u-other", username="st_other", email="o@naman.vn", role=UserRole.STAFF.value, store_id="store-10004", is_active=True)
    branch_staff = User(id="u-td", username="st_td", email="td@naman.vn", role=UserRole.STAFF.value, store_id="store-10001", is_active=True)

    app.dependency_overrides[get_db] = override_db
    edit_body = {
        "customer_confirmed": True,
        "edit_reason": "Khách đồng ý đổi",
        "items": [{"sku": "SKU-A", "status": "UPDATED", "quantity": 1}, {"sku": "SKU-B", "status": "UNCHANGED", "quantity": 1}],
    }

    try:
        # RBAC denials (403)
        for denied_user in (ro_user, cs_user, other_staff):
            app.dependency_overrides[get_current_user] = lambda u=denied_user: u
            res = client.put("/api/v1/orders/ord-grab-1/edit", json=edit_body)
            assert res.status_code == 403

        # Allowed same-store STAFF -> 200 OK
        channel_registry.register(grabmart_adapter)
        app.dependency_overrides[get_current_user] = lambda: branch_staff
        with patch.object(grabmart_adapter, "sync_inventory", new=AsyncMock()):
            with patch("app.modules.grabmart.adapter.grabmart_client.edit_order", new=AsyncMock(return_value={})):
                ok_res = client.put("/api/v1/orders/ord-grab-1/edit", json=edit_body)
        assert ok_res.status_code == 200
        payload = ok_res.json()
        assert payload["success"] is True
        assert payload["data"]["is_edited"] is True
        assert payload["data"]["virtual_contact"]["PIN"] == "1234"
        assert payload["data"]["grab_edit_context"]["canEditByStatus"] is True

        # ShopeeFood outbound PUT /edit returns 422 ValidationError (channel only supports Merchant App edit + S2S pull)
        channel_registry.register(shopeefood_adapter)
        foody_edit = client.put("/api/v1/orders/ord-foody-1/edit", json=edit_body)
        assert foody_edit.status_code == 422

        # Unsupported sync-detail on GrabMart returns 422 ValidationError
        grab_sync = client.post("/api/v1/orders/ord-grab-1/sync-detail")
        assert grab_sync.status_code == 422

        # Supported sync-detail on ShopeeFood returns 200
        s2s_ok = {
            "reply": {
                "code": "SF-CODE-99",
                "status": 4,
                "restaurant": {"partner_restaurant_id": "foody-rest-10001"},
                "order_value": {"value": 50000},
                "total_value": {"value": 60000},
                "customer_bill": {"shipping_fee": {"value": 10000}},
                "dish_groups": [{"dishes": [{"partner_dish_id": "SKU-A", "dish_name": "Sữa Tươi A", "quantity": 1, "original_price": {"value": 50000}}]}],
            }
        }
        with patch.object(shopeefood_adapter, "sync_inventory", new=AsyncMock()):
            with patch("app.modules.shopeefood.adapter.shopeefood_client.get_order_details", new=AsyncMock(return_value=s2s_ok)):
                sf_sync = client.post("/api/v1/orders/ord-foody-1/sync-detail")
        assert sf_sync.status_code == 200
        assert sf_sync.json()["data"]["total_amount"] == 60000.0
    finally:
        app.dependency_overrides.clear()


def test_orders_html_edit_order_ui_and_security_invariants():
    orders_path = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates" / "orders.html"
    html = orders_path.read_text(encoding="utf-8")

    # UI markers for GrabMart 2-way Edit & ShopeeFood sync
    for marker in (
        'id="edit-order-modal"',
        'id="edit-order-confirmed"',
        'id="edit-order-reason"',
        "openEditOrderModal",
        "submitEditOrder",
        "syncShopeeFoodOrderDetail",
        "order.virtual_contact",
        "outOfStockInstruction",
        "encodeURIComponent(orderId)",
    ):
        assert marker in html, f"Missing expected UI marker in orders.html: {marker}"

    # No raw template interpolation inside onclick handlers
    bad_onclick = re.findall(r'onclick="[^"]*\$\{[^"]*"', html)
    assert not bad_onclick, f"Unsafe template interpolation inside onclick: {bad_onclick}"
