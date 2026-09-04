from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import check_store_access, get_current_user, require_store_manager
from app.core.database import get_db
from app.core.exceptions import NotFoundError
from app.core.responses import APIResponse
from app.models.inventory import StoreInventory
from app.models.store import Store
from app.models.user import User
from app.schemas.inventory import (
    InventoryBatchUpdateRequest,
    StoreInventoryResponse,
)
from app.services.inventory_service import InventoryService

router = APIRouter(prefix="/inventory", tags=["Multi-Store Inventory"])


@router.get("/stores/{store_id}", response_model=APIResponse[List[StoreInventoryResponse]])
async def get_store_inventory(
    store_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve all SKU inventory records for a given store."""
    store_stmt = select(Store).where((Store.id == store_id) | (Store.code == store_id))
    store = (await db.execute(store_stmt)).scalar_one_or_none()
    if not store:
        raise NotFoundError("Store", store_id)

    # Check authorization for store
    check_store_access(current_user, store.id)

    stmt = select(StoreInventory).where(StoreInventory.store_id == store.id).order_by(StoreInventory.sku)
    res = await db.execute(stmt)
    inventories = res.scalars().all()
    return APIResponse.ok(data=inventories)


@router.post("/batch-update", response_model=APIResponse[List[StoreInventoryResponse]])
async def batch_update_inventory(
    payload: InventoryBatchUpdateRequest,
    current_user: User = Depends(require_store_manager),
    db: AsyncSession = Depends(get_db)
):
    """
    Batch update physical stock counts per store and optionally
    trigger real-time stock sync to active partner channels (ShopeeFood, GrabMart).
    """
    # Check authorization for target store
    check_store_access(current_user, payload.store_id)

    updated = await InventoryService.update_store_inventory(payload, db)
    return APIResponse.ok(
        data=updated,
        message=f"Đã cập nhật tồn kho cho {len(updated)} SKU và kích hoạt đồng bộ đa kênh."
    )

