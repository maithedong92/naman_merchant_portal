import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional
import httpx

from app.core.config import get_settings

logger = logging.getLogger("naman_portal.modules.shopeemart")
settings = get_settings()


class ShopeeMartClient:
    """
    HTTP Client for ShopeeMart / Shopee Open Platform API v2.
    Handles HMAC-SHA256 signing and request lifecycle.
    """

    def __init__(self):
        self.base_url = settings.SHOPEEMART_BASE_URL.rstrip("/")
        self.partner_id = settings.SHOPEEMART_PARTNER_ID
        self.partner_key = settings.SHOPEEMART_PARTNER_KEY
        self.shop_id = settings.SHOPEEMART_SHOP_ID

    def generate_sign(
        self,
        path: str,
        timestamp: int,
        access_token: str = "",
        shop_id: Optional[str] = None
    ) -> str:
        """
        Shopee Open API v2 Signature Calculation:
        base_string = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
        """
        target_shop = shop_id if shop_id else (self.shop_id or "")
        base_string = f"{self.partner_id}{path}{timestamp}{access_token}{target_shop}"
        signature = hmac.new(
            self.partner_key.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return signature

    async def call_api(
        self,
        method: str,
        path: str,
        access_token: str = "",
        shop_id: Optional[str] = None,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Send authenticated request to Shopee Open Platform API v2."""
        timestamp = int(time.time())
        target_shop = shop_id if shop_id else (self.shop_id or "")
        sign = self.generate_sign(path, timestamp, access_token, target_shop)

        query_params = {
            "partner_id": self.partner_id,
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

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                # In sandbox without credentials, mock response
                if not self.partner_id or not self.partner_key:
                    logger.warning("ShopeeMart credentials not configured. Running in sandbox mode.")
                    return {"error": "", "message": "sandbox_mode", "response": {}}

                resp = await client.request(
                    method=method,
                    url=full_url,
                    params=query_params,
                    json=body
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as ex:
                logger.error(f"ShopeeMart API Error at {path}: {str(ex)}")
                raise


shopeemart_client = ShopeeMartClient()
