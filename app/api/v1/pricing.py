import hmac
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_store_access_by_key,
    get_current_user,
    require_head_office_manager,
    require_store_manager,
)
from app.core.config import get_settings
from app.core.database import get_db
from app.core.responses import APIResponse
from app.core.security import redact_sensitive_headers
from app.models.user import User
from app.schemas.product import (
    ChannelPublishToggleResult,
    PoppiesChannelPublishStatusRequest,
    PricingSnapshotSyncResponse,
    ProductChannelPublishOverviewResponse,
)
from app.services.poppies_service import poppies_service

router = APIRouter(prefix="/pricing", tags=["Pricing & Channel Governance"])
settings = get_settings()


@router.get("/channels", response_model=APIResponse[Dict[str, Any]])
async def get_pricing_channels(
    current_user: User = Depends(get_current_user),
):
    """
    Get standardized 13 Canonical sales channels metadata conforming strictly to Poppies ERP spec.
    Includes Arito accounting mapping (`ma_kenh`), `has_partner_pricing`, `inherited_from`, and aliases.
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
    Get ON/OFF publish status, store inheritance, and effective price of a SKU across all 13 Canonical channels.
    """
    overview = await poppies_service.get_sku_channel_publish_overview(db=db, sku=sku)
    return APIResponse.ok(data=overview)


@router.post("/channel-publish-status", response_model=APIResponse[ChannelPublishToggleResult])
async def set_channel_publish_status(
    payload: PoppiesChannelPublishStatusRequest,
    current_user: User = Depends(require_head_office_manager),
    db: AsyncSession = Depends(get_db)
):
    """
    Toggle ON/OFF selling status for 1 SKU on a specified Canonical/alias channel.
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


@router.post("/snapshots/sync", response_model=APIResponse[PricingSnapshotSyncResponse])
async def sync_pricing_snapshot(
    branch_code: str = Query("10001", description="Store/branch code (10001, 10004, 10005, 10006)"),
    channel_code: str = Query("STORE", description="Canonical or alias channel code"),
    since_version: Optional[str] = Query(None, description="Optional delta version cursor"),
    format: str = Query("map", pattern="^(map|list)$", description="Snapshot format: map or list"),
    current_user: User = Depends(require_store_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Pull Multi-Channel Pricing & Exclusions Snapshot from Poppies ERP (`GET /api/v1/pos/pricing/snapshots`).
    Strictly Fail-Closed: on HTTP 5xx or timeout, returns HTTP 503/504 and retains Last-Known-Good cache.
    """
    store = await check_store_access_by_key(db, current_user, branch_code)
    effective_branch = (
        store.code if store is not None and isinstance(getattr(store, "code", None), str) else branch_code
    )
    res = await poppies_service.sync_channel_pricing_snapshot(
        db=db,
        branch_code=effective_branch,
        channel_code=channel_code,
        since_version=since_version,
        fmt=format,
    )
    return APIResponse.ok(
        data=res,
        message=f"Đã đồng bộ Pricing Snapshot kênh {res['channel_code']} (chi nhánh {effective_branch}).",
    )


@router.post("/sync-poppies", response_model=APIResponse[Dict[str, Any]])
async def sync_and_match_pricing_from_poppies(
    branch_code: str = Query("10001", description="Mã chi nhánh đối chiếu giá bán"),
    sync_master_skus: bool = Query(False, description="Đồng bộ kèm Master SKU & CTTK nếu cần"),
    current_user: User = Depends(require_head_office_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Đồng bộ & matching toàn bộ 14+ kênh bán hàng Canonical kèm Pricing & Exclusions Snapshots
    từ Poppies ERP về NAM-UMP.
    """
    res = await poppies_service.sync_and_match_all_from_poppies(
        db=db,
        branch_code=branch_code,
        sync_master_skus=sync_master_skus,
        sync_stock=sync_master_skus,
    )
    return APIResponse.ok(
        data=res,
        message="Đã đồng bộ & matching kênh bán hàng + bảng giá đa kênh từ Poppies ERP!",
    )


@router.post("/webhook/price-changed", status_code=status.HTTP_200_OK)
async def poppies_price_change_webhook(
    request: Request,
    payload: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    x_poppies_webhook_key: Optional[str] = Header(None, alias="X-Poppies-Webhook-Key"),
    x_event_id: Optional[str] = Header(None, alias="X-Event-Id"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    Webhook listener from Poppies ERP PricingOutboxDispatcher.
    Receives price_changed, CHANNEL_PUBLISH_STATUS_CHANGED, and CHANNEL_PRICING_STATUS_CHANGED events.
    Guarantees idempotency, out-of-order protection, and safe multi-channel cache synchronization.
    """
    expected_key = (getattr(settings, "POPPIES_WEBHOOK_KEY", None) or "").strip()
    provided_key = (x_poppies_webhook_key or "").strip()
    if not expected_key or not provided_key or not hmac.compare_digest(provided_key, expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Mã xác thực Webhook không hợp lệ."
        )

    res = await poppies_service.process_price_change_webhook(
        db=db,
        payload=payload,
        event_id_header=(x_event_id or idempotency_key),
        headers=redact_sensitive_headers(dict(request.headers)),
    )
    return APIResponse.ok(data=res)
