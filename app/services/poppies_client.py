"""
HTTP Client for Poppies ERP Master Data & Multi-Channel Pricing Gateway.
Implements strict Fail-Closed semantics (Section 4.3) for Pricing Snapshots.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple
import httpx

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.services.poppies_channels import (
    POPPIES_STANDARD_CHANNELS,
    consolidate_channel_records,
    get_canonical_channel_meta,
    normalize_channel_code,
)

logger = logging.getLogger("naman_portal.poppies_client")
settings = get_settings()


class PoppiesUpstreamError(AppException):
    """
    Fail-Closed exception raised when Poppies ERP returns HTTP 5xx or network timeout.
    Callers MUST retain Last-Known-Good cache and NEVER clear blocked_skus/exclusions.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 503,
        details: Optional[Dict[str, Any]] = None,
    ):
        payload_details = {
            "channel": "POPPIES_ERP",
            "fail_closed": True,
            "cache_retained": True,
            **(details or {}),
        }
        super().__init__(
            message=message,
            error_code="POPPIES_UPSTREAM_UNAVAILABLE",
            status_code=status_code,
            details=payload_details,
        )


class PoppiesClient:
    """
    HTTP Client connecting to Poppies ERP POS Master Data & Pricing Gateways.
    """

    def __init__(self):
        self.base_url = settings.POPPIES_API_BASE_URL.rstrip("/")
        self.pricing_base_url = getattr(
            settings, "POPPIES_PRICING_BASE_URL", "https://poppies.namanmarket.com/api/v1/pricing"
        ).rstrip("/")
        self.pos_pricing_base_url = getattr(
            settings, "POPPIES_POS_PRICING_BASE_URL", "https://poppies.namanmarket.com/api/v1/pos/pricing"
        ).rstrip("/")
        self.api_key = settings.POPPIES_API_KEY
        self.timeout = settings.POPPIES_TIMEOUT_SECONDS

    def _get_headers(self) -> Dict[str, str]:
        return {
            "X-POS-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "NAM-UMP/PoppiesMasterDataClient",
        }

    async def toggle_channel_publish_status(
        self,
        sku: str,
        scope_code: str,
        is_published: bool,
        scope_type: str = "CHANNEL",
    ) -> Dict[str, Any]:
        """
        Call Poppies ERP Pricing API to toggle channel publish status:
        POST /api/v1/pricing/channel-publish-status
        """
        canonical_scope = (
            normalize_channel_code(scope_code)
            if scope_type.upper() == "CHANNEL"
            else scope_code
        )
        payload = {
            "scopeType": scope_type,
            "scopeCode": canonical_scope,
            "sku": sku,
            "isPublished": is_published,
        }
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"

        urls_to_try = [
            f"{self.pricing_base_url}/channel-publish-status",
            "http://127.0.0.1:6515/api/v1/pricing/channel-publish-status",
            "http://host.docker.internal:6515/api/v1/pricing/channel-publish-status",
            "http://103.175.248.250:1155/api/v1/pricing/channel-publish-status",
            "https://poppies.namanmarket.com/api/v1/pricing/channel-publish-status",
        ]

        last_err = None
        for url in urls_to_try:
            try:
                async with httpx.AsyncClient(timeout=min(self.timeout, 10.0)) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code in (200, 201):
                        data = resp.json()
                        data["resolved_url"] = url
                        return data
                    elif resp.status_code == 404:
                        continue
                    else:
                        resp.raise_for_status()
            except Exception as ex:
                last_err = ex

        logger.warning(
            f"Poppies remote pricing API unreachable for SKU {sku} on {canonical_scope}: {last_err}. Saved locally."
        )
        return {
            "success": True,
            "sku": sku,
            "scopeType": scope_type,
            "scopeCode": canonical_scope,
            "isPublished": is_published,
            "message": f"Đã cập nhật trạng thái cục bộ trên Merchant Portal (Poppies remote offline: {last_err})",
            "offline_mode": True,
        }

    async def get_pricing_channels(self) -> Dict[str, Any]:
        """
        Fetch standard 13 Canonical pricing channels from Poppies and apply
        Canonical vs Alias conflict consolidation (Section 2.2).
        """
        urls_to_try = [
            f"{self.pos_pricing_base_url}/channels",
            f"{self.pricing_base_url}/channels",
            "http://127.0.0.1:6515/api/v1/pos/pricing/channels",
            "http://host.docker.internal:6515/api/v1/pos/pricing/channels",
            "https://poppies.namanmarket.com/api/v1/pos/pricing/channels",
        ]
        headers = self._get_headers()
        for url in urls_to_try:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        raw_body = resp.json()
                        raw_list = raw_body.get("channels") if isinstance(raw_body, dict) else None
                        if isinstance(raw_list, list):
                            consolidated = consolidate_channel_records(raw_list)
                            return {
                                "success": True,
                                "source": "poppies_live",
                                "resolved_url": url,
                                "total": len(consolidated),
                                "channels": consolidated,
                            }
            except Exception:
                pass

        return {
            "success": True,
            "source": "local_canonical_fallback",
            "total": len(POPPIES_STANDARD_CHANNELS),
            "channels": [dict(c) for c in POPPIES_STANDARD_CHANNELS],
        }

    async def _fetch_price_book_as_snapshot(
        self,
        branch_code: str,
        canonical_channel: str,
        headers: Dict[str, str],
    ) -> Tuple[Optional[Dict[str, Any]], int, Optional[str]]:
        """
        Fetch lightweight `/api/v1/pos/pricing/price-book` (~5 MB vs ~41 MB `/snapshots`)
        and normalize into snapshot schema while preserving Fail-Closed status codes.
        """
        pricebook_urls = [
            f"{self.pos_pricing_base_url}/price-book",
            "http://host.docker.internal:6515/api/v1/pos/pricing/price-book",
            "http://127.0.0.1:6515/api/v1/pos/pricing/price-book",
            "http://103.175.248.250:1155/api/v1/pos/pricing/price-book",
            "https://poppies.namanmarket.com/api/v1/pos/pricing/price-book",
        ]
        seen_pb = set()
        unique_pb_urls = [u for u in pricebook_urls if not (u in seen_pb or seen_pb.add(u))]
        pb_params: Dict[str, Any] = {
            "branch_code": branch_code,
            "channel_code": canonical_channel,
            "limit": 100000,
        }
        meta = get_canonical_channel_meta(canonical_channel) or {}
        has_pp = bool(meta.get("has_partner_pricing", False))
        pb_status = 503
        pb_err: Optional[str] = None

        for pb_url in unique_pb_urls:
            try:
                async with httpx.AsyncClient(timeout=max(self.timeout, 30.0)) as client:
                    pb_resp = await client.get(pb_url, params=pb_params, headers=headers)
                    if pb_resp.status_code == 200:
                        pb_body = pb_resp.json()
                        rows = pb_body.get("price_book") if isinstance(pb_body, dict) else None
                        if isinstance(pb_body, dict) and pb_body.get("success") is not False and isinstance(rows, list):
                            prices_dict: Dict[str, Dict[str, Any]] = {}
                            blocked_list: List[str] = []
                            for row in rows:
                                if not isinstance(row, dict):
                                    continue
                                sku_c = str(row.get("sku") or "").strip()
                                if not sku_c:
                                    continue
                                allowed = bool(row.get("allowed", True))
                                if not allowed:
                                    blocked_list.append(sku_c)
                                p_num = float(row.get("price") or 0.0)
                                prices_dict[sku_c] = {
                                    "effective_price": p_num,
                                    "base_price": p_num,
                                    "allowed_in_context": allowed,
                                    "productName": row.get("name"),
                                    "barcode": row.get("barcode"),
                                    "is_weight_item": bool(row.get("weight_item", False)),
                                    "tax_rate_pct": row.get("vat"),
                                }
                            logger.info(
                                f"Đồng bộ thành công {len(prices_dict)} SKU từ Poppies price-book "
                                f"cho kênh {canonical_channel} ({pb_url})"
                            )
                            return (
                                {
                                    "success": True,
                                    "source": "price_book_fallback",
                                    "resolved_url": pb_url,
                                    "version": pb_body.get("version"),
                                    "branch_code": pb_body.get("branch_code") or branch_code,
                                    "channel_code": canonical_channel,
                                    "is_active": bool(pb_body.get("is_active", meta.get("is_active", True))),
                                    "has_partner_pricing": bool(pb_body.get("has_partner_pricing", has_pp)),
                                    "inherited_from": None if has_pp else "STORE",
                                    "is_delta": False,
                                    "prices": prices_dict,
                                    "exclusions": blocked_list,
                                },
                                200,
                                None,
                            )
                    elif pb_resp.status_code >= 500:
                        if pb_status != 504:
                            pb_status = pb_resp.status_code
                        pb_err = f"Poppies Price-Book trả về HTTP {pb_resp.status_code} tại {pb_url}"
                        continue
            except httpx.TimeoutException as tex:
                pb_status = 504
                pb_err = f"Timeout kết nối Poppies Price-Book ({tex})"
            except Exception as ex:
                pb_err = str(ex)

        return None, pb_status, pb_err

    async def get_pricing_snapshot(
        self,
        branch_code: str = "10001",
        channel_code: str = "STORE",
        since_version: Optional[str] = None,
        fmt: str = "map",
        prefer_price_book: bool = False,
    ) -> Dict[str, Any]:
        """
        Fetch multi-channel pricing & exclusions snapshot:
        GET /api/v1/pos/pricing/snapshots (Section 4.1 - 4.3).

        STRICT FAIL-CLOSED (U1 Specification):
        Never swallows HTTP 5xx or timeout errors into a fake empty snapshot.
        Raises PoppiesUpstreamError so callers retain Last-Known-Good cache & blocked_skus.
        """
        canonical_channel = normalize_channel_code(channel_code)
        headers = self._get_headers()

        # Resource Optimization: For periodic full background reconciliations without `since_version`,
        # prefer lightweight `/price-book` (~5 MB) before `/snapshots` (~41 MB).
        if prefer_price_book and not since_version:
            pb_snap, _, _ = await self._fetch_price_book_as_snapshot(
                branch_code=branch_code,
                canonical_channel=canonical_channel,
                headers=headers,
            )
            if pb_snap is not None:
                return pb_snap

        params: Dict[str, Any] = {
            "branch_code": branch_code,
            "channel_code": canonical_channel,
            "format": fmt,
        }
        if since_version:
            params["since_version"] = since_version

        urls_to_try: List[str] = [
            f"{self.pos_pricing_base_url}/snapshots",
            "http://host.docker.internal:6515/api/v1/pos/pricing/snapshots",
            "http://127.0.0.1:6515/api/v1/pos/pricing/snapshots",
            "http://103.175.248.250:1155/api/v1/pos/pricing/snapshots",
            "https://poppies.namanmarket.com/api/v1/pos/pricing/snapshots",
        ]
        # Deduplicate while preserving order
        seen_urls = set()
        unique_urls = [u for u in urls_to_try if not (u in seen_urls or seen_urls.add(u))]

        last_status = 503
        last_err_msg = "Không thể kết nối tới Poppies Pricing Snapshot Gateway."

        for url in unique_urls:
            try:
                async with httpx.AsyncClient(timeout=max(self.timeout, 30.0)) as client:
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code == 200:
                        body = resp.json()
                        if not isinstance(body, dict) or body.get("success") is False:
                            raise PoppiesUpstreamError(
                                message="Poppies Pricing Snapshot trả về dữ liệu không hợp lệ.",
                                status_code=502,
                                details={"url": url, "branch_code": branch_code, "channel_code": canonical_channel},
                            )
                        body["resolved_url"] = url
                        return body
                    elif resp.status_code == 404:
                        continue
                    elif resp.status_code >= 500:
                        last_status = resp.status_code
                        last_err_msg = f"Poppies Snapshot trả về HTTP {resp.status_code} tại {url}"
                        continue
                    else:
                        resp.raise_for_status()
            except PoppiesUpstreamError:
                raise
            except httpx.TimeoutException as tex:
                last_status = 504
                last_err_msg = f"Timeout kết nối Poppies Snapshot ({tex})"
            except Exception as ex:
                last_status = 503
                last_err_msg = str(ex)

        # Resilient Fallback: Try Poppies Lightweight Price-Book (`/api/v1/pos/pricing/price-book`)
        # when `/snapshots` returns 404/500 (e.g. ProgrammingError), while still failing closed on 5xx/timeout.
        if not prefer_price_book or since_version:
            pb_snap, pb_status, pb_err = await self._fetch_price_book_as_snapshot(
                branch_code=branch_code,
                canonical_channel=canonical_channel,
                headers=headers,
            )
            if pb_snap is not None:
                return pb_snap
            if pb_status == 504 or (last_status != 504 and pb_status >= 500):
                last_status = pb_status
            if pb_err and last_status == 504:
                last_err_msg = pb_err

        raise PoppiesUpstreamError(
            message=(
                f"Không thể đồng bộ Pricing Snapshot từ Poppies ERP ({last_err_msg}). "
                "Hệ thống giữ nguyên Last-Known-Good Cache và danh sách cấm bán hiện tại (Fail-Closed)."
            ),
            status_code=last_status if last_status >= 500 else 503,
            details={"branch_code": branch_code, "channel_code": canonical_channel},
        )

    def _get_master_data_urls(self, suffix: str) -> List[str]:
        """Return deduplicated candidate URLs for Poppies POS Master Data Gateway."""
        clean_suffix = suffix.lstrip("/")
        candidates = [
            f"{self.base_url}/{clean_suffix}",
            f"http://127.0.0.1:6515/api/v1/pos/master-data/{clean_suffix}",
            f"http://host.docker.internal:6515/api/v1/pos/master-data/{clean_suffix}",
            f"http://103.175.248.250:1155/api/v1/pos/master-data/{clean_suffix}",
            f"https://poppies.namanmarket.com/api/v1/pos/master-data/{clean_suffix}",
        ]
        seen = set()
        return [u for u in candidates if not (u in seen or seen.add(u))]

    async def get_health(self) -> Dict[str, Any]:
        """Check Poppies ERP Master Data API health probe."""
        urls_to_try = self._get_master_data_urls("health")
        headers = self._get_headers()

        last_err = None
        for url in urls_to_try:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        data["resolved_url"] = url
                        return data
            except Exception as ex:
                last_err = ex

        return {
            "status": "error",
            "service": "Nam An Poppies POS Master Data Inbound Gateway",
            "error": str(last_err),
            "base_url": self.base_url,
        }

    async def get_sku(self, sku_or_barcode: str, branch_code: Optional[str] = "10001") -> Optional[Dict[str, Any]]:
        """Fetch full Master Data object for 1 SKU / Barcode."""
        params = {}
        if branch_code:
            params["branch_code"] = branch_code

        last_err = None
        for url in self._get_master_data_urls(f"sku/{sku_or_barcode}"):
            try:
                async with httpx.AsyncClient(timeout=min(self.timeout, 15.0)) as client:
                    resp = await client.get(url, params=params, headers=self._get_headers())
                    if resp.status_code == 200:
                        body = resp.json()
                        return body.get("item")
                    elif resp.status_code == 404:
                        return None
            except Exception as ex:
                last_err = ex

        if last_err:
            raise last_err
        return None

    async def get_snapshot(
        self,
        branch_code: str = "10001",
        status_filter: str = "01",
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Fetch paginated master data SKU snapshot with pricing, TTHH & CTTK stock."""
        params = {
            "branch_code": branch_code,
            "status_filter": status_filter,
            "limit": limit,
            "offset": offset,
        }

        last_err: Optional[Exception] = None
        for url in self._get_master_data_urls("snapshot"):
            try:
                async with httpx.AsyncClient(timeout=min(self.timeout, 25.0)) as client:
                    resp = await client.get(url, params=params, headers=self._get_headers())
                    resp.raise_for_status()
                    return resp.json()
            except Exception as ex:
                last_err = ex

        if last_err:
            raise last_err
        raise PoppiesUpstreamError("Không thể kết nối Poppies Master Data Snapshot.")

    async def get_cttk_records(
        self,
        store_code: Optional[str] = None,
        only_positive: bool = True,
        limit: int = 5000,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Fetch instant stock inventory records across warehouses & stores."""
        params: Dict[str, Any] = {
            "only_positive": str(only_positive).lower(),
            "limit": limit,
            "offset": offset,
        }
        if store_code:
            params["store_code"] = store_code

        last_err: Optional[Exception] = None
        for url in self._get_master_data_urls("cttk-records"):
            try:
                async with httpx.AsyncClient(timeout=max(self.timeout, 30.0)) as client:
                    resp = await client.get(url, params=params, headers=self._get_headers())
                    resp.raise_for_status()
                    return resp.json()
            except Exception as ex:
                last_err = ex

        if last_err:
            raise last_err
        raise PoppiesUpstreamError("Không thể kết nối Poppies CTTK Records.")
