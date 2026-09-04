import logging
from datetime import datetime, timezone
from typing import Any, Dict, List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import NotFoundError
from app.models.inventory import InventoryStockLog, StoreInventory
from app.models.product import Product
from app.models.store import Store, StoreChannelMapping
from app.schemas.inventory import InventoryBatchUpdateRequest, InventoryStockUpdateItem
from app.schemas.sync import SyncResult
from app.services.channel_registry import channel_registry

logger = logging.getLogger("naman_portal.inventory_service")


class InventoryService:
    """Core Service managing multi-store inventory calculation and channel sync."""

    @staticmethod
    async def update_store_inventory(
        payload: InventoryBatchUpdateRequest,
        db: AsyncSession
    ) -> List[StoreInventory]:
        """
        Update inventory quantities for a batch of SKUs at a store.
        """
        # Resolve store
        store_stmt = select(Store).where((Store.id == payload.store_id) | (Store.code == payload.store_id))
        store_res = await db.execute(store_stmt)
        store = store_res.scalar_one_or_none()
        if not store:
            raise NotFoundError("Store", payload.store_id)

        updated_inventories: List[StoreInventory] = []
        channel_stock_items: List[Dict[str, Any]] = []

        for item in payload.items:
            # Find product by SKU
            prod_stmt = select(Product).where(Product.sku == item.sku.strip())
            prod_res = await db.execute(prod_stmt)
            product = prod_res.scalar_one_or_none()
            if not product:
                logger.warning(f"SKU '{item.sku}' không tồn tại trong hệ thống. Bỏ qua.")
                continue

            # Find or create StoreInventory
            inv_stmt = select(StoreInventory).where(
                StoreInventory.store_id == store.id,
                StoreInventory.product_id == product.id
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
                    last_synced_at=datetime.now(timezone.utc)
                )
                db.add(inventory)
            else:
                inventory.total_stock = new_total
                inventory.safety_stock = safety
                inventory.available_stock = available
                inventory.is_out_of_stock = is_oos
                inventory.last_synced_at = datetime.now(timezone.utc)

            # Record stock audit log
            stock_log = InventoryStockLog(
                store_id=store.id,
                sku=product.sku,
                change_type="SYNC_UPDATE",
                quantity_before=qty_before,
                quantity_delta=new_total - qty_before,
                quantity_after=new_total,
                note="Cập nhật tồn kho tự động qua API"
            )
            db.add(stock_log)
            updated_inventories.append(inventory)

            channel_stock_items.append({
                "sku": product.sku,
                "available_stock": available,
                "is_out_of_stock": is_oos
            })

        await db.commit()

        # If sync_to_channels is True, dispatch stock updates to all active channels for this store
        if payload.sync_to_channels and channel_stock_items:
            await InventoryService.dispatch_stock_to_channels(store.id, channel_stock_items, db)

        return updated_inventories

    @staticmethod
    async def dispatch_stock_to_channels(
        store_id: str,
        stock_items: List[Dict[str, Any]],
        db: AsyncSession
    ) -> List[SyncResult]:
        """Dispatch inventory changes to external channel adapters."""
        mappings_stmt = select(StoreChannelMapping).where(
            StoreChannelMapping.store_id == store_id,
            StoreChannelMapping.is_active == True
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
                    db=db
                )
                results.append(res)
            except Exception as ex:
                logger.error(f"Lỗi khi đẩy tồn kho lên kênh {mapping.channel.code}: {str(ex)}")

        return results
