import asyncio
import logging
from typing import Any, Dict, List, Optional
import httpx

from app.core.config import get_settings

logger = logging.getLogger("naman_portal.haravan.client")


class HaravanClient:
    """
    Client bất đồng bộ (Async) tương tác với Haravan Omnichannel Orders API.
    Lấy danh sách đơn hàng đa kênh (Website Nam An, Shopee, Lazada, POS...).
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 30.0,
    ):
        settings = get_settings()
        self.base_url = (base_url or settings.HARAVAN_BASE_URL).rstrip("/")
        self.token = token or settings.HARAVAN_TOKEN
        self.timeout = timeout

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def check_connection(self) -> Dict[str, Any]:
        """Kiểm tra tính hợp lệ của token và kết nối tới Haravan API."""
        url = f"{self.base_url}/orders.json"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, headers=self.headers, params={"limit": 1})
                if res.status_code == 200:
                    data = res.json()
                    orders = data.get("orders", [])
                    return {
                        "connected": True,
                        "status_code": 200,
                        "sample_order_count": len(orders),
                        "message": "Kết nối Haravan Omnichannel API thành công.",
                    }
                elif res.status_code == 401:
                    return {
                        "connected": False,
                        "status_code": 401,
                        "message": "Haravan Bearer Token không hợp lệ hoặc đã hết hạn.",
                    }
                else:
                    return {
                        "connected": False,
                        "status_code": res.status_code,
                        "message": f"Haravan API trả về mã lỗi HTTP {res.status_code}: {res.text[:200]}",
                    }
        except Exception as ex:
            logger.error(f"Lỗi kết nối Haravan API: {ex}")
            return {
                "connected": False,
                "status_code": 500,
                "message": f"Lỗi ngoại lệ khi gọi Haravan API: {str(ex)}",
            }

    async def fetch_latest_orders(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Lấy danh sách top N đơn hàng mới nhất (order: updated_at desc) từ Haravan.
        Tự động phân trang (mỗi trang tối đa 50 đơn theo quy định của Haravan).
        """
        all_orders: List[Dict[str, Any]] = []
        page = 1
        page_size = 50
        max_pages = (limit + page_size - 1) // page_size

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            while page <= max_pages and len(all_orders) < limit:
                params = {
                    "limit": page_size,
                    "page": page,
                    "order": "updated_at desc",
                }
                url = f"{self.base_url}/orders.json"
                try:
                    res = await client.get(url, headers=self.headers, params=params)
                except httpx.RequestError as exc:
                    logger.error(f"Lỗi kết nối tới Haravan (trang {page}): {exc}")
                    break

                if res.status_code == 200:
                    data = res.json()
                    orders = data.get("orders", [])
                    if not orders:
                        break
                    all_orders.extend(orders)
                    if len(all_orders) >= limit or len(orders) < page_size:
                        break
                    page += 1
                    await asyncio.sleep(0.2)  # Delay nhẹ giữa các trang tránh burst rate limit
                elif res.status_code == 429:
                    retry_after = int(res.headers.get("Retry-After", 10))
                    logger.warning(f"Haravan Rate Limit (429), chờ {retry_after}s...")
                    await asyncio.sleep(retry_after)
                elif res.status_code == 401:
                    logger.error("Haravan Bearer Token hết hạn hoặc không có quyền truy cập.")
                    break
                else:
                    logger.error(f"Lỗi Haravan HTTP {res.status_code}: {res.text[:200]}")
                    break

        logger.info(f"Đã lấy thành công {len(all_orders[:limit])} đơn hàng từ Haravan API.")
        return all_orders[:limit]
