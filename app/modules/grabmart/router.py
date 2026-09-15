import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_user
from app.core.database import get_db
from app.core.responses import APIResponse
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.store import Store, StoreChannelMapping
from app.models.user import User
from app.modules.grabmart.adapter import GrabMartChannelAdapter
from app.modules.grabmart.schemas import (
    GrabCancelOrderRequest,
    GrabMarkOrderReadyRequest,
    GrabOAuthTokenRequest,
    GrabOAuthTokenResponse,
    GrabOrderPrepareRequest,
)
from app.modules.grabmart.service import grabmart_client
from app.services.order_service import OrderService

logger = logging.getLogger("naman_portal.modules.grabmart.router")

router = APIRouter(prefix="/grabmart", tags=["Channel - GrabMart VN"])
adapter = GrabMartChannelAdapter()


# ==============================================================================
# 1. Partner OAuth Endpoint (GrabMart calls to authenticate before webhooks)
# ==============================================================================

@router.post(
    "/oauth/token",
    response_model=GrabOAuthTokenResponse,
    summary="GrabMart Partner OAuth Token Endpoint",
    description="GrabMart calls this endpoint to obtain an access token before making webhook calls.",
)
async def get_partner_oauth_token(request: Request):
    """
    Implements GrabMart Partner OAuth Token Webhook specification.
    Returns Bearer token to authorize GrabMart webhook requests.
    Supports application/json, application/x-www-form-urlencoded, and query params.
    """
    client_id = "grabmart_client"
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
            client_id = data.get("client_id", client_id)
        elif "form" in content_type:
            form = await request.form()
            client_id = form.get("client_id", client_id)
        else:
            try:
                data = await request.json()
                client_id = data.get("client_id", client_id)
            except Exception:
                form = await request.form()
                client_id = form.get("client_id", client_id)
    except Exception as ex:
        logger.debug(f"Parsing body in get_partner_oauth_token: {ex}")

    if client_id == "grabmart_client" and request.query_params.get("client_id"):
        client_id = request.query_params.get("client_id")

    logger.info(f"GrabMart requested partner OAuth token for client_id: {client_id}")
    return GrabOAuthTokenResponse(
        access_token="naman_partner_bearer_token_for_grabmart",
        token_type="Bearer",
        expires_in=86400,
    )


# ==============================================================================
# 2. Inbound Webhooks from GrabMart
# ==============================================================================

@router.post(
    "/orders",
    summary="GrabMart Submit Order Webhook (Standard)",
    include_in_schema=False,
)
@router.post(
    "/webhooks/order",
    summary="GrabMart Submit Order Webhook",
    description="Called by GrabMart when a customer places an order. Ingests order into Unified Order schema.",
)
async def receive_grabmart_order(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    raw_body = await request.body()
    headers = dict(request.headers)

    try:
        response = await adapter.handle_order_webhook(headers, raw_body, db)
        return response
    except Exception as ex:
        logger.error(f"Error handling GrabMart order webhook: {str(ex)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to process GrabMart order: {str(ex)}",
        )


@router.put(
    "/order/state",
    summary="GrabMart Push Order State Webhook (Standard)",
    include_in_schema=False,
)
@router.put(
    "/webhooks/order/state",
    summary="GrabMart Push Order State Webhook",
    description="Called by GrabMart on state changes (DRIVER_ALLOCATED, COLLECTED, DELIVERED, CANCELLED, FAILED).",
)
async def receive_grabmart_order_state(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    raw_body = await request.body()
    headers = dict(request.headers)

    try:
        response = await adapter.handle_order_state_webhook(headers, raw_body, db)
        return response
    except Exception as ex:
        logger.error(f"Error handling GrabMart order state webhook: {str(ex)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to process GrabMart order state: {str(ex)}",
        )


@router.get(
    "/merchant/menu",
    summary="GrabMart Get Mart Menu Webhook (Standard)",
    include_in_schema=False,
)
@router.get(
    "/menu",
    summary="GrabMart Get Mart Menu Webhook",
    description="GrabMart calls this endpoint to pull the latest store catalog/menu JSON.",
)
async def get_mart_menu_webhook(
    merchantID: Optional[str] = Header(None),
    partnerMerchantID: Optional[str] = Header(None),
    BusinessType: Optional[int] = Query(default=1),
    merchant_id_query: Optional[str] = Query(None, alias="merchantID"),
    partner_merchant_id_query: Optional[str] = Query(None, alias="partnerMerchantID"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns GrabMart Menu v1.1.3 JSON structure with sellingTimes and categories.
    Handles merchantID and partnerMerchantID from either headers or query params.
    """
    grab_merchant_id = merchantID or merchant_id_query or "GM-THAO-DIEN"
    partner_store_code = partnerMerchantID or partner_merchant_id_query or "10001"
    logger.info(f"GrabMart requested menu for merchantID: {grab_merchant_id}, partnerMerchantID: {partner_store_code}")

    # Check cache first (only if partnerMerchantID matches)
    cached_menu = adapter.get_cached_menu(grab_merchant_id)
    if cached_menu and cached_menu.get("partnerMerchantID") == partner_store_code:
        return cached_menu

    # Resolve store by mapping or code
    stmt = (
        select(StoreChannelMapping)
        .join(Channel)
        .where(
            Channel.code == "GRABMART",
            (StoreChannelMapping.partner_store_id == grab_merchant_id)
            | (StoreChannelMapping.partner_store_id == partner_store_code)
        )
    )
    res = await db.execute(stmt)
    mapping = res.scalar_one_or_none()

    if mapping:
        store_id = mapping.store_id
    else:
        # Check by Store code directly (e.g. "10001")
        store_by_code_res = await db.execute(select(Store).where(Store.code == partner_store_code, Store.is_active == True))
        store_by_code = store_by_code_res.scalar_one_or_none()
        if store_by_code:
            store_id = store_by_code.id
        else:
            # Fallback to first active store
            store_res = await db.execute(select(Store).where(Store.is_active == True).limit(1))
            first_store = store_res.scalar_one_or_none()
            store_id = first_store.id if first_store else "default_store"

    menu_payload = await adapter.build_catalog_menu(
        store_id=store_id,
        partner_store_id=grab_merchant_id,
        partner_merchant_id=partner_store_code,
        db=db,
    )
    return menu_payload


@router.post(
    "/menu/sync/state",
    summary="GrabMart Menu Sync State Webhook",
    description="GrabMart calls this endpoint to notify the status of a background menu sync.",
)
async def receive_menu_sync_state(request: Request):
    try:
        data = await request.json()
        logger.info(f"GrabMart menu sync state callback: {data}")
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/menus",
    summary="GrabMart Push Grab Menu Webhook",
    description="GrabMart calls this endpoint to push the Grab store menu to partner POS.",
)
async def receive_push_grab_menu(request: Request):
    try:
        data = await request.json()
        logger.info(f"GrabMart push menu callback: merchantID={data.get('merchantID')}")
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/status",
    summary="GrabMart Push Store Integration Status Webhook",
    description="GrabMart calls this endpoint to notify store integration status changes.",
)
async def receive_store_integration_status(request: Request):
    try:
        data = await request.json()
        logger.info(f"GrabMart integration status callback: {data}")
    except Exception:
        pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ==============================================================================
# 3. Staff & Management Operations (Merchant Portal -> GrabMart)
# ==============================================================================

@router.post(
    "/stores/{store_id}/sync-menu",
    response_model=APIResponse[Dict[str, Any]],
    summary="Trigger Menu Sync to GrabMart for Store",
    description="Builds catalog and notifies GrabMart to synchronize menu.",
)
async def trigger_menu_sync(
    store_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # Resolve partner_store_id for this store on GrabMart
    stmt = (
        select(StoreChannelMapping)
        .join(Channel)
        .where(
            Channel.code == "GRABMART",
            StoreChannelMapping.store_id == store_id,
        )
    )
    res = await db.execute(stmt)
    mapping = res.scalar_one_or_none()
    partner_store_id = mapping.partner_store_id if mapping else f"GM-{store_id[:8]}"

    sync_result = await adapter.sync_menu(
        store_id=store_id,
        partner_store_id=partner_store_id,
        db=db,
    )

    if not sync_result.success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=sync_result.message,
        )

    return APIResponse.ok(
        data=sync_result.model_dump(),
        message=sync_result.message,
    )


@router.post(
    "/stores/{store_id}/sync-stock",
    response_model=APIResponse[Dict[str, Any]],
    summary="Trigger Real-time Inventory Stock Sync to GrabMart",
)
async def trigger_stock_sync(
    store_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # Fetch active inventories for this store
    inv_stmt = (
        select(StoreInventory)
        .where(StoreInventory.store_id == store_id)
        .options(selectinload(StoreInventory.product))
    )
    inv_res = await db.execute(inv_stmt)
    inventories = inv_res.scalars().all()

    stock_items = []
    for inv in inventories:
        stock_items.append({
            "sku": inv.product.sku if inv.product else inv.product_id,
            "is_out_of_stock": inv.is_out_of_stock,
            "available_stock": inv.available_stock,
            "price": int(inv.product.base_price) if inv.product else None,
        })

    # Resolve partner store id
    stmt = (
        select(StoreChannelMapping)
        .join(Channel)
        .where(
            Channel.code == "GRABMART",
            StoreChannelMapping.store_id == store_id,
        )
    )
    res = await db.execute(stmt)
    mapping = res.scalar_one_or_none()
    partner_store_id = mapping.partner_store_id if mapping else f"GM-{store_id[:8]}"

    sync_result = await adapter.sync_inventory(
        store_id=store_id,
        partner_store_id=partner_store_id,
        stock_items=stock_items,
        db=db,
    )

    return APIResponse.ok(
        data=sync_result.model_dump(),
        message=sync_result.message,
    )


@router.post(
    "/orders/{order_id}/prepare",
    response_model=APIResponse[Dict[str, Any]],
    summary="Accept or Reject GrabMart Order",
    description="Portal staff accepts or rejects an inbound GrabMart order.",
)
async def prepare_order(
    order_id: str,
    payload: GrabOrderPrepareRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    order_stmt = select(UnifiedOrder).where(UnifiedOrder.id == order_id)
    order_res = await db.execute(order_stmt)
    order = order_res.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    target_status = UnifiedOrderStatus.ACCEPTED if payload.toState == "Accepted" else UnifiedOrderStatus.CANCELLED
    updated_order = await OrderService.update_order_status(
        order_id=order.id,
        payload=OrderStatusUpdateSchema(
            new_status=target_status,
            note=f"GrabMart manual action: {payload.toState}",
            changed_by=current_user.username,
            cancellation_reason=f"GrabMart action: {payload.toState}" if target_status == UnifiedOrderStatus.CANCELLED else None,
        ),
        db=db,
        sync_to_channel=True,
    )

    return APIResponse.ok(
        data={"order_id": order.id, "channel_order_id": order.channel_order_id, "status": updated_order.status},
        message=f"Đơn hàng đã được cập nhật sang trạng thái {updated_order.status}",
    )


@router.post(
    "/orders/{order_id}/ready",
    response_model=APIResponse[Dict[str, Any]],
    summary="Mark GrabMart Order as Ready for Pickup",
)
async def mark_order_ready(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    order_stmt = select(UnifiedOrder).where(UnifiedOrder.id == order_id)
    order_res = await db.execute(order_stmt)
    order = order_res.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    updated_order = await OrderService.update_order_status(
        order_id=order.id,
        payload=OrderStatusUpdateSchema(
            new_status=UnifiedOrderStatus.READY,
            note="Đơn hàng đã đóng gói sẵn sàng cho tài xế Grab lấy",
            changed_by=current_user.username,
        ),
        db=db,
        sync_to_channel=True,
    )

    return APIResponse.ok(
        data={"order_id": order.id, "status": updated_order.status},
        message="Đã báo đơn hàng sẵn sàng giao cho tài xế GrabMart",
    )


@router.post(
    "/orders/{order_id}/cancel",
    response_model=APIResponse[Dict[str, Any]],
    summary="Cancel GrabMart Order with Reason Code",
)
async def cancel_order(
    order_id: str,
    payload: GrabCancelOrderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    order_stmt = select(UnifiedOrder).where(UnifiedOrder.id == order_id)
    order_res = await db.execute(order_stmt)
    order = order_res.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    updated_order = await OrderService.update_order_status(
        order_id=order.id,
        payload=OrderStatusUpdateSchema(
            new_status=UnifiedOrderStatus.CANCELLED,
            note=f"Hủy đơn GrabMart (Mã lý do {payload.cancelCode})",
            cancellation_reason=f"Mã lý do {payload.cancelCode}",
            changed_by=current_user.username,
        ),
        db=db,
        sync_to_channel=True,
    )

    return APIResponse.ok(
        data={"order_id": order.id, "status": updated_order.status},
        message=f"Đơn hàng GrabMart đã được hủy (Mã {payload.cancelCode})",
    )

