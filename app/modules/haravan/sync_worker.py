import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
import pymongo
from pymongo import MongoClient

from app.core.config import get_settings
from app.modules.haravan.client import HaravanClient

logger = logging.getLogger("naman_portal.haravan.sync")


class HaravanSyncWorker:
    """
    Background Worker đồng bộ đơn hàng từ Haravan Omnichannel về MongoDB.
    Đảm bảo 100% nguyên tắc bảo toàn trường 'internal' (phân tuyến & trạng thái xử lý).
    """

    def __init__(self, client: Optional[HaravanClient] = None):
        self.settings = get_settings()
        self.client = client or HaravanClient()
        self._mongo_client: Optional[MongoClient] = None
        self._is_running = False
        self._background_task: Optional[asyncio.Task] = None

        # Statistics & Status
        self.last_sync_time: Optional[datetime] = None
        self.last_sync_count: int = 0
        self.total_synced_in_session: int = 0
        self.last_error: Optional[str] = None

    def get_mongo_collection(self):
        """Khởi tạo kết nối an toàn tới MongoDB collection đơn hàng."""
        if self._mongo_client is None:
            self._mongo_client = MongoClient(
                self.settings.MONGO_URI,
                serverSelectionTimeoutMS=5000,
                maxPoolSize=20,
            )
        db = self._mongo_client[self.settings.MONGO_DB]
        return db[self.settings.MONGO_ORDERS_COLLECTION]

    def close(self):
        """Đóng kết nối MongoDB khi tắt app."""
        if self._mongo_client:
            try:
                self._mongo_client.close()
            except Exception:
                pass
            self._mongo_client = None

    @staticmethod
    def normalize_order_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
        """Chuẩn hóa cấu trúc đơn hàng trước khi upsert vào MongoDB."""
        clean_doc = doc.copy()
        if "id" in clean_doc:
            clean_doc["id"] = str(clean_doc["id"])

        # Luôn loại bỏ 'internal' khỏi payload cập nhật để không ghi đè dữ liệu phân tuyến
        if "internal" in clean_doc:
            del clean_doc["internal"]

        return clean_doc

    def upsert_order(self, col, order_doc: Dict[str, Any]) -> bool:
        """
        Lưu đơn hàng vào MongoDB:
        - Dùng $set cho toàn bộ dữ liệu đơn hàng cập nhật từ Haravan (đã loại bỏ 'internal').
        - Dùng $setOnInsert cho 'internal' (chỉ gán giá trị mặc định khi đơn MỚI được thêm lần đầu).
        """
        haravan_id = order_doc.get("id")
        if not haravan_id:
            return False

        default_internal = {
            "order_processing_status": "Đơn hàng mới",
            "assigned_store": None,
            "assignment_reason": None,
            "assignment_note": None,
            "assigned_at": None,
            "assigned_by": None,
            "history": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": "Đồng bộ đơn hàng từ Haravan",
                    "user": "system_haravan_sync",
                    "details": "Đơn hàng mới được tiếp nhận qua Haravan Omnichannel",
                }
            ],
            "sync_info": {
                "sync_time": datetime.now(timezone.utc).isoformat(),
                "sync_source": "HARAVAN",
                "sync_method": "BACKGROUND_WORKER",
            },
        }

        try:
            update_op = {
                "$set": order_doc,
                "$setOnInsert": {"internal": default_internal},
            }
            res = col.update_one({"id": haravan_id}, update_op, upsert=True)
            return True
        except Exception as e:
            logger.error(f"Lỗi khi upsert đơn hàng {haravan_id}: {e}")
            return False

    async def sync_once(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Thực hiện một chu kỳ đồng bộ đơn hàng từ Haravan API về MongoDB.
        """
        effective_limit = limit or self.settings.HARAVAN_SYNC_LIMIT
        logger.info(f"Bắt đầu đồng bộ Haravan (tối đa {effective_limit} đơn)...")

        try:
            orders = await self.client.fetch_latest_orders(limit=effective_limit)
            if not orders:
                self.last_sync_time = datetime.now(timezone.utc)
                self.last_sync_count = 0
                return {"success": True, "processed": 0, "skipped": 0, "total": 0}

            # Thực hiện ghi MongoDB trong ThreadPool để không block async event loop
            def _write_to_mongo():
                col = self.get_mongo_collection()
                processed = 0
                skipped = 0
                for raw in orders:
                    if not raw.get("order_number") or not raw.get("id"):
                        skipped += 1
                        continue
                    norm = self.normalize_order_doc(raw)
                    if self.upsert_order(col, norm):
                        processed += 1
                    else:
                        skipped += 1
                return processed, skipped

            loop = asyncio.get_running_loop()
            processed, skipped = await loop.run_in_executor(None, _write_to_mongo)

            self.last_sync_time = datetime.now(timezone.utc)
            self.last_sync_count = processed
            self.total_synced_in_session += processed
            self.last_error = None

            logger.info(
                f"✅ Đồng bộ Haravan hoàn tất: {processed} đơn được lưu/cập nhật, {skipped} bỏ qua."
            )
            return {
                "success": True,
                "processed": processed,
                "skipped": skipped,
                "total": len(orders),
                "timestamp": self.last_sync_time.isoformat(),
            }

        except Exception as ex:
            error_msg = f"Lỗi trong chu kỳ đồng bộ Haravan: {str(ex)}"
            logger.error(error_msg)
            self.last_error = error_msg
            return {"success": False, "error": error_msg}

    async def _background_loop(self):
        """Vòng lặp chạy nền định kỳ mỗi HARAVAN_SYNC_INTERVAL_SECONDS."""
        interval = max(10, self.settings.HARAVAN_SYNC_INTERVAL_SECONDS)
        logger.info(f"🚀 Haravan Sync Worker đã kích hoạt (chu kỳ {interval}s).")
        self._is_running = True

        while self._is_running:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                logger.info("Haravan Sync Worker nhận lệnh dừng.")
                break
            except Exception as e:
                logger.error(f"Ngoại lệ trong Haravan Sync loop: {e}")

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

        self._is_running = False
        logger.info("Haravan Sync Worker đã kết thúc an toàn.")

    def start_background_worker(self):
        """Khởi chạy worker ngầm nếu được cấu hình bật HARAVAN_ENABLED."""
        if not self.settings.HARAVAN_ENABLED:
            logger.info("Cấu hình HARAVAN_ENABLED=False. Bỏ qua chạy sync worker.")
            return

        if self._background_task is None or self._background_task.done():
            self._background_task = asyncio.create_task(self._background_loop())

    def stop_background_worker(self):
        """Dừng worker an toàn."""
        self._is_running = False
        if self._background_task and not self._background_task.done():
            self._background_task.cancel()
        self.close()

    def get_status(self) -> Dict[str, Any]:
        """Lấy thông tin trạng thái hoạt động của Worker."""
        return {
            "is_enabled": self.settings.HARAVAN_ENABLED,
            "is_running": self._is_running,
            "interval_seconds": self.settings.HARAVAN_SYNC_INTERVAL_SECONDS,
            "sync_limit": self.settings.HARAVAN_SYNC_LIMIT,
            "last_sync_time": (
                self.last_sync_time.isoformat() if self.last_sync_time else None
            ),
            "last_sync_count": self.last_sync_count,
            "total_synced_in_session": self.total_synced_in_session,
            "last_error": self.last_error,
        }


# Singleton worker instance
haravan_sync_worker = HaravanSyncWorker()
