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
from app.modules.grabmart.schemas import (
    GrabCategory,
    GrabCurrency,
    GrabDaySchedule,
    GrabMenuItem,
    GrabMartMenuPayload,
    GrabPushOrderStateWebhook,
    GrabSellingPeriod,
    GrabSellingTime,
    GrabServiceHours,
    GrabSubcategory,
    GrabSubmitOrderWebhook,
)
from app.modules.grabmart.service import grabmart_client

logger = logging.getLogger("naman_portal.modules.grabmart.adapter")

# In-memory store menu cache for GrabMart pull requests
_store_menu_cache: Dict[str, Dict[str, Any]] = {}


class GrabMartChannelAdapter(BaseChannelAdapter):
    """Channel Adapter for GrabMart Partner POS API v1.1.3."""

    @property
    def channel_code(self) -> str:
        return "GRABMART"

    @property
    def display_name(self) -> str:
        return "GrabMart VN"

    # ==========================================================================
    # 1. Menu & Catalog Synchronization (v1.1.3)
    # ==========================================================================

    async def build_catalog_menu(
        self,
        store_id: str,
        partner_store_id: str,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """
        Build GrabMart Menu v1.1.3 catalog payload from local database.
        Includes sellingTimes, categories, subcategories, items, prices in VND.
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

        # Build category map: category -> subcategory -> items
        category_map: Dict[str, Dict[str, Any]] = {}

        for product, inv in rows:
            cat_name = product.category.name if product.category else "Bách Hóa Tổng Hợp"
            cat_id = product.category.code if product.category else "general"

            if cat_id not in category_map:
                category_map[cat_id] = {
                    "id": str(cat_id),
                    "name": cat_name,
                    "sequence": product.category.sequence if product.category else 0,
                    "subcategories": {
                        "sub_default": {
                            "id": f"{cat_id}_sub",
                            "name": cat_name,
                            "sequence": 0,
                            "items": [],
                        }
                    },
                }

            is_available = (
                "AVAILABLE"
                if (inv and not inv.is_out_of_stock and inv.available_stock > 0)
                else "UNAVAILABLE"
            )
            stock_qty = inv.available_stock if (inv and is_available == "AVAILABLE") else 0

            item_dict = GrabMenuItem(
                id=str(product.sku),
                name=product.name,
                price=int(product.base_price),
                availableStatus=is_available,
                maxStock=max(0, stock_qty),
                photos=[product.image_url] if product.image_url else [],
                barcodes=[product.barcode] if product.barcode else [],
                description=product.description or "",
                sellingTimeID="standard_schedule",
            ).model_dump()

            category_map[cat_id]["subcategories"]["sub_default"]["items"].append(item_dict)

        # Assemble Categories
        categories_output: List[GrabCategory] = []
        for cat in category_map.values():
            sub_list: List[GrabSubcategory] = []
            for sub in cat["subcategories"].values():
                sub_list.append(
                    GrabSubcategory(
                        id=sub["id"],
                        name=sub["name"],
                        sequence=sub["sequence"],
                        items=[GrabMenuItem(**itm) for itm in sub["items"]],
                    )
                )
            categories_output.append(
                GrabCategory(
                    id=cat["id"],
                    name=cat["name"],
                    sequence=cat["sequence"],
                    subcategories=sub_list,
                )
            )

        # Define Standard Selling Time (06:00 to 22:00 local time daily)
        service_hours = GrabServiceHours(
            mon=GrabDaySchedule(openPeriodType="OpenPeriod", periods=[GrabSellingPeriod()]),
            tue=GrabDaySchedule(openPeriodType="OpenPeriod", periods=[GrabSellingPeriod()]),
            wed=GrabDaySchedule(openPeriodType="OpenPeriod", periods=[GrabSellingPeriod()]),
            thu=GrabDaySchedule(openPeriodType="OpenPeriod", periods=[GrabSellingPeriod()]),
            fri=GrabDaySchedule(openPeriodType="OpenPeriod", periods=[GrabSellingPeriod()]),
            sat=GrabDaySchedule(openPeriodType="OpenPeriod", periods=[GrabSellingPeriod()]),
            sun=GrabDaySchedule(openPeriodType="OpenPeriod", periods=[GrabSellingPeriod()]),
        )
        selling_times = [
            GrabSellingTime(
                id="standard_schedule",
                name="Nam An Daily Schedule",
                startTime="2024-01-01 00:00:00",
                endTime="2030-12-31 23:59:59",
                serviceHours=service_hours,
            )
        ]

        payload = GrabMartMenuPayload(
            merchantID=partner_store_id,
            partnerMerchantID=store_id,
            currency=GrabCurrency(code="VND", symbol="₫", exponent=0),
            sellingTimes=selling_times,
            categories=categories_output,
        )

        menu_dict = payload.model_dump()
        _store_menu_cache[partner_store_id] = menu_dict
        return menu_dict

    async def sync_menu(
        self,
        store_id: str,
        partner_store_id: str,
        db: AsyncSession,
    ) -> SyncResult:
        """
        Generate GrabMart v1.1.3 menu and notify GrabMart to pull/update menu.
        Endpoint: POST /partner/v1/merchant/menu/notification
        """
        enabled = await channel_service.is_feature_enabled(self.channel_code, "menu_sync_enabled", db)
        if not enabled:
            logger.info(f"Đồng bộ thực đơn {self.channel_code} bị bỏ qua do tính năng đang TẮT.")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=False,
                message="Tính năng 'Đồng bộ thực đơn' cho GrabMart hiện đang TẮT trong cài đặt quản trị.",
            )

        try:
            menu_data = await self.build_catalog_menu(store_id, partner_store_id, db)
            total_items = sum(
                len(sub.items)
                for cat in menu_data.get("categories", [])
                for sub in cat.get("subcategories", [])
            )

            # Notify GrabMart that new menu is ready
            notify_resp = await grabmart_client.notify_menu_update(merchant_id=partner_store_id)

            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=True,
                total_synced=total_items,
                message=f"Đã chuẩn bị {total_items} sản phẩm và thông báo cập nhật menu tới GrabMart (Outlet: {partner_store_id})",
                details={"notification_response": notify_resp},
            )
        except Exception as ex:
            logger.error(f"Lỗi khi đồng bộ menu GrabMart (Store {store_id}): {str(ex)}")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=False,
                message=f"Lỗi đồng bộ menu GrabMart: {str(ex)}",
            )

    def get_cached_menu(self, merchant_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve pre-built menu from cache for GrabMart GET webhook."""
        return _store_menu_cache.get(merchant_id)

    # ==========================================================================
    # 2. Inventory / Stock Synchronization
    # ==========================================================================

    async def sync_inventory(
        self,
        store_id: str,
        partner_store_id: str,
        stock_items: List[Dict[str, Any]],
        db: AsyncSession,
    ) -> SyncResult:
        """
        Batch update item stock and availability status on GrabMart.
        Conforms to GrabMart rule: maxStock must be 0 if UNAVAILABLE, > 0 if AVAILABLE.
        """
        enabled = await channel_service.is_feature_enabled(self.channel_code, "stock_sync_enabled", db)
        if not enabled:
            logger.info(f"Đồng bộ tồn kho {self.channel_code} bị bỏ qua do tính năng đang TẮT.")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=False,
                message="Tính năng 'Đồng bộ tồn kho' cho GrabMart hiện đang TẮT trong cài đặt quản trị.",
            )

        try:
            items_payload = []
            for itm in stock_items:
                is_out = itm.get("is_out_of_stock", False)
                stock_qty = int(itm.get("available_stock", 10))
                items_payload.append({
                    "id": str(itm.get("sku") or itm.get("product_id")),
                    "price": int(itm.get("price")) if itm.get("price") else None,
                    "availableStatus": "UNAVAILABLE" if is_out or stock_qty <= 0 else "AVAILABLE",
                    "maxStock": 0 if is_out or stock_qty <= 0 else stock_qty,
                })

            response = await grabmart_client.batch_update_items(
                merchant_id=partner_store_id,
                items=items_payload,
            )

            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=True,
                total_synced=len(items_payload),
                message=f"Đã cập nhật tồn kho {len(items_payload)} sản phẩm lên GrabMart ({partner_store_id})",
                details={"api_response": response},
            )
        except Exception as ex:
            logger.error(f"Lỗi khi cập nhật tồn kho GrabMart: {str(ex)}")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=False,
                message=f"Lỗi cập nhật tồn kho GrabMart: {str(ex)}",
            )

    # ==========================================================================
    # 3. Inbound Webhook Handling (Submit Order & Order State)
    # ==========================================================================

    async def handle_order_webhook(
        self,
        headers: Dict[str, str],
        raw_body: bytes,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """
        Process GrabMart Submit Order Webhook.
        Converts GrabMart schema into UnifiedOrder and stores in PostgreSQL.
        """
        enabled = await channel_service.is_feature_enabled(self.channel_code, "order_webhook_enabled", db)
        if not enabled:
            logger.warning("Nhận webhook đơn hàng GrabMart nhưng tính năng 'Nhận đơn qua Webhook' đang TẮT.")
            return {
                "status": "REJECTED",
                "message": "Tính năng nhận webhook đơn hàng GrabMart hiện đang tạm dừng bởi Quản trị viên."
            }

        payload_dict = json.loads(raw_body.decode("utf-8"))
        webhook = GrabSubmitOrderWebhook(**payload_dict)

        channel_order_id = webhook.orderID
        partner_store_id = webhook.partnerMerchantID or webhook.merchantID

        receiver = webhook.receiver or {}
        address_info = receiver.address if isinstance(receiver, dict) else (receiver.address if hasattr(receiver, "address") else None)
        
        full_address = None
        if address_info:
            full_address = address_info.address if hasattr(address_info, "address") else address_info.get("address")
            instruction = address_info.deliveryInstruction if hasattr(address_info, "deliveryInstruction") else address_info.get("deliveryInstruction")
            if instruction:
                full_address = f"{full_address} (Ghi chú giao: {instruction})"

        auto_confirm = await channel_service.is_feature_enabled(self.channel_code, "auto_confirm_enabled", db)
        is_auto = auto_confirm or (webhook.featureFlags and webhook.featureFlags.orderAcceptedType == "AUTO")
        initial_status = UnifiedOrderStatus.ACCEPTED if is_auto else UnifiedOrderStatus.PENDING

        order_data = {
            "display_order_id": webhook.shortOrderNumber or channel_order_id[-6:],
            "initial_status": initial_status,
            "subtotal_amount": float(webhook.price.subtotal),
            "discount_amount": float(webhook.price.merchantFundPromo),
            "delivery_fee": float(webhook.price.deliveryFee),
            "total_amount": float(webhook.price.total),
            "customer_name": receiver.name if hasattr(receiver, "name") else receiver.get("name"),
            "customer_phone": receiver.phones if hasattr(receiver, "phones") else receiver.get("phones"),
            "delivery_address": full_address,
            "raw_payload": payload_dict,
        }

        items_data = []
        for itm in webhook.items:
            unit_price = float(itm.price)
            qty = itm.quantity
            items_data.append({
                "sku": itm.id,
                "item_name": itm.name or f"GrabMart Product {itm.id}",
                "quantity": qty,
                "unit_price": unit_price,
                "total_price": unit_price * qty,
                "notes": itm.specifications,
            })

        order, created = await OrderService.create_or_get_inbound_order(
            channel_code=self.channel_code,
            partner_store_id=partner_store_id,
            channel_order_id=channel_order_id,
            order_data=order_data,
            items_data=items_data,
            db=db,
        )

        return {
            "status": "ACCEPTED",
            "orderID": order.channel_order_id,
            "shortOrderNumber": webhook.shortOrderNumber,
            "internal_order_code": order.order_code,
            "created": created,
        }

    async def handle_order_state_webhook(
        self,
        headers: Dict[str, str],
        raw_body: bytes,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """
        Process GrabMart Push Order State Webhook.
        Maps Grab states (DRIVER_ALLOCATED, COLLECTED, DELIVERED, CANCELLED) to UnifiedOrderStatus.
        """
        payload_dict = json.loads(raw_body.decode("utf-8"))
        state_webhook = GrabPushOrderStateWebhook(**payload_dict)

        # 1. Resolve Channel
        channel_stmt = select(Channel).where(Channel.code == self.channel_code)
        channel_res = await db.execute(channel_stmt)
        channel = channel_res.scalar_one_or_none()
        if not channel:
            return {"status": "ERROR", "message": "GrabMart channel not registered"}

        # 2. Find Order by channel_order_id
        order_stmt = select(UnifiedOrder).where(
            UnifiedOrder.channel_id == channel.id,
            UnifiedOrder.channel_order_id == state_webhook.orderID,
        )
        order_res = await db.execute(order_stmt)
        order = order_res.scalar_one_or_none()

        if not order:
            logger.warning(f"GrabMart state update received for unknown order: {state_webhook.orderID}")
            return {"status": "ORDER_NOT_FOUND", "orderID": state_webhook.orderID}

        # 3. Map GrabMart state to UnifiedOrderStatus
        state_map = {
            "ACCEPTED": UnifiedOrderStatus.ACCEPTED,
            "DRIVER_ALLOCATED": order.status,  # Order remains in current status; track driver ETA
            "DRIVER_ARRIVED": order.status,
            "COLLECTED": UnifiedOrderStatus.PICKED_UP,
            "DELIVERED": UnifiedOrderStatus.DELIVERED,
            "CANCELLED": UnifiedOrderStatus.CANCELLED,
            "FAILED": UnifiedOrderStatus.CANCELLED,
        }
        target_status = state_map.get(state_webhook.state, order.status)
        reason_note = state_webhook.message or state_webhook.code or f"GrabMart state: {state_webhook.state}"

        if state_webhook.driverETA:
            reason_note += f" (Tài xế đến trong {state_webhook.driverETA}s)"

        if target_status != order.status:
            await OrderService.update_order_status(
                order_id=order.id,
                payload=OrderStatusUpdateSchema(
                    new_status=target_status,
                    note=reason_note,
                    changed_by="GRABMART_WEBHOOK",
                    cancellation_reason=reason_note if target_status == UnifiedOrderStatus.CANCELLED else None,
                ),
                db=db,
                sync_to_channel=False,  # Already from channel
            )

        return {
            "status": "OK",
            "orderID": state_webhook.orderID,
            "internal_order_code": order.order_code,
            "state": state_webhook.state,
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
        reason: Optional[str] = None,
    ) -> bool:
        """
        Send state transition from Nam An Merchant Portal to GrabMart.
        - ACCEPTED -> POST /partner/v1/order/prepare (Accepted)
        - READY -> POST /partner/v1/order/ready (markStatus: 1)
        - CANCELLED -> POST /partner/v1/order/cancel (cancelCode: 1001)
        """
        try:
            if new_status == UnifiedOrderStatus.ACCEPTED:
                await grabmart_client.accept_or_reject_order(channel_order_id, to_state="Accepted")
            elif new_status == UnifiedOrderStatus.READY:
                await grabmart_client.mark_order_ready(channel_order_id)
            elif new_status == UnifiedOrderStatus.CANCELLED:
                await grabmart_client.cancel_order(
                    order_id=channel_order_id,
                    merchant_id=partner_store_id,
                    cancel_code=1001,
                )
            return True
        except Exception as ex:
            logger.error(f"Lỗi khi gửi cập nhật trạng thái lên GrabMart: {str(ex)}")
            return False

