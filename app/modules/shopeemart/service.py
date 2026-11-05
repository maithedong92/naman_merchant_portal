from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional
import httpx

from app.core.config import get_settings
from app.modules.shopeemart.schemas import (
    ShopeeMartAuthUrlResponse,
    ShopeeMartConnectionStatus,
    ShopeeMartTokenResponse,
)

logger = logging.getLogger("naman_portal.modules.shopeemart.service")
settings = get_settings()

TOKEN_STORE_PATH = Path("data/shopeemart_tokens.json")
CREDENTIALS_STORE_PATH = Path("data/shopeemart_credentials.json")


class ShopeeMartClient:
    """
    Production-grade HTTP Client for ShopeeMart / Shopee Open Platform API v2.
    Implements:
    - HMAC-SHA256 signing for Public APIs, Shop APIs, and Push Webhook Verification.
    - Automatic OAuth 2.0 lifecycle: Code exchange, Persistent token storage across workers, Proactive refresh.
    - Full Mart & Outlet shop operations: Stock batch update, Ship order, Cancel order.
    - Graceful fallback to sandbox/simulation mode when credentials are not configured.
    """

    def __init__(self):
        self.base_url = settings.SHOPEEMART_BASE_URL.rstrip("/")
        self.partner_id = settings.SHOPEEMART_PARTNER_ID
        self.partner_key = settings.SHOPEEMART_PARTNER_KEY
        self.default_shop_id = settings.SHOPEEMART_SHOP_ID
        # Token cache format: {shop_id: {"access_token": str, "refresh_token": str, "expires_at": float}}
        self._token_cache: Dict[str, Dict[str, Any]] = {}
        self._load_credentials()
        self._load_token_cache()

    def _load_credentials(self):
        """Load dynamically updated Live credentials across workers if saved on disk."""
        try:
            if CREDENTIALS_STORE_PATH.exists():
                raw = CREDENTIALS_STORE_PATH.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    if data.get("partner_id"):
                        self.partner_id = str(data["partner_id"]).strip()
                    if data.get("partner_key"):
                        self.partner_key = str(data["partner_key"]).strip()
                    if data.get("shop_id"):
                        self.default_shop_id = str(data["shop_id"]).strip()
                    if data.get("base_url"):
                        self.base_url = str(data["base_url"]).rstrip("/")
        except Exception as ex:
            logger.debug(f"Could not load ShopeeMart credentials store: {ex}")

    def save_credentials(
        self,
        partner_id: str,
        partner_key: str,
        shop_id: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        """Persist Live Partner ID, Partner Key, and Shop ID across all worker processes."""
        self.partner_id = str(partner_id).strip()
        self.partner_key = str(partner_key).strip()
        if shop_id is not None and str(shop_id).strip():
            self.default_shop_id = str(shop_id).strip()
        if base_url:
            self.base_url = str(base_url).rstrip("/")

        payload = {
            "partner_id": self.partner_id,
            "partner_key": self.partner_key,
            "shop_id": self.default_shop_id,
            "base_url": self.base_url,
            "updated_at_iso": datetime.now(timezone.utc).isoformat(),
        }
        try:
            CREDENTIALS_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CREDENTIALS_STORE_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as ex:
            logger.warning(f"Failed to persist ShopeeMart credentials to disk: {ex}")

    @property
    def is_configured(self) -> bool:
        """True if real Shopee Open Platform credentials are configured."""
        self._load_credentials()
        if not self.partner_id or not self.partner_key:
            return False
        pid_str = str(self.partner_id).strip()
        pkey_str = str(self.partner_key).strip()
        if pid_str.startswith("your_") or not pid_str.isdigit() or pkey_str.startswith("your_"):
            return False
        return True

    # ==========================================================================
    # 1. Signature Algorithms (Shopee Open API v2 Standard)
    # ==========================================================================

    def generate_public_sign(self, path: str, timestamp: int) -> str:
        """
        Shopee Public API Signature (used for token auth endpoints):
        base_string = f"{partner_id}{path}{timestamp}"
        """
        if not self.partner_key:
            return "mock_public_signature"
        base_string = f"{self.partner_id}{path}{timestamp}"
        return hmac.new(
            self.partner_key.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def generate_shop_sign(
        self,
        path: str,
        timestamp: int,
        access_token: str = "",
        shop_id: Optional[str] = None
    ) -> str:
        """
        Shopee Shop API Signature (used for product, inventory, and order endpoints):
        base_string = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
        """
        if not self.partner_key:
            return "mock_shop_signature"
        target_shop = shop_id if shop_id is not None else (self.default_shop_id or "")
        base_string = f"{self.partner_id}{path}{timestamp}{access_token}{target_shop}"
        return hmac.new(
            self.partner_key.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def verify_webhook_signature(
        self,
        webhook_url: str,
        raw_body: bytes,
        signature: Optional[str]
    ) -> bool:
        """
        Verify incoming push notification signature from Shopee Open Platform.
        Shopee calculates signature: HMAC-SHA256(partner_key, f"{webhook_url}|{raw_body_utf8}")
        """
        if not self.is_configured:
            # In sandbox/unconfigured mode, accept incoming webhooks
            return True

        if not signature:
            logger.warning("ShopeeMart webhook received without Authorization signature header.")
            return False

        try:
            body_str = raw_body.decode("utf-8")
            base_string = f"{webhook_url}|{body_str}"
            expected_sign = hmac.new(
                self.partner_key.encode("utf-8"),
                base_string.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            is_valid = hmac.compare_digest(expected_sign.lower(), signature.lower())
            if not is_valid:
                logger.error("ShopeeMart webhook signature verification failed.")
            return is_valid
        except Exception as ex:
            logger.error(f"Error during ShopeeMart webhook signature verification: {ex}")
            return False

    # ==========================================================================
    # 2. OAuth 2.0 Authorization & Token Lifecycle
    # ==========================================================================

    def get_authorization_url(self, redirect_url: str) -> ShopeeMartAuthUrlResponse:
        """
        Generate the Shopee Shop Authorization URL for the merchant admin to grant access.
        Endpoint: /api/v2/shop/auth_partner
        """
        self._load_credentials()
        timestamp = int(time.time())
        path = "/api/v2/shop/auth_partner"
        sign = self.generate_public_sign(path, timestamp)

        auth_url = (
            f"{self.base_url}{path}?"
            f"partner_id={self.partner_id}&"
            f"timestamp={timestamp}&"
            f"sign={sign}&"
            f"redirect={redirect_url}"
        )
        return ShopeeMartAuthUrlResponse(
            auth_url=auth_url,
            partner_id=str(self.partner_id or "SANDBOX"),
            redirect_url=redirect_url
        )

    async def exchange_code_for_token(
        self,
        code: str,
        shop_id: str
    ) -> ShopeeMartTokenResponse:
        """
        Exchange one-time authorization code for permanent Shopee access & refresh tokens.
        Endpoint: POST /api/v2/auth/token/get
        """
        if not self.is_configured:
            # Sandbox simulation
            mock_resp = ShopeeMartTokenResponse(
                access_token="mock_shopee_access_token_sandbox",
                refresh_token="mock_shopee_refresh_token_sandbox",
                expire_in=14400,
                shop_id=int(shop_id) if (shop_id and str(shop_id).isdigit()) else None
            )
            self._save_token_cache(str(shop_id), mock_resp.access_token, mock_resp.refresh_token, mock_resp.expire_in)
            return mock_resp

        timestamp = int(time.time())
        path = "/api/v2/auth/token/get"
        sign = self.generate_public_sign(path, timestamp)

        query_params = {
            "partner_id": self.partner_id,
            "timestamp": timestamp,
            "sign": sign,
        }
        body = {
            "code": code,
            "shop_id": int(shop_id),
            "partner_id": int(self.partner_id),
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self.base_url}{path}", params=query_params, json=body)
            data = resp.json()

            if resp.status_code != 200 or data.get("error"):
                err_msg = data.get("message", "Failed to exchange Shopee auth code")
                logger.error(f"Shopee token exchange error: {data}")
                raise RuntimeError(f"ShopeeMart OAuth Error: {err_msg}")

            access_token = data.get("access_token", "")
            refresh_token = data.get("refresh_token", "")
            expire_in = int(data.get("expire_in", 14400))

            self._save_token_cache(str(shop_id), access_token, refresh_token, expire_in)

            return ShopeeMartTokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
                expire_in=expire_in,
                shop_id=int(shop_id)
            )

    async def refresh_access_token(self, shop_id: Optional[str] = None) -> ShopeeMartTokenResponse:
        """
        Proactively refresh an expiring Shopee access token using its refresh token.
        Endpoint: POST /api/v2/auth/access_token/get
        """
        target_shop = str(shop_id or self.default_shop_id or "")
        if not target_shop:
            raise ValueError("Shop ID is required to refresh ShopeeMart token.")
        cached = self._token_cache.get(target_shop)

        if not self.is_configured:
            # Sandbox simulation
            mock_resp = ShopeeMartTokenResponse(
                access_token="mock_refreshed_access_token_sandbox",
                refresh_token="mock_refreshed_refresh_token_sandbox",
                expire_in=14400,
                shop_id=int(target_shop) if target_shop.isdigit() else None
            )
            self._save_token_cache(target_shop, mock_resp.access_token, mock_resp.refresh_token, mock_resp.expire_in)
            return mock_resp

        if not cached or not cached.get("refresh_token"):
            raise ValueError(f"No refresh_token found in cache for ShopeeMart shop {target_shop}")

        timestamp = int(time.time())
        path = "/api/v2/auth/access_token/get"
        sign = self.generate_public_sign(path, timestamp)

        query_params = {
            "partner_id": self.partner_id,
            "timestamp": timestamp,
            "sign": sign,
        }
        body = {
            "refresh_token": cached["refresh_token"],
            "shop_id": int(target_shop),
            "partner_id": int(self.partner_id),
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self.base_url}{path}", params=query_params, json=body)
            data = resp.json()

            if resp.status_code != 200 or data.get("error"):
                logger.error(f"Shopee refresh token error: {data}")
                raise RuntimeError(f"ShopeeMart Token Refresh Error: {data.get('message')}")

            access_token = data.get("access_token", "")
            refresh_token = data.get("refresh_token", cached["refresh_token"])
            expire_in = int(data.get("expire_in", 14400))

            self._save_token_cache(target_shop, access_token, refresh_token, expire_in)

            return ShopeeMartTokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
                expire_in=expire_in,
                shop_id=int(target_shop)
            )

    async def get_valid_access_token(self, shop_id: Optional[str] = None) -> str:
        """Return a valid cached access token, automatically refreshing if close to expiry."""
        self._load_token_cache()
        target_shop = str(shop_id or self.default_shop_id or "")
        cached = self._token_cache.get(target_shop) if target_shop else None
        if not cached and self._token_cache:
            # Fallback to first available authorized shop if shop_id was not explicitly passed
            first_shop_id = next(iter(self._token_cache.keys()))
            target_shop = first_shop_id
            cached = self._token_cache.get(first_shop_id)

        now = time.time()
        # If token exists in cache and has > 5 minutes remaining
        if cached and cached.get("access_token") and cached.get("expires_at", 0) > (now + 300):
            return cached["access_token"]

        if not self.is_configured:
            return cached.get("access_token", "mock_shopeemart_access_token") if cached else "mock_shopeemart_access_token"

        # Otherwise refresh if refresh_token is present
        if cached and cached.get("refresh_token") and target_shop:
            try:
                refreshed = await self.refresh_access_token(target_shop)
                return refreshed.access_token
            except Exception as ex:
                logger.warning(f"Could not refresh Shopee token automatically: {ex}")

        # Fallback to cached token even if close to expiry, or raise if empty
        if cached and cached.get("access_token"):
            return cached["access_token"]

        return "mock_shopeemart_access_token"

    def _load_token_cache(self):
        """Load persistent Shopee OAuth tokens from disk so all workers share state."""
        try:
            if TOKEN_STORE_PATH.exists():
                raw = TOKEN_STORE_PATH.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    self._token_cache.update(data)
        except Exception as ex:
            logger.debug(f"Could not load ShopeeMart token store: {ex}")

    def _save_token_cache(self, shop_id: str, access_token: str, refresh_token: str, expire_in: int):
        """Helper to cache token with TTL in memory and persist to disk across workers."""
        self._load_token_cache()
        self._token_cache[str(shop_id)] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": time.time() + expire_in,
            "expires_at_iso": (datetime.now(timezone.utc) + timedelta(seconds=expire_in)).isoformat(),
            "updated_at_iso": datetime.now(timezone.utc).isoformat(),
        }
        try:
            TOKEN_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_STORE_PATH.write_text(
                json.dumps(self._token_cache, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as ex:
            logger.warning(f"Failed to persist ShopeeMart token store to disk: {ex}")

    # ==========================================================================
    # 3. Core API Execution Engine
    # ==========================================================================

    async def call_api(
        self,
        method: str,
        path: str,
        shop_id: Optional[str] = None,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        require_token: bool = True
    ) -> Dict[str, Any]:
        """Send signed authenticated request to Shopee Open Platform API v2."""
        target_shop = str(shop_id if shop_id is not None else (self.default_shop_id or ""))
        timestamp = int(time.time())

        access_token = ""
        if require_token:
            access_token = await self.get_valid_access_token(target_shop)

        sign = self.generate_shop_sign(path, timestamp, access_token, target_shop)

        query_params = {
            "partner_id": self.partner_id or "SANDBOX",
            "timestamp": timestamp,
            "sign": sign,
        }
        if access_token:
            query_params["access_token"] = access_token
        if target_shop:
            query_params["shop_id"] = target_shop

        if params:
            query_params.update(params)

        full_url = f"{self.base_url}{path}"

        if not self.is_configured:
            logger.info(f"ShopeeMart API simulated [{method}] {path} (Sandbox mode)")
            return {
                "error": "",
                "message": "sandbox_mode_success",
                "response": {
                    "method": method,
                    "path": path,
                    "shop_id": target_shop,
                    "simulated": True
                }
            }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.request(
                    method=method,
                    url=full_url,
                    params=query_params,
                    json=body
                )
                if resp.status_code >= 400:
                    err_text = resp.text
                    logger.error(f"ShopeeMart API HTTP {resp.status_code} at {path}: {err_text}")
                    try:
                        err_json = resp.json()
                        err_msg = err_json.get("message") or err_json.get("error") or err_text
                    except Exception:
                        err_msg = err_text
                    raise RuntimeError(f"ShopeeMart API Error {resp.status_code} [{path}]: {err_msg}")

                data = resp.json()
                if data.get("error"):
                    logger.error(f"ShopeeMart API returned error at {path}: {data.get('error')} - {data.get('message')}")
                    raise RuntimeError(f"ShopeeMart Error [{data.get('error')}]: {data.get('message')}")
                return data
            except Exception as ex:
                logger.error(f"ShopeeMart HTTP Exception at {path}: {str(ex)}")
                raise

    # ==========================================================================
    # 4. Domain-Specific Business APIs (Mart & Outlet)
    # ==========================================================================

    async def get_order_detail(self, order_sn: str, shop_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch full details of an order from Shopee.
        Endpoint: GET /api/v2/order/get_order_detail
        """
        return await self.call_api(
            method="GET",
            path="/api/v2/order/get_order_detail",
            shop_id=shop_id,
            params={
                "order_sn_list": order_sn,
                "response_optional_fields": "recipient_address,item_list,total_amount,buyer_user,cancel_reason"
            }
        )

    async def get_shipping_parameter(self, order_sn: str, shop_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Query required logistics parameters (pickup address/time_slot vs dropoff) before shipping an order.
        Endpoint: GET /api/v2/logistics/get_shipping_parameter
        """
        return await self.call_api(
            method="GET",
            path="/api/v2/logistics/get_shipping_parameter",
            shop_id=shop_id,
            params={"order_sn": order_sn}
        )

    async def ship_order(self, order_sn: str, shop_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Notify Shopee that goods are packed and ready for carrier pickup (Ready to ship).
        Automatically queries v2.logistics.get_shipping_parameter to determine pickup vs dropoff parameters
        for Shopee-integrated 3PL carriers (SPX Express, Instant, GrabExpress).
        Endpoint: POST /api/v2/logistics/ship_order
        """
        ship_body: Dict[str, Any] = {"order_sn": order_sn}

        try:
            param_resp = await self.get_shipping_parameter(order_sn=order_sn, shop_id=shop_id)
            resp_data = param_resp.get("response") or param_resp
            info_needed = resp_data.get("info_needed") or {}
            pickup_info = resp_data.get("pickup") or {}
            address_list = pickup_info.get("address_list") or []

            if "pickup" in info_needed or address_list:
                pickup_payload: Dict[str, Any] = {}
                if address_list:
                    # Pick default pickup address or first available
                    chosen_addr = address_list[0]
                    for addr in address_list:
                        flags = addr.get("address_flag") or []
                        if "pickup_address" in flags or "default_address" in flags:
                            chosen_addr = addr
                            break
                    if chosen_addr.get("address_id") is not None:
                        pickup_payload["address_id"] = chosen_addr["address_id"]
                    time_slots = chosen_addr.get("time_slot_list") or []
                    if time_slots and time_slots[0].get("pickup_time_id") is not None:
                        pickup_payload["pickup_time_id"] = time_slots[0]["pickup_time_id"]
                ship_body["pickup"] = pickup_payload
            elif "dropoff" in info_needed:
                ship_body["dropoff"] = {}
            elif "non_integrated" in info_needed:
                ship_body["non_integrated"] = {}
            else:
                ship_body["dropoff"] = {}
        except Exception as ex:
            logger.warning(f"Could not fetch get_shipping_parameter for {order_sn}, falling back to default ship payload: {ex}")
            ship_body["dropoff"] = {}

        return await self.call_api(
            method="POST",
            path="/api/v2/logistics/ship_order",
            shop_id=shop_id,
            body=ship_body
        )

    async def cancel_order(
        self,
        order_sn: str,
        shop_id: Optional[str] = None,
        cancel_reason: str = "OUT_OF_STOCK",
        item_list: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Cancel order on Shopee due to stock shortage or store operational constraints.
        Endpoint: POST /api/v2/order/cancel_order
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/order/cancel_order",
            shop_id=shop_id,
            body={
                "order_sn": order_sn,
                "cancel_reason": cancel_reason,
                "item_list": item_list or []
            }
        )

    async def get_item_base_info(
        self,
        item_id_list: List[int],
        shop_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch base product details (including item_sku, item_name, price, stock) for up to 50 item_ids.
        Endpoint: GET /api/v2/product/get_item_base_info
        """
        joined_ids = ",".join(str(i) for i in item_id_list)
        return await self.call_api(
            method="GET",
            path="/api/v2/product/get_item_base_info",
            shop_id=shop_id,
            params={"item_id_list": joined_ids}
        )

    async def update_stock(
        self,
        shop_id: str,
        stock_list: List[Dict[str, Any]],
        item_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Update item available stock on Shopee Open Platform v2.
        Supports both single-item Shopee v2 standard (item_id + stock_list with seller_stock)
        and batch stock_list payloads.
        Endpoint: POST /api/v2/product/update_stock
        """
        body: Dict[str, Any] = {"stock_list": stock_list}
        if item_id is not None:
            body["item_id"] = int(item_id)
        return await self.call_api(
            method="POST",
            path="/api/v2/product/update_stock",
            shop_id=shop_id,
            body=body
        )

    async def batch_update_outlet_price(
        self,
        shop_id: str,
        price_list: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Batch update retail outlet prices as per Shopee Mart Working Sheet.
        Endpoint: POST /api/v2/product/batch_update_outlet_price (or update_price fallback)
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/product/batch_update_outlet_price",
            shop_id=shop_id,
            body={"price_list": price_list}
        )

    # ==========================================================================
    # 5. Product Listing, Publishing & Mass Outlet Management
    # ==========================================================================

    async def upload_image_from_url(self, image_url: str) -> Optional[str]:
        """
        Download an image from Poppies/Haravan CDN and upload to Shopee Media Space
        to obtain a valid Shopee image_id required by v2.product.add_item.
        Endpoint: POST /api/v2/media_space/upload_image
        """
        if not image_url:
            return None
        if not self.is_configured:
            digest = hashlib.md5(image_url.encode("utf-8")).hexdigest()[:12]
            return f"vn-sandbox-img-{digest}"

        timestamp = int(time.time())
        path = "/api/v2/media_space/upload_image"
        sign = self.generate_public_sign(path, timestamp)
        query_params = {
            "partner_id": self.partner_id,
            "timestamp": timestamp,
            "sign": sign,
        }

        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            img_resp = await client.get(image_url)
            img_resp.raise_for_status()
            content_type = img_resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            ext = "png" if "png" in content_type else "jpg"
            files = {"image": (f"product.{ext}", img_resp.content, content_type)}

            up_resp = await client.post(
                f"{self.base_url}{path}",
                params=query_params,
                files=files,
            )
            data = up_resp.json()
            if up_resp.status_code != 200 or data.get("error"):
                raise RuntimeError(
                    f"Shopee upload_image error [{data.get('error')}]: {data.get('message')}"
                )
            image_info = (data.get("response") or {}).get("image_info") or {}
            return image_info.get("image_id")

    async def get_logistics_channel_list(self, shop_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fetch enabled 3PL logistics channels configured on the Shopee shop.
        Endpoint: GET /api/v2/logistics/get_channel_list
        """
        if not self.is_configured:
            return [{"logistic_id": 5001, "enabled": True}]

        resp = await self.call_api(
            method="GET",
            path="/api/v2/logistics/get_channel_list",
            shop_id=shop_id,
        )
        channels = (resp.get("response") or {}).get("logistics_channel_list") or []
        enabled_list: List[Dict[str, Any]] = []
        for ch in channels:
            if ch.get("enabled") and not ch.get("mask_channel_id"):
                cid = ch.get("logistics_channel_id")
                if cid is not None:
                    enabled_list.append({"logistic_id": int(cid), "enabled": True})
        return enabled_list

    async def recommend_category(self, item_name: str, shop_id: Optional[str] = None) -> int:
        """
        Ask Shopee v2 to recommend the valid leaf category_id for an item name.
        Endpoint: GET /api/v2/product/category_recommend
        Returns the last element (leaf category) from the category chain.
        """
        if not self.is_configured:
            return 100144

        try:
            resp = await self.call_api(
                method="GET",
                path="/api/v2/product/category_recommend",
                shop_id=shop_id,
                params={"item_name": item_name},
            )
            cat_ids = (resp.get("response") or {}).get("category_id") or []
            if isinstance(cat_ids, list) and cat_ids:
                # Shopee returns path from root to leaf, so the leaf is the last item: [-1]
                return int(cat_ids[-1])
        except Exception as ex:
            logger.warning(f"Could not auto-recommend Shopee category for '{item_name}': {ex}")
        return 100144

    @staticmethod
    def _estimate_weight_kg(
        item_name: str,
        net_weight_kg: Optional[float] = None,
        default_net_kg: float = 0.4
    ) -> float:
        """
        Tính trọng lượng đóng gói sản phẩm (package gross weight in kg):
        Quy tắc nghiệp vụ: Cứ +100 gram (0.1 kg) khối lượng bao bì / đóng gói
        so với khối lượng tịnh (net weight) của sản phẩm.
        """
        # 1. Nếu có khối lượng tịnh truyền vào trực tiếp
        if net_weight_kg is not None and net_weight_kg > 0:
            return max(0.1, round(float(net_weight_kg) + 0.1, 3))

        # 2. Bóc tách khối lượng tịnh từ tên sản phẩm tiếng Việt (ví dụ: '250G', '1.5kg', '500ml')
        m_kg = re.search(r"(\d+(?:[.,]\d+)?)\s*kg\b", item_name, flags=re.IGNORECASE)
        if m_kg:
            val = float(m_kg.group(1).replace(",", "."))
            return max(0.1, round(val + 0.1, 3))

        m_g = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:g|gr|gram)\b", item_name, flags=re.IGNORECASE)
        if m_g:
            val = float(m_g.group(1).replace(",", ".")) / 1000.0
            return max(0.1, round(val + 0.1, 3))

        m_ml = re.search(r"(\d+(?:[.,]\d+)?)\s*ml\b", item_name, flags=re.IGNORECASE)
        if m_ml:
            val = float(m_ml.group(1).replace(",", ".")) / 1000.0
            return max(0.1, round(val + 0.1, 3))

        # 3. Mặc định: net weight 0.4 kg + 0.1 kg đóng gói = 0.5 kg
        return max(0.1, round(default_net_kg + 0.1, 3))

    async def push_erp_product_to_shopee(
        self,
        *,
        sku: str,
        name: str,
        price: float,
        stock: int,
        unit: Optional[str] = None,
        barcode: Optional[str] = None,
        description: Optional[str] = None,
        image_url: Optional[str] = None,
        shop_id: Optional[str] = None,
        category_id: Optional[int] = None,
        net_weight_kg: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        End-to-end helper to push a Poppies ERP product to Shopee Open Platform v2:
        1. Uploads product image to Shopee Media Space (v2.media_space.upload_image)
        2. Resolves leaf category_id (v2.product.category_recommend)
        3. Queries enabled 3PL channels (v2.logistics.get_channel_list)
        4. Calls v2.product.add_item with complete Shopee Live schema
        """
        target_shop = str(shop_id or self.default_shop_id or "")
        if not target_shop and self._token_cache:
            target_shop = next(iter(self._token_cache.keys()))

        # Ensure Shopee title length rules (10..120 chars)
        clean_name = (name or "").strip()
        if len(clean_name) < 10:
            clean_name = f"{clean_name} - Nam An Market"
        clean_name = clean_name[:120]

        # Ensure Shopee description length rules (>= 30 chars)
        clean_desc = (description or "").strip()
        if len(clean_desc) < 40:
            details = [
                f"{clean_name} — Phân phối chính hãng bởi hệ thống siêu thị thực phẩm Nam An Market.",
                f"- Mã sản phẩm (SKU): {sku}",
            ]
            if barcode and str(barcode).strip():
                details.append(f"- Mã vạch (Barcode): {str(barcode).strip()}")
            if unit and str(unit).strip():
                details.append(f"- Quy cách / Đơn vị tính: {str(unit).strip()}")
            details.append("- Cam kết chất lượng an toàn thực phẩm, nguồn gốc xuất xứ rõ ràng, bảo quản tiêu chuẩn tại chuỗi cửa hàng Nam An Market.")
            clean_desc = "\n".join(details)

        image_id = await self.upload_image_from_url(image_url) if image_url else None
        resolved_category_id = category_id or await self.recommend_category(clean_name, shop_id=target_shop)
        logistic_info = await self.get_logistics_channel_list(shop_id=target_shop)
        weight_kg = self._estimate_weight_kg(clean_name, net_weight_kg=net_weight_kg)

        item_payload: Dict[str, Any] = {
            "original_price": float(round(price)),
            "description": clean_desc,
            "weight": weight_kg,
            "item_name": clean_name,
            "item_status": "NORMAL",
            "dimension": {
                "package_length": 20,
                "package_width": 15,
                "package_height": 8,
            },
            "category_id": int(resolved_category_id),
            "item_sku": str(sku),
            "brand": {
                "brand_id": 0,
                "original_brand_name": "NoBrand",
            },
            "seller_stock": [
                {"stock": max(0, int(stock))}
            ],
        }
        if image_id:
            item_payload["image"] = {"image_id_list": [image_id]}
        if logistic_info:
            item_payload["logistic_info"] = logistic_info

        api_result = await self.add_item(shop_id=target_shop, item_data=item_payload)
        return {
            "mode": "LIVE" if self.is_configured else "SANDBOX_SIMULATION",
            "is_configured": self.is_configured,
            "shop_id": target_shop or None,
            "payload_sent": item_payload,
            "shopee_response": api_result,
        }

    async def add_item(self, shop_id: str, item_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a new Mart SKU on the primary Mart shop.
        Endpoint: POST /api/v2/product/add_item
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/product/add_item",
            shop_id=shop_id,
            body=item_data
        )

    async def update_item(self, shop_id: str, item_id: int, update_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update existing Mart item details.
        Endpoint: POST /api/v2/product/update_item
        """
        payload = {"item_id": item_id, **update_data}
        return await self.call_api(
            method="POST",
            path="/api/v2/product/update_item",
            shop_id=shop_id,
            body=payload
        )

    async def publish_item_to_outlet_shop(
        self,
        shop_id: str,
        item_id: int,
        outlet_shop_id_list: List[int],
        price_list: Optional[List[Dict[str, Any]]] = None,
        stock_list: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Publish created Mart item to one or more Outlet Shops.
        Endpoint: POST /api/v2/product/publish_item_to_outlet_shop
        """
        payload: Dict[str, Any] = {
            "mart_item_id": int(item_id),
            "outlet_shop_id_list": outlet_shop_id_list
        }
        if price_list:
            payload["price_list"] = price_list
        if stock_list:
            payload["stock_list"] = stock_list

        return await self.call_api(
            method="POST",
            path="/api/v2/product/publish_item_to_outlet_shop",
            shop_id=shop_id,
            body=payload
        )

    async def batch_update_outlet_stock(
        self,
        shop_id: str,
        outlet_stock_list: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Batch update stock across multiple Outlet shops.
        Endpoint: POST /api/v2/product/batch_update_outlet_stock
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/product/batch_update_outlet_stock",
            shop_id=shop_id,
            body={"outlet_stock_list": outlet_stock_list}
        )

    # ==========================================================================
    # 6. Logistics, Tracking & Airway Bill (AWB) Management
    # ==========================================================================

    async def get_tracking_number(self, order_sn: str, shop_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Retrieve carrier tracking number for the order.
        Endpoint: GET /api/v2/logistics/get_tracking_number
        """
        return await self.call_api(
            method="GET",
            path="/api/v2/logistics/get_tracking_number",
            shop_id=shop_id,
            params={"order_sn": order_sn}
        )

    async def create_shipping_document(
        self,
        order_sn: str,
        document_type: str = "THERMAL_AIR_WAYBILL",
        shop_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate printable shipping document / AWB label.
        Endpoint: POST /api/v2/logistics/create_shipping_document
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/logistics/create_shipping_document",
            shop_id=shop_id,
            body={
                "order_list": [{
                    "order_sn": order_sn,
                    "shipping_document_type": document_type
                }]
            }
        )

    async def update_price(
        self,
        shop_id: str,
        item_id: int,
        original_price: float,
    ) -> Dict[str, Any]:
        """
        Update item original retail price for primary Mart shop.
        Endpoint: POST /api/v2/product/update_price
        """
        payload = {
            "item_id": int(item_id),
            "price_list": [{"original_price": float(round(original_price, 2))}]
        }
        return await self.call_api(
            method="POST",
            path="/api/v2/product/update_price",
            shop_id=shop_id,
            body=payload,
        )

    async def batch_publish_item_to_outlet_shop(
        self,
        shop_id: str,
        publish_list: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Batch publish multiple Mart items to Outlet shops.
        Endpoint: POST /api/v2/product/batch_publish_item_to_outlet_shop
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/product/batch_publish_item_to_outlet_shop",
            shop_id=shop_id,
            body={"publish_list": publish_list},
        )

    async def get_return_dispute_reason(
        self,
        return_sn: str,
        shop_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Query available dispute reasons for a return request.
        Endpoint: GET /api/v2/returns/get_return_dispute_reason
        """
        return await self.call_api(
            method="GET",
            path="/api/v2/returns/get_return_dispute_reason",
            shop_id=shop_id,
            params={"return_sn": return_sn},
        )

    async def get_shipping_document_parameter(
        self,
        order_sn: str,
        shop_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Query available shipping document type and parameters for an order.
        Endpoint: GET /api/v2/logistics/get_shipping_document_parameter
        """
        return await self.call_api(
            method="GET",
            path="/api/v2/logistics/get_shipping_document_parameter",
            shop_id=shop_id,
            params={"order_sn": order_sn},
        )

    async def get_shipping_document_result(
        self,
        order_sn: str,
        shop_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Check the generation status of the shipping document (AWB).
        Endpoint: GET /api/v2/logistics/get_shipping_document_result
        """
        return await self.call_api(
            method="GET",
            path="/api/v2/logistics/get_shipping_document_result",
            shop_id=shop_id,
            params={"order_sn": order_sn},
        )

    async def download_shipping_document_bytes(
        self,
        order_sn: str,
        document_type: str = "THERMAL_AIR_WAYBILL",
        shop_id: Optional[str] = None,
    ) -> bytes:
        """
        Download real printable Airway Bill PDF bytes directly from Shopee.
        Endpoint: POST /api/v2/logistics/download_shipping_document
        """
        target_shop = str(shop_id if shop_id is not None else (self.default_shop_id or ""))
        timestamp = int(time.time())
        path = "/api/v2/logistics/download_shipping_document"
        access_token = await self.get_valid_access_token(target_shop)
        sign = self.generate_shop_sign(path, timestamp, access_token, target_shop)

        query_params = {
            "partner_id": self.partner_id or "SANDBOX",
            "timestamp": timestamp,
            "sign": sign,
            "access_token": access_token,
        }
        if target_shop:
            query_params["shop_id"] = target_shop

        if not self.is_configured:
            # Fallback mock minimal PDF byte array for sandbox testing
            return b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/MediaBox[0 0 300 144]/Parent 2 0 R/Resources<<>>>>endobj\nxref\n0 4\n0000000000 65535 f\n0000000010 00000 n\n0000000053 00000 n\n0000000102 00000 n\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n185\n%%EOF"

        full_url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                full_url,
                params=query_params,
                json={"order_list": [{"order_sn": order_sn, "shipping_document_type": document_type}]},
            )
            resp.raise_for_status()
            return resp.content

    async def download_shipping_document(
        self,
        order_sn: str,
        document_type: str = "THERMAL_AIR_WAYBILL",
        shop_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Wrapper returning shipping document result dict."""
        bytes_data = await self.download_shipping_document_bytes(order_sn, document_type, shop_id)
        return {
            "order_sn": order_sn,
            "document_type": document_type,
            "bytes_length": len(bytes_data) if bytes_data else 0,
            "message": "sandbox_mode_success" if not self.is_configured else "download_success",
        }

    # ==========================================================================
    # 7. Order Enhancements & Buyer Cancellation Handling
    # ==========================================================================

    async def get_order_list(
        self,
        time_from: int,
        time_to: int,
        order_status: Optional[str] = None,
        page_size: int = 50,
        shop_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Query order list by time range and status for reconciliation.
        Endpoint: GET /api/v2/order/get_order_list
        """
        params: Dict[str, Any] = {
            "time_range_field": "create_time",
            "time_from": time_from,
            "time_to": time_to,
            "page_size": page_size
        }
        if order_status:
            params["order_status"] = order_status

        return await self.call_api(
            method="GET",
            path="/api/v2/order/get_order_list",
            shop_id=shop_id,
            params=params
        )

    async def handle_buyer_cancellation(
        self,
        order_sn: str,
        operation: str = "ACCEPT",
        shop_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Accept or reject buyer's cancellation request.
        Endpoint: POST /api/v2/order/handle_buyer_cancellation
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/order/handle_buyer_cancellation",
            shop_id=shop_id,
            body={
                "order_sn": order_sn,
                "operation": operation
            }
        )

    # ==========================================================================
    # 8. Return & Refund (RR) Management
    # ==========================================================================

    async def get_return_list(
        self,
        page_no: int = 0,
        page_size: int = 20,
        shop_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Retrieve list of return and refund applications.
        Endpoint: GET /api/v2/returns/get_return_list
        """
        return await self.call_api(
            method="GET",
            path="/api/v2/returns/get_return_list",
            shop_id=shop_id,
            params={"page_no": page_no, "page_size": page_size}
        )

    async def get_return_detail(self, return_sn: str, shop_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Retrieve details of a specific return request.
        Endpoint: GET /api/v2/returns/get_return_detail
        """
        return await self.call_api(
            method="GET",
            path="/api/v2/returns/get_return_detail",
            shop_id=shop_id,
            params={"return_sn": return_sn}
        )

    async def confirm_return(self, return_sn: str, shop_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Accept buyer's return / full refund application.
        Endpoint: POST /api/v2/returns/confirm
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/returns/confirm",
            shop_id=shop_id,
            body={"return_sn": return_sn}
        )

    async def dispute_return(
        self,
        return_sn: str,
        dispute_reason: str,
        dispute_text_reason: str = "",
        email: str = "",
        shop_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Escalate return dispute to Shopee dispute mediation center.
        Endpoint: POST /api/v2/returns/dispute
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/returns/dispute",
            shop_id=shop_id,
            body={
                "return_sn": return_sn,
                "dispute_reason": dispute_reason,
                "dispute_text_reason": dispute_text_reason,
                "email": email or ""
            }
        )

    # ==========================================================================
    # 9. Financials & Escrow Settlement (Optional)
    # ==========================================================================

    async def get_escrow_detail(self, order_sn: str, shop_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Retrieve order escrow settlement statement.
        Endpoint: GET /api/v2/payment/get_escrow_detail
        """
        return await self.call_api(
            method="GET",
            path="/api/v2/payment/get_escrow_detail",
            shop_id=shop_id,
            params={"order_sn": order_sn}
        )

    async def get_wallet_transaction_list(
        self,
        page_no: int = 1,
        page_size: int = 20,
        shop_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get wallet transaction history.
        Endpoint: GET /api/v2/payment/get_wallet_transaction_list
        """
        return await self.call_api(
            method="GET",
            path="/api/v2/payment/get_wallet_transaction_list",
            shop_id=shop_id,
            params={"page_no": page_no, "page_size": page_size}
        )

    # ==========================================================================
    # 8. Customer Reviews & Ratings Management (v2.product.get_comment)
    # ==========================================================================

    async def get_comments(
        self,
        shop_id: Optional[str] = None,
        item_id: Optional[int] = None,
        comment_id: Optional[int] = None,
        cursor: str = "",
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """
        Query customer reviews, ratings and comments from Shopee Open Platform v2.
        Endpoint: GET /api/v2/product/get_comment
        """
        params: Dict[str, Any] = {
            "page_size": max(1, min(100, page_size)),
            "cursor": cursor or "",
        }
        if item_id is not None:
            params["item_id"] = int(item_id)
        if comment_id is not None:
            params["comment_id"] = int(comment_id)

        return await self.call_api(
            method="GET",
            path="/api/v2/product/get_comment",
            shop_id=shop_id,
            params=params,
        )

    async def reply_comment(
        self,
        comment_id: int,
        comment: str,
        shop_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send seller reply to a buyer's review / comment.
        Endpoint: POST /api/v2/product/reply_comment
        """
        payload = {
            "comment_id": int(comment_id),
            "comment": comment.strip(),
        }
        return await self.call_api(
            method="POST",
            path="/api/v2/product/reply_comment",
            shop_id=shop_id,
            body=payload,
        )

    def get_connection_status(self, shop_id: Optional[str] = None) -> ShopeeMartConnectionStatus:
        """Inspect and report the health of credentials and active access tokens."""
        self._load_token_cache()
        target_shop = str(shop_id or self.default_shop_id or "")
        cached = self._token_cache.get(target_shop) if target_shop else None
        if not cached and self._token_cache:
            target_shop = next(iter(self._token_cache.keys()))
            cached = self._token_cache.get(target_shop)

        has_valid_token = False
        expires_at_iso = None

        if not self.is_configured:
            has_valid_token = True
            expires_at_iso = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        elif cached:
            now = time.time()
            if cached.get("expires_at", 0) > now:
                has_valid_token = True
                expires_at_iso = cached.get("expires_at_iso")

        return ShopeeMartConnectionStatus(
            is_configured=self.is_configured,
            has_valid_token=has_valid_token,
            token_expires_at=expires_at_iso,
            is_sandbox_mode=not self.is_configured,
            partner_id=self.partner_id if self.is_configured else None,
            shop_id=target_shop or None
        )


shopeemart_client = ShopeeMartClient()
