from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.interfaces.channel_adapter import BaseChannelAdapter
from app.models.inventory import StoreInventory
from app.models.order import UnifiedOrderStatus
from app.models.product import Product
from app.schemas.sync import SyncResult
from app.services.order_service import OrderService
from app.modules.shopeemart.service import shopeemart_client

logger = logging.getLogger("naman_portal.modules.shopeemart.adapter")


class ShopeeMartChannelAdapter(BaseChannelAdapter):
    """Channel Adapter for ShopeeMart / Shopee Open Platform API v2."""

    @property
    def channel_code(self) -> str:
        return "SHOPEEMART"

    @property
    def display_name(self) -> str:
        return "ShopeeMart / Shopee Fresh VN"

    async def sync_menu(
        self,
        store_id: str,
        partner_store_id: str,
        db: AsyncSession
    ) -> SyncResult:
        """Synchronize product listings and prices to ShopeeMart shop."""
        try:
            query = select(Product).where(Product.is_active == True)
            res = await db.execute(query)
            products = res.scalars().all()

            # Call Shopee Open API to update items in bulk
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
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=False,
                message=str(ex)
            )

    async def sync_inventory(
        self,
        store_id: str,
        partner_store_id: str,
        stock_items: List[Dict[str, Any]],
        db: AsyncSession
    ) -> SyncResult:
        """Push batch stock updates to ShopeeMart (v2.product.update_stock)."""
        try:
            stock_list = []
            for item in stock_items:
                stock_list.append({
                    "seller_sku": str(item["sku"]),
                    "normal_stock": int(item.get("available_stock", 0)) if not item.get("is_out_of_stock", False) else 0
                })

            resp = await shopeemart_client.call_api(
                method="POST",
                path="/api/v2/product/update_stock",
                shop_id=partner_store_id,
                body={"stock_list": stock_list}
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
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=False,
                message=str(ex)
            )

    async def handle_order_webhook(
        self,
        headers: Dict[str, str],
        raw_body: bytes,
        db: AsyncSession
    ) -> Dict[str, Any]:
        """Ingest incoming order push notification from ShopeeMart."""
        payload = json.loads(raw_body.decode("utf-8"))
        data = payload.get("data", payload)
        order_sn = str(data.get("order_sn", ""))
        shop_id = str(data.get("shop_id", ""))

        # Map Shopee order status
        raw_status = data.get("status", "READY_TO_SHIP")
        status_map = {
            "UNPAID": UnifiedOrderStatus.PENDING,
            "READY_TO_SHIP": UnifiedOrderStatus.ACCEPTED,
            "PROCESSED": UnifiedOrderStatus.PREPARING,
            "SHIPPED": UnifiedOrderStatus.PICKED_UP,
            "COMPLETED": UnifiedOrderStatus.DELIVERED,
            "CANCELLED": UnifiedOrderStatus.CANCELLED,
        }
        order_status = status_map.get(raw_status, UnifiedOrderStatus.ACCEPTED)

        recipient = data.get("recipient_address", {})
        customer_name = recipient.get("name")
        customer_phone = recipient.get("phone")
        address = recipient.get("full_address")

        order_data = {
            "display_order_id": order_sn[-6:],
            "initial_status": order_status,
            "subtotal_amount": float(data.get("total_amount", 0.0)),
            "discount_amount": 0.0,
            "delivery_fee": float(data.get("actual_shipping_fee", 0.0)),
            "total_amount": float(data.get("total_amount", 0.0)),
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "delivery_address": address,
            "order_time": datetime.now(timezone.utc),
            "raw_payload": payload
        }

        items_data = []
        for itm in data.get("item_list", []):
            qty = int(itm.get("model_quantity_purchased", 1))
            price = float(itm.get("model_discounted_price", 0.0))
            items_data.append({
                "sku": itm.get("item_sku") or str(itm.get("item_id")),
                "item_name": itm.get("item_name", "Shopee Item"),
                "quantity": qty,
                "unit_price": price,
                "total_price": price * qty
            })

        order, created = await OrderService.create_or_get_inbound_order(
            channel_code=self.channel_code,
            partner_store_id=shop_id,
            channel_order_id=order_sn,
            order_data=order_data,
            items_data=items_data,
            db=db
        )

        return {
            "code": 0,
            "message": "success",
            "order_sn": order.channel_order_id,
            "internal_order_code": order.order_code,
            "created": created
        }

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
            if new_status == UnifiedOrderStatus.PREPARING:
                # Ready to ship call /api/v2/logistics/ship_order
                await shopeemart_client.call_api(
                    method="POST",
                    path="/api/v2/logistics/ship_order",
                    shop_id=partner_store_id,
                    body={"order_sn": channel_order_id}
                )
            elif new_status == UnifiedOrderStatus.CANCELLED:
                # Cancel order call /api/v2/order/cancel_order
                await shopeemart_client.call_api(
                    method="POST",
                    path="/api/v2/order/cancel_order",
                    shop_id=partner_store_id,
                    body={"order_sn": channel_order_id, "cancel_reason": reason or "OUT_OF_STOCK"}
                )
            return True
        except Exception as ex:
            logger.error(f"Lỗi khi cập nhật trạng thái đơn lên ShopeeMart: {str(ex)}")
            return False
