import logging
import time
from typing import Any, Dict, List, Optional
import httpx

from app.core.config import get_settings

logger = logging.getLogger("naman_portal.modules.grabmart")
settings = get_settings()


class GrabMartClient:
    """HTTP Client for GrabMart Partner POS API v1.1.3."""

    def __init__(self):
        self.base_url = settings.GRABMART_BASE_URL.rstrip("/")
        self.client_id = settings.GRABMART_CLIENT_ID
        self.client_secret = settings.GRABMART_CLIENT_SECRET
        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0

    @property
    def is_configured(self) -> bool:
        """Check if GrabMart API credentials are valid non-placeholder values."""
        return bool(
            self.client_id
            and self.client_secret
            and "your_grab" not in self.client_id
            and "your_grab" not in self.client_secret
        )

    async def get_access_token(self) -> str:
        """Fetch or return cached OAuth2 Bearer token using Client Credentials Grant."""
        now = time.time()
        if self._cached_token and now < (self._token_expires_at - 60):
            return self._cached_token

        if not self.is_configured:
            logger.info("GrabMart credentials not configured or sandbox mode. Using simulated token.")
            self._cached_token = "mock_grabmart_pos_token"
            self._token_expires_at = now + 3600
            return self._cached_token

        token_url = f"{self.base_url}/grabid/v1/oauth2/token"
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "scope": settings.GRABMART_SCOPE,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(token_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                self._cached_token = data["access_token"]
                self._token_expires_at = now + data.get("expires_in", 3600)
                return self._cached_token
            except Exception as ex:
                logger.error(f"Failed to fetch GrabMart OAuth token: {str(ex)}")
                raise

    async def call_api(
        self,
        method: str,
        endpoint: str,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send authenticated request with Bearer token to GrabMart API."""
        if not self.is_configured:
            logger.info(
                f"[SIMULATION] GrabMart API call: {method} {endpoint} | Params: {params} | Body: {body}"
            )
            return {"simulated": True, "status": "OK", "endpoint": endpoint}

        token = await self.get_access_token()
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=body,
                params=params,
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    # ==========================================================================
    # 1. Menu & Catalog Management
    # ==========================================================================

    async def notify_menu_update(self, merchant_id: str) -> Dict[str, Any]:
        """
        Notify GrabMart that the merchant menu has been updated.
        Endpoint: POST /partner/v1/merchant/menu/notification
        """
        endpoint = "/partner/v1/merchant/menu/notification"
        payload = {"merchantID": merchant_id}
        return await self.call_api("POST", endpoint, body=payload)

    async def update_item_record(
        self,
        merchant_id: str,
        item_id: str,
        price: Optional[int] = None,
        available_status: str = "AVAILABLE",
        max_stock: int = 10,
    ) -> Dict[str, Any]:
        """
        Update a single item price, availability, or stock on GrabMart.
        Endpoint: PUT /partner/v1/menu
        """
        endpoint = "/partner/v1/menu"
        payload: Dict[str, Any] = {
            "merchantID": merchant_id,
            "field": "ITEM",
            "id": str(item_id),
            "availableStatus": available_status,
            "maxStock": max_stock if available_status == "AVAILABLE" else 0,
        }
        if price is not None:
            payload["price"] = int(price)
        return await self.call_api("PUT", endpoint, body=payload)

    async def batch_update_items(
        self,
        merchant_id: str,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Batch update item availability / stock.
        Iterates and updates items or calls batch endpoint.
        """
        results = []
        for itm in items:
            res = await self.update_item_record(
                merchant_id=merchant_id,
                item_id=str(itm.get("id") or itm.get("sku")),
                price=itm.get("price"),
                available_status=itm.get("availableStatus", "AVAILABLE"),
                max_stock=int(itm.get("maxStock", 10)),
            )
            results.append(res)
        return {"total_updated": len(results), "items": results}

    # ==========================================================================
    # 2. Order Lifecycle Actions (POS -> GrabMart)
    # ==========================================================================

    async def accept_or_reject_order(
        self,
        order_id: str,
        to_state: str = "Accepted",
    ) -> Dict[str, Any]:
        """
        Manually accept or reject an inbound order within 5 minutes.
        Endpoint: POST /partner/v1/order/prepare
        toState: "Accepted" | "Rejected"
        """
        endpoint = "/partner/v1/order/prepare"
        payload = {"orderID": str(order_id), "toState": to_state}
        return await self.call_api("POST", endpoint, body=payload)

    async def mark_order_ready(self, order_id: str) -> Dict[str, Any]:
        """
        Notify GrabMart driver that the order items are packaged and ready for pickup.
        Endpoint: POST /partner/v1/order/ready
        """
        endpoint = "/partner/v1/order/ready"
        payload = {"orderID": str(order_id), "markStatus": 1}
        return await self.call_api("POST", endpoint, body=payload)

    async def check_cancelable(
        self,
        order_id: str,
        merchant_id: str,
    ) -> Dict[str, Any]:
        """
        Check if an order can still be cancelled on GrabMart.
        Endpoint: GET /partner/v1/order/cancelable
        """
        endpoint = "/partner/v1/order/cancelable"
        params = {"orderID": str(order_id), "merchantID": str(merchant_id)}
        return await self.call_api("GET", endpoint, params=params)

    async def cancel_order(
        self,
        order_id: str,
        merchant_id: str,
        cancel_code: int = 1001,
    ) -> Dict[str, Any]:
        """
        Cancel order on GrabMart with designated cancellation reason code.
        Endpoint: POST /partner/v1/order/cancel
        cancelCode: 1001 (Out of stock), 1002 (Too busy), 1003 (Shop closed)
        """
        endpoint = "/partner/v1/order/cancel"
        payload = {
            "orderID": str(order_id),
            "merchantID": str(merchant_id),
            "cancelCode": cancel_code,
        }
        return await self.call_api("POST", endpoint, body=payload)


grabmart_client = GrabMartClient()

