from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user, require_super_admin
from app.core.database import get_db
from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.core.responses import APIResponse
from app.models.channel import Channel
from app.models.store import Store, StoreChannelMapping
from app.models.user import User
from app.schemas.store import (
    StoreChannelMappingCreate,
    StoreChannelMappingDetail,
    StoreChannelMappingResponse,
    StoreChannelMappingUpdate,
    StoreCreate,
    StoreResponse,
    StoreUpdate,
)

router = APIRouter(prefix="/stores", tags=["Stores & Outlets"])


@router.get("", response_model=APIResponse[List[StoreResponse]])
async def list_stores(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """List all physical stores / outlets of Nam An Market."""
    stmt = select(Store).options(selectinload(Store.channel_mappings)).order_by(Store.code)
    res = await db.execute(stmt)
    stores = res.scalars().all()
    return APIResponse.ok(data=stores)


@router.post("", response_model=APIResponse[StoreResponse], status_code=201)
async def create_store(
    payload: StoreCreate,
    current_admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db)
):
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


@router.get("/mappings", response_model=APIResponse[List[StoreChannelMappingDetail]])
async def list_all_store_channel_mappings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """List all store-to-channel partner mappings with store and channel names."""
    stmt = (
        select(StoreChannelMapping)
        .options(
            selectinload(StoreChannelMapping.store),
            selectinload(StoreChannelMapping.channel)
        )
        .order_by(StoreChannelMapping.created_at.desc())
    )
    res = await db.execute(stmt)
    mappings = res.scalars().all()

    data = []
    for m in mappings:
        data.append(
            StoreChannelMappingDetail(
                id=m.id,
                store_id=m.store_id,
                channel_id=m.channel_id,
                partner_store_id=m.partner_store_id,
                is_active=m.is_active,
                channel_config=m.channel_config,
                created_at=m.created_at,
                updated_at=m.updated_at,
                store_code=m.store.code if m.store else None,
                store_name=m.store.name if m.store else None,
                channel_code=m.channel.code if m.channel else None,
                channel_name=m.channel.name if m.channel else None,
            )
        )
    return APIResponse.ok(data=data)


@router.get("/{store_id}", response_model=APIResponse[StoreResponse])
async def get_store(
    store_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
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
    current_admin: User = Depends(require_super_admin),
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


@router.put("/{store_id}/channels/{channel_id}", response_model=APIResponse[StoreChannelMappingResponse])
async def update_or_create_store_channel_mapping(
    store_id: str,
    channel_id: str,
    payload: StoreChannelMappingUpdate,
    current_admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Cập nhật hoặc tạo mới (upsert) liên kết giữa chi nhánh Nam An và mã cửa hàng trên sàn đối tác
    (ví dụ: cập nhật Outlet ID ShopeeMart, Store ID GrabMart, Partner ID ShopeeFood).
    """
    store_stmt = select(Store).where((Store.id == store_id) | (Store.code == store_id))
    store = (await db.execute(store_stmt)).scalar_one_or_none()
    if not store:
        raise NotFoundError("Store", store_id)

    channel_stmt = select(Channel).where((Channel.id == channel_id) | (Channel.code == channel_id.upper()))
    channel = (await db.execute(channel_stmt)).scalar_one_or_none()
    if not channel:
        raise NotFoundError("Channel", channel_id)

    mapping_stmt = select(StoreChannelMapping).where(
        StoreChannelMapping.store_id == store.id,
        StoreChannelMapping.channel_id == channel.id
    )
    mapping = (await db.execute(mapping_stmt)).scalar_one_or_none()

    if mapping:
        if payload.partner_store_id is not None:
            mapping.partner_store_id = payload.partner_store_id
        if payload.is_active is not None:
            mapping.is_active = payload.is_active
        if payload.channel_config is not None:
            mapping.channel_config = payload.channel_config
    else:
        if not payload.partner_store_id:
            raise BadRequestError("partner_store_id là bắt buộc để tạo liên kết mới.")
        mapping = StoreChannelMapping(
            store_id=store.id,
            channel_id=channel.id,
            partner_store_id=payload.partner_store_id,
            is_active=payload.is_active if payload.is_active is not None else True,
            channel_config=payload.channel_config
        )
        db.add(mapping)

    await db.commit()
    await db.refresh(mapping)
    return APIResponse.ok(data=mapping, message="Cập nhật cấu hình ánh xạ kênh thành công.")


@router.delete("/{store_id}/channels/{channel_id}", response_model=APIResponse[dict])
async def delete_store_channel_mapping(
    store_id: str,
    channel_id: str,
    current_admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db)
):
    """Xóa bỏ liên kết kênh bán lẻ của chi nhánh."""
    store_stmt = select(Store).where((Store.id == store_id) | (Store.code == store_id))
    store = (await db.execute(store_stmt)).scalar_one_or_none()
    if not store:
        raise NotFoundError("Store", store_id)

    channel_stmt = select(Channel).where((Channel.id == channel_id) | (Channel.code == channel_id.upper()))
    channel = (await db.execute(channel_stmt)).scalar_one_or_none()
    if not channel:
        raise NotFoundError("Channel", channel_id)

    mapping_stmt = select(StoreChannelMapping).where(
        StoreChannelMapping.store_id == store.id,
        StoreChannelMapping.channel_id == channel.id
    )
    mapping = (await db.execute(mapping_stmt)).scalar_one_or_none()
    if not mapping:
        raise NotFoundError(f"Liên kết giữa chi nhánh {store.code} và kênh {channel.code} không tồn tại.")

    await db.delete(mapping)
    await db.commit()
    return APIResponse.ok(data={"deleted": True}, message="Đã xóa liên kết kênh của chi nhánh thành công.")
