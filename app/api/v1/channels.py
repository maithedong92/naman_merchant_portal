from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_super_admin
from app.core.database import get_db
from app.core.exceptions import ConflictError, NotFoundError
from app.core.responses import APIResponse
from app.models.channel import Channel
from app.models.user import User
from app.schemas.channel import (
    ChannelCreate,
    ChannelFeatureStatus,
    ChannelFeatureUpdate,
    ChannelResponse,
)
from app.services.channel_registry import channel_registry
from app.services.channel_service import channel_service

router = APIRouter(prefix="/channels", tags=["Sales Channels"])


def _format_channel_response(channel: Channel) -> ChannelResponse:
    features_dict = channel_service.extract_features(channel)
    resp = ChannelResponse.model_validate(channel)
    resp.features = ChannelFeatureStatus(**features_dict)
    return resp


@router.get("", response_model=APIResponse[List[ChannelResponse]])
async def list_channels(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """List all registered e-commerce and delivery channels with feature flags."""
    # Ensure default channels are seeded in DB
    await channel_service.ensure_default_channels(db)

    stmt = select(Channel).order_by(Channel.code)
    res = await db.execute(stmt)
    channels = res.scalars().all()

    enriched = [_format_channel_response(c) for c in channels]
    return APIResponse.ok(data=enriched)


@router.get("/{channel_code}", response_model=APIResponse[ChannelResponse])
async def get_channel(
    channel_code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get details and feature flags of a specific channel."""
    channel = await channel_service.get_channel_by_code(channel_code, db)
    if not channel:
        raise NotFoundError(f"Không tìm thấy kênh với mã '{channel_code.upper()}'.")

    return APIResponse.ok(data=_format_channel_response(channel))


@router.patch("/{channel_code}/features", response_model=APIResponse[ChannelResponse])
async def update_channel_features(
    channel_code: str,
    payload: ChannelFeatureUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Update feature toggles (active, order_webhook, auto_confirm, stock_sync, menu_sync) for a channel.
    Allows proactive control of each capability per sales channel.
    """
    updates = payload.model_dump(exclude_unset=True)
    channel = await channel_service.update_feature_flags(channel_code, updates, db)
    if not channel:
        raise NotFoundError(f"Không tìm thấy kênh '{channel_code.upper()}' để cập nhật.")

    return APIResponse.ok(
        data=_format_channel_response(channel),
        message=f"Đã cập nhật tính năng cho kênh {channel_code.upper()} thành công."
    )


@router.post("", response_model=APIResponse[ChannelResponse], status_code=201)
async def create_channel(
    payload: ChannelCreate,
    current_admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db)
):
    """Register a new channel (e.g. SHOPEEFOOD, GRABMART)."""
    code_upper = payload.code.upper()
    existing = await channel_service.get_channel_by_code(code_upper, db)

    if existing:
        raise ConflictError(f"Kênh với mã '{code_upper}' đã tồn tại.")

    channel = Channel(
        code=code_upper,
        name=payload.name,
        description=payload.description,
        is_active=payload.is_active,
        base_url=payload.base_url,
        webhook_secret=payload.webhook_secret,
        config=payload.config or {}
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return APIResponse.ok(
        data=_format_channel_response(channel),
        message="Đăng ký kênh bán hàng thành công"
    )


@router.get("/registered-adapters", response_model=APIResponse[List[str]])
async def get_registered_adapters(
    current_user: User = Depends(get_current_user)
):
    """Returns list of active channel adapters loaded in current memory registry."""
    return APIResponse.ok(
        data=channel_registry.list_channels(),
        message="Danh sách các adapter đã được nạp vào Registry"
    )
