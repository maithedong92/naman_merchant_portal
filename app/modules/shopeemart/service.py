from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import logging
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


class ShopeeMartClient:
    """
    Production-grade HTTP Client for ShopeeMart / Shopee Open Platform API v2.
    Implements:
    - HMAC-SHA256 signing for Public APIs, Shop APIs, and Push Webhook Verification.
    - Automatic OAuth 2.0 lifecycle: Code exchange, In-memory token caching, Proactive refresh.
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

    @property
    def is_configured(self) -> bool:
        """True if real Shopee Open Platform credentials are configured."""
        if not self.partner_id or not self.partner_key:
            return False
        pid_str = str(self.partner_id).strip()
        if pid_str.startswith("your_") or not pid_str.isdigit():
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
                shop_id=int(shop_id) if shop_id.isdigit() else 10001
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
        target_shop = str(shop_id or self.default_shop_id or "10001")
        cached = self._token_cache.get(target_shop)

        if not self.is_configured:
            # Sandbox simulation
            mock_resp = ShopeeMartTokenResponse(
                access_token="mock_refreshed_access_token_sandbox",
                refresh_token="mock_refreshed_refresh_token_sandbox",
                expire_in=14400,
                shop_id=int(target_shop) if target_shop.isdigit() else 10001
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
        target_shop = str(shop_id or self.default_shop_id or "10001")
        cached = self._token_cache.get(target_shop)

        now = time.time()
        # If token exists in cache and has > 5 minutes remaining
        if cached and cached.get("access_token") and cached.get("expires_at", 0) > (now + 300):
            return cached["access_token"]

        if not self.is_configured:
            return cached.get("access_token", "mock_shopeemart_access_token") if cached else "mock_shopeemart_access_token"

        # Otherwise refresh if refresh_token is present
        if cached and cached.get("refresh_token"):
            try:
                refreshed = await self.refresh_access_token(target_shop)
                return refreshed.access_token
            except Exception as ex:
                logger.warning(f"Could not refresh Shopee token automatically: {ex}")

        # Fallback to cached token even if close to expiry, or raise if empty
        if cached and cached.get("access_token"):
            return cached["access_token"]

        return "mock_shopeemart_access_token"

    def _save_token_cache(self, shop_id: str, access_token: str, refresh_token: str, expire_in: int):
        """Helper to cache token with TTL."""
        self._token_cache[str(shop_id)] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": time.time() + expire_in,
            "expires_at_iso": (datetime.now(timezone.utc) + timedelta(seconds=expire_in)).isoformat(),
        }

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
        target_shop = str(shop_id if shop_id is not None else (self.default_shop_id or "10001"))
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
                resp.raise_for_status()
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

    async def ship_order(self, order_sn: str, shop_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Notify Shopee that goods are packed and ready for carrier pickup (Ready to ship).
        Endpoint: POST /api/v2/logistics/ship_order
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/logistics/ship_order",
            shop_id=shop_id,
            body={
                "order_sn": order_sn,
                "dropoff": {}
            }
        )

    async def cancel_order(
        self,
        order_sn: str,
        shop_id: Optional[str] = None,
        cancel_reason: str = "OUT_OF_STOCK"
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
                "item_list": []
            }
        )

    async def update_stock(
        self,
        shop_id: str,
        stock_list: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Batch update item available stock for a specific Outlet Shop.
        Endpoint: POST /api/v2/product/update_stock
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/product/update_stock",
            shop_id=shop_id,
            body={"stock_list": stock_list}
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
            "item_id": item_id,
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

    async def download_shipping_document(
        self,
        order_sn: str,
        shop_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Download printable AWB PDF for attaching to packages.
        Endpoint: POST /api/v2/logistics/download_shipping_document
        """
        return await self.call_api(
            method="POST",
            path="/api/v2/logistics/download_shipping_document",
            shop_id=shop_id,
            body={"order_list": [{"order_sn": order_sn}]}
        )

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
                "email": email or "support@namanmarket.com"
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

    def get_connection_status(self, shop_id: Optional[str] = None) -> ShopeeMartConnectionStatus:
        """Inspect and report the health of credentials and active access tokens."""
        target_shop = str(shop_id or self.default_shop_id or "10001")
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
            partner_id=self.partner_id if self.is_configured else "SANDBOX",
            shop_id=target_shop
        )


shopeemart_client = ShopeeMartClient()
