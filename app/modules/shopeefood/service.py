import json
import logging
import random
import time
from typing import Any, Dict, List, Optional
import httpx

from app.core.config import get_settings
from app.core.security import generate_hmac_sha256

logger = logging.getLogger("naman_portal.modules.shopeefood.service")
settings = get_settings()


class ShopeeFoodClient:
    """
    HTTP Client for Foody / ShopeeFood External S2S API Integration v7.0.2.
    Implements HMAC-SHA256 request signing and Foody protocol envelope.
    """

    def __init__(self):
        self.base_url = settings.SHOPEEFOOD_BASE_URL.rstrip("/")
        self.app_id = str(settings.SHOPEEFOOD_APP_ID)
        self.app_key = settings.SHOPEEFOOD_APP_KEY

    def build_signature(self, method: str, full_url: str, body_str: str) -> str:
        """
        Generate HMAC-SHA256 signature according to Foody External API specs:
        base_string = f"{method}|{full_url}|{body_str}"
        Signature = HEX(HMAC-SHA256(Key=key.decode('hex'), Data=base_string))
        """
        base_string = f"{method.upper()}|{full_url}|{body_str}"
        return generate_hmac_sha256(self.app_key, base_string, is_hex_key=True)

    def build_headers(self, method: str, full_url: str, body_json_str: str) -> Dict[str, str]:
        """Build standard Foody S2S request headers with Authorization signature."""
        signature = self.build_signature(method, full_url, body_json_str)
        request_id = str(random.randint(100000, 999999999))
        return {
            "Authorization": f"Signature {signature}",
            "X-Foody-App-Id": self.app_id,
            "X-Foody-Api-Version": "1",
            "X-Foody-Request-Id": request_id,
            "X-Foody-Country": settings.SHOPEEFOOD_COUNTRY,
            "X-Foody-Language": settings.SHOPEEFOOD_LANGUAGE,
            "Content-Type": "application/json",
        }

    async def call_api(
        self,
        method: str,
        endpoint: str,
        body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Send authenticated signed request to Foody API."""
        full_url = f"{self.base_url}{endpoint}"
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = self.build_headers(method, full_url, body_str)

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.request(
                    method=method.upper(),
                    url=full_url,
                    headers=headers,
                    content=body_str.encode("utf-8") if body_str else None
                )
                response.raise_for_status()
                res_data = response.json()
                logger.info(f"ShopeeFood API {method} {endpoint} -> {res_data.get('result', 'ok')}")
                return res_data
            except httpx.HTTPStatusError as err:
                logger.error(f"ShopeeFood API HTTP error {err.response.status_code}: {err.response.text}")
                return {"result": "error", "error": f"HTTP {err.response.status_code}: {err.response.text}"}
            except Exception as ex:
                logger.error(f"ShopeeFood API connection error: {str(ex)}")
                return {"result": "error", "error": str(ex)}

    async def notify_menu_sync(self, partner_restaurant_id: str) -> Dict[str, Any]:
        """
        Notify ShopeeFood to pull/refresh menu for a store.
        Endpoint: POST /s2s/menu/sync
        """
        return await self.call_api(
            method="POST",
            endpoint="/s2s/menu/sync",
            body={"partner_restaurant_id": str(partner_restaurant_id)}
        )

    async def update_dish_status(
        self,
        partner_restaurant_id: str,
        dishes: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Update availability status of dishes (AVAILABLE = 1, OUT_OF_STOCK = 2).
        Endpoint: POST /s2s/dish/set_statuses
        """
        return await self.call_api(
            method="POST",
            endpoint="/s2s/dish/set_statuses",
            body={
                "partner_restaurant_id": str(partner_restaurant_id),
                "dishes": dishes
            }
        )

    async def get_order_details(self, order_code: str) -> Dict[str, Any]:
        """
        Retrieve full order details from Foody.
        Endpoint: POST /s2s/order/get_details
        """
        return await self.call_api(
            method="POST",
            endpoint="/s2s/order/get_details",
            body={"order_code": str(order_code)}
        )

    async def update_order_status(
        self,
        order_code: str,
        status: int,  # 0: CONFIRM, 2: OUT_OF_SERVICE (Cancel), etc.
        partner_restaurant_id: Optional[str] = None,
        reason_ids: Optional[List[int]] = None,
        merchant_note: Optional[str] = None,
        serial: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update merchant order status on ShopeeFood.
        Endpoint: POST /s2s/order/update
        Status codes:
        - 0: CONFIRM
        - 2: OUT_OF_SERVICE (Merchant requesting cancellation)
        """
        body: Dict[str, Any] = {
            "order_code": str(order_code),
            "status": status,
        }
        if serial:
            body["serial"] = serial
        if partner_restaurant_id:
            body["partner_restaurant_id"] = str(partner_restaurant_id)
        if merchant_note:
            body["merchant_note"] = merchant_note
        if reason_ids:
            body["cancel_reasons"] = {"reason_ids": reason_ids}

        return await self.call_api(
            method="POST",
            endpoint="/s2s/order/update",
            body=body
        )


shopeefood_client = ShopeeFoodClient()
