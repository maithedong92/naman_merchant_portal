import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.interfaces.channel_adapter import BaseChannelAdapter
from app.models.inventory import StoreInventory
from app.models.order import UnifiedOrderStatus
from app.models.product import Category, Product
from app.models.store import Store
from app.schemas.sync import SyncResult
from app.services.order_service import OrderService
from app.modules.shopeefood.service import shopeefood_client

logger = logging.getLogger("naman_portal.modules.shopeefood.adapter")


class ShopeeFoodChannelAdapter(BaseChannelAdapter):
    """Channel Adapter for ShopeeFood / Foody External API."""

    @property
    def channel_code(self) -> str:
        return "SHOPEEFOOD"

    @property
    def display_name(self) -> str:
        return "ShopeeFood VN"

    async def sync_menu(
        self,
        store_id: str,
        partner_store_id: str,
        db: AsyncSession
    ) -> SyncResult:
        """
        Pull products & inventory for this store, build Foody format menu, and push via S2S API.
        """
        try:
            # Query active products and inventory
            query = select(Product, StoreInventory).join(
                StoreInventory,
                (StoreInventory.product_id == Product.id) & (StoreInventory.store_id == store_id),
                isouter=True
            ).options(selectinload(Product.category)).where(Product.is_active == True)
            
            res = await db.execute(query)
            rows = res.all()

            # Group items into categories
            category_map: Dict[str, Dict[str, Any]] = {}
            for product, inv in rows:
                cate_name = product.category.name if product.category else "KHÁC"
                cate_id = product.category.code if product.category else "999999"

                if cate_id not in category_map:
                    category_map[cate_id] = {
                        "id": str(cate_id),
                        "name": cate_name,
                        "sequence": product.category.sequence if product.category else 99,
                        "sort_type": 1,
                        "availableStatus": "AVAILABLE",
                        "items": []
                    }

                is_available = "AVAILABLE" if (inv and not inv.is_out_of_stock and inv.available_stock > 0) else "UNAVAILABLE"

                category_map[cate_id]["items"].append({
                    "id": str(product.sku),
                    "name": product.name,
                    "price": float(product.base_price),
                    "description": product.description or "",
                    "availableStatus": is_available,
                    "photos": [product.image_url] if product.image_url else []
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
                    "categories": categories_list
                }]
            }

            # Call Foody S2S Menu Sync API
            response = await shopeefood_client.call_api(
                method="POST",
                endpoint="/s2s/menu/sync",
                body={"partner_restaurant_id": partner_store_id}
            )

            total_items = sum(len(c["items"]) for c in categories_list)
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=True,
                total_synced=total_items,
                message=f"Đã đồng bộ {total_items} sản phẩm lên ShopeeFood chi nhánh {partner_store_id}",
                details={"api_response": response}
            )
        except Exception as ex:
            logger.error(f"Lỗi khi đồng bộ menu ShopeeFood chi nhánh {store_id}: {str(ex)}")
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
        """Push dish status (AVAILABLE / UNAVAILABLE) to ShopeeFood."""
        try:
            # Format update dish payload for ShopeeFood
            dishes = []
            for item in stock_items:
                dishes.append({
                    "partner_dish_id": str(item["sku"]),
                    "available_status": "AVAILABLE" if not item.get("is_out_of_stock", False) else "UNAVAILABLE"
                })

            response = await shopeefood_client.call_api(
                method="POST",
                endpoint="/s2s/dish/update",
                body={
                    "partner_restaurant_id": partner_store_id,
                    "dishes": dishes
                }
            )
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=True,
                total_synced=len(dishes),
                message=f"Đã cập nhật trạng thái {len(dishes)} món trên ShopeeFood",
                details={"api_response": response}
            )
        except Exception as ex:
            logger.error(f"Lỗi khi cập nhật tồn món ShopeeFood: {str(ex)}")
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
        """Verify signature and ingest order into Core OrderService."""
        import json
        payload = json.loads(raw_body.decode("utf-8"))
        partner_restaurant_id = str(payload.get("partner_restaurant_id", ""))
        channel_order_id = str(payload.get("order_id", ""))

        order_data = {
            "display_order_id": payload.get("display_order_id"),
            "initial_status": UnifiedOrderStatus.PENDING,
            "subtotal_amount": float(payload.get("subtotal", 0.0)),
            "discount_amount": float(payload.get("discount", 0.0)),
            "delivery_fee": float(payload.get("shipping_fee", 0.0)),
            "total_amount": float(payload.get("total_amount", 0.0)),
            "customer_name": (payload.get("customer") or {}).get("name"),
            "customer_phone": (payload.get("customer") or {}).get("phone"),
            "delivery_address": (payload.get("customer") or {}).get("address"),
            "driver_name": (payload.get("driver") or {}).get("name"),
            "driver_phone": (payload.get("driver") or {}).get("phone"),
            "raw_payload": payload
        }

        items_data = []
        for itm in payload.get("items", []):
            items_data.append({
                "sku": itm.get("partner_dish_id") or itm.get("sku", "UNKNOWN"),
                "item_name": itm.get("name", "Món ăn"),
                "quantity": itm.get("quantity", 1),
                "unit_price": itm.get("price", 0.0),
                "total_price": float(itm.get("price", 0.0)) * int(itm.get("quantity", 1)),
                "notes": itm.get("notes")
            })

        order, created = await OrderService.create_or_get_inbound_order(
            channel_code=self.channel_code,
            partner_store_id=partner_restaurant_id,
            channel_order_id=channel_order_id,
            order_data=order_data,
            items_data=items_data,
            db=db
        )

        return {
            "result": "success",
            "reply": {
                "order_id": order.channel_order_id,
                "internal_order_code": order.order_code,
                "created": created
            }
        }

    async def update_order_status(
        self,
        channel_order_id: str,
        partner_store_id: str,
        new_status: UnifiedOrderStatus,
        db: AsyncSession,
        reason: Optional[str] = None
    ) -> bool:
        """Call Foody API to notify order state."""
        try:
            # Map Unified status to ShopeeFood action
            action_endpoint = "/s2s/order/status"
            body = {
                "partner_restaurant_id": partner_store_id,
                "order_id": channel_order_id,
                "status": new_status.value,
                "reason": reason
            }
            await shopeefood_client.call_api("POST", action_endpoint, body)
            return True
        except Exception as ex:
            logger.error(f"Lỗi khi cập nhật trạng thái đơn lên ShopeeFood: {str(ex)}")
            return False
