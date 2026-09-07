import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import NotFoundError
from app.models.channel import Channel
from app.models.inventory import InventoryStockLog, StoreInventory
from app.models.product import Category, Product
from app.models.store import Store, StoreChannelMapping
from app.schemas.inventory import (
    InventoryBatchUpdateRequest,
    InventoryItemOverview,
    InventoryOverviewSummary,
    InventoryStockUpdateItem,
    InventoryToggleStatusRequest,
)
from app.schemas.sync import SyncResult
from app.services.channel_registry import channel_registry

logger = logging.getLogger("naman_portal.inventory_service")


SAMPLE_CATEGORIES = [
    {"code": "SEAFOOD_MEAT", "name": "Thịt & Thủy Hải Sản Tươi Sống", "sequence": 1},
    {"code": "FRUITS_VEGGIES", "name": "Rau Củ & Trái Cây Hữu Cơ", "sequence": 2},
    {"code": "DAIRY_DELI", "name": "Bơ Sữa & Thực Phẩm Nhập Khẩu", "sequence": 3},
    {"code": "BAKERY_PANTRY", "name": "Bánh Mì & Đồ Khô Cao Cấp", "sequence": 4},
    {"code": "WINE_BEVERAGES", "name": "Rượu Vang & Đồ Uống", "sequence": 5},
]

SAMPLE_PRODUCTS = [
    # Meat & Seafood
    {
        "sku": "SKU-SALMON-01",
        "barcode": "8935001001",
        "name": "Cá Hồi Tươi Na Uy Fillet (500g)",
        "category_code": "SEAFOOD_MEAT",
        "unit": "Khay",
        "base_price": 320000.0,
        "image_url": "https://images.unsplash.com/photo-1519708227418-c8fd9a32b7a2?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 25.0
    },
    {
        "sku": "SKU-BEEF-01",
        "barcode": "8935001002",
        "name": "Thăn Ngoại Bò Úc Black Angus (400g)",
        "category_code": "SEAFOOD_MEAT",
        "unit": "Khay",
        "base_price": 290000.0,
        "image_url": "https://images.unsplash.com/photo-1603048588665-791ca8aea617?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 18.0
    },
    {
        "sku": "SKU-PORK-01",
        "barcode": "8935001003",
        "name": "Ba Rọi Heo Quế Iberico Tây Ban Nha (300g)",
        "category_code": "SEAFOOD_MEAT",
        "unit": "Khay",
        "base_price": 210000.0,
        "image_url": "https://images.unsplash.com/photo-1607623814075-e51df1bdc82f?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 12.0
    },
    {
        "sku": "SKU-PRAWN-01",
        "barcode": "8935001004",
        "name": "Tôm Sú Tươi Sinh Thái Cà Mau (500g)",
        "category_code": "SEAFOOD_MEAT",
        "unit": "Hộp",
        "base_price": 245000.0,
        "image_url": "https://images.unsplash.com/photo-1565680018434-b513d5e5fd47?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 15.0
    },
    # Fruits & Veggies
    {
        "sku": "SKU-AVOCADO-01",
        "barcode": "8935002001",
        "name": "Bơ Sáp 034 Hữu Cơ Lâm Đồng (1kg)",
        "category_code": "FRUITS_VEGGIES",
        "unit": "Kg",
        "base_price": 110000.0,
        "image_url": "https://images.unsplash.com/photo-1523049673857-eb18f1d7b578?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 30.0
    },
    {
        "sku": "SKU-APPLE-01",
        "barcode": "8935002002",
        "name": "Táo Envy New Zealand Size 70 (1kg)",
        "category_code": "FRUITS_VEGGIES",
        "unit": "Kg",
        "base_price": 220000.0,
        "image_url": "https://images.unsplash.com/photo-1560806887-1e4cd0b6cbd6?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 40.0
    },
    {
        "sku": "SKU-CHERRY-01",
        "barcode": "8935002003",
        "name": "Cherry Đỏ Mỹ Size 9.5 (Hộp 500g)",
        "category_code": "FRUITS_VEGGIES",
        "unit": "Hộp",
        "base_price": 340000.0,
        "image_url": "https://images.unsplash.com/photo-1528825871115-3581a5387919?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 8.0
    },
    {
        "sku": "SKU-SALAD-01",
        "barcode": "8935002004",
        "name": "Combo 5 Loại Rau Salad Thủy Canh (500g)",
        "category_code": "FRUITS_VEGGIES",
        "unit": "Gói",
        "base_price": 85000.0,
        "image_url": "https://images.unsplash.com/photo-1540420773420-3366772f4999?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 20.0
    },
    # Dairy & Deli
    {
        "sku": "SKU-MILK-01",
        "barcode": "8935003001",
        "name": "Sữa Tươi Thanh Trùng Dalat Milk 950ml",
        "category_code": "DAIRY_DELI",
        "unit": "Chai",
        "base_price": 48000.0,
        "image_url": "https://images.unsplash.com/photo-1550583724-b2692b85b150?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 35.0
    },
    {
        "sku": "SKU-CHEESE-01",
        "barcode": "8935003002",
        "name": "Phô Mai Brie De Meaux Pháp (200g)",
        "category_code": "DAIRY_DELI",
        "unit": "Hộp",
        "base_price": 165000.0,
        "image_url": "https://images.unsplash.com/photo-1486297678162-eb2a19b0a32d?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 14.0
    },
    {
        "sku": "SKU-BUTTER-01",
        "barcode": "8935003003",
        "name": "Bơ Nhạt Anchor New Zealand (227g)",
        "category_code": "DAIRY_DELI",
        "unit": "Khối",
        "base_price": 78000.0,
        "image_url": "https://images.unsplash.com/photo-1589985270826-4b7bb135bc9d?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 22.0
    },
    {
        "sku": "SKU-YOGURT-01",
        "barcode": "8935003004",
        "name": "Sữa Chua Hy Lạp Farmers Union (1kg)",
        "category_code": "DAIRY_DELI",
        "unit": "Hũ",
        "base_price": 195000.0,
        "image_url": "https://images.unsplash.com/photo-1488477181946-6428a0291777?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 16.0
    },
    # Bakery & Pantry
    {
        "sku": "SKU-BRIOCHE-01",
        "barcode": "8935004001",
        "name": "Bánh Mì Hoa Cúc Harrys Brioche Pháp (515g)",
        "category_code": "BAKERY_PANTRY",
        "unit": "Ổ",
        "base_price": 135000.0,
        "image_url": "https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 25.0
    },
    {
        "sku": "SKU-OLIVEOIL-01",
        "barcode": "8935004002",
        "name": "Dầu Oliu Borges Extra Virgin Tây Ban Nha 500ml",
        "category_code": "BAKERY_PANTRY",
        "unit": "Chai",
        "base_price": 190000.0,
        "image_url": "https://images.unsplash.com/photo-1474979266404-7eaacbcd87c5?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 18.0
    },
    {
        "sku": "SKU-PASTA-01",
        "barcode": "8935004003",
        "name": "Mì Ý Barilla Spaghetti No.5 Ý (500g)",
        "category_code": "BAKERY_PANTRY",
        "unit": "Gói",
        "base_price": 62000.0,
        "image_url": "https://images.unsplash.com/photo-1621996346565-e3d5d6281242?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 30.0
    },
    {
        "sku": "SKU-WINE-01",
        "barcode": "8935005001",
        "name": "Rượu Vang Đỏ Montes Alpha Cabernet Sauvignon 750ml",
        "category_code": "WINE_BEVERAGES",
        "unit": "Chai",
        "base_price": 690000.0,
        "image_url": "https://images.unsplash.com/photo-1510812431401-41d2bd2722f3?auto=format&fit=crop&w=400&q=80",
        "initial_stock": 10.0
    },
]


class InventoryService:
    """Core Service managing multi-store inventory calculation and channel sync."""

    @classmethod
    async def seed_sample_catalog(cls, db: AsyncSession) -> Dict[str, int]:
        """Seed master categories, products, store-channel mappings, and inventory across all 4 Nam An branches."""
        # 1. Categories
        cat_map = {}
        for c_def in SAMPLE_CATEGORIES:
            stmt = select(Category).where(Category.code == c_def["code"])
            res = await db.execute(stmt)
            cat = res.scalar_one_or_none()
            if not cat:
                cat = Category(code=c_def["code"], name=c_def["name"], sequence=c_def["sequence"])
                db.add(cat)
                await db.flush()
            cat_map[c_def["code"]] = cat

        # 2. Products
        prod_map = {}
        for p_def in SAMPLE_PRODUCTS:
            stmt = select(Product).where(Product.sku == p_def["sku"])
            res = await db.execute(stmt)
            prod = res.scalar_one_or_none()
            cat = cat_map.get(p_def["category_code"])
            if not prod:
                prod = Product(
                    sku=p_def["sku"],
                    barcode=p_def["barcode"],
                    name=p_def["name"],
                    unit=p_def["unit"],
                    base_price=p_def["base_price"],
                    image_url=p_def["image_url"],
                    category_id=cat.id if cat else None,
                    is_active=True,
                )
                db.add(prod)
                await db.flush()
            prod_map[p_def["sku"]] = prod

        # 3. Stores & Channel Mappings
        stores_stmt = select(Store).order_by(Store.code)
        stores_res = await db.execute(stores_stmt)
        stores = stores_res.scalars().all()

        channels_stmt = select(Channel)
        channels_res = await db.execute(channels_stmt)
        channels = channels_res.scalars().all()

        for store in stores:
            for ch in channels:
                map_stmt = select(StoreChannelMapping).where(
                    StoreChannelMapping.store_id == store.id,
                    StoreChannelMapping.channel_id == ch.id,
                )
                existing_map = (await db.execute(map_stmt)).scalar_one_or_none()
                if not existing_map:
                    new_map = StoreChannelMapping(
                        store_id=store.id,
                        channel_id=ch.id,
                        partner_store_id=store.code,
                        is_active=True,
                    )
                    db.add(new_map)

        # 4. Store Inventories across all 4 branches
        inv_created = 0
        for store in stores:
            for p_def in SAMPLE_PRODUCTS:
                prod = prod_map[p_def["sku"]]
                inv_stmt = select(StoreInventory).where(
                    StoreInventory.store_id == store.id,
                    StoreInventory.product_id == prod.id,
                )
                inv = (await db.execute(inv_stmt)).scalar_one_or_none()
                if not inv:
                    stock_val = float(p_def["initial_stock"])
                    # Vary slightly per store to look authentic
                    if store.code == "10004":
                        stock_val = max(0.0, stock_val - 5.0)
                    elif store.code == "10005":
                        stock_val = max(0.0, stock_val - 8.0)
                    elif store.code == "10006":
                        stock_val = max(0.0, stock_val + 4.0)

                    inv = StoreInventory(
                        store_id=store.id,
                        product_id=prod.id,
                        sku=prod.sku,
                        total_stock=stock_val,
                        reserved_stock=0.0,
                        available_stock=stock_val,
                        safety_stock=2.0,
                        is_out_of_stock=(stock_val <= 0.0),
                        last_synced_at=datetime.now(timezone.utc),
                    )
                    db.add(inv)
                    inv_created += 1

        await db.commit()
        return {
            "categories": len(cat_map),
            "products": len(prod_map),
            "inventories_created": inv_created,
        }

    @classmethod
    async def get_store_inventory_overview(
        cls,
        store_code: str,
        category_id: Optional[str] = None,
        status: Optional[str] = None,  # ALL, IN_STOCK, OUT_OF_STOCK, LOW_STOCK
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        db: Optional[AsyncSession] = None,
    ) -> Tuple[List[InventoryItemOverview], int, InventoryOverviewSummary]:
        """Fetch consolidated store inventory list with live stock levels, category info, and summary KPIs."""
        if db is None:
            return [], 0, InventoryOverviewSummary()

        # 1. Resolve store
        store_stmt = select(Store).where((Store.code == str(store_code)) | (Store.id == str(store_code)))
        store = (await db.execute(store_stmt)).scalar_one_or_none()
        if not store:
            # Fallback to first store
            first_store = (await db.execute(select(Store).order_by(Store.code))).scalar_one_or_none()
            if not first_store:
                return [], 0, InventoryOverviewSummary()
            store = first_store

        # Check if products exist; if not, seed default catalog
        prod_count = (await db.execute(select(func.count(Product.id)))).scalar_one()
        if prod_count == 0:
            await cls.seed_sample_catalog(db)

        # 2. Build Query
        # Join Product, Category, StoreInventory
        stmt = (
            select(Product, Category, StoreInventory)
            .outerjoin(Category, Product.category_id == Category.id)
            .outerjoin(
                StoreInventory,
                (StoreInventory.product_id == Product.id) & (StoreInventory.store_id == store.id),
            )
            .where(Product.is_active == True)
        )

        if category_id:
            stmt = stmt.where(Product.category_id == category_id)

        if search:
            search_clean = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(
                    Product.name.ilike(search_clean),
                    Product.sku.ilike(search_clean),
                    Product.barcode.ilike(search_clean),
                )
            )

        # Execute total fetch for counters
        all_res = await db.execute(stmt)
        all_rows = all_res.all()

        total_skus = len(all_rows)
        in_stock_count = 0
        out_of_stock_count = 0
        low_stock_count = 0

        filtered_rows = []
        for prod, cat, inv in all_rows:
            is_oos = bool(inv.is_out_of_stock) if inv else False
            avail = float(inv.available_stock) if inv else 0.0

            if is_oos or avail <= 0:
                out_of_stock_count += 1
            else:
                in_stock_count += 1
                if avail <= 5.0:
                    low_stock_count += 1

            # Filter by status if specified
            if status == "IN_STOCK" and (is_oos or avail <= 0):
                continue
            if status == "OUT_OF_STOCK" and (not is_oos and avail > 0):
                continue
            if status == "LOW_STOCK" and (is_oos or avail > 5.0 or avail <= 0):
                continue

            filtered_rows.append((prod, cat, inv))

        total_filtered = len(filtered_rows)
        summary = InventoryOverviewSummary(
            total_skus=total_skus,
            in_stock_count=in_stock_count,
            out_of_stock_count=out_of_stock_count,
            low_stock_count=low_stock_count,
        )

        # Pagination on filtered rows
        start_idx = (page - 1) * page_size
        paged_rows = filtered_rows[start_idx : start_idx + page_size]

        items = []
        for prod, cat, inv in paged_rows:
            total_s = float(inv.total_stock) if inv else 0.0
            reserved_s = float(inv.reserved_stock) if inv else 0.0
            avail_s = float(inv.available_stock) if inv else 0.0
            safety_s = float(inv.safety_stock) if inv else 0.0
            is_oos = bool(inv.is_out_of_stock) if inv else (avail_s <= 0)
            synced_at = inv.last_synced_at if inv else None

            items.append(
                InventoryItemOverview(
                    product_id=prod.id,
                    sku=prod.sku,
                    name=prod.name,
                    barcode=prod.barcode,
                    unit=prod.unit,
                    base_price=float(prod.base_price or 0.0),
                    category_id=cat.id if cat else None,
                    category_name=cat.name if cat else "Chưa phân loại",
                    image_url=prod.image_url,
                    store_id=store.id,
                    store_code=store.code,
                    store_name=store.name,
                    total_stock=total_s,
                    reserved_stock=reserved_s,
                    available_stock=avail_s,
                    safety_stock=safety_s,
                    is_out_of_stock=is_oos,
                    last_synced_at=synced_at,
                )
            )

        return items, total_filtered, summary

    @classmethod
    async def toggle_item_status(
        cls,
        payload: InventoryToggleStatusRequest,
        db: AsyncSession,
    ) -> Tuple[StoreInventory, List[SyncResult]]:
        """1-Click Toggle for in-stock / out-of-stock. Pushes real-time stock = 0 or restored stock to ShopeeFood & GrabMart."""
        # 1. Resolve store
        store_stmt = select(Store).where((Store.code == str(payload.store_code)) | (Store.id == str(payload.store_code)))
        store = (await db.execute(store_stmt)).scalar_one_or_none()
        if not store:
            raise NotFoundError("Store", payload.store_code)

        # 2. Resolve product
        prod_stmt = select(Product).where(Product.sku == payload.sku.strip())
        prod = (await db.execute(prod_stmt)).scalar_one_or_none()
        if not prod:
            raise NotFoundError("Product", payload.sku)

        # 3. Resolve or create StoreInventory
        inv_stmt = select(StoreInventory).where(
            StoreInventory.store_id == store.id,
            StoreInventory.product_id == prod.id,
        )
        inv = (await db.execute(inv_stmt)).scalar_one_or_none()

        now = datetime.now(timezone.utc)
        qty_before = float(inv.available_stock) if inv else 0.0

        if not inv:
            new_avail = 0.0 if payload.is_out_of_stock else (payload.available_stock or 10.0)
            inv = StoreInventory(
                store_id=store.id,
                product_id=prod.id,
                sku=prod.sku,
                total_stock=new_avail,
                reserved_stock=0.0,
                available_stock=new_avail,
                safety_stock=0.0,
                is_out_of_stock=payload.is_out_of_stock,
                last_synced_at=now,
            )
            db.add(inv)
        else:
            inv.is_out_of_stock = payload.is_out_of_stock
            if payload.is_out_of_stock:
                inv.available_stock = 0.0
            else:
                if payload.available_stock is not None:
                    inv.available_stock = float(payload.available_stock)
                    inv.total_stock = max(float(inv.total_stock), float(payload.available_stock))
                elif inv.available_stock <= 0:
                    inv.available_stock = 10.0
                    inv.total_stock = max(float(inv.total_stock), 10.0)
            inv.last_synced_at = now

        # Log movement
        audit_log = InventoryStockLog(
            store_id=store.id,
            sku=prod.sku,
            change_type="OUT_OF_STOCK_TOGGLE" if payload.is_out_of_stock else "IN_STOCK_TOGGLE",
            quantity_before=qty_before,
            quantity_delta=float(inv.available_stock) - qty_before,
            quantity_after=float(inv.available_stock),
            note=f"Chuyển trạng thái sang {'HẾT HÀNG (Tạm ngưng)' if payload.is_out_of_stock else 'CÒN HÀNG'}",
        )
        db.add(audit_log)
        await db.commit()
        await db.refresh(inv)

        # 4. Dispatch stock update to ShopeeFood and GrabMart
        sync_results: List[SyncResult] = []
        if payload.sync_to_channels:
            stock_items = [{
                "sku": prod.sku,
                "available_stock": float(inv.available_stock),
                "is_out_of_stock": bool(inv.is_out_of_stock),
            }]
            sync_results = await cls.dispatch_stock_to_channels(store.id, stock_items, db)

        return inv, sync_results

    @classmethod
    async def quick_update_stock(
        cls,
        store_code: str,
        sku: str,
        new_stock: float,
        sync_to_channels: bool,
        db: AsyncSession,
    ) -> Tuple[StoreInventory, List[SyncResult]]:
        """Quickly update stock number for a single SKU at a store."""
        store_stmt = select(Store).where((Store.code == str(store_code)) | (Store.id == str(store_code)))
        store = (await db.execute(store_stmt)).scalar_one_or_none()
        if not store:
            raise NotFoundError("Store", store_code)

        prod_stmt = select(Product).where(Product.sku == sku.strip())
        prod = (await db.execute(prod_stmt)).scalar_one_or_none()
        if not prod:
            raise NotFoundError("Product", sku)

        inv_stmt = select(StoreInventory).where(
            StoreInventory.store_id == store.id,
            StoreInventory.product_id == prod.id,
        )
        inv = (await db.execute(inv_stmt)).scalar_one_or_none()

        now = datetime.now(timezone.utc)
        qty_before = float(inv.available_stock) if inv else 0.0
        new_avail = max(0.0, float(new_stock))
        is_oos = (new_avail <= 0.0)

        if not inv:
            inv = StoreInventory(
                store_id=store.id,
                product_id=prod.id,
                sku=prod.sku,
                total_stock=new_avail,
                reserved_stock=0.0,
                available_stock=new_avail,
                safety_stock=0.0,
                is_out_of_stock=is_oos,
                last_synced_at=now,
            )
            db.add(inv)
        else:
            inv.available_stock = new_avail
            inv.total_stock = new_avail
            inv.is_out_of_stock = is_oos
            inv.last_synced_at = now

        audit_log = InventoryStockLog(
            store_id=store.id,
            sku=prod.sku,
            change_type="QUICK_UPDATE",
            quantity_before=qty_before,
            quantity_delta=new_avail - qty_before,
            quantity_after=new_avail,
            note=f"Cập nhật nhanh tồn kho khả dụng thành {new_avail}",
        )
        db.add(audit_log)
        await db.commit()
        await db.refresh(inv)

        sync_results: List[SyncResult] = []
        if sync_to_channels:
            stock_items = [{
                "sku": prod.sku,
                "available_stock": new_avail,
                "is_out_of_stock": is_oos,
            }]
            sync_results = await cls.dispatch_stock_to_channels(store.id, stock_items, db)

        return inv, sync_results

    @classmethod
    async def sync_all_store_inventory(
        cls,
        store_code: str,
        db: AsyncSession,
    ) -> List[SyncResult]:
        """Push batch inventory of all active SKUs in this store to GrabMart & ShopeeFood."""
        store_stmt = select(Store).where((Store.code == str(store_code)) | (Store.id == str(store_code)))
        store = (await db.execute(store_stmt)).scalar_one_or_none()
        if not store:
            raise NotFoundError("Store", store_code)

        inv_stmt = select(StoreInventory).where(StoreInventory.store_id == store.id)
        res = await db.execute(inv_stmt)
        inventories = res.scalars().all()

        stock_items = []
        now = datetime.now(timezone.utc)
        for inv in inventories:
            inv.last_synced_at = now
            stock_items.append({
                "sku": inv.sku,
                "available_stock": float(inv.available_stock),
                "is_out_of_stock": bool(inv.is_out_of_stock),
            })
        await db.commit()

        if stock_items:
            return await cls.dispatch_stock_to_channels(store.id, stock_items, db)
        return []

    @staticmethod
    async def update_store_inventory(
        payload: InventoryBatchUpdateRequest,
        db: AsyncSession,
    ) -> List[StoreInventory]:
        """Update inventory quantities for a batch of SKUs at a store."""
        store_stmt = select(Store).where((Store.id == payload.store_id) | (Store.code == payload.store_id))
        store_res = await db.execute(store_stmt)
        store = store_res.scalar_one_or_none()
        if not store:
            raise NotFoundError("Store", payload.store_id)

        updated_inventories: List[StoreInventory] = []
        channel_stock_items: List[Dict[str, Any]] = []

        for item in payload.items:
            prod_stmt = select(Product).where(Product.sku == item.sku.strip())
            prod_res = await db.execute(prod_stmt)
            product = prod_res.scalar_one_or_none()
            if not product:
                continue

            inv_stmt = select(StoreInventory).where(
                StoreInventory.store_id == store.id,
                StoreInventory.product_id == product.id,
            )
            inv_res = await db.execute(inv_stmt)
            inventory = inv_res.scalar_one_or_none()

            qty_before = float(inventory.total_stock) if inventory else 0.0
            new_total = float(item.total_stock)
            safety = float(item.safety_stock or (inventory.safety_stock if inventory else 0.0))
            reserved = float(inventory.reserved_stock) if inventory else 0.0
            available = max(0.0, new_total - reserved - safety)
            is_oos = (available <= 0.0)

            if not inventory:
                inventory = StoreInventory(
                    store_id=store.id,
                    product_id=product.id,
                    sku=product.sku,
                    total_stock=new_total,
                    reserved_stock=reserved,
                    available_stock=available,
                    safety_stock=safety,
                    is_out_of_stock=is_oos,
                    last_synced_at=datetime.now(timezone.utc),
                )
                db.add(inventory)
            else:
                inventory.total_stock = new_total
                inventory.safety_stock = safety
                inventory.available_stock = available
                inventory.is_out_of_stock = is_oos
                inventory.last_synced_at = datetime.now(timezone.utc)

            stock_log = InventoryStockLog(
                store_id=store.id,
                sku=product.sku,
                change_type="SYNC_UPDATE",
                quantity_before=qty_before,
                quantity_delta=new_total - qty_before,
                quantity_after=new_total,
                note="Cập nhật tồn kho tự động qua API",
            )
            db.add(stock_log)
            updated_inventories.append(inventory)

            channel_stock_items.append({
                "sku": product.sku,
                "available_stock": available,
                "is_out_of_stock": is_oos,
            })

        await db.commit()

        if payload.sync_to_channels and channel_stock_items:
            await InventoryService.dispatch_stock_to_channels(store.id, channel_stock_items, db)

        return updated_inventories

    @staticmethod
    async def dispatch_stock_to_channels(
        store_id: str,
        stock_items: List[Dict[str, Any]],
        db: AsyncSession,
    ) -> List[SyncResult]:
        """Dispatch inventory changes to external channel adapters."""
        mappings_stmt = select(StoreChannelMapping).where(
            StoreChannelMapping.store_id == store_id,
            StoreChannelMapping.is_active == True,
        ).options(selectinload(StoreChannelMapping.channel))
        mappings_res = await db.execute(mappings_stmt)
        mappings = mappings_res.scalars().all()

        results: List[SyncResult] = []
        for mapping in mappings:
            adapter = channel_registry.get(mapping.channel.code)
            if not adapter:
                continue
            try:
                res = await adapter.sync_inventory(
                    store_id=store_id,
                    partner_store_id=mapping.partner_store_id,
                    stock_items=stock_items,
                    db=db,
                )
                results.append(res)
            except Exception as ex:
                logger.error(f"Lỗi khi đẩy tồn kho lên kênh {mapping.channel.code}: {str(ex)}")

        return results
