# -*- coding: utf-8 -*-
import asyncio
import sys
from datetime import datetime, timezone
from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.services.channel_service import ChannelService
from app.models.store import Store
from app.models.channel import Channel
from app.models.order import UnifiedOrder, OrderItem, UnifiedOrderStatus

async def seed():
    async with AsyncSessionLocal() as db:
        channels = await ChannelService.ensure_default_channels(db)
        stores = (await db.execute(select(Store))).scalars().all()
        print(f"Total stores in DB: {len(stores)}")
        for s in stores:
            print(f" - Store {s.code}: {s.name.encode('ascii', 'replace').decode('ascii')}")

        existing_orders = (await db.execute(select(UnifiedOrder))).scalars().all()
        print(f"Existing orders in DB: {len(existing_orders)}")

        if len(existing_orders) == 0:
            print("Creating sample multi-channel orders...")
            store_dict = {s.code: s for s in stores}
            channel_dict = {c.code: c for c in channels}

            # Order 1: ShopeeFood PENDING at Thao Dien
            o1 = UnifiedOrder(
                order_code="NAMAN-SPF-240907-001",
                channel_order_id="SPF-98234120",
                display_order_id="SPF-4120",
                channel_id=channel_dict["SHOPEEFOOD"].id,
                store_id=store_dict["10001"].id,
                status=UnifiedOrderStatus.PENDING,
                order_time=datetime.now(timezone.utc),
                customer_name="Trần Minh Quân",
                customer_phone="0903123456",
                delivery_address="Villa 14, Thảo Điền, P. Thảo Điền, TP. Thủ Đức (Ghi chú giao: Bấm chuông cổng phụ)",
                subtotal_amount=540000.0,
                discount_amount=40000.0,
                delivery_fee=25000.0,
                total_amount=525000.0,
                items=[
                    OrderItem(sku="SKU-FISH-01", item_name="Cá Hồi Na Uy Tươi Fillet (500g)", quantity=1, unit_price=320000.0, total_price=320000.0, notes="Cắt lát ăn sashimi giúp em"),
                    OrderItem(sku="SKU-FRUIT-08", item_name="Bơ Sáp 034 Hữu Cơ (1kg)", quantity=1, unit_price=120000.0, total_price=120000.0),
                    OrderItem(sku="SKU-MILK-02", item_name="Sữa Tươi Thanh Trùng Dalat Milk 950ml", quantity=2, unit_price=50000.0, total_price=100000.0)
                ]
            )

            # Order 2: GrabMart ACCEPTED at Hung Phuc
            o2 = UnifiedOrder(
                order_code="NAMAN-GRB-240907-002",
                channel_order_id="GRB-6629104",
                display_order_id="GRB-9104",
                channel_id=channel_dict["GRABMART"].id,
                store_id=store_dict["10004"].id,
                status=UnifiedOrderStatus.ACCEPTED,
                order_time=datetime.now(timezone.utc),
                customer_name="Nguyễn Hoàng Mai",
                customer_phone="0988765432",
                delivery_address="Căn hộ B12-08, Chung cư Scenic Valley 1, Phú Mỹ Hưng, Q.7",
                driver_name="Lê Văn Hải",
                driver_phone="0933445566",
                driver_license_plate="59C2-789.12",
                subtotal_amount=780000.0,
                discount_amount=50000.0,
                delivery_fee=30000.0,
                total_amount=760000.0,
                items=[
                    OrderItem(sku="SKU-BEEF-03", item_name="Thăn Ngoại Bò Úc Black Angus (400g)", quantity=2, unit_price=290000.0, total_price=580000.0, notes="Hút chân không riêng từng miếng"),
                    OrderItem(sku="SKU-OIL-01", item_name="Dầu Oliu Borges Extra Virgin 500ml", quantity=1, unit_price=200000.0, total_price=200000.0)
                ]
            )

            # Order 3: ShopeeMart READY at Mai Chi Tho
            o3 = UnifiedOrder(
                order_code="NAMAN-SPM-240907-003",
                channel_order_id="SPM-1102934",
                display_order_id="SPM-2934",
                channel_id=channel_dict["SHOPEEMART"].id,
                store_id=store_dict["10005"].id,
                status=UnifiedOrderStatus.READY,
                order_time=datetime.now(timezone.utc),
                customer_name="Đỗ Thanh Trúc",
                customer_phone="0912334455",
                delivery_address="Block A, Imperia An Phú, P. An Phú, TP. Thủ Đức",
                driver_name="Phạm Quốc Huy",
                driver_phone="0977889900",
                driver_license_plate="59B1-456.78",
                subtotal_amount=385000.0,
                discount_amount=0.0,
                delivery_fee=22000.0,
                total_amount=407000.0,
                items=[
                    OrderItem(sku="SKU-VEG-05", item_name="Combo Rau Hữu Cơ Đà Lạt (5 món)", quantity=1, unit_price=165000.0, total_price=165000.0),
                    OrderItem(sku="SKU-APL-02", item_name="Táo Envy New Zealand Size 70 (1kg)", quantity=1, unit_price=220000.0, total_price=220000.0)
                ]
            )

            db.add_all([o1, o2, o3])
            await db.commit()
            print("Successfully seeded 3 sample orders!")
        else:
            print("Orders already seeded.")

if __name__ == "__main__":
    asyncio.run(seed())
