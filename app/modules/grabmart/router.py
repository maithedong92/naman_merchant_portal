import hmac
import json
import logging
import secrets
import traceback
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import check_store_access, get_current_active_user, reject_non_channel_mutator
from app.core.config import get_settings
from app.core.database import get_db, get_optional_db
from app.core.exceptions import AppException, ForbiddenError
from app.core.responses import APIResponse
from app.core.security import create_partner_token, redact_sensitive_headers, verify_partner_token
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.operational_error import ErrorSeverity
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.store import Store, StoreChannelMapping
from app.models.user import User, UserRole
from app.modules.grabmart.adapter import GrabMartChannelAdapter, STORE_CODE_BY_GRAB_MERCHANT
from app.modules.grabmart.schemas import (
    GrabCancelOrderRequest,
    GrabMarkOrderReadyRequest,
    GrabOAuthTokenRequest,
    GrabOAuthTokenResponse,
    GrabOrderPrepareRequest,
)
from app.modules.grabmart.service import grabmart_client
from app.schemas.order import OrderStatusUpdateSchema
from app.services.error_service import error_service
from app.services.order_service import OrderService
from app.services.poppies_channels import get_channel_family_codes

logger = logging.getLogger("naman_portal.modules.grabmart.router")

router = APIRouter(prefix="/grabmart", tags=["Channel - GrabMart VN"])
adapter = GrabMartChannelAdapter()
settings = get_settings()


def _reject_read_only(current_user: User) -> None:
    reject_non_channel_mutator(current_user)


async def verify_grabmart_webhook_bearer(request: Request) -> None:
    """Fail-closed Bearer token verification for all GrabMart inbound webhook calls."""
    auth_header = (request.headers.get("authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid GrabMart Bearer authorization header",
        )
    token = auth_header[7:].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty GrabMart Bearer token",
        )
    static_inbound = (getattr(settings, "GRABMART_INBOUND_BEARER_TOKEN", "") or "").strip()
    if static_inbound and hmac.compare_digest(token, static_inbound):
        return
    if verify_partner_token(token, "grabmart") is not None:
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired GrabMart Bearer token",
    )


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
    Returns a signed short-lived Bearer JWT to authorize GrabMart webhook requests.
    Supports application/json and application/x-www-form-urlencoded bodies.
    """
    client_id = ""
    client_secret: Optional[str] = None
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
            client_id = str(data.get("client_id") or "")
            client_secret = data.get("client_secret")
        elif "form" in content_type:
            form = await request.form()
            client_id = str(form.get("client_id") or "")
            client_secret = form.get("client_secret")
        else:
            try:
                data = await request.json()
                client_id = str(data.get("client_id") or "")
                client_secret = data.get("client_secret")
            except Exception:
                form = await request.form()
                client_id = str(form.get("client_id") or "")
                client_secret = form.get("client_secret")
    except Exception as ex:
        logger.debug(f"Parsing body in get_partner_oauth_token: {ex}")

    configured_secret = (settings.GRABMART_CLIENT_SECRET or "").strip()
    partner_webhook_secret = (getattr(settings, "GRABMART_PARTNER_CLIENT_SECRET", "") or "").strip()
    partner_client_id = (getattr(settings, "GRABMART_PARTNER_CLIENT_ID", "naman_grabmart_pos") or "").strip()

    if client_id == partner_client_id and partner_webhook_secret:
        expected_secret = partner_webhook_secret
    else:
        expected_secret = partner_webhook_secret or configured_secret

    if not expected_secret or "your_grab" in expected_secret:
        logger.error("GrabMart OAuth rejected: GRABMART_CLIENT_SECRET / GRABMART_PARTNER_CLIENT_SECRET chưa được cấu hình.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="GrabMart OAuth credentials not configured",
        )

    provided_secret = (str(client_secret) if client_secret is not None else "").strip()
    if not provided_secret or not secrets.compare_digest(provided_secret, expected_secret):
        logger.warning(f"GrabMart OAuth token request rejected: invalid client_secret for client_id={client_id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid GrabMart client credentials",
        )

    logger.info(f"GrabMart requested partner OAuth token for client_id: {client_id}")
    access_token = create_partner_token("grabmart", client_id or "grabmart_client", expires_seconds=86400)
    return GrabOAuthTokenResponse(
        access_token=access_token,
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
    _: None = Depends(verify_grabmart_webhook_bearer),
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    raw_body = await request.body()
    headers = dict(request.headers)
    safe_headers = redact_sensitive_headers(headers)

    if db is None:
        logger.error("GrabMart order webhook rejected: database unavailable.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )

    try:
        response = await adapter.handle_order_webhook(headers, raw_body, db)
        return response
    except Exception as ex:
        logger.error(f"Error handling GrabMart order webhook: {str(ex)}", exc_info=True)
        is_validation_err = isinstance(ex, (PydanticValidationError, json.JSONDecodeError, ValueError)) or (
            isinstance(ex, AppException) and 400 <= getattr(ex, "status_code", 500) < 500
        )
        err_status = status.HTTP_400_BAD_REQUEST if is_validation_err else status.HTTP_500_INTERNAL_SERVER_ERROR
        if db is not None:
            try:
                try:
                    await db.rollback()
                except Exception:
                    pass
                await error_service.log_error(
                    db=db,
                    error_code="GRABMART_SUBMIT_ORDER_ERROR",
                    message=f"Lỗi khi tiếp nhận đơn hàng GrabMart: {str(ex)}",
                    severity=ErrorSeverity.CRITICAL,
                    module="GRABMART",
                    stack_trace=traceback.format_exc(),
                    endpoint="/webhooks/order",
                    http_method="POST",
                    request_payload={"headers": safe_headers, "body": raw_body.decode("utf-8", errors="ignore")[:2000]} if raw_body else None,
                )
            except Exception as log_ex:
                logger.error(f"Failed to record operational error: {log_ex}")
        return JSONResponse(
            status_code=err_status,
            content={"status": "FAILED", "message": "Dữ liệu đơn hàng không hợp lệ." if is_validation_err else "Lỗi xử lý đơn hàng GrabMart."},
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
    _: None = Depends(verify_grabmart_webhook_bearer),
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    raw_body = await request.body()
    headers = dict(request.headers)
    safe_headers = redact_sensitive_headers(headers)

    try:
        if db is not None:
            response = await adapter.handle_order_state_webhook(headers, raw_body, db)
            return response
        else:
            logger.warning("GrabMart order state received in offline DB mode.")
            return Response(status_code=status.HTTP_204_NO_CONTENT)
    except Exception as ex:
        logger.error(f"Error handling GrabMart order state webhook: {str(ex)}", exc_info=True)
        if db is not None:
            try:
                try:
                    await db.rollback()
                except Exception:
                    pass
                await error_service.log_error(
                    db=db,
                    error_code="GRABMART_ORDER_STATE_ERROR",
                    message=f"Lỗi khi cập nhật trạng thái đơn GrabMart: {str(ex)}",
                    severity=ErrorSeverity.ERROR,
                    module="GRABMART",
                    stack_trace=traceback.format_exc(),
                    endpoint="/webhooks/order/state",
                    http_method="PUT",
                    request_payload={"headers": safe_headers, "body": raw_body.decode("utf-8", errors="ignore")[:2000]} if raw_body else None,
                )
            except Exception as log_ex:
                logger.error(f"Failed to record operational error: {log_ex}")
        return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    _: None = Depends(verify_grabmart_webhook_bearer),
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    """
    Returns GrabMart Menu v1.1.3 JSON structure with sellingTimes and categories.
    Handles merchantID and partnerMerchantID from either headers or query params.
    Resilient design: falls back to standard catalog if database connection is offline.
    """
    grab_merchant_id = merchantID or merchant_id_query or "GM-THAO-DIEN"
    partner_store_code = (
        partnerMerchantID
        or partner_merchant_id_query
        or STORE_CODE_BY_GRAB_MERCHANT.get(grab_merchant_id)
        or "10001"
    )
    logger.info(f"GrabMart requested menu for merchantID: {grab_merchant_id}, partnerMerchantID: {partner_store_code}")

    # Check cache first (only if partnerMerchantID matches)
    cached_menu = adapter.get_cached_menu(grab_merchant_id)
    if cached_menu and cached_menu.get("partnerMerchantID") == partner_store_code:
        return cached_menu

    store_id = "default_store"
    if db is not None:
        try:
            # Resolve store by mapping or code (Canonical GRAB + Legacy GRABMART aware)
            stmt = (
                select(StoreChannelMapping)
                .join(Channel)
                .options(selectinload(StoreChannelMapping.store))
                .where(
                    Channel.code.in_(get_channel_family_codes("GRAB")),
                    (StoreChannelMapping.partner_store_id == grab_merchant_id)
                    | (StoreChannelMapping.partner_store_id == partner_store_code)
                )
            )
            res = await db.execute(stmt)
            mapping = res.scalars().first()

            if mapping:
                store_id = mapping.store_id
                if mapping.store and mapping.store.code:
                    partner_store_code = mapping.store.code
            else:
                # Check by Store code directly (e.g. "10001")
                store_by_code_res = await db.execute(select(Store).where(Store.code == partner_store_code, Store.is_active == True))
                store_by_code = store_by_code_res.scalars().first()
                if store_by_code:
                    store_id = store_by_code.id
                else:
                    # Fallback to first active store
                    store_res = await db.execute(select(Store).where(Store.is_active == True).limit(1))
                    first_store = store_res.scalars().first()
                    store_id = first_store.id if first_store else "default_store"
        except Exception as ex:
            logger.warning(f"Failed to query store for GrabMart menu: {ex}. Using default store.")

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
async def receive_menu_sync_state(
    request: Request,
    _: None = Depends(verify_grabmart_webhook_bearer),
):
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
async def receive_push_grab_menu(
    request: Request,
    _: None = Depends(verify_grabmart_webhook_bearer),
):
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
async def receive_store_integration_status(
    request: Request,
    _: None = Depends(verify_grabmart_webhook_bearer),
):
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
    _reject_read_only(current_user)
    check_store_access(current_user, store_id)

    # Resolve partner_store_id for this store on GrabMart (GRAB / GRABMART)
    stmt = (
        select(StoreChannelMapping)
        .join(Channel)
        .where(
            Channel.code.in_(get_channel_family_codes("GRAB")),
            StoreChannelMapping.store_id == store_id,
        )
    )
    res = await db.execute(stmt)
    mapping = res.scalars().first()
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
    _reject_read_only(current_user)
    check_store_access(current_user, store_id)

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
            "price": None,
        })

    # Resolve partner store id (GRAB / GRABMART)
    stmt = (
        select(StoreChannelMapping)
        .join(Channel)
        .where(
            Channel.code.in_(get_channel_family_codes("GRAB")),
            StoreChannelMapping.store_id == store_id,
        )
    )
    res = await db.execute(stmt)
    mapping = res.scalars().first()
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
    _reject_read_only(current_user)

    order_stmt = select(UnifiedOrder).where(UnifiedOrder.id == order_id)
    order_res = await db.execute(order_stmt)
    order = order_res.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if getattr(order, "store_id", None):
        check_store_access(current_user, order.store_id)

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
    _reject_read_only(current_user)

    order_stmt = select(UnifiedOrder).where(UnifiedOrder.id == order_id)
    order_res = await db.execute(order_stmt)
    order = order_res.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if getattr(order, "store_id", None):
        check_store_access(current_user, order.store_id)

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
    _reject_read_only(current_user)

    order_stmt = select(UnifiedOrder).where(UnifiedOrder.id == order_id)
    order_res = await db.execute(order_stmt)
    order = order_res.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if getattr(order, "store_id", None):
        check_store_access(current_user, order.store_id)

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

