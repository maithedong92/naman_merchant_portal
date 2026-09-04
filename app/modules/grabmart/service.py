import logging
import time
from typing import Any, Dict, Optional
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

    async def get_access_token(self) -> str:
        """Fetch or return cached OAuth2 Bearer token using Client Credentials Grant."""
        now = time.time()
        if self._cached_token and now < (self._token_expires_at - 60):
            return self._cached_token

        token_url = f"{self.base_url}/grabid/v1/oauth2/token"
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "scope": settings.GRABMART_SCOPE
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                # In sandbox/dev without credentials, return a mock token
                if not self.client_id or not self.client_secret:
                    logger.warning("GrabMart client credentials not configured. Using sandbox token.")
                    self._cached_token = "mock_grabmart_token"
                    self._token_expires_at = now + 3600
                    return self._cached_token

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
        params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Send authenticated request with Bearer token to GrabMart API."""
        token = await self.get_access_token()
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=body,
                params=params
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}


grabmart_client = GrabMartClient()
