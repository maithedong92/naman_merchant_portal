from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.database import Base
from app.core.exceptions import ValidationError
from app.models.channel import Channel
from app.models.inventory import InventoryStockLog, StoreInventory
from app.models.operational_error import OperationalErrorLog
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.product import Category, Product
from app.models.store import Store, StoreChannelMapping
from app.schemas.order import OrderStatusUpdateSchema
from app.services.inventory_service import InventoryService
from app.services.order_service import OrderService
from app.services.poppies_service import PoppiesMasterDataSyncService


@pytest_asyncio.fixture
async def db_session():
    """Create an isolated in-memory SQLite async database for each test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def _seed_base_entities(
    db: AsyncSession,
    *,
    total_stock: float = 10.0,
    reserved_stock: float = 0.0,
    safety_stock: float = 2.0,
    create_inventory: bool = True,
):
    store = Store(id="st-1", code="10001", name="Thảo Điền", is_active=True)
    channel = Channel(id="ch-gm", code="GRABMART", name="GrabMart", is_active=True)
    category = Category(id="cat-1", code="FRESH", name="Fresh", sequence=1)
    product_a = Product(
        id="prod-a",
        sku="SKU-A",
        name="Sản phẩm A",
        unit="Hộp",
        base_price=50000.0,
        category_id=category.id,
        is_active=True,
    )
    product_b = Product(
        id="prod-b",
        sku="SKU-B",
        name="Sản phẩm B",
        unit="Gói",
        base_price=30000.0,
        category_id=category.id,
        is_active=True,
    )
    mapping = StoreChannelMapping(
        id="scm-1",
        store_id=store.id,
        channel_id=channel.id,
        partner_store_id="GM-10001",
        is_active=True,
    )
    db.add_all([store, channel, category, product_a, product_b, mapping])

    inv_a = None
    if create_inventory:
        avail = InventoryService.compute_available_stock(total_stock, reserved_stock, safety_stock)
        inv_a = StoreInventory(
            id="inv-a",
            store_id=store.id,
            product_id=product_a.id,
            sku="SKU-A",
            total_stock=total_stock,
            reserved_stock=reserved_stock,
            available_stock=avail,
            safety_stock=safety_stock,
            is_out_of_stock=(avail <= 0.0),
        )
        inv_b = StoreInventory(
            id="inv-b",
            store_id=store.id,
            product_id=product_b.id,
            sku="SKU-B",
            total_stock=total_stock,
            reserved_stock=reserved_stock,
            available_stock=avail,
            safety_stock=safety_stock,
            is_out_of_stock=(avail <= 0.0),
        )
        db.add_all([inv_a, inv_b])

    await db.commit()
    return store, channel, inv_a


@pytest.mark.asyncio
async def test_order_ingestion_reserves_virtual_stock(db_session: AsyncSession):
    """Order ingestion increments reserved_stock, reduces available_stock, and sets RESERVED."""
    store, _channel, _inv_a = await _seed_base_entities(db_session, total_stock=10.0, safety_stock=2.0)

    order, created = await OrderService.create_or_get_inbound_order(
        channel_code="GRABMART",
        partner_store_id="GM-10001",
        channel_order_id="GM-ORD-001",
        order_data={"total_amount": 130000.0},
        items_data=[
            {"sku": "SKU-B", "item_name": "B", "quantity": 1, "unit_price": 30000.0, "total_price": 30000.0},
            {"sku": "SKU-A", "item_name": "A", "quantity": 2, "unit_price": 50000.0, "total_price": 100000.0},
            {"sku": "SKU-A", "item_name": "A", "quantity": 1, "unit_price": 50000.0, "total_price": 50000.0},
        ],
        db=db_session,
    )

    assert created is True
    assert order.stock_reservation_status == "RESERVED"

    inv_a = (
        await db_session.execute(
            select(StoreInventory).where(StoreInventory.store_id == store.id, StoreInventory.sku == "SKU-A")
        )
    ).scalar_one()
    inv_b = (
        await db_session.execute(
            select(StoreInventory).where(StoreInventory.store_id == store.id, StoreInventory.sku == "SKU-B")
        )
    ).scalar_one()

    # SKU-A: total=10, reserved=3 (2+1), safety=2 -> available=5
    assert float(inv_a.total_stock) == 10.0
    assert float(inv_a.reserved_stock) == 3.0
    assert float(inv_a.available_stock) == 5.0
    assert inv_a.is_out_of_stock is False

    # SKU-B: total=10, reserved=1, safety=2 -> available=7
    assert float(inv_b.total_stock) == 10.0
    assert float(inv_b.reserved_stock) == 1.0
    assert float(inv_b.available_stock) == 7.0

    logs = (
        await db_session.execute(
            select(InventoryStockLog).where(InventoryStockLog.reference_id == order.order_code)
        )
    ).scalars().all()
    assert {l.sku: l.change_type for l in logs} == {"SKU-A": "ORDER_RESERVE", "SKU-B": "ORDER_RESERVE"}


@pytest.mark.asyncio
async def test_duplicate_order_ingestion_is_idempotent(db_session: AsyncSession):
    """Duplicate ingestion / retry does not double-reserve virtual stock."""
    store, _channel, _inv_a = await _seed_base_entities(db_session, total_stock=10.0, safety_stock=2.0)

    order1, created1 = await OrderService.create_or_get_inbound_order(
        channel_code="GRABMART",
        partner_store_id="GM-10001",
        channel_order_id="GM-DUP-001",
        order_data={"total_amount": 100000.0},
        items_data=[{"sku": "SKU-A", "item_name": "A", "quantity": 2, "unit_price": 50000.0, "total_price": 100000.0}],
        db=db_session,
    )
    order2, created2 = await OrderService.create_or_get_inbound_order(
        channel_code="GRABMART",
        partner_store_id="GM-10001",
        channel_order_id="GM-DUP-001",
        order_data={"total_amount": 100000.0},
        items_data=[{"sku": "SKU-A", "item_name": "A", "quantity": 2, "unit_price": 50000.0, "total_price": 100000.0}],
        db=db_session,
    )

    assert created1 is True
    assert created2 is False
    assert order1.id == order2.id

    inv_a = (
        await db_session.execute(
            select(StoreInventory).where(StoreInventory.store_id == store.id, StoreInventory.sku == "SKU-A")
        )
    ).scalar_one()
    assert float(inv_a.reserved_stock) == 2.0
    assert float(inv_a.available_stock) == 6.0


@pytest.mark.asyncio
async def test_concurrent_integrity_error_returns_existing_order(db_session: AsyncSession):
    """If a concurrent insert triggers IntegrityError on ix_channel_order_unique, rollback and return existing."""
    await _seed_base_entities(db_session, total_stock=10.0, safety_stock=2.0)

    existing, _ = await OrderService.create_or_get_inbound_order(
        channel_code="GRABMART",
        partner_store_id="GM-10001",
        channel_order_id="GM-RACE-001",
        order_data={"total_amount": 50000.0},
        items_data=[{"sku": "SKU-A", "item_name": "A", "quantity": 1, "unit_price": 50000.0, "total_price": 50000.0}],
        db=db_session,
    )

    # Simulate race where initial pre-check returns None, then flush raises IntegrityError
    orig_execute = db_session.execute
    call_count = {"n": 0}

    async def raced_execute(stmt, *args, **kwargs):
        res = await orig_execute(stmt, *args, **kwargs)
        # First UnifiedOrder query (pre-check) returns empty to simulate race condition
        if "unified_orders" in str(stmt) and call_count["n"] == 0:
            call_count["n"] += 1
            fake_res = MagicMock()
            fake_res.scalar_one_or_none.return_value = None
            return fake_res
        return res

    with patch.object(db_session, "execute", side_effect=raced_execute):
        recovered, created = await OrderService.create_or_get_inbound_order(
            channel_code="GRABMART",
            partner_store_id="GM-10001",
            channel_order_id="GM-RACE-001",
            order_data={"total_amount": 50000.0},
            items_data=[{"sku": "SKU-A", "item_name": "A", "quantity": 1, "unit_price": 50000.0, "total_price": 50000.0}],
            db=db_session,
        )

    assert created is False
    assert recovered.id == existing.id


@pytest.mark.asyncio
async def test_cancel_releases_reserved_stock(db_session: AsyncSession):
    """Pre-fulfillment cancellation releases reserved_stock and sets stock_reservation_status='RELEASED'."""
    store, _channel, _inv_a = await _seed_base_entities(db_session, total_stock=5.0, safety_stock=2.0)

    order, _ = await OrderService.create_or_get_inbound_order(
        channel_code="GRABMART",
        partner_store_id="GM-10001",
        channel_order_id="GM-CAN-001",
        order_data={"total_amount": 150000.0},
        items_data=[{"sku": "SKU-A", "item_name": "A", "quantity": 3, "unit_price": 50000.0, "total_price": 150000.0}],
        db=db_session,
    )

    inv_mid = (
        await db_session.execute(
            select(StoreInventory).where(StoreInventory.store_id == store.id, StoreInventory.sku == "SKU-A")
        )
    ).scalar_one()
    assert float(inv_mid.available_stock) == 0.0
    assert inv_mid.is_out_of_stock is True

    cancelled = await OrderService.update_order_status(
        order_id=order.id,
        payload=OrderStatusUpdateSchema(
            new_status=UnifiedOrderStatus.CANCELLED,
            cancellation_reason="Customer cancelled",
            changed_by="STAFF",
        ),
        db=db_session,
        sync_to_channel=False,
    )

    assert cancelled.status == UnifiedOrderStatus.CANCELLED
    assert cancelled.stock_reservation_status == "RELEASED"

    inv_after = (
        await db_session.execute(
            select(StoreInventory).where(StoreInventory.store_id == store.id, StoreInventory.sku == "SKU-A")
        )
    ).scalar_one()
    assert float(inv_after.total_stock) == 5.0
    assert float(inv_after.reserved_stock) == 0.0
    assert float(inv_after.available_stock) == 3.0
    assert inv_after.is_out_of_stock is False


@pytest.mark.asyncio
async def test_delivered_and_picked_up_release_reserved_without_touching_total_stock(db_session: AsyncSession):
    """PICKED_UP / DELIVERED releases reserved_stock ('CONSUMED') WITHOUT decrementing total_stock."""
    store, _channel, _inv_a = await _seed_base_entities(db_session, total_stock=10.0, safety_stock=2.0)

    order, _ = await OrderService.create_or_get_inbound_order(
        channel_code="GRABMART",
        partner_store_id="GM-10001",
        channel_order_id="GM-DEL-001",
        order_data={"total_amount": 100000.0},
        items_data=[{"sku": "SKU-A", "item_name": "A", "quantity": 2, "unit_price": 50000.0, "total_price": 100000.0}],
        db=db_session,
    )

    # First transition to PICKED_UP -> CONSUMED, total_stock stays 10.0
    picked = await OrderService.update_order_status(
        order_id=order.id,
        payload=OrderStatusUpdateSchema(new_status=UnifiedOrderStatus.PICKED_UP, changed_by="DRIVER"),
        db=db_session,
        sync_to_channel=False,
    )
    assert picked.stock_reservation_status == "CONSUMED"

    inv_after_pickup = (
        await db_session.execute(
            select(StoreInventory).where(StoreInventory.store_id == store.id, StoreInventory.sku == "SKU-A")
        )
    ).scalar_one()
    assert float(inv_after_pickup.total_stock) == 10.0
    assert float(inv_after_pickup.reserved_stock) == 0.0
    assert float(inv_after_pickup.available_stock) == 8.0

    # Subsequent transition to DELIVERED must be idempotent (no negative reserved_stock, total_stock still 10.0)
    delivered = await OrderService.update_order_status(
        order_id=order.id,
        payload=OrderStatusUpdateSchema(new_status=UnifiedOrderStatus.DELIVERED, changed_by="DRIVER"),
        db=db_session,
        sync_to_channel=False,
    )
    assert delivered.stock_reservation_status == "CONSUMED"

    inv_after_delivery = (
        await db_session.execute(
            select(StoreInventory).where(StoreInventory.store_id == store.id, StoreInventory.sku == "SKU-A")
        )
    ).scalar_one()
    assert float(inv_after_delivery.total_stock) == 10.0
    assert float(inv_after_delivery.reserved_stock) == 0.0
    assert float(inv_after_delivery.available_stock) == 8.0


@pytest.mark.asyncio
async def test_insufficient_virtual_stock_logs_warning_in_default_mode(db_session: AsyncSession):
    """Default mode (VIRTUAL_STOCK_STRICT_MODE=False) allows order and logs OperationalErrorLog."""
    await _seed_base_entities(db_session, total_stock=3.0, safety_stock=2.0)

    with (
        patch("app.services.order_service.get_settings", return_value=Settings(VIRTUAL_STOCK_STRICT_MODE=False)),
        patch("app.modules.grabmart.adapter.GrabMartChannelAdapter.sync_inventory", new=AsyncMock(return_value=True)),
    ):
        order, created = await OrderService.create_or_get_inbound_order(
            channel_code="GRABMART",
            partner_store_id="GM-10001",
            channel_order_id="GM-LOW-001",
            order_data={"total_amount": 250000.0},
            items_data=[{"sku": "SKU-A", "item_name": "A", "quantity": 5, "unit_price": 50000.0, "total_price": 250000.0}],
            db=db_session,
        )

    assert created is True
    assert order.stock_reservation_status == "RESERVED"

    err_logs = (await db_session.execute(select(OperationalErrorLog))).scalars().all()
    assert len(err_logs) == 1
    assert err_logs[0].severity == "WARNING"
    assert err_logs[0].module == "INVENTORY"
    assert err_logs[0].error_code == "INSUFFICIENT_VIRTUAL_STOCK"


@pytest.mark.asyncio
async def test_insufficient_virtual_stock_raises_in_strict_mode(db_session: AsyncSession):
    """Strict mode (VIRTUAL_STOCK_STRICT_MODE=True) raises ValidationError and rolls back order."""
    await _seed_base_entities(db_session, total_stock=3.0, safety_stock=2.0)

    with patch("app.services.order_service.get_settings", return_value=Settings(VIRTUAL_STOCK_STRICT_MODE=True)):
        with pytest.raises(ValidationError):
            await OrderService.create_or_get_inbound_order(
                channel_code="GRABMART",
                partner_store_id="GM-10001",
                channel_order_id="GM-STRICT-001",
                order_data={"total_amount": 250000.0},
                items_data=[{"sku": "SKU-A", "item_name": "A", "quantity": 5, "unit_price": 50000.0, "total_price": 250000.0}],
                db=db_session,
            )

    orders = (await db_session.execute(select(UnifiedOrder))).scalars().all()
    assert orders == []


@pytest.mark.asyncio
async def test_poppies_cttk_sync_preserves_reserved_and_safety_stock(db_session: AsyncSession):
    """Poppies CTTK sync updates total_stock while keeping reserved_stock and safety_stock intact."""
    store, _channel, _inv_a = await _seed_base_entities(
        db_session,
        total_stock=10.0,
        reserved_stock=3.0,
        safety_stock=2.0,
    )

    svc = PoppiesMasterDataSyncService()
    svc.client = MagicMock()
    svc.client.get_cttk_records = AsyncMock(
        return_value={
            "items": [
                {"sku": "SKU-A", "storeCode": "10001", "slTon": 15.0, "unitName": "Hộp"},
                {"sku": "SKU-B", "storeCode": "10001", "slTon": 4.0, "unitName": "Gói"},
            ]
        }
    )

    summary = await svc.sync_stock_cttk_realtime(db=db_session, store_code="10001")
    assert summary["success"] is True
    assert summary["inventories_updated"] == 2
    assert set(summary["availability_changed_skus"]) == {"SKU-A", "SKU-B"}

    inv_a = (
        await db_session.execute(
            select(StoreInventory).where(StoreInventory.store_id == store.id, StoreInventory.sku == "SKU-A")
        )
    ).scalar_one()
    inv_b = (
        await db_session.execute(
            select(StoreInventory).where(StoreInventory.store_id == store.id, StoreInventory.sku == "SKU-B")
        )
    ).scalar_one()

    # SKU-A: total=15, reserved=3, safety=2 -> available=10
    assert float(inv_a.total_stock) == 15.0
    assert float(inv_a.reserved_stock) == 3.0
    assert float(inv_a.safety_stock) == 2.0
    assert float(inv_a.available_stock) == 10.0
    assert inv_a.is_out_of_stock is False

    # SKU-B: total=4, reserved=3, safety=2 -> available=0, OOS=True
    assert float(inv_b.total_stock) == 4.0
    assert float(inv_b.reserved_stock) == 3.0
    assert float(inv_b.safety_stock) == 2.0
    assert float(inv_b.available_stock) == 0.0
    assert inv_b.is_out_of_stock is True


@pytest.mark.asyncio
async def test_quick_update_stock_respects_reserved_and_safety_stock(db_session: AsyncSession):
    """InventoryService.quick_update_stock recomputes available_stock respecting reserved_stock and safety_stock."""
    store, _channel, _inv_a = await _seed_base_entities(
        db_session,
        total_stock=10.0,
        reserved_stock=3.0,
        safety_stock=2.0,
    )

    inv, _sync_res = await InventoryService.quick_update_stock(
        store_code="10001",
        sku="SKU-A",
        new_stock=12.0,
        sync_to_channels=False,
        db=db_session,
    )

    assert float(inv.total_stock) == 12.0
    assert float(inv.reserved_stock) == 3.0
    assert float(inv.safety_stock) == 2.0
    assert float(inv.available_stock) == 7.0
    assert inv.is_out_of_stock is False
