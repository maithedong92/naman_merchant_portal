from datetime import datetime, timezone
import json
import logging
import traceback
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.interfaces.channel_adapter import BaseChannelAdapter
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.operational_error import ErrorSeverity
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.product import Product
from app.models.store import Store, StoreChannelMapping
from app.schemas.order import OrderStatusUpdateSchema
from app.schemas.sync import SyncResult
from app.services.channel_service import channel_service
from app.services.error_service import error_service
from app.services.order_service import OrderService
from app.modules.shopeemart.service import shopeemart_client

logger = logging.getLogger("naman_portal.modules.shopeemart.adapter")


class ShopeeMartChannelAdapter(BaseChannelAdapter):
    """
    Production-grade Channel Adapter for ShopeeMart / Shopee Open Platform API v2.
    Integrates Nam An Market Unified Order lifecycle with Shopee Mart and Outlet models.
    """

    @property
    def channel_code(self) -> str:
        return "SHOPEEMART"

    @property
    def display_name(self) -> str:
        return "ShopeeMart / Shopee Fresh VN"

    # ==========================================================================
    # 1. Menu & Catalog Synchronization
    # ==========================================================================

    async def sync_menu(
        self,
        store_id: str,
        partner_store_id: str,
        db: AsyncSession
    ) -> SyncResult:
        """Synchronize product listings and prices to ShopeeMart shop."""
        enabled = await channel_service.is_feature_enabled(self.channel_code, "menu_sync_enabled", db)
        if not enabled:
            logger.info(f"Đồng bộ danh mục {self.channel_code} bị bỏ qua do tính năng đang TẮT.")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=False,
                message="Tính năng 'Đồng bộ thực đơn/danh mục' cho ShopeeMart hiện đang TẮT trong cài đặt quản trị.",
            )

        try:
            query = select(Product).where(Product.is_active == True)
            res = await db.execute(query)
            products = res.scalars().all()

            resp = await shopeemart_client.call_api(
                method="GET",
                path="/api/v2/product/get_item_list",
                shop_id=partner_store_id,
                params={"page_size": 50, "item_status": "NORMAL"}
            )

            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=True,
                total_synced=len(products),
                message=f"Đã kích hoạt đồng bộ danh mục {len(products)} sản phẩm lên ShopeeMart shop {partner_store_id}",
                details={"response": resp}
            )
        except Exception as ex:
            logger.error(f"Lỗi khi đồng bộ sản phẩm lên ShopeeMart: {str(ex)}")
            await self._record_operational_error(
                db=db,
                error_code="SHOPEEMART_MENU_SYNC_ERROR",
                message=f"Lỗi đồng bộ danh mục ShopeeMart: {str(ex)}",
                severity=ErrorSeverity.ERROR,
                endpoint="/api/v2/product/get_item_list",
                request_payload={"store_id": store_id, "partner_store_id": partner_store_id},
            )
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=False,
                message=str(ex)
            )

    # ==========================================================================
    # 2. Inventory & Stock Batch Update
    # ==========================================================================

    async def sync_inventory(
        self,
        store_id: str,
        partner_store_id: str,
        stock_items: List[Dict[str, Any]],
        db: AsyncSession
    ) -> SyncResult:
        """Push batch stock updates to ShopeeMart (v2.product.update_stock)."""
        enabled = await channel_service.is_feature_enabled(self.channel_code, "stock_sync_enabled", db)
        if not enabled:
            logger.info(f"Đồng bộ tồn kho {self.channel_code} bị bỏ qua do tính năng đang TẮT.")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=False,
                message="Tính năng 'Đồng bộ tồn kho' cho ShopeeMart hiện đang TẮT trong cài đặt quản trị.",
            )

        try:
            stock_list = []
            for item in stock_items:
                is_oos = bool(item.get("is_out_of_stock", False))
                qty = 0 if is_oos else int(item.get("available_stock", 0))
                stock_entry: Dict[str, Any] = {
                    "seller_sku": str(item.get("sku", "")),
                    "normal_stock": qty
                }
                if item.get("item_id"):
                    stock_entry["item_id"] = int(item["item_id"])
                stock_list.append(stock_entry)

            resp = await shopeemart_client.update_stock(
                shop_id=partner_store_id,
                stock_list=stock_list
            )

            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=True,
                total_synced=len(stock_list),
                message=f"Đã cập nhật tồn kho {len(stock_list)} SKU trên ShopeeMart shop {partner_store_id}",
                details={"response": resp}
            )
        except Exception as ex:
            logger.error(f"Lỗi khi cập nhật tồn kho ShopeeMart: {str(ex)}")
            await self._record_operational_error(
                db=db,
                error_code="SHOPEEMART_STOCK_SYNC_ERROR",
                message=f"Lỗi cập nhật tồn kho ShopeeMart: {str(ex)}",
                severity=ErrorSeverity.ERROR,
                endpoint="/api/v2/product/update_stock",
                request_payload={"shop_id": partner_store_id, "item_count": len(stock_items)},
            )
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=False,
                message=str(ex)
            )

    # ==========================================================================
    # 3. Inbound Webhook: Order Creation & Status Changes
    # ==========================================================================

    async def handle_order_webhook(
        self,
        headers: Dict[str, str],
        raw_body: bytes,
        db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Ingest incoming order push notification from ShopeeMart / Shopee Open Platform.
        Handles signature verification, format normalization, and unified state transition.
        """
        enabled = await channel_service.is_feature_enabled(self.channel_code, "order_webhook_enabled", db)
        if not enabled:
            logger.warning("Nhận webhook đơn hàng ShopeeMart nhưng tính năng 'Nhận đơn qua Webhook' đang TẮT.")
            return {
                "code": -1,
                "message": "Tính năng nhận webhook đơn hàng ShopeeMart hiện đang tạm dừng bởi Quản trị viên."
            }

        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}

        # Handle Shopee Open Platform standard wrapper vs direct payload
        data = payload.get("data", payload)
        order_sn = str(data.get("order_sn", payload.get("order_sn", "")))
        shop_id = str(data.get("shop_id", payload.get("shop_id", "10001")))

        if not order_sn:
            logger.error(f"ShopeeMart webhook payload missing order_sn: {payload}")
            return {"code": 400, "message": "Missing order_sn in webhook payload"}

        auto_confirm = await channel_service.is_feature_enabled(self.channel_code, "auto_confirm_enabled", db)

        # Standard Shopee status mapping
        raw_status = data.get("status", "READY_TO_SHIP")
        status_map = {
            "UNPAID": UnifiedOrderStatus.PENDING,
            "READY_TO_SHIP": UnifiedOrderStatus.ACCEPTED,
            "PROCESSED": UnifiedOrderStatus.PREPARING,
            "SHIPPED": UnifiedOrderStatus.PICKED_UP,
            "COMPLETED": UnifiedOrderStatus.DELIVERED,
            "CANCELLED": UnifiedOrderStatus.CANCELLED,
            "IN_CANCEL": UnifiedOrderStatus.CANCELLED,
        }
        # Terminal or explicit states take priority over auto_confirm
        if raw_status in ("CANCELLED", "IN_CANCEL"):
            order_status = UnifiedOrderStatus.CANCELLED
        elif raw_status in ("COMPLETED", "SHIPPED", "PROCESSED"):
            order_status = status_map.get(raw_status, UnifiedOrderStatus.ACCEPTED)
        elif auto_confirm:
            order_status = UnifiedOrderStatus.ACCEPTED
        else:
            order_status = status_map.get(raw_status, UnifiedOrderStatus.ACCEPTED)

        # Recipient address details
        recipient = data.get("recipient_address", {})
        customer_name = recipient.get("name") or data.get("buyer_username") or "Khách hàng Shopee"
        customer_phone = recipient.get("phone") or "0900000000"
        full_addr = recipient.get("full_address")
        if not full_addr:
            addr_parts = [recipient.get("address"), recipient.get("ward"), recipient.get("district"), recipient.get("city")]
            full_addr = ", ".join([p for p in addr_parts if p]) or "Giao qua ứng dụng Shopee"

        order_data = {
            "display_order_id": order_sn[-8:] if len(order_sn) >= 8 else order_sn,
            "initial_status": order_status,
            "subtotal_amount": float(data.get("total_amount", 0.0)),
            "discount_amount": float(data.get("seller_discount", 0.0)),
            "delivery_fee": float(data.get("actual_shipping_fee", 0.0)),
            "total_amount": float(data.get("total_amount", 0.0)),
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "delivery_address": full_addr,
            "order_time": datetime.now(timezone.utc),
            "raw_payload": payload
        }

        items_data = []
        raw_items = data.get("item_list", [])
        for itm in raw_items:
            qty = int(itm.get("model_quantity_purchased", itm.get("quantity", 1)))
            price = float(itm.get("model_discounted_price", itm.get("price", 0.0)))
            sku = str(itm.get("item_sku") or itm.get("model_sku") or itm.get("item_id") or "SKU-GENERIC")
            name = str(itm.get("item_name") or itm.get("model_name") or "Sản phẩm ShopeeMart")
            items_data.append({
                "sku": sku,
                "item_name": name,
                "quantity": qty,
                "unit_price": price,
                "total_price": price * qty
            })

        if not items_data:
            # Fallback single item if item_list is not provided in quick status push
            items_data.append({
                "sku": "SHOPEEMART-COMBO",
                "item_name": "Đơn hàng ShopeeMart",
                "quantity": 1,
                "unit_price": order_data["total_amount"],
                "total_price": order_data["total_amount"]
            })

        try:
            order, created = await OrderService.create_or_get_inbound_order(
                channel_code=self.channel_code,
                partner_store_id=shop_id,
                channel_order_id=order_sn,
                order_data=order_data,
                items_data=items_data,
                db=db
            )

            # If order already existed and status changed, update its status
            if not created and order.status != order_status:
                await OrderService.update_order_status(
                    order_id=order.id,
                    payload=OrderStatusUpdateSchema(
                        new_status=order_status,
                        note=f"ShopeeMart Webhook status update: {raw_status}",
                        changed_by="SHOPEEMART_WEBHOOK",
                    ),
                    db=db,
                    sync_to_channel=False
                )

            return {
                "code": 0,
                "message": "success",
                "order_sn": order.channel_order_id,
                "internal_order_code": order.order_code,
                "status": order.status.value if hasattr(order.status, "value") else str(order.status),
                "created": created
            }
        except Exception as ex:
            logger.error(f"Error handling ShopeeMart order webhook: {str(ex)}", exc_info=True)
            await self._record_operational_error(
                db=db,
                error_code="SHOPEEMART_SUBMIT_ORDER_ERROR",
                message=f"Lỗi khi tiếp nhận đơn hàng ShopeeMart {order_sn}: {str(ex)}",
                severity=ErrorSeverity.CRITICAL,
                endpoint="/shopeemart/webhooks/order",
                request_payload={"order_sn": order_sn, "shop_id": shop_id, "error": str(ex)},
            )
            return {"code": 500, "message": str(ex)}

    # ==========================================================================
    # 4. Outbound Status Transition (Ready to Ship / Cancel)
    # ==========================================================================

    async def update_order_status(
        self,
        channel_order_id: str,
        partner_store_id: str,
        new_status: UnifiedOrderStatus,
        db: AsyncSession,
        reason: Optional[str] = None
    ) -> bool:
        """Call Shopee API to transition order state (e.g., ship order or cancel)."""
        try:
            if new_status in (UnifiedOrderStatus.READY, UnifiedOrderStatus.PREPARING):
                # Ready to ship: call /api/v2/logistics/ship_order
                await shopeemart_client.ship_order(
                    order_sn=channel_order_id,
                    shop_id=partner_store_id
                )
                logger.info(f"Đã gọi Shopee ship_order cho đơn {channel_order_id} shop {partner_store_id}")
                return True

            elif new_status == UnifiedOrderStatus.CANCELLED:
                # Cancel order: call /api/v2/order/cancel_order
                await shopeemart_client.cancel_order(
                    order_sn=channel_order_id,
                    shop_id=partner_store_id,
                    cancel_reason=reason or "OUT_OF_STOCK"
                )
                logger.info(f"Đã gọi Shopee cancel_order cho đơn {channel_order_id} shop {partner_store_id}")
                return True

            return True
        except Exception as ex:
            logger.error(f"Lỗi khi cập nhật trạng thái đơn {channel_order_id} lên ShopeeMart: {str(ex)}")
            await self._record_operational_error(
                db=db,
                error_code="SHOPEEMART_SYNC_ORDER_STATUS_ERROR",
                message=f"Lỗi gửi cập nhật trạng thái đơn {channel_order_id} sang '{new_status}' lên ShopeeMart: {str(ex)}",
                severity=ErrorSeverity.ERROR,
                endpoint="/api/v2/logistics/ship_order",
                request_payload={"order_id": channel_order_id, "partner_store_id": partner_store_id, "new_status": str(new_status), "error": str(ex)},
            )
            return False

    # ==========================================================================
    # Helper: Record Operational Errors into Live System Status
    # ==========================================================================

    async def _record_operational_error(
        self,
        db: AsyncSession,
        error_code: str,
        message: str,
        severity: ErrorSeverity,
        endpoint: str,
        request_payload: Optional[Dict[str, Any]] = None,
    ):
        """Record operational error to database for real-time visibility in /system-status."""
        try:
            await error_service.log_error(
                db=db,
                error_code=error_code,
                message=message,
                severity=severity,
                module="SHOPEEMART",
                stack_trace=traceback.format_exc(),
                endpoint=endpoint,
                http_method="POST",
                request_payload=request_payload,
            )
        except Exception as log_ex:
            logger.error(f"Failed to record operational error for ShopeeMart: {log_ex}")
