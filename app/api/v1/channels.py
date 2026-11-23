from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, get_current_user, require_store_manager, require_super_admin
from app.core.database import get_db
from app.core.exceptions import ConflictError, NotFoundError
from app.core.responses import APIResponse
from app.models.channel import Channel
from app.models.store import Store, StoreChannelMapping
from app.models.user import User
from app.schemas.channel import (
    ChannelCreate,
    ChannelFeatureStatus,
    ChannelFeatureUpdate,
    ChannelResponse,
    sanitize_channel_config,
)
from app.services.channel_registry import channel_registry
from app.services.channel_service import channel_service
from app.services.poppies_channels import get_channel_family_codes, normalize_channel_code
from app.services.poppies_service import poppies_service

router = APIRouter(prefix="/channels", tags=["Sales Channels"])


def _format_channel_response(channel: Channel) -> ChannelResponse:
    features_dict = channel_service.extract_features(channel)
    resp = ChannelResponse.model_validate(channel)
    resp.config = sanitize_channel_config(resp.config)
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


@router.get("/registered-adapters", response_model=APIResponse[List[str]])
async def get_registered_adapters(
    current_user: User = Depends(get_current_user)
):
    """Returns list of active channel adapters loaded in current memory registry."""
    return APIResponse.ok(
        data=channel_registry.list_channels(),
        message="Danh sách các adapter đã được nạp vào Registry"
    )


@router.get("/poppies-matching", response_model=APIResponse[Dict[str, Any]])
async def get_poppies_channel_matching(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Đối soát trực tiếp 14+ kênh bán hàng Canonical từ Poppies ERP với bảng `channels`,
    `store_channel_mappings`, `ChannelRegistry` và trạng thái giá/cấm bán tại NAM-UMP.
    """
    report = await poppies_service.get_poppies_channel_matching_report(db=db)
    return APIResponse.ok(
        data=report,
        message="Đã tải báo cáo đối soát & matching kênh Poppies ERP.",
    )


@router.post("/sync-poppies", response_model=APIResponse[Dict[str, Any]])
async def sync_and_match_channels_from_poppies(
    branch_code: str = Query("10001", description="Mã chi nhánh đối chiếu giá bán"),
    sync_pricing: bool = Query(
        False,
        description="Đồng bộ kèm Pricing & Exclusions Snapshot cho các sàn S2S (GRAB, SHOPEEFOOD, SHOPEEMART)",
    ),
    sync_catalog: bool = Query(
        False,
        description="Đồng bộ kèm toàn bộ Master SKU & tồn kho CTTK từ Poppies ERP",
    ),
    current_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Đồng bộ danh sách kênh chuẩn Canonical từ Poppies ERP (`GET /api/v1/pos/pricing/channels`),
    tự động gộp các mã kênh legacy trùng lặp (ví dụ `GRABMART` -> `GRAB`, `POS_OFFLINE` -> `STORE`)
    mà không mất liên kết cửa hàng hay lịch sử đơn hàng, đồng thời khớp nối Adapter & 4 chi nhánh.
    """
    if sync_catalog or sync_pricing:
        full_res = await poppies_service.sync_and_match_all_from_poppies(
            db=db,
            branch_code=branch_code,
            sync_master_skus=sync_catalog,
            sync_stock=sync_catalog,
            pricing_channels=["GRAB", "SHOPEEFOOD", "SHOPEEMART"] if sync_pricing else [],
        )
        return APIResponse.ok(
            data=full_res,
            message=(
                f"Đã đồng bộ & matching thành công {full_res['channel_reconciliation']['total_poppies_channels']} "
                "kênh chuẩn từ Poppies ERP!"
            ),
        )

    rec_res = await poppies_service.reconcile_channels_from_poppies(
        db=db, sync_store_outlets=True
    )
    return APIResponse.ok(
        data=rec_res,
        message=(
            f"Đã đồng bộ & matching chính xác {rec_res['total_poppies_channels']} kênh bán hàng từ Poppies ERP!"
        ),
    )


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
    current_user: User = Depends(require_super_admin),
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
    code_upper = normalize_channel_code(payload.code)
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


@router.post("/{channel_code}/sync-menu")
async def sync_channel_menu(
    channel_code: str,
    store_id: Optional[str] = Query(None, description="ID hoặc mã chi nhánh (mặc định 10001)"),
    current_user: User = Depends(require_store_manager),
    db: AsyncSession = Depends(get_db),
):
    """Trigger menu/catalog synchronization for a specific channel and store."""
    code_upper = channel_code.upper()
    adapter = channel_registry.get(code_upper)
    if not adapter:
        raise NotFoundError("ChannelAdapter", code_upper)

    target_store_key = store_id or current_user.store_id or "10001"
    store_stmt = select(Store).where((Store.id == target_store_key) | (Store.code == target_store_key))
    store_obj = (await db.execute(store_stmt)).scalar_one_or_none()
    effective_store_id = store_obj.id if store_obj else target_store_key
    check_store_access(current_user, effective_store_id)

    mapping_stmt = (
        select(StoreChannelMapping)
        .join(Channel)
        .where(
            Channel.code.in_(get_channel_family_codes(code_upper)),
            StoreChannelMapping.store_id == effective_store_id,
        )
    )
    mapping_obj = (await db.execute(mapping_stmt)).scalars().first()
    partner_store_id = (
        mapping_obj.partner_store_id
        if (mapping_obj and mapping_obj.partner_store_id)
        else (store_obj.code if store_obj else target_store_key)
    )

    result = await adapter.sync_menu(
        store_id=effective_store_id,
        partner_store_id=partner_store_id,
        db=db,
    )
    return APIResponse.ok(
        data=result.model_dump(),
        message=result.message,
    )
