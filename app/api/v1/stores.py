from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.exceptions import ConflictError, NotFoundError
from app.core.responses import APIResponse
from app.models.store import Store, StoreChannelMapping
from app.schemas.store import (
    StoreChannelMappingCreate,
    StoreChannelMappingResponse,
    StoreCreate,
    StoreResponse,
    StoreUpdate,
)

router = APIRouter(prefix="/stores", tags=["Stores & Outlets"])


@router.get("", response_model=APIResponse[List[StoreResponse]])
async def list_stores(db: AsyncSession = Depends(get_db)):
    """List all physical stores / outlets of Nam An Market."""
    stmt = select(Store).options(selectinload(Store.channel_mappings)).order_by(Store.code)
    res = await db.execute(stmt)
    stores = res.scalars().all()
    return APIResponse.ok(data=stores)


@router.post("", response_model=APIResponse[StoreResponse], status_code=201)
async def create_store(payload: StoreCreate, db: AsyncSession = Depends(get_db)):
    """Create a new store (e.g. 10001 - Thảo Điền)."""
    existing = await db.execute(select(Store).where(Store.code == payload.code))
    if existing.scalar_one_or_none():
        raise ConflictError(f"Cửa hàng với mã '{payload.code}' đã tồn tại.")

    store = Store(**payload.model_dump())
    db.add(store)
    await db.commit()
    
    # Reload with relations
    stmt = select(Store).where(Store.id == store.id).options(selectinload(Store.channel_mappings))
    created = (await db.execute(stmt)).scalar_one()
    return APIResponse.ok(data=created, message="Tạo cửa hàng thành công")


@router.get("/{store_id}", response_model=APIResponse[StoreResponse])
async def get_store(store_id: str, db: AsyncSession = Depends(get_db)):
    """Get single store details including channel partner mappings."""
    stmt = select(Store).where((Store.id == store_id) | (Store.code == store_id)).options(
        selectinload(Store.channel_mappings)
    )
    res = await db.execute(stmt)
    store = res.scalar_one_or_none()
    if not store:
        raise NotFoundError("Store", store_id)
    return APIResponse.ok(data=store)


@router.post("/{store_id}/channels", response_model=APIResponse[StoreChannelMappingResponse], status_code=201)
async def map_store_to_channel(
    store_id: str,
    payload: StoreChannelMappingCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Map a Nam An store to an external channel partner ID
    (e.g., store 10001 maps to ShopeeFood partner_restaurant_id '10001').
    """
    store_stmt = select(Store).where((Store.id == store_id) | (Store.code == store_id))
    store = (await db.execute(store_stmt)).scalar_one_or_none()
    if not store:
        raise NotFoundError("Store", store_id)

    # Check mapping duplicate
    mapping_stmt = select(StoreChannelMapping).where(
        StoreChannelMapping.store_id == store.id,
        StoreChannelMapping.channel_id == payload.channel_id
    )
    existing_mapping = (await db.execute(mapping_stmt)).scalar_one_or_none()
    if existing_mapping:
        raise ConflictError("Cửa hàng đã được liên kết với kênh bán lẻ này.")

    mapping = StoreChannelMapping(
        store_id=store.id,
        channel_id=payload.channel_id,
        partner_store_id=payload.partner_store_id,
        is_active=payload.is_active,
        channel_config=payload.channel_config
    )
    db.add(mapping)
    await db.commit()
    await db.refresh(mapping)
    return APIResponse.ok(data=mapping, message="Liên kết kênh bán lẻ cho chi nhánh thành công")
