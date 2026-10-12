import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple
from bson import ObjectId
from pymongo import MongoClient

from app.core.config import get_settings
from app.core.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.models.user import User, UserRole
from app.services.arito_service import arito_service

logger = logging.getLogger("naman_portal.haravan_orders")


class HaravanOrderService:
    """
    Dịch vụ quản lý vòng đời đơn hàng Haravan Omnichannel và quy trình phân tuyến chi nhánh.
    Tuân thủ 100% cấu trúc dữ liệu và logic nghiệp vụ của ứng dụng pmdonhang:
      - Trạng thái chuẩn: 'Đơn hàng mới', 'Đã phân tuyến', 'Yêu cầu phân tuyến lại',
                          'Đã đóng gói', 'Đã gửi vận đơn', 'Hoàn thành', 'Đã hủy'.
      - Các chi nhánh: 10001 (21TD), 10004 (46NLB), 10005 (17MCT), 10006 (303NVT), 10008 (Ecom).
      - Chụp snapshot tồn kho Arito ERP khi phân tuyến.
      - Gỡ bỏ assigned_store = None khi yêu cầu phân tuyến lại.
    """

    VALID_PROCESSING_STATUSES = [
        "Đơn hàng mới",
        "Đã phân tuyến",
        "Yêu cầu phân tuyến lại",
        "Đã đóng gói",
        "Đã gửi vận đơn",
        "Hoàn thành",
        "Đã hủy",
    ]

    ASSIGNMENT_STORES = {
        "10001": "10001 - 21TD (21 Thảo Điền)",
        "10004": "10004 - 46NLB (46 Hưng Phúc Q.7)",
        "10005": "10005 - 17MCT (17 Mai Chí Thọ)",
        "10006": "10006 - 303NVT (303 Nguyễn Văn Trỗi)",
        "10008": "10008 - Ecom (Kho Online)",
    }

    ASSIGNMENT_REASONS = [
        "Gần cửa hàng",
        "Cửa hàng có sẵn hàng",
        "Shipper gần nhất",
        "Khách yêu cầu",
        "Khác",
    ]

    REASSIGNMENT_REASONS = [
        "Cửa hàng không có hàng",
        "Cửa hàng quá tải",
        "Địa chỉ giao hàng quá xa",
        "Khách hàng yêu cầu đổi cửa hàng",
        "Lỗi phân tuyến ban đầu",
        "Khác",
    ]

    SHIPPING_CARRIERS = [
        "Nam An Market",
        "Ahamove",
        "Giao Hang Nhanh (GHN)",
        "Grab Express",
        "Đối tác (sàn TMDT)",
        "Khác",
    ]

    def __init__(self, mongo_uri: Optional[str] = None):
        self.settings = get_settings()
        self.mongo_uri = mongo_uri or self.settings.MONGO_URI
        self._client: Optional[MongoClient] = None

    def _get_orders_collection(self):
        if self._client is None:
            self._client = MongoClient(
                self.mongo_uri,
                serverSelectionTimeoutMS=5000,
                maxPoolSize=25,
            )
        db = self._client[self.settings.MONGO_DB]
        return db[self.settings.MONGO_ORDERS_COLLECTION]

    def close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _serialize_mongo_doc(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """Chuyển ObjectId và các kiểu dữ liệu Mongo sang JSON serializable."""
        res = doc.copy()
        if "_id" in res:
            res["_id"] = str(res["_id"])
        return res

    def list_orders_sync(
        self,
        current_user: User,
        status: Optional[str] = None,
        store_code: Optional[str] = None,
        source: Optional[str] = None,
        search: Optional[str] = None,
        unassigned_only: bool = False,
        incomplete_only: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Lấy danh sách đơn hàng từ MongoDB kèm Store-Level Isolation RBAC:
        - Staff / Store Manager chỉ thấy đơn hàng có assigned_store khớp với chi nhánh của họ.
        - SuperAdmin / Dispatcher thấy tất cả và có thể lọc đơn chưa phân tuyến hoặc chưa hoàn thành.
        """
        col = self._get_orders_collection()
        query: Dict[str, Any] = {}

        # 1. Store Isolation RBAC
        is_admin = current_user.is_superuser or current_user.role == UserRole.SUPER_ADMIN.value
        user_store = current_user.store_code or (
            current_user.store.code if current_user.store else None
        )

        if not is_admin:
            if not user_store:
                return [], 0
            # Staff chỉ thấy đơn đã phân tuyến về cửa hàng mình
            query["internal.assigned_store"] = user_store
        else:
            # SuperAdmin có thể lọc theo store_code hoặc lọc đơn chưa phân tuyến
            if unassigned_only:
                query["$or"] = [
                    {"internal.assigned_store": None},
                    {"internal.assigned_store": {"$exists": False}},
                    {"internal.assigned_store": ""},
                ]
            elif store_code:
                query["internal.assigned_store"] = store_code

        # 2. Bộ lọc trạng thái xử lý nội bộ (hỗ trợ filter "Chưa hoàn thành" như pmdonhang cũ)
        if incomplete_only or (status and status == "Chưa hoàn thành"):
            query["internal.order_processing_status"] = {"$nin": ["Hoàn thành", "Đã hủy"]}
        elif status and status != "Tất cả trạng thái":
            query["internal.order_processing_status"] = status

        # 3. Bộ lọc nguồn đơn hàng
        if source and source != "Tất cả nguồn":
            query["source"] = source.lower()

        # 4. Tìm kiếm từ khóa (order_number, tên, sđt)
        if search and search.strip():
            kw = search.strip()
            regex = {"$regex": kw, "$options": "i"}
            query["$or"] = [
                {"order_number": regex},
                {"customer.phone": regex},
                {"shipping_address.phone": regex},
                {"customer.first_name": regex},
                {"customer.last_name": regex},
                {"billing_address.name": regex},
            ]

        skip = max(0, (page - 1) * page_size)
        total = col.count_documents(query)
        cursor = (
            col.find(query)
            .sort("created_at", -1)
            .skip(skip)
            .limit(page_size)
        )

        items = [self._serialize_mongo_doc(doc) for doc in cursor]
        return items, total

    async def list_orders(
        self,
        current_user: User,
        status: Optional[str] = None,
        store_code: Optional[str] = None,
        source: Optional[str] = None,
        search: Optional[str] = None,
        unassigned_only: bool = False,
        incomplete_only: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[Dict[str, Any]], int]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self.list_orders_sync,
            current_user,
            status,
            store_code,
            source,
            search,
            unassigned_only,
            incomplete_only,
            page,
            page_size,
        )

    def count_incomplete_orders_sync(
        self,
        current_user: User,
        store_code: Optional[str] = None,
    ) -> int:
        """
        Đếm tổng số đơn hàng Haravan chưa hoàn thành theo phân quyền (giống pmdonhang):
        - Staff / Store Manager: Đếm các đơn chưa hoàn thành của chi nhánh mình.
        - SuperAdmin: Đếm toàn bộ đơn chưa hoàn thành của toàn chuỗi (hoặc theo store_code).
        """
        col = self._get_orders_collection()
        query: Dict[str, Any] = {
            "internal.order_processing_status": {"$nin": ["Hoàn thành", "Đã hủy"]}
        }
        is_admin = current_user.is_superuser or current_user.role == UserRole.SUPER_ADMIN.value
        user_store = current_user.store_code or (
            current_user.store.code if current_user.store else None
        )
        if not is_admin:
            if not user_store:
                return 0
            query["internal.assigned_store"] = user_store
        elif store_code:
            query["internal.assigned_store"] = store_code
        return col.count_documents(query)

    async def count_incomplete_orders(
        self,
        current_user: User,
        store_code: Optional[str] = None,
    ) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self.count_incomplete_orders_sync,
            current_user,
            store_code,
        )

    def get_order_by_id_or_number_sync(self, order_id_or_sn: str) -> Optional[Dict[str, Any]]:
        col = self._get_orders_collection()
        query = {
            "$or": [
                {"id": order_id_or_sn},
                {"order_number": order_id_or_sn},
            ]
        }
        if ObjectId.is_valid(order_id_or_sn):
            query["$or"].append({"_id": ObjectId(order_id_or_sn)})

        doc = col.find_one(query)
        return self._serialize_mongo_doc(doc) if doc else None

    async def get_order_detail(self, order_id_or_sn: str, current_user: User) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        order = await loop.run_in_executor(None, self.get_order_by_id_or_number_sync, order_id_or_sn)
        if not order:
            raise NotFoundError(f"Không tìm thấy đơn hàng '{order_id_or_sn}' trong hệ thống Haravan.")

        # RBAC Check
        is_admin = current_user.is_superuser or current_user.role == UserRole.SUPER_ADMIN.value
        user_store = current_user.store_code or (
            current_user.store.code if current_user.store else None
        )
        assigned_store = order.get("internal", {}).get("assigned_store")

        if not is_admin and assigned_store != user_store:
            raise ForbiddenError(f"Bạn không có quyền truy cập đơn hàng của chi nhánh '{assigned_store}'.")

        # Tra cứu tồn kho Arito ERP theo thời gian thực cho các SKU trong đơn
        skus = [it.get("sku") for it in order.get("line_items", []) if it.get("sku")]
        stock_live = await arito_service.get_inventory_by_skus(skus)
        order["arito_live_stock"] = stock_live

        return order

    def _append_history(self, order: Dict[str, Any], action: str, user: str, details: str = ""):
        if "internal" not in order:
            order["internal"] = {}
        if "history" not in order["internal"] or not isinstance(order["internal"]["history"], list):
            order["internal"]["history"] = []

        order["internal"]["history"].append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "user": user,
            "details": details,
        })

    async def assign_order(
        self,
        order_id_or_sn: str,
        store_code: str,
        reason: str,
        note: str,
        current_user: User,
    ) -> Dict[str, Any]:
        """
        Nghiệp vụ: Phân tuyến đơn hàng cho chi nhánh (Follow 100% pmdonhang)
        - Chụp snapshot tồn kho Arito
        - Gán assigned_store, lý do, ghi chú, người phân
        - Cập nhật trạng thái 'Đã phân tuyến'
        """
        if store_code not in self.ASSIGNMENT_STORES:
            raise BadRequestError(f"Mã cửa hàng '{store_code}' không hợp lệ. Chọn 1 trong: {list(self.ASSIGNMENT_STORES.keys())}")

        order = await self.get_order_detail(order_id_or_sn, current_user)
        skus = [it.get("sku") for it in order.get("line_items", []) if it.get("sku")]

        # Snapshot tồn kho Arito
        stock_snapshot = await arito_service.get_inventory_by_skus(skus)

        now_str = datetime.now(timezone.utc).isoformat()
        username = current_user.username

        update_fields = {
            "internal.order_processing_status": "Đã phân tuyến",
            "internal.assigned_store": store_code,
            "internal.assignment_reason": reason,
            "internal.assignment_note": note,
            "internal.assigned_by": username,
            "internal.assigned_at": now_str,
            "internal.assignment_stock": stock_snapshot,
        }

        history_note = f"Phân tuyến cho cửa hàng {store_code} ({self.ASSIGNMENT_STORES.get(store_code)}) - Lý do: {reason}"
        if note:
            history_note += f" - Ghi chú: {note}"

        col = self._get_orders_collection()
        col.update_one(
            {"id": order["id"]},
            {
                "$set": update_fields,
                "$push": {
                    "internal.history": {
                        "timestamp": now_str,
                        "action": "Phân tuyến đơn hàng",
                        "user": username,
                        "details": history_note,
                    }
                },
            },
        )

        logger.info(f"✅ Đã phân tuyến đơn {order.get('order_number')} cho chi nhánh {store_code} bởi {username}.")
        return await self.get_order_detail(order["id"], current_user)

    async def request_reassignment(
        self,
        order_id_or_sn: str,
        reason: str,
        note: str,
        current_user: User,
    ) -> Dict[str, Any]:
        """
        Nghiệp vụ: Yêu cầu phân tuyến lại (Follow 100% pmdonhang)
        - QUAN TRỌNG: Gỡ bỏ assigned_store = None để cửa hàng cũ không còn thấy đơn
        - Lưu lý do yêu cầu phân tuyến lại
        - Chuyển trạng thái 'Yêu cầu phân tuyến lại', trả đơn về hàng đợi tổng đài
        """
        order = await self.get_order_detail(order_id_or_sn, current_user)
        old_store = order.get("internal", {}).get("assigned_store", "Chưa gán")

        now_str = datetime.now(timezone.utc).isoformat()
        username = current_user.username

        update_fields = {
            "internal.order_processing_status": "Yêu cầu phân tuyến lại",
            "internal.assigned_store": None,
            "internal.assignment_reason": None,
            "internal.assignment_note": None,
            "internal.assigned_by": None,
            "internal.assigned_at": None,
            "internal.reassignment_reason": reason,
            "internal.reassignment_note": note,
            "internal.reassignment_requested_by": username,
            "internal.reassignment_requested_at": now_str,
        }

        history_note = f"Yêu cầu phân tuyến lại từ chi nhánh {old_store} - Lý do: {reason}"
        if note:
            history_note += f" - Ghi chú: {note}"
        history_note += " (Đã gỡ chi nhánh, trả đơn về tổng đài)"

        col = self._get_orders_collection()
        col.update_one(
            {"id": order["id"]},
            {
                "$set": update_fields,
                "$push": {
                    "internal.history": {
                        "timestamp": now_str,
                        "action": "Yêu cầu phân tuyến lại",
                        "user": username,
                        "details": history_note,
                    }
                },
            },
        )

        logger.info(f"⚠️ Chi nhánh {old_store} đã yêu cầu phân tuyến lại đơn {order.get('order_number')}.")
        # Lấy lại đơn với quyền admin
        loop = asyncio.get_running_loop()
        updated_doc = await loop.run_in_executor(None, self.get_order_by_id_or_number_sync, order["id"])
        return updated_doc

    async def pack_order(
        self,
        order_id_or_sn: str,
        current_user: User,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Chuyển trạng thái 'Đã đóng gói'."""
        order = await self.get_order_detail(order_id_or_sn, current_user)
        now_str = datetime.now(timezone.utc).isoformat()
        username = current_user.username

        history_note = "Chi nhánh đã hoàn tất soạn hàng và đóng gói"
        if note:
            history_note += f" - Ghi chú: {note}"

        col = self._get_orders_collection()
        col.update_one(
            {"id": order["id"]},
            {
                "$set": {
                    "internal.order_processing_status": "Đã đóng gói",
                    "internal.packed_at": now_str,
                    "internal.packed_by": username,
                },
                "$push": {
                    "internal.history": {
                        "timestamp": now_str,
                        "action": "Đã đóng gói",
                        "user": username,
                        "details": history_note,
                    }
                },
            },
        )
        return await self.get_order_detail(order["id"], current_user)

    async def ship_order(
        self,
        order_id_or_sn: str,
        tracking_code: str,
        carrier: str,
        current_user: User,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Chuyển trạng thái 'Đã gửi vận đơn' kèm mã vận đơn và đơn vị ship."""
        order = await self.get_order_detail(order_id_or_sn, current_user)
        now_str = datetime.now(timezone.utc).isoformat()
        username = current_user.username

        history_note = f"Đã gửi vận đơn - Đơn vị: {carrier}, Mã vận đơn: {tracking_code}"
        if note:
            history_note += f" - Ghi chú: {note}"

        col = self._get_orders_collection()
        col.update_one(
            {"id": order["id"]},
            {
                "$set": {
                    "internal.order_processing_status": "Đã gửi vận đơn",
                    "internal.tracking_code": tracking_code,
                    "internal.shipping_carrier": carrier,
                    "internal.shipped_at": now_str,
                    "internal.shipped_by": username,
                },
                "$push": {
                    "internal.history": {
                        "timestamp": now_str,
                        "action": "Đã gửi vận đơn",
                        "user": username,
                        "details": history_note,
                    }
                },
            },
        )
        return await self.get_order_detail(order["id"], current_user)

    async def complete_order(
        self,
        order_id_or_sn: str,
        actual_total: float,
        actual_shipping: float,
        actual_carrier: str,
        tracking_code: str,
        note: str,
        current_user: User,
    ) -> Dict[str, Any]:
        """
        Nghiệp vụ: Hoàn thành đơn hàng (Follow 100% OrderCompletionDialog của pmdonhang)
        - Lưu completion_info đầy đủ: actual_total, actual_shipping, actual_carrier, tracking_code, note, completed_by
        - Cập nhật trạng thái 'Hoàn thành'
        """
        order = await self.get_order_detail(order_id_or_sn, current_user)
        now_str = datetime.now(timezone.utc).isoformat()
        username = current_user.username

        completion_info = {
            "actual_total": str(actual_total),
            "actual_shipping": str(actual_shipping),
            "actual_carrier": actual_carrier,
            "tracking_code": tracking_code,
            "note": note,
            "completed_by": username,
            "completed_at": now_str,
        }

        history_note = (
            f"Hoàn thành đơn hàng - Tổng tiền thực tế: {actual_total:,.0f} VNĐ, "
            f"Phí ship: {actual_shipping:,.0f} VNĐ, Đơn vị: {actual_carrier}"
        )
        if tracking_code:
            history_note += f", Vận đơn: {tracking_code}"
        if note:
            history_note += f" - Ghi chú: {note}"

        col = self._get_orders_collection()
        col.update_one(
            {"id": order["id"]},
            {
                "$set": {
                    "internal.order_processing_status": "Hoàn thành",
                    "internal.completion_info": completion_info,
                },
                "$push": {
                    "internal.history": {
                        "timestamp": now_str,
                        "action": "Hoàn thành đơn hàng",
                        "user": username,
                        "details": history_note,
                    }
                },
            },
        )

        logger.info(f"🎉 Hoàn thành đơn {order.get('order_number')} thành công bởi {username}.")
        return await self.get_order_detail(order["id"], current_user)

    async def cancel_order(
        self,
        order_id_or_sn: str,
        reason: str,
        note: str,
        current_user: User,
    ) -> Dict[str, Any]:
        """
        Nghiệp vụ: Hủy đơn hàng (Follow 100% OrderCancelDialog của pmdonhang)
        - Lưu cancel_info: reason, note, cancelled_at, cancelled_by
        - Cập nhật trạng thái 'Đã hủy'
        """
        order = await self.get_order_detail(order_id_or_sn, current_user)
        now_str = datetime.now(timezone.utc).isoformat()
        username = current_user.username

        cancel_info = {
            "reason": reason,
            "note": note,
            "cancelled_by": username,
            "cancelled_at": now_str,
        }

        history_note = f"Hủy đơn hàng - Lý do: {reason}"
        if note:
            history_note += f" - Ghi chú: {note}"

        col = self._get_orders_collection()
        col.update_one(
            {"id": order["id"]},
            {
                "$set": {
                    "internal.order_processing_status": "Đã hủy",
                    "internal.cancel_info": cancel_info,
                },
                "$push": {
                    "internal.history": {
                        "timestamp": now_str,
                        "action": "Hủy đơn hàng",
                        "user": username,
                        "details": history_note,
                    }
                },
            },
        )

        logger.info(f"❌ Đã hủy đơn {order.get('order_number')} - Lý do: {reason}.")
        return await self.get_order_detail(order["id"], current_user)

    async def update_admin_note(
        self,
        order_id_or_sn: str,
        note: str,
        current_user: User,
    ) -> Dict[str, Any]:
        """
        Nghiệp vụ: Cập nhật ghi chú nội bộ (admin) cho đơn hàng Haravan (Follow 100% pmdonhang)
        - Lưu internal.admin_notes
        - Thêm lịch sử 'Chỉnh sửa ghi chú admin'
        """
        order = await self.get_order_detail(order_id_or_sn, current_user)
        now_str = datetime.now(timezone.utc).isoformat()
        username = current_user.username

        col = self._get_orders_collection()
        col.update_one(
            {"id": order["id"]},
            {
                "$set": {
                    "internal.admin_notes": note,
                },
                "$push": {
                    "internal.history": {
                        "timestamp": now_str,
                        "action": "Chỉnh sửa ghi chú admin",
                        "user": username,
                        "details": f"Cập nhật ghi chú nội bộ: {note}",
                    }
                },
            },
        )
        logger.info(f"📝 Đã cập nhật ghi chú admin cho đơn {order.get('order_number')} bởi {username}.")
        return await self.get_order_detail(order["id"], current_user)


# Singleton instance
haravan_order_service = HaravanOrderService()
