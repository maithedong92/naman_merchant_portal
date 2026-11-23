import ipaddress
import json
import logging
import os
import re
import secrets
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    _is_trusted_proxy,
    check_store_access,
    get_current_active_user,
    reject_non_channel_mutator,
)
from app.core.database import get_db
from app.core.exceptions import ForbiddenError
from app.core.security import redact_sensitive_headers, verify_hmac_sha256
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
    reject_non_channel_mutator(current_user)


def _is_trusted_webhook_proxy(request: Request) -> bool:
    """Return True only when direct TCP peer is in TRUSTED_PROXY_CIDRS (or TestClient in test env)."""
    peer_ip = request.client.host if request.client else None
    if not peer_ip:
        return False
    current_settings = get_settings()
    if peer_ip == "testclient" and str(getattr(current_settings, "ENVIRONMENT", "")).lower() == "test":
        return True
    try:
        peer_addr = ipaddress.ip_address(peer_ip)
        for cidr in getattr(current_settings, "TRUSTED_PROXY_CIDRS", []):
            if peer_addr in ipaddress.ip_network(cidr, strict=False):
                return True
    except ValueError:
        return _is_trusted_proxy(peer_ip)
    return False


def _get_public_base_url() -> str:
    current_settings = get_settings()
    return (
        getattr(current_settings, "PUBLIC_BASE_URL", "")
        or os.environ.get("PUBLIC_BASE_URL", "")
        or ""
    ).strip().rstrip("/")


def _build_canonical_webhook_url(request: Request) -> str:
    """Reconstruct canonical public URL from PUBLIC_BASE_URL or trusted proxy X-Forwarded-* headers."""
    query_suffix = f"?{request.url.query}" if request.url.query else ""
    public_base = _get_public_base_url()
    if public_base:
        return f"{public_base}{request.url.path}{query_suffix}"

    if _is_trusted_webhook_proxy(request):
        raw_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        proto = raw_proto if raw_proto in ("http", "https") else request.url.scheme
        raw_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
        if raw_host and not any(ch in raw_host for ch in ("/", "\\", " ", "\r", "\n")):
            host = raw_host
        else:
            host = request.headers.get("host") or request.url.netloc
        return f"{proto}://{host}{request.url.path}{query_suffix}"

    return str(request.url)


# ==============================================================================
# 1. Inbound Order & Status Webhooks
# ==============================================================================

async def _verify_shopeefood_order_via_s2s(
    raw_body: bytes,
    request: Request,
    db: AsyncSession,
) -> Dict[str, Any]:
    """
    Fail-closed Proof-of-Order verification via Foody S2S `get_order_details` when an
    inbound Foody webhook omits the Authorization signature header.
    """
    current_settings = get_settings()
    s2s_fallback_enabled = bool(
        getattr(current_settings, "SHOPEEFOOD_WEBHOOK_S2S_VERIFY_FALLBACK", True)
    )
    app_key = getattr(shopeefood_client, "app_key", "") or getattr(
        current_settings, "SHOPEEFOOD_APP_KEY", ""
    )
    if not s2s_fallback_enabled or not app_key or "your_" in str(app_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing ShopeeFood Authorization signature header",
        )

    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    order_code = str(payload.get("order_code") or payload.get("order_id") or "").strip()
    if not order_code:
        logger.warning(
            f"ShopeeFood webhook rejected: missing Authorization and missing order_code (keys={sorted(payload.keys())})."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing ShopeeFood Authorization signature header",
        )

    verify_resp = await shopeefood_client.get_order_details(order_code, mark_as_received=0)
    reply = verify_resp.get("reply") if isinstance(verify_resp, dict) else None
    if (
        not isinstance(verify_resp, dict)
        or verify_resp.get("result") != "success"
        or not isinstance(reply, dict)
        or str(reply.get("code") or "").strip() != order_code
    ):
        logger.warning(
            f"ShopeeFood webhook S2S verification failed for order_code={order_code}: "
            f"result={verify_resp.get('result') if isinstance(verify_resp, dict) else type(verify_resp).__name__}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ShopeeFood S2S order verification failed",
        )

    reply_rest = reply.get("restaurant") if isinstance(reply.get("restaurant"), dict) else {}
    reply_partner_id = str(reply_rest.get("partner_restaurant_id") or "").strip()
    reply_rest_id = str(reply_rest.get("id") or "").strip()
    payload_partner_id = str(payload.get("partner_restaurant_id") or "").strip()
    payload_rest_id = str(payload.get("restaurant_id") or "").strip()
    if payload_partner_id and (not reply_partner_id or payload_partner_id != reply_partner_id):
        logger.warning(
            f"ShopeeFood webhook rejected: partner_restaurant_id mismatch "
            f"(payload={payload_partner_id}, s2s={reply_partner_id}) for order_code={order_code}."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ShopeeFood store mismatch on S2S verification",
        )
    if payload_rest_id and (not reply_rest_id or payload_rest_id != reply_rest_id):
        logger.warning(
            f"ShopeeFood webhook rejected: restaurant_id mismatch "
            f"(payload={payload_rest_id}, s2s={reply_rest_id}) for order_code={order_code}."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ShopeeFood store mismatch on S2S verification",
        )

    candidate_ids = [cid for cid in (reply_partner_id, reply_rest_id) if cid]
    store_verified = False
    if candidate_ids:
        scm_stmt = (
            select(StoreChannelMapping)
            .join(Channel, Channel.id == StoreChannelMapping.channel_id)
            .join(Store, Store.id == StoreChannelMapping.store_id)
            .where(
                Channel.code.in_(["SHOPEEFOOD", "SHOPEE_FOOD", "FOODY"]),
                StoreChannelMapping.partner_store_id.in_(candidate_ids),
                StoreChannelMapping.is_active == True,
                Store.is_active == True,
            )
        )
        scm_res = await db.execute(scm_stmt)
        if scm_res.scalars().first() is not None:
            store_verified = True
        elif reply_partner_id:
            store_stmt = select(Store).where(
                Store.code == reply_partner_id,
                Store.is_active == True,
            )
            store_res = await db.execute(store_stmt)
            store_verified = store_res.scalars().first() is not None

    if not store_verified:
        logger.warning(
            f"ShopeeFood webhook rejected: verified order_code={order_code} does not map to any active store "
            f"(candidates={candidate_ids})."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ShopeeFood store mapping verification failed",
        )

    logger.info(
        f"ShopeeFood webhook verified via live S2S get_order_details for "
        f"order_code={order_code} store={reply_partner_id or reply_rest_id}"
    )
    return reply


@router.post("/shopeefood/webhooks/order")
@router.post("/shopeefood/webhooks/update_order")
@router.post("/shopeefoodapi/order/")
@router.post("/shopeefoodapi/order")
@router.post("/shopeefoodapi/update_order/")
@router.post("/shopeefoodapi/update_order")
async def receive_shopeefood_order_webhook(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_foody_app_id: Optional[str] = Header(None, alias="X-Foody-App-Id"),
    db: AsyncSession = Depends(get_db)
):
    """
    Inbound Webhook endpoint receiving real-time orders from ShopeeFood / Foody.
    Verifies HMAC-SHA256 signature when supplied, or performs fail-closed S2S
    `get_order_details` proof-of-order verification when signature header is omitted.
    """
    raw_body = await request.body()
    headers = dict(request.headers)

    public_base = _get_public_base_url()
    public_url = _build_canonical_webhook_url(request)

    sig_header = (
        authorization
        or request.headers.get("x-foody-signature")
        or request.headers.get("x-signature")
    )
    verified_order_details: Optional[Dict[str, Any]] = None

    if sig_header:
        sig = (
            sig_header.replace("Signature ", "").strip()
            if sig_header.startswith("Signature ")
            else sig_header.strip()
        )
        full_url = str(request.url)
        effective_full_url = None if public_base else full_url
        alt_public_url = public_url.rstrip("/") if not request.url.query and public_url.endswith("/") else public_url
        try:
            valid = shopeefood_client.verify_hmac_sha256(
                raw_body,
                sig,
                method=request.method,
                full_url=effective_full_url,
                public_url=public_url,
            )
            if not valid and alt_public_url != public_url:
                valid = shopeefood_client.verify_hmac_sha256(
                    raw_body,
                    sig,
                    method=request.method,
                    full_url=effective_full_url.rstrip("/") if effective_full_url else None,
                    public_url=alt_public_url,
                )
        except TypeError:
            valid = shopeefood_client.verify_hmac_sha256(
                raw_body,
                sig,
                method=request.method,
                full_url=effective_full_url,
                public_url=public_url,
            )

        current_app_key = getattr(shopeefood_client, "app_key", "") or getattr(
            get_settings(), "SHOPEEFOOD_APP_KEY", ""
        )
        if not valid and current_app_key:
            body_str = raw_body.decode("utf-8", errors="ignore") if raw_body else ""
            for check_url in (public_url, effective_full_url):
                if not check_url:
                    continue
                base_string = f"{request.method.upper()}|{check_url}|{body_str}"
                if verify_hmac_sha256(current_app_key, base_string, sig, is_hex_key=True):
                    valid = True
                    break

        if not valid:
            logger.warning("ShopeeFood webhook signature verification failed.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="ShopeeFood webhook signature verification failed",
            )
    else:
        client_ip = request.headers.get("x-real-ip") or (
            request.client.host if request.client else "unknown"
        )
        logger.warning(
            f"ShopeeFood webhook missing Authorization header: path={request.url.path} "
            f"client_ip={client_ip} header_keys={sorted(request.headers.keys())}"
        )
        verified_order_details = await _verify_shopeefood_order_via_s2s(
            raw_body=raw_body,
            request=request,
            db=db,
        )

    try:
        if verified_order_details is not None:
            try:
                response = await adapter.handle_order_webhook(
                    headers,
                    raw_body,
                    db,
                    verified_order_details=verified_order_details,
                )
            except TypeError:
                response = await adapter.handle_order_webhook(headers, raw_body, db)
        else:
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
                request_payload={"headers": redact_sensitive_headers(headers), "body": raw_body.decode("utf-8", errors="ignore")[:2000]} if raw_body else None,
            )
        except Exception as log_ex:
            logger.error(f"Failed to record operational error: {log_ex}")
        return {"result": "failed", "error": "Lỗi xử lý webhook đơn hàng ShopeeFood."}


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
    Secured by HTTP Basic Authentication.
    Stores:
    - 21 Thảo Điền: 10001
    - 46 Hưng Phúc: 10004
    - 17 Mai Chí Thọ: 10005
    - 303 Nguyễn Văn Trỗi: 10006
    """
    # 1. Validate Basic Auth credentials from environment settings (fail-closed)
    expected_user = (getattr(settings, "SHOPEEFOOD_MENU_USERNAME", "") or "").strip()
    expected_pass = (getattr(settings, "SHOPEEFOOD_MENU_PASSWORD", "") or "").strip()
    if (
        not expected_user
        or not expected_pass
        or expected_user.startswith("your_")
        or expected_pass.startswith("your_")
        or not credentials
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"}
        )

    correct_username = secrets.compare_digest(credentials.username, expected_user)
    correct_password = secrets.compare_digest(credentials.password, expected_pass)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"}
        )

    safe_store_code = re.sub(r"[^A-Za-z0-9_-]", "", store_code or "")
    if not safe_store_code or safe_store_code != store_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mã cửa hàng không hợp lệ.")

    # 2. Check if cached menu exists
    cached = adapter.get_cached_menu(safe_store_code)
    if cached:
        return Response(
            content=json.dumps(cached, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{safe_store_code}.json"'}
        )

    # 3. Find matching store in database or build catalog
    stmt = select(Store).where(Store.code == safe_store_code)
    res = await db.execute(stmt)
    store = res.scalar_one_or_none()
    store_id = store.id if store else safe_store_code

    catalog = await adapter.build_catalog_menu(
        store_id=store_id,
        partner_store_id=safe_store_code,
        db=db
    )

    return Response(
        content=json.dumps(catalog, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{safe_store_code}.json"'}
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

    check_store_access(current_user, resolved_store_id)

    res = await adapter.sync_menu(
        store_id=resolved_store_id,
        partner_store_id=resolved_partner_store_id,
        db=db,
    )
    return res
