from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_super_admin
from app.core.database import get_db
from app.core.exceptions import ConflictError
from app.core.responses import APIResponse
from app.models.channel import Channel
from app.models.user import User
from app.schemas.channel import ChannelCreate, ChannelResponse
from app.services.channel_registry import channel_registry

router = APIRouter(prefix="/channels", tags=["Sales Channels"])


@router.get("", response_model=APIResponse[List[ChannelResponse]])
async def list_channels(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """List all registered e-commerce and delivery channels."""
    stmt = select(Channel).order_by(Channel.code)
    res = await db.execute(stmt)
    channels = res.scalars().all()
    return APIResponse.ok(data=channels)


@router.post("", response_model=APIResponse[ChannelResponse], status_code=201)
async def create_channel(
    payload: ChannelCreate,
    current_admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db)
):
    """Register a new channel (e.g. SHOPEEFOOD, GRABMART)."""
    code_upper = payload.code.upper()
    existing = await db.execute(select(Channel).where(Channel.code == code_upper))

    if existing.scalar_one_or_none():
        raise ConflictError(f"Kênh với mã '{code_upper}' đã tồn tại.")

    channel = Channel(
        code=code_upper,
        name=payload.name,
        description=payload.description,
        is_active=payload.is_active,
        base_url=payload.base_url,
        webhook_secret=payload.webhook_secret,
        config=payload.config
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return APIResponse.ok(data=channel, message="Đăng ký kênh bán hàng thành công")


@router.get("/registered-adapters", response_model=APIResponse[List[str]])
async def get_registered_adapters(
    current_user: User = Depends(get_current_user)
):
    """Returns list of active channel adapters loaded in current memory registry."""
    return APIResponse.ok(
        data=channel_registry.list_channels(),
        message="Danh sách các adapter đã được nạp vào Registry"
    )

