from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_store_manager
from app.core.config import get_settings
from app.core.database import get_db
from app.core.responses import APIResponse
from app.models.user import User
from app.schemas.product import (
    ChannelPublishStatusItem,
    ChannelPublishToggleResult,
    PoppiesChannelPublishStatusRequest,
    ProductChannelPublishOverviewResponse,
    ProductChannelPublishToggleRequest,
)
from app.services.poppies_service import POPPIES_STANDARD_CHANNELS, poppies_service

router = APIRouter(prefix="/pricing", tags=["Pricing & Channel Governance"])
settings = get_settings()


@router.get("/channels", response_model=APIResponse[Dict[str, Any]])
async def get_pricing_channels():
    """
    Get standardized sales channels metadata conforming strictly to Poppies ERP spec.
    Includes Arito accounting mapping (ma_kenh), POS code, and group.
    """
    channels = await poppies_service.client.get_pricing_channels()
    return APIResponse.ok(data=channels)


@router.get("/products/{sku}/channels", response_model=APIResponse[ProductChannelPublishOverviewResponse])
async def get_product_channels_overview(
    sku: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get ON/OFF status of a SKU across all 10 standard sales channels.
    """
    overview = await poppies_service.get_sku_channel_publish_overview(db=db, sku=sku)
    return APIResponse.ok(data=overview)


@router.post("/channel-publish-status", response_model=APIResponse[ChannelPublishToggleResult])
async def set_channel_publish_status(
    payload: PoppiesChannelPublishStatusRequest,
    current_user: User = Depends(require_store_manager),
    db: AsyncSession = Depends(get_db)
):
    """
    Toggle ON/OFF selling status for 1 SKU on a specified channel.
    Conforms directly to Poppies ERP API: POST /api/v1/pricing/channel-publish-status.
    Propagates changes immediately to Poppies ERP.
    """
    res = await poppies_service.toggle_sku_channel_publish(
        db=db,
        sku=payload.sku,
        channel_code=payload.scopeCode,
        is_published=payload.isPublished,
        sync_to_poppies=True
    )
    return APIResponse.ok(data=res, message=res["message"])


@router.post("/webhook/price-changed", status_code=status.HTTP_200_OK)
async def poppies_price_change_webhook(
    payload: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    x_poppies_webhook_key: Optional[str] = Header(None, alias="X-Poppies-Webhook-Key"),
    x_event_id: Optional[str] = Header(None, alias="X-Event-Id")
):
    """
    Webhook listener from Poppies ERP PricingOutboxDispatcher.
    Receives price_changed and CHANNEL_PUBLISH_STATUS_CHANGED events.
    Guarantees idempotency and safe multi-channel cache synchronization.
    """
    expected_key = getattr(settings, "POPPIES_WEBHOOK_KEY", None)
    if expected_key and x_poppies_webhook_key and x_poppies_webhook_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Mã xác thực Webhook không hợp lệ."
        )

    res = await poppies_service.process_price_change_webhook(db=db, payload=payload)
    return APIResponse.ok(data=res)
