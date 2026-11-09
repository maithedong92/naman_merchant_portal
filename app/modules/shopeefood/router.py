import json
import logging
import secrets
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import check_store_access, get_current_active_user
from app.core.database import get_db
from app.core.exceptions import ForbiddenError
from app.core.security import verify_hmac_sha256
from app.core.config import get_settings
from app.models.channel import Channel
from app.models.operational_error import ErrorSeverity
from app.models.store import Store, StoreChannelMapping
from app.models.user import User, UserRole
from app.modules.shopeefood.adapter import ShopeeFoodChannelAdapter
from app.modules.shopeefood.service import shopeefood_client
from app.services.error_service import error_service
import traceback

logger = logging.getLogger("naman_portal.modules.shopeefood.router")
router = APIRouter(tags=["ShopeeFood / Foody S2S"])
adapter = ShopeeFoodChannelAdapter()
settings = get_settings()

security = HTTPBasic(auto_error=False)


def _reject_read_only(current_user: User) -> None:
    role_val = current_user.role.value if hasattr(current_user.role, "value") else str(current_user.role)
    if role_val == UserRole.READ_ONLY.value and not getattr(current_user, "is_superuser", False):
        raise ForbiddenError("Tài khoản chỉ xem không có quyền thực hiện thao tác này.")


# ==============================================================================
# 1. Inbound Order & Status Webhooks
# ==============================================================================

@router.post("/shopeefood/webhooks/order")
@router.post("/shopeefood/webhooks/update_order")
async def receive_shopeefood_order_webhook(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_foody_app_id: Optional[str] = Header(None, alias="X-Foody-App-Id"),
    db: AsyncSession = Depends(get_db)
):
    """
    Inbound Webhook endpoint receiving real-time orders from ShopeeFood / Foody.
    Supports signature validation and feature flag check ('order_webhook_enabled').
    """
    raw_body = await request.body()
    headers = dict(request.headers)

    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    public_url = f"{proto}://{host}{request.url.path}"
    if request.url.query:
        public_url = f"{public_url}?{request.url.query}"

    # Enforce signature check when Authorization header is provided, or when request arrives via reverse proxy
    if authorization:
        sig = (
            authorization.replace("Signature ", "").strip()
            if authorization.startswith("Signature ")
            else authorization.strip()
        )
        full_url = str(request.url)
        try:
            valid = shopeefood_client.verify_hmac_sha256(
                raw_body,
                sig,
                method=request.method,
                full_url=full_url,
                public_url=public_url,
            )
        except TypeError:
            valid = shopeefood_client.verify_hmac_sha256(raw_body, sig)

        if not valid:
            body_str = raw_body.decode("utf-8", errors="ignore") if raw_body else ""
            base_string = f"{request.method.upper()}|{full_url}|{body_str}"
            valid = verify_hmac_sha256(settings.SHOPEEFOOD_APP_KEY, base_string, sig, is_hex_key=True)

        if not valid:
            logger.warning("ShopeeFood webhook signature verification failed.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="ShopeeFood webhook signature verification failed",
            )
    elif request.headers.get("x-forwarded-proto") or request.headers.get("x-real-ip"):
        logger.warning("ShopeeFood webhook rejected: missing Authorization signature header from external proxy.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing ShopeeFood Authorization signature header",
        )

    try:
        response = await adapter.handle_order_webhook(headers, raw_body, db)
        return response
    except Exception as ex:
        logger.error(f"Lỗi khi xử lý webhook đơn hàng ShopeeFood: {str(ex)}")
        try:
            try:
                await db.rollback()
            except Exception:
                pass
            await error_service.log_error(
                db=db,
                error_code="SHOPEEFOOD_WEBHOOK_ORDER_ERROR",
                message=f"Lỗi khi xử lý webhook đơn hàng ShopeeFood: {str(ex)}",
                severity=ErrorSeverity.CRITICAL,
                module="SHOPEEFOOD",
                stack_trace=traceback.format_exc(),
                endpoint=str(request.url.path),
                http_method="POST",
                request_payload={"headers": headers, "body": raw_body.decode("utf-8", errors="ignore")[:2000]} if raw_body else None,
            )
        except Exception as log_ex:
            logger.error(f"Failed to record operational error: {log_ex}")
        return {"result": "failed", "error": str(ex)}


# ==============================================================================
# 2. Public Store Menu JSON Endpoint (Basic Auth)
# According to 'Namanmarket X ShopeeFoodAPI Documentation.pdf'
# ==============================================================================

@router.get("/shopeefoodapi/{store_code}.json")
async def get_shopeefood_store_menu_json(
    store_code: str,
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db)
):
    """
    Public Menu endpoint used by ShopeeFood crawler to fetch store catalog.
    Secured by HTTP Basic Authentication (namanmarket / shopeefoodapi).
    Stores:
    - 21 Thảo Điền: 10001
    - 46 Hưng Phúc: 10004
    - 17 Mai Chí Thọ: 10005
    - 303 Nguyễn Văn Trỗi: 10006
    """
    # 1. Validate Basic Auth credentials
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"}
        )

    correct_username = secrets.compare_digest(credentials.username, "namanmarket")
    correct_password = secrets.compare_digest(credentials.password, "shopeefoodapi")
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"}
        )

    # 2. Check if cached menu exists
    cached = adapter.get_cached_menu(store_code)
    if cached:
        return Response(
            content=json.dumps(cached, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{store_code}.json"'}
        )

    # 3. Find matching store in database or build catalog
    stmt = select(Store).where(Store.code == store_code)
    res = await db.execute(stmt)
    store = res.scalar_one_or_none()
    store_id = store.id if store else store_code

    catalog = await adapter.build_catalog_menu(
        store_id=store_id,
        partner_store_id=store_code,
        db=db
    )

    return Response(
        content=json.dumps(catalog, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{store_code}.json"'}
    )


# ==============================================================================
# 3. Manual / Testing Sync Trigger Endpoints
# ==============================================================================

@router.post("/shopeefood/sync/menu/{partner_store_id}")
async def trigger_shopeefood_menu_sync(
    partner_store_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Trigger manual menu synchronization for a ShopeeFood outlet."""
    _reject_read_only(current_user)

    resolved_store_id = partner_store_id
    resolved_partner_store_id = partner_store_id
    try:
        mapping_stmt = (
            select(StoreChannelMapping)
            .join(Channel)
            .options(selectinload(StoreChannelMapping.store))
            .where(
                Channel.code.in_(["SHOPEEFOOD", "SHOPEE_FOOD", "FOODY"]),
                (StoreChannelMapping.partner_store_id == partner_store_id)
                | (StoreChannelMapping.store_id == partner_store_id),
            )
        )
        mapping_res = await db.execute(mapping_stmt)
        mapping = mapping_res.scalars().first()
        if mapping and getattr(mapping, "store_id", None) and isinstance(mapping.store_id, str):
            resolved_store_id = mapping.store_id
            if getattr(mapping, "partner_store_id", None) and isinstance(mapping.partner_store_id, str):
                resolved_partner_store_id = mapping.partner_store_id
        else:
            store_stmt = select(Store).where(
                (Store.code == partner_store_id) | (Store.id == partner_store_id)
            )
            store_res = await db.execute(store_stmt)
            store_obj = store_res.scalars().first()
            if store_obj and getattr(store_obj, "id", None) and isinstance(store_obj.id, str):
                resolved_store_id = store_obj.id
                if getattr(store_obj, "code", None) and isinstance(store_obj.code, str):
                    resolved_partner_store_id = store_obj.code
    except Exception as ex:
        logger.warning(f"Could not resolve store for ShopeeFood menu sync ({partner_store_id}): {ex}")

    if getattr(current_user, "store_id", None):
        check_store_access(current_user, resolved_store_id)

    res = await adapter.sync_menu(
        store_id=resolved_store_id,
        partner_store_id=resolved_partner_store_id,
        db=db,
    )
    return res
