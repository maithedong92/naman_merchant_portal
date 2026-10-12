import asyncio
import logging
from typing import Any, Dict, List, Optional
from pymongo import MongoClient

from app.core.config import get_settings

logger = logging.getLogger("naman_portal.arito_service")


class AritoInventoryService:
    """
    Dịch vụ tra cứu tồn kho thực tế từ hệ thống ERP Arito (MongoDB collection: arito_database.cttk).
    Hỗ trợ đối soát và kiểm tra tồn kho theo từng chi nhánh:
      - 10001: Siêu thị 21 Thảo Điền, TP. Thủ Đức
      - 10004: Siêu thị 46 Hưng Phúc, Phú Mỹ Hưng, Q.7
      - 10005: Siêu thị 17 Mai Chí Thọ, TP. Thủ Đức
      - 10006: Siêu thị 303 Nguyễn Văn Trỗi, Q. Phú Nhuận
    """

    STORE_BRANCH_NAMES = {
        "10001": "21 Thảo Điền, Thủ Đức",
        "10004": "46 Hưng Phúc, Phú Mỹ Hưng Q.7",
        "10005": "17 Mai Chí Thọ, Thủ Đức",
        "10006": "303 Nguyễn Văn Trỗi, Phú Nhuận",
        "10008": "Kho Tổng",
    }

    def __init__(self, mongo_uri: Optional[str] = None):
        self.settings = get_settings()
        self.mongo_uri = mongo_uri or self.settings.MONGO_URI
        self._client: Optional[MongoClient] = None

    def _get_cttk_collection(self):
        """Khởi tạo kết nối tới collection cttk của arito_database."""
        if self._client is None:
            self._client = MongoClient(
                self.mongo_uri,
                serverSelectionTimeoutMS=5000,
                maxPoolSize=15,
            )
        db = self._client[self.settings.ARITO_DB]
        return db[self.settings.ARITO_INVENTORY_COLLECTION]

    def close(self):
        """Đóng kết nối MongoDB khi tắt service."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def check_connection(self) -> Dict[str, Any]:
        """Kiểm tra kết nối và số lượng bản ghi tồn kho trong arito_database."""
        try:
            col = self._get_cttk_collection()
            count = col.estimated_document_count()
            return {
                "connected": True,
                "database": self.settings.ARITO_DB,
                "collection": self.settings.ARITO_INVENTORY_COLLECTION,
                "total_records": count,
                "message": f"Kết nối Arito ERP thành công. Hiện có {count:,} bản ghi tồn kho.",
            }
        except Exception as ex:
            logger.error(f"Lỗi kết nối Arito ERP: {ex}")
            return {
                "connected": False,
                "database": self.settings.ARITO_DB,
                "collection": self.settings.ARITO_INVENTORY_COLLECTION,
                "total_records": 0,
                "message": f"Không thể kết nối Arito ERP: {str(ex)}",
            }

    def get_inventory_by_skus_sync(self, skus: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Truy vấn tồn kho đồng bộ từ collection cttk theo danh sách mã vật tư (ma_vt / SKU).
        Pipeline: match ma_vt in skus, group theo (ma_vt, ma_vi_tri), sum(sl_ton).
        """
        if not skus:
            return {}

        clean_skus = [str(s).strip() for s in skus if str(s).strip()]
        if not clean_skus:
            return {}

        try:
            col = self._get_cttk_collection()
            pipeline = [
                {
                    "$match": {
                        "ma_vt": {"$in": clean_skus},
                        "ma_lo": {"$ne": ""},  # Loại bỏ lô hàng rỗng
                    }
                },
                {
                    "$group": {
                        "_id": {
                            "ma_vt": "$ma_vt",
                            "ma_vi_tri": "$ma_vi_tri",
                        },
                        "total_qty": {"$sum": "$sl_ton"},
                    }
                },
            ]

            results = col.aggregate(pipeline)
            inventory_map: Dict[str, Dict[str, Any]] = {}

            # Khởi tạo khung mẫu cho các SKU
            for sku in clean_skus:
                inventory_map[sku] = {
                    "sku": sku,
                    "branches": {
                        code: {"branch_name": name, "stock": 0.0}
                        for code, name in self.STORE_BRANCH_NAMES.items()
                    },
                    "total_stock_all_branches": 0.0,
                    "raw_locations": {},
                }

            for doc in results:
                sku = doc["_id"].get("ma_vt")
                loc = str(doc["_id"].get("ma_vi_tri", "")).strip()
                qty = float(doc.get("total_qty", 0.0))

                if sku in inventory_map:
                    inventory_map[sku]["raw_locations"][loc] = qty
                    # Thử match trực tiếp hoặc loại bỏ hậu tố vị trí kho (ví dụ: 10001A -> 10001)
                    clean_loc = loc.rstrip("AaBb")
                    target_branch = (
                        loc
                        if loc in inventory_map[sku]["branches"]
                        else (clean_loc if clean_loc in inventory_map[sku]["branches"] else None)
                    )
                    if target_branch:
                        inventory_map[sku]["branches"][target_branch]["stock"] += qty
                        inventory_map[sku]["total_stock_all_branches"] += qty

            return inventory_map
        except Exception as e:
            logger.error(f"Lỗi khi truy vấn tồn kho Arito: {e}")
            return {}

    async def get_inventory_by_skus(self, skus: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Phiên bản bất đồng bộ (Non-blocking) chạy trong ThreadPool để không nghẽn FastAPI event loop.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_inventory_by_skus_sync, skus)

    async def recommend_best_store_for_order(
        self, items: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Gợi ý chi nhánh tối ưu nhất để phân tuyến dựa trên số lượng tồn kho đáp ứng của các sản phẩm.
        items: danh sách sản phẩm gồm [{"sku": "...", "quantity": 1}, ...]
        """
        skus = [it.get("sku") for it in items if it.get("sku")]
        if not skus:
            return {"recommended_store": None, "reason": "Đơn hàng không có mã SKU"}

        stock_data = await self.get_inventory_by_skus(skus)
        store_scores: Dict[str, int] = {code: 0 for code in self.STORE_BRANCH_NAMES}
        store_missing_skus: Dict[str, List[str]] = {
            code: [] for code in self.STORE_BRANCH_NAMES
        }

        for item in items:
            sku = item.get("sku")
            req_qty = float(item.get("quantity", 1))
            sku_info = stock_data.get(sku, {})
            branches = sku_info.get("branches", {})

            for code in self.STORE_BRANCH_NAMES:
                curr_stock = branches.get(code, {}).get("stock", 0.0)
                if curr_stock >= req_qty:
                    store_scores[code] += 1
                else:
                    store_missing_skus[code].append(sku)

        # Sắp xếp các chi nhánh theo số lượng sản phẩm đáp ứng đủ nhiều nhất
        sorted_stores = sorted(
            store_scores.items(), key=lambda kv: kv[1], reverse=True
        )
        best_code, score = sorted_stores[0]

        return {
            "recommended_store": best_code,
            "store_name": self.STORE_BRANCH_NAMES.get(best_code),
            "fulfilled_items_count": score,
            "total_items_count": len(items),
            "missing_items": store_missing_skus.get(best_code, []),
            "ranking": [
                {
                    "store_code": c,
                    "store_name": self.STORE_BRANCH_NAMES.get(c),
                    "fulfilled_items": s,
                    "missing_skus": store_missing_skus.get(c, []),
                }
                for c, s in sorted_stores
            ],
            "stock_details": stock_data,
        }


# Singleton instance
arito_service = AritoInventoryService()
