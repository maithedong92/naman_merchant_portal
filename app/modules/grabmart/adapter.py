import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.interfaces.channel_adapter import BaseChannelAdapter
from app.models.inventory import StoreInventory
from app.models.order import UnifiedOrderStatus
from app.models.product import Category, Product
from app.schemas.sync import SyncResult
from app.services.order_service import OrderService
from app.modules.grabmart.service import grabmart_client

logger = logging.getLogger("naman_portal.modules.grabmart.adapter")


class GrabMartChannelAdapter(BaseChannelAdapter):
    """Channel Adapter for GrabMart Partner POS API v1.1.3."""

    @property
    def channel_code(self) -> str:
        return "GRABMART"

    @property
    def display_name(self) -> str:
        return "GrabMart VN"

    async def sync_menu(
        self,
        store_id: str,
        partner_store_id: str,
        db: AsyncSession
    ) -> SyncResult:
        """Format catalog and push to GrabMart Partner POS API."""
        try:
            query = select(Product, StoreInventory).join(
                StoreInventory,
                (StoreInventory.product_id == Product.id) & (StoreInventory.store_id == store_id),
                isouter=True
            ).options(selectinload(Product.category)).where(Product.is_active == True)
            
            res = await db.execute(query)
            rows = res.all()

            category_map: Dict[str, Dict[str, Any]] = {}
            for product, inv in rows:
                cate_name = product.category.name if product.category else "General"
                cate_id = product.category.code if product.category else "general"

                if cate_id not in category_map:
                    category_map[cate_id] = {
                        "id": str(cate_id),
                        "name": cate_name,
                        "sequence": product.category.sequence if product.category else 0,
                        "items": []
                    }

                is_available = "AVAILABLE" if (inv and not inv.is_out_of_stock and inv.available_stock > 0) else "UNAVAILABLE"

                category_map[cate_id]["items"].append({
                    "id": str(product.sku),
                    "name": product.name,
                    "price": int(product.base_price),
                    "availableStatus": is_available,
                    "photos": [product.image_url] if product.image_url else []
                })

            categories_list = list(category_map.values())
            menu_payload = {
                "merchantID": partner_store_id,
                "currency": {"code": "VND", "symbol": "₫", "exponent": 0},
                "categories": categories_list
            }

            # Call GrabMart menu push API
            # /partner/v1/merchant/{merchantID}/menu
            response = await grabmart_client.call_api(
                method="POST",
                endpoint=f"/partner/v1/merchant/{partner_store_id}/menu",
                body=menu_payload
            )

            total_items = sum(len(c["items"]) for c in categories_list)
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=True,
                total_synced=total_items,
                message=f"Đã đồng bộ {total_items} sản phẩm lên GrabMart outlet {partner_store_id}",
                details={"api_response": response}
            )
        except Exception as ex:
            logger.error(f"Lỗi khi đồng bộ menu GrabMart chi nhánh {store_id}: {str(ex)}")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=False,
                message=f"Lỗi đồng bộ menu: {str(ex)}"
            )

    async def sync_inventory(
        self,
        store_id: str,
        partner_store_id: str,
        stock_items: List[Dict[str, Any]],
        db: AsyncSession
    ) -> SyncResult:
        """Batch update item availability on GrabMart."""
        try:
            # PUT /partner/v1/merchant/{merchantID}/menu/records
            items_payload = []
            for itm in stock_items:
                items_payload.append({
                    "itemID": str(itm["sku"]),
                    "status": "AVAILABLE" if not itm.get("is_out_of_stock", False) else "UNAVAILABLE"
                })

            response = await grabmart_client.call_api(
                method="PUT",
                endpoint=f"/partner/v1/merchant/{partner_store_id}/menu/records",
                body={"items": items_payload}
            )
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=True,
                total_synced=len(items_payload),
                message=f"Đã cập nhật tồn {len(items_payload)} sản phẩm lên GrabMart",
                details={"api_response": response}
            )
        except Exception as ex:
            logger.error(f"Lỗi khi cập nhật tồn kho GrabMart: {str(ex)}")
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
        """Ingest incoming GrabMart order webhook."""
        import json
        payload = json.loads(raw_body.decode("utf-8"))
        channel_order_id = str(payload.get("orderID", ""))
        partner_store_id = str(payload.get("partnerMerchantID") or payload.get("merchantID", ""))

        price_info = payload.get("price", {})
        subtotal = float(price_info.get("subtotal", 0.0))
        discount = float(price_info.get("merchantFundedPromo", 0.0))
        delivery = float(price_info.get("deliveryFee", 0.0))
        total = float(price_info.get("orderTotal", subtotal + delivery - discount))

        order_data = {
            "display_order_id": payload.get("shortOrderNumber"),
            "initial_status": UnifiedOrderStatus.PENDING,
            "subtotal_amount": subtotal,
            "discount_amount": discount,
            "delivery_fee": delivery,
            "total_amount": total,
            "customer_name": (payload.get("customer") or {}).get("name"),
            "customer_phone": (payload.get("customer") or {}).get("phone"),
            "delivery_address": (payload.get("delivery") or {}).get("address"),
            "driver_name": (payload.get("driver") or {}).get("name"),
            "driver_phone": (payload.get("driver") or {}).get("phone"),
            "raw_payload": payload
        }

        items_data = []
        for itm in payload.get("items", []):
            items_data.append({
                "sku": itm.get("id") or itm.get("itemID", "UNKNOWN"),
                "item_name": itm.get("name", "GrabMart Product"),
                "quantity": int(itm.get("quantity", 1)),
                "unit_price": float(itm.get("price", 0.0)),
                "total_price": float(itm.get("price", 0.0)) * int(itm.get("quantity", 1)),
                "notes": itm.get("instructions")
            })

        order, created = await OrderService.create_or_get_inbound_order(
            channel_code=self.channel_code,
            partner_store_id=partner_store_id,
            channel_order_id=channel_order_id,
            order_data=order_data,
            items_data=items_data,
            db=db
        )

        return {
            "status": "ACCEPTED",
            "orderID": order.channel_order_id,
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
        """Send state update to GrabMart API."""
        try:
            # GrabMart state transition endpoints:
            # Mark ready: POST /partner/v1/order/ready
            # Cancel: PUT /partner/v1/order/cancel
            if new_status == UnifiedOrderStatus.READY:
                endpoint = "/partner/v1/order/ready"
                await grabmart_client.call_api("POST", endpoint, body={"orderID": channel_order_id})
            elif new_status == UnifiedOrderStatus.CANCELLED:
                endpoint = "/partner/v1/order/cancel"
                await grabmart_client.call_api("PUT", endpoint, body={"orderID": channel_order_id, "reason": reason or "Out of stock"})
            return True
        except Exception as ex:
            logger.error(f"Lỗi khi cập nhật trạng thái đơn lên GrabMart: {str(ex)}")
            return False
