import json
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.interfaces.channel_adapter import BaseChannelAdapter
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.product import Category, Product
from app.models.store import Store, StoreChannelMapping
from app.schemas.order import OrderStatusUpdateSchema
from app.schemas.sync import SyncResult
from app.services.channel_service import channel_service
from app.services.order_service import OrderService
from app.modules.shopeefood.service import shopeefood_client

logger = logging.getLogger("naman_portal.modules.shopeefood.adapter")

# In-memory store menu cache for ShopeeFood pull requests (/shopeefoodapi/{store_code}.json)
_shopeefood_menu_cache: Dict[str, Dict[str, Any]] = {}


class ShopeeFoodChannelAdapter(BaseChannelAdapter):
    """
    Channel Adapter for ShopeeFood / Foody External S2S API v7.0.2.
    Integrates granular feature toggles:
    - order_webhook_enabled: toggle real-time order ingest
    - auto_confirm_enabled: toggle automatic order confirmation
    - stock_sync_enabled: toggle dish stock/availability sync
    - menu_sync_enabled: toggle menu catalog synchronization
    """

    @property
    def channel_code(self) -> str:
        return "SHOPEEFOOD"

    @property
    def display_name(self) -> str:
        return "ShopeeFood VN"

    # ==========================================================================
    # 1. Menu & Catalog Synchronization
    # ==========================================================================

    async def build_catalog_menu(
        self,
        store_id: str,
        partner_store_id: str,
        db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Build Foody / ShopeeFood JSON catalog format from local database.
        Includes sections, categories, dishes, prices in VND, and operating hours.
        """
        query = (
            select(Product, StoreInventory)
            .join(
                StoreInventory,
                (StoreInventory.product_id == Product.id) & (StoreInventory.store_id == store_id),
                isouter=True,
            )
            .options(selectinload(Product.category))
            .where(Product.is_active == True)
        )
        res = await db.execute(query)
        rows = res.all()

        # Group items into categories
        category_map: Dict[str, Dict[str, Any]] = {}
        for product, inv in rows:
            cate_name = product.category.name if product.category else "Bách Hóa Tổng Hợp"
            cate_id = product.category.code if product.category else "general"

            if cate_id not in category_map:
                category_map[cate_id] = {
                    "id": str(cate_id),
                    "name": cate_name,
                    "sequence": product.category.sequence if product.category else 99,
                    "sort_type": 1,
                    "availableStatus": "AVAILABLE",
                    "items": [],
                }

            is_available = (
                "AVAILABLE"
                if (inv and not inv.is_out_of_stock and inv.available_stock > 0)
                else "UNAVAILABLE"
            )

            category_map[cate_id]["items"].append({
                "id": str(product.sku),
                "name": product.name,
                "price": float(product.base_price),
                "description": product.description or "",
                "availableStatus": is_available,
                "photos": [product.image_url] if product.image_url else [],
            })

        categories_list = list(category_map.values())
        categories_list.sort(key=lambda x: x["sequence"])

        menu_payload = {
            "partnerMerchantID": partner_store_id,
            "currency": {"code": "VND", "symbol": "₫", "exponent": 0},
            "sections": [{
                "id": 1,
                "name": "Regular Menu",
                "serviceHours": {
                    "mon": [{"startTime": "07:00", "endTime": "21:30"}],
                    "tue": [{"startTime": "07:00", "endTime": "21:30"}],
                    "wed": [{"startTime": "07:00", "endTime": "21:30"}],
                    "thu": [{"startTime": "07:00", "endTime": "21:30"}],
                    "fri": [{"startTime": "07:00", "endTime": "21:30"}],
                    "sat": [{"startTime": "07:00", "endTime": "21:30"}],
                    "sun": [{"startTime": "07:00", "endTime": "21:30"}],
                },
                "categories": categories_list,
            }]
        }

        _shopeefood_menu_cache[partner_store_id] = menu_payload
        return menu_payload

    async def sync_menu(
        self,
        store_id: str,
        partner_store_id: str,
        db: AsyncSession
    ) -> SyncResult:
        """Sync menu catalog to ShopeeFood if feature is enabled."""
        # Check feature toggle
        enabled = await channel_service.is_feature_enabled(self.channel_code, "menu_sync_enabled", db)
        if not enabled:
            logger.info(f"Đồng bộ thực đơn {self.channel_code} bị bỏ qua do tính năng đang TẮT.")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=False,
                message="Tính năng 'Đồng bộ thực đơn' cho ShopeeFood hiện đang TẮT trong cài đặt quản trị.",
            )

        try:
            menu_data = await self.build_catalog_menu(store_id, partner_store_id, db)
            total_items = sum(
                len(c["items"])
                for s in menu_data.get("sections", [])
                for c in s.get("categories", [])
            )

            # Notify ShopeeFood S2S API that new menu is ready
            notify_resp = await shopeefood_client.notify_menu_sync(partner_restaurant_id=partner_store_id)

            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=True,
                total_synced=total_items,
                message=f"Đã chuẩn bị {total_items} món ăn và thông báo đồng bộ thực đơn tới ShopeeFood (Cửa hàng: {partner_store_id})",
                details={"notification_response": notify_resp},
            )
        except Exception as ex:
            logger.error(f"Lỗi khi đồng bộ menu ShopeeFood chi nhánh {store_id}: {str(ex)}")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=False,
                message=f"Lỗi đồng bộ menu ShopeeFood: {str(ex)}",
            )

    def get_cached_menu(self, store_code: str) -> Optional[Dict[str, Any]]:
        """Retrieve pre-built menu from cache for ShopeeFood GET /shopeefoodapi/{code}.json."""
        return _shopeefood_menu_cache.get(store_code)

    # ==========================================================================
    # 2. Inventory / Stock Synchronization
    # ==========================================================================

    async def sync_inventory(
        self,
        store_id: str,
        partner_store_id: str,
        stock_items: List[Dict[str, Any]],
        db: AsyncSession
    ) -> SyncResult:
        """Push dish status (AVAILABLE / OUT_OF_STOCK) to ShopeeFood if feature is enabled."""
        enabled = await channel_service.is_feature_enabled(self.channel_code, "stock_sync_enabled", db)
        if not enabled:
            logger.info(f"Đồng bộ tồn kho {self.channel_code} bị bỏ qua do tính năng đang TẮT.")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=False,
                message="Tính năng 'Đồng bộ tồn kho' cho ShopeeFood hiện đang TẮT trong cài đặt quản trị.",
            )

        try:
            dishes = []
            for item in stock_items:
                is_out = item.get("is_out_of_stock", False)
                dishes.append({
                    "partner_dish_id": str(item.get("sku") or item.get("product_id")),
                    "status": 2 if is_out else 1,  # 1: AVAILABLE, 2: OUT_OF_STOCK
                })

            response = await shopeefood_client.update_dish_status(
                partner_restaurant_id=partner_store_id,
                dishes=dishes,
            )

            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=True,
                total_synced=len(dishes),
                message=f"Đã cập nhật trạng thái tồn {len(dishes)} món trên ShopeeFood ({partner_store_id})",
                details={"api_response": response},
            )
        except Exception as ex:
            logger.error(f"Lỗi khi cập nhật tồn kho ShopeeFood: {str(ex)}")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=False,
                message=f"Lỗi cập nhật tồn kho ShopeeFood: {str(ex)}",
            )

    # ==========================================================================
    # 3. Inbound Webhook Handling (Orders & Status Updates)
    # ==========================================================================

    async def handle_order_webhook(
        self,
        headers: Dict[str, str],
        raw_body: bytes,
        db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Process incoming ShopeeFood order webhook.
        Checks if 'order_webhook_enabled' is active.
        If 'auto_confirm_enabled' is active, auto-transitions order to ACCEPTED and notifies Foody.
        """
        enabled = await channel_service.is_feature_enabled(self.channel_code, "order_webhook_enabled", db)
        if not enabled:
            logger.warning("Nhận webhook đơn hàng ShopeeFood nhưng tính năng 'Nhận đơn qua Webhook' đang TẮT.")
            return {
                "result": "failed",
                "error": "Tính năng nhận webhook đơn hàng ShopeeFood hiện đang tạm dừng bởi Quản trị viên."
            }

        payload = json.loads(raw_body.decode("utf-8"))
        partner_restaurant_id = str(
            payload.get("partner_restaurant_id") or payload.get("restaurant_id") or "10001"
        )
        channel_order_id = str(payload.get("order_id") or payload.get("order_code") or "")

        # Check auto-confirm toggle
        auto_confirm = await channel_service.is_feature_enabled(self.channel_code, "auto_confirm_enabled", db)
        initial_status = UnifiedOrderStatus.ACCEPTED if auto_confirm else UnifiedOrderStatus.PENDING

        customer_info = payload.get("customer") or {}
        driver_info = payload.get("driver") or {}

        order_data = {
            "display_order_id": payload.get("display_order_id") or (channel_order_id[-6:] if channel_order_id else None),
            "initial_status": initial_status,
            "subtotal_amount": float(payload.get("subtotal", 0.0)),
            "discount_amount": float(payload.get("discount", 0.0)),
            "delivery_fee": float(payload.get("shipping_fee", 0.0)),
            "total_amount": float(payload.get("total_amount", 0.0)),
            "customer_name": customer_info.get("name") if isinstance(customer_info, dict) else None,
            "customer_phone": customer_info.get("phone") if isinstance(customer_info, dict) else None,
            "delivery_address": customer_info.get("address") if isinstance(customer_info, dict) else None,
            "driver_name": driver_info.get("name") if isinstance(driver_info, dict) else None,
            "driver_phone": driver_info.get("phone") if isinstance(driver_info, dict) else None,
            "raw_payload": payload,
        }

        items_data = []
        for itm in payload.get("items", []):
            qty = int(itm.get("quantity", 1))
            unit_price = float(itm.get("price", 0.0))
            items_data.append({
                "sku": str(itm.get("partner_dish_id") or itm.get("sku") or itm.get("dish_id") or "UNKNOWN"),
                "item_name": itm.get("name") or itm.get("dish_name") or "Món ShopeeFood",
                "quantity": qty,
                "unit_price": unit_price,
                "total_price": unit_price * qty,
                "notes": itm.get("notes") or itm.get("note"),
            })

        order, created = await OrderService.create_or_get_inbound_order(
            channel_code=self.channel_code,
            partner_store_id=partner_restaurant_id,
            channel_order_id=channel_order_id,
            order_data=order_data,
            items_data=items_data,
            db=db,
        )

        # If auto-confirm is enabled, trigger outbound confirmation to ShopeeFood
        if auto_confirm and created:
            try:
                await shopeefood_client.update_order_status(
                    order_code=channel_order_id,
                    status=0,  # 0: CONFIRM
                    partner_restaurant_id=partner_restaurant_id,
                )
                logger.info(f"Đã tự động xác nhận đơn hàng ShopeeFood {channel_order_id} (Auto-Confirm ON)")
            except Exception as cf_ex:
                logger.warning(f"Lỗi khi gửi tự động xác nhận đơn {channel_order_id} tới ShopeeFood: {str(cf_ex)}")

        return {
            "result": "success",
            "reply": {
                "order_id": order.channel_order_id,
                "internal_order_code": order.order_code,
                "status": order.status.value,
                "auto_confirmed": auto_confirm,
                "created": created,
            }
        }

    # ==========================================================================
    # 4. Outbound Order Lifecycle Operations
    # ==========================================================================

    async def update_order_status(
        self,
        channel_order_id: str,
        partner_store_id: str,
        new_status: UnifiedOrderStatus,
        db: AsyncSession,
        reason: Optional[str] = None
    ) -> bool:
        """
        Send state transition from Nam An Merchant Portal to ShopeeFood S2S API.
        - ACCEPTED -> POST /s2s/order/update (status: 0 CONFIRM)
        - CANCELLED -> POST /s2s/order/update (status: 2 OUT_OF_SERVICE, reason_ids: [79])
        """
        try:
            if new_status == UnifiedOrderStatus.ACCEPTED:
                res = await shopeefood_client.update_order_status(
                    order_code=channel_order_id,
                    status=0,
                    partner_restaurant_id=partner_store_id,
                )
                return res.get("result") == "success"

            elif new_status == UnifiedOrderStatus.CANCELLED:
                res = await shopeefood_client.update_order_status(
                    order_code=channel_order_id,
                    status=2,  # OUT_OF_SERVICE
                    partner_restaurant_id=partner_store_id,
                    reason_ids=[79],  # Out of Stock
                    merchant_note=reason or "Hết hàng tại chi nhánh",
                )
                return res.get("result") == "success"

            # For other statuses like READY, PICKED_UP, DELIVERED
            return True
        except Exception as ex:
            logger.error(f"Lỗi khi cập nhật trạng thái đơn lên ShopeeFood: {str(ex)}")
            return False
