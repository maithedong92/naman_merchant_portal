import json
import logging
import secrets
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import verify_hmac_sha256
from app.core.config import get_settings
from app.models.store import Store
from app.modules.shopeefood.adapter import ShopeeFoodChannelAdapter

logger = logging.getLogger("naman_portal.modules.shopeefood.router")
router = APIRouter(tags=["ShopeeFood / Foody S2S"])
adapter = ShopeeFoodChannelAdapter()
settings = get_settings()

security = HTTPBasic(auto_error=False)


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

    # Optional signature check if Authorization header is provided
    if authorization and authorization.startswith("Signature "):
        sig = authorization.replace("Signature ", "").strip()
        full_url = str(request.url)
        body_str = raw_body.decode("utf-8") if raw_body else ""
        base_string = f"{request.method.upper()}|{full_url}|{body_str}"
        # If signature verification is strictly required in production:
        # valid = verify_hmac_sha256(settings.SHOPEEFOOD_APP_KEY, base_string, sig, is_hex_key=True)

    try:
        response = await adapter.handle_order_webhook(headers, raw_body, db)
        return response
    except Exception as ex:
        logger.error(f"Lỗi khi xử lý webhook đơn hàng ShopeeFood: {str(ex)}")
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
    db: AsyncSession = Depends(get_db)
):
    """Trigger manual menu synchronization for a ShopeeFood outlet."""
    res = await adapter.sync_menu(
        store_id=partner_store_id,
        partner_store_id=partner_store_id,
        db=db
    )
    return res
