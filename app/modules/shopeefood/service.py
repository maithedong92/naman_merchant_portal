import json
import logging
import time
from typing import Any, Dict, Optional
import httpx

from app.core.config import get_settings
from app.core.security import generate_hmac_sha256

logger = logging.getLogger("naman_portal.modules.shopeefood")
settings = get_settings()


class ShopeeFoodClient:
    """HTTP Client for Foody / ShopeeFood External API Integration."""

    def __init__(self):
        self.base_url = settings.SHOPEEFOOD_BASE_URL.rstrip("/")
        self.app_id = settings.SHOPEEFOOD_APP_ID
        self.app_key = settings.SHOPEEFOOD_APP_KEY

    def build_signature(self, method: str, full_url: str, body_str: str) -> str:
        """
        Generate HMAC-SHA256 signature according to Foody External API specs:
        base_string = f"{method}|{full_url}|{body_str}"
        """
        base_string = f"{method.upper()}|{full_url}|{body_str}"
        return generate_hmac_sha256(self.app_key, base_string, is_hex_key=True)

    def build_headers(self, method: str, full_url: str, body_json_str: str) -> Dict[str, str]:
        """Build required Foody S2S request headers."""
        signature = self.build_signature(method, full_url, body_json_str)
        return {
            "Authorization": f"Signature {signature}",
            "X-Foody-App-Id": self.app_id,
            "X-Foody-Api-Version": "1",
            "X-Foody-Request-Id": str(int(time.time() * 1000)),
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
        """Send authenticated signed POST request to Foody API."""
        full_url = f"{self.base_url}{endpoint}"
        body_str = json.dumps(body) if body is not None else "{}"
        headers = self.build_headers(method, full_url, body_str)

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.request(
                    method=method,
                    url=full_url,
                    headers=headers,
                    content=body_str.encode("utf-8")
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as err:
                logger.error(f"ShopeeFood API HTTP error {err.response.status_code}: {err.response.text}")
                raise
            except Exception as ex:
                logger.error(f"ShopeeFood API connection error: {str(ex)}")
                raise


shopeefood_client = ShopeeFoodClient()
