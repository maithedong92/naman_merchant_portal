from collections import OrderedDict
import json
import logging
import re
import time
import traceback
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, selectinload

from app.core.exceptions import ValidationError
from app.interfaces.channel_adapter import BaseChannelAdapter
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.operational_error import ErrorSeverity
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.product import Category, Product
from app.models.store import Store, StoreChannelMapping
from app.schemas.order import OrderStatusUpdateSchema
from app.schemas.sync import SyncResult
from app.services.channel_service import channel_service
from app.services.error_service import error_service
from app.services.order_service import OrderService
from app.services.poppies_channel_push import (
    apply_channel_governance_to_stock_items,
    compute_channel_sale_price,
    load_channel_governance_for_products,
)
from app.modules.shopeefood.service import shopeefood_client

logger = logging.getLogger("naman_portal.modules.shopeefood.adapter")

# In-memory store menu cache for ShopeeFood pull requests (/shopeefoodapi/{store_code}.json)
_shopeefood_menu_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_shopeefood_menu_cache_ts: Dict[str, float] = {}
MENU_CACHE_TTL_SECONDS = 300.0
MAX_MENU_CACHE_ENTRIES = 50


def _normalize_menu_cache_key(store_code: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", str(store_code or ""))


def _extract_money_val(val: Any) -> float:
    """Extract float amount from either a scalar or Foody `{\"value\": ...}` money object."""
    if isinstance(val, dict):
        val = val.get("value")
    if val is None or isinstance(val, bool):
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _extract_s2s_items(dish_groups: Any) -> List[Dict[str, Any]]:
    """Flatten Foody `/s2s/order/get_details` nested `dish_groups[].dishes[]` into internal item dicts."""
    items: List[Dict[str, Any]] = []
    if not isinstance(dish_groups, list):
        return items
    for grp in dish_groups:
        if not isinstance(grp, dict):
            continue
        for dish in grp.get("dishes") or []:
            if not isinstance(dish, dict):
                continue
            qty = max(1, int(dish.get("quantity") or 1))
            unit_price = _extract_money_val(dish.get("price"))
            if unit_price <= 0:
                unit_price = _extract_money_val(dish.get("original_price"))
            total_price = _extract_money_val(dish.get("total"))
            items.append({
                "sku": str(dish.get("partner_dish_id") or dish.get("dish_id") or "UNKNOWN"),
                "item_name": dish.get("dish_name") or dish.get("name") or "Món ShopeeFood",
                "quantity": qty,
                "unit_price": unit_price,
                "total_price": total_price if total_price > 0 else unit_price * qty,
                "notes": dish.get("note") or dish.get("notes"),
            })
    return items


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
            .options(selectinload(Product.category), defer(Product.metadata_json))
            .where(Product.is_active == True)
        )
        res = await db.execute(query)
        rows = res.all()

        is_ch_active, has_partner_pricing, map_by_pid = await load_channel_governance_for_products(
            db=db,
            raw_channel_code="SHOPEEFOOD",
            product_ids=[str(r[0].id) for r in rows] if rows else None,
        )

        # Match GrabMart's curated online SKU selection & 2,800-item cap
        MAX_SHOPEEFOOD_MENU_ITEMS = 2800
        sorted_rows = sorted(
            rows,
            key=lambda r: (
                0 if (is_ch_active and (map_by_pid.get(str(r[0].id)).is_enabled if map_by_pid.get(str(r[0].id)) is not None else True)) else 1,
                0 if (r[0].image_url or "").strip() else 1,
                0 if (r[1] and not r[1].is_out_of_stock and float(r[1].available_stock or 0) >= 1) else 1,
                str(r[0].sku),
            ),
        )

        # Group items into categories
        category_map: Dict[str, Dict[str, Any]] = {}
        total_added = 0
        for product, inv in sorted_rows:
            if total_added >= MAX_SHOPEEFOOD_MENU_ITEMS:
                break

            ch_map = map_by_pid.get(str(product.id))
            erp_price = (
                float(ch_map.custom_price)
                if (
                    has_partner_pricing
                    and ch_map is not None
                    and ch_map.custom_price is not None
                    and float(ch_map.custom_price) > 0
                )
                else float(product.base_price or 0)
            )
            item_price = compute_channel_sale_price(
                erp_price, getattr(product, "conversion_rate", 1.0)
            )
            if item_price <= 0:
                continue

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

            is_enabled_on_channel = bool(
                is_ch_active and (ch_map.is_enabled if ch_map is not None else True)
            )
            is_available = (
                "AVAILABLE"
                if (
                    is_enabled_on_channel
                    and inv
                    and not inv.is_out_of_stock
                    and inv.available_stock > 0
                )
                else "UNAVAILABLE"
            )

            category_map[cate_id]["items"].append({
                "id": str(product.sku),
                "name": product.name,
                "price": item_price,
                "description": product.description or "",
                "availableStatus": is_available,
                "photos": [product.image_url] if product.image_url else [],
            })
            total_added += 1

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

        cache_key = _normalize_menu_cache_key(partner_store_id) or str(partner_store_id)
        _shopeefood_menu_cache[cache_key] = menu_payload
        _shopeefood_menu_cache.move_to_end(cache_key)
        _shopeefood_menu_cache_ts[cache_key] = time.monotonic()
        while len(_shopeefood_menu_cache) > MAX_MENU_CACHE_ENTRIES:
            evicted_key, _ = _shopeefood_menu_cache.popitem(last=False)
            _shopeefood_menu_cache_ts.pop(evicted_key, None)
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
            if not isinstance(notify_resp, dict) or notify_resp.get("result") != "success":
                err_msg = (
                    notify_resp.get("error")
                    if isinstance(notify_resp, dict)
                    else str(notify_resp)
                ) or "ShopeeFood S2S API did not return success"
                return SyncResult(
                    channel_code=self.channel_code,
                    store_id=store_id,
                    sync_type="MENU",
                    success=False,
                    total_synced=0,
                    message=f"Đồng bộ thực đơn ShopeeFood thất bại (Cửa hàng: {partner_store_id}): {err_msg}",
                    details={"notification_response": notify_resp},
                )

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
            if db is not None:
                try:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    await error_service.log_error(
                        db=db,
                        error_code="SHOPEEFOOD_SYNC_MENU_ERROR",
                        message=f"Lỗi khi đồng bộ menu ShopeeFood chi nhánh {store_id}: {str(ex)}",
                        severity=ErrorSeverity.ERROR,
                        module="SHOPEEFOOD",
                        stack_trace=traceback.format_exc(),
                        endpoint="/shopeefood/menu/sync",
                        http_method="POST",
                    )
                except Exception as log_ex:
                    logger.error(f"Failed to record operational error: {log_ex}")
            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="MENU",
                success=False,
                message=f"Lỗi đồng bộ menu ShopeeFood: {str(ex)}",
            )

    def get_cached_menu(self, store_code: str) -> Optional[Dict[str, Any]]:
        """Retrieve pre-built menu from cache for ShopeeFood GET /shopeefoodapi/{code}.json."""
        cache_key = _normalize_menu_cache_key(store_code)
        if not cache_key:
            return None
        cached = _shopeefood_menu_cache.get(cache_key)
        if cached is None:
            return None
        cached_at = _shopeefood_menu_cache_ts.get(cache_key)
        if cached_at is not None and (time.monotonic() - cached_at) > MENU_CACHE_TTL_SECONDS:
            _shopeefood_menu_cache.pop(cache_key, None)
            _shopeefood_menu_cache_ts.pop(cache_key, None)
            return None
        _shopeefood_menu_cache.move_to_end(cache_key)
        return cached

    def invalidate_cached_menu(self, *store_codes: str) -> None:
        """Evict cached store menus when SKU prices or availability change."""
        for code in store_codes:
            if code:
                _shopeefood_menu_cache.pop(code, None)
                _shopeefood_menu_cache_ts.pop(code, None)
                norm = _normalize_menu_cache_key(code)
                if norm:
                    _shopeefood_menu_cache.pop(norm, None)
                    _shopeefood_menu_cache_ts.pop(norm, None)

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
        """Push dish status (AVAILABLE / OUT_OF_STOCK) and optional price updates to ShopeeFood."""
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
            has_price_updates = any(it.get("price") is not None for it in stock_items)
            governed_items = await apply_channel_governance_to_stock_items(
                db=db, raw_channel_code="SHOPEEFOOD", stock_items=stock_items
            )
            dishes = []
            price_dishes = []
            for item in governed_items:
                partner_dish_id = str(item.get("sku") or item.get("product_id"))
                is_out = item.get("is_out_of_stock", False)
                dishes.append({
                    "partner_dish_id": partner_dish_id,
                    "status": 2 if is_out else 1,  # 1: AVAILABLE, 2: OUT_OF_STOCK
                })
                if has_price_updates and item.get("price") is not None and float(item["price"]) > 0:
                    price_dishes.append({
                        "partner_dish_id": partner_dish_id,
                        "price": int(round(float(item["price"]))),
                    })

            response = await shopeefood_client.update_dish_status(
                partner_restaurant_id=partner_store_id,
                dishes=dishes,
            )
            status_ok = isinstance(response, dict) and response.get("result") == "success"

            price_resp: Optional[Dict[str, Any]] = None
            menu_notify_resp: Optional[Dict[str, Any]] = None
            price_ok = False
            menu_ok = False
            if has_price_updates:
                self.invalidate_cached_menu(partner_store_id)
                if price_dishes:
                    price_resp = await shopeefood_client.bulk_update_dishes(
                        partner_restaurant_id=partner_store_id,
                        dishes=price_dishes,
                    )
                    price_ok = isinstance(price_resp, dict) and price_resp.get("result") == "success"
                    if not price_ok:
                        menu_enabled = await channel_service.is_feature_enabled(
                            self.channel_code, "menu_sync_enabled", db
                        )
                        if menu_enabled:
                            logger.warning(
                                f"ShopeeFood bulk_update_dishes trả về {price_resp}; "
                                f"kích hoạt notify_menu_sync dự phòng cho cửa hàng {partner_store_id}."
                            )
                            menu_notify_resp = await shopeefood_client.notify_menu_sync(
                                partner_restaurant_id=partner_store_id
                            )
                            menu_ok = (
                                isinstance(menu_notify_resp, dict)
                                and menu_notify_resp.get("result") == "success"
                            )

            details: Dict[str, Any] = {"api_response": response}
            if price_resp is not None:
                details["price_update_response"] = price_resp
            if menu_notify_resp is not None:
                details["menu_sync_fallback_response"] = menu_notify_resp

            price_step_ok = (not has_price_updates) or (not price_dishes) or price_ok or menu_ok
            overall_ok = (status_ok and price_step_ok) or (has_price_updates and (price_ok or menu_ok))
            if not overall_ok:
                err_msg = (
                    (price_resp.get("error") if isinstance(price_resp, dict) else None)
                    or (response.get("error") if isinstance(response, dict) else None)
                    or (response.get("result") if isinstance(response, dict) else str(response))
                    or "ShopeeFood S2S API did not return success"
                )
                return SyncResult(
                    channel_code=self.channel_code,
                    store_id=store_id,
                    sync_type="STOCK",
                    success=False,
                    total_synced=0,
                    message=f"Cập nhật tồn kho ShopeeFood thất bại ({partner_store_id}): {err_msg}",
                    details=details,
                )

            return SyncResult(
                channel_code=self.channel_code,
                store_id=store_id,
                sync_type="STOCK",
                success=True,
                total_synced=len(price_dishes) if has_price_updates and not status_ok else len(dishes),
                message=f"Đã cập nhật trạng thái tồn {len(dishes)} món trên ShopeeFood ({partner_store_id})",
                details=details,
            )
        except Exception as ex:
            logger.error(f"Lỗi khi cập nhật tồn kho ShopeeFood: {str(ex)}")
            if db is not None:
                try:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    await error_service.log_error(
                        db=db,
                        error_code="SHOPEEFOOD_SYNC_STOCK_ERROR",
                        message=f"Lỗi khi cập nhật tồn kho ShopeeFood (Store {store_id}): {str(ex)}",
                        severity=ErrorSeverity.ERROR,
                        module="SHOPEEFOOD",
                        stack_trace=traceback.format_exc(),
                        endpoint="/shopeefood/inventory/sync",
                        http_method="POST",
                    )
                except Exception as log_ex:
                    logger.error(f"Failed to record operational error: {log_ex}")
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

    @staticmethod
    def _map_webhook_status(raw_status: Any) -> Optional[UnifiedOrderStatus]:
        """Map ShopeeFood / Foody numeric or string status to UnifiedOrderStatus."""
        if raw_status is None:
            return None
        if isinstance(raw_status, int) or (isinstance(raw_status, str) and raw_status.strip().isdigit()):
            code = int(str(raw_status).strip())
            # Foody External API v7.0.2 OrderMerchantStatus & OrderUpdateStatus codes
            numeric_map = {
                0: UnifiedOrderStatus.ACCEPTED,   # OrderUpdateStatus.CONFIRM
                1: UnifiedOrderStatus.PICKED_UP,  # OrderMerchantStatus.PICKED
                2: UnifiedOrderStatus.DELIVERED,  # OrderMerchantStatus.DELIVERED
                3: UnifiedOrderStatus.ACCEPTED,   # OrderMerchantStatus.CONFIRMED
                5: UnifiedOrderStatus.PENDING,    # OrderMerchantStatus.M_ASSIGNED
                6: UnifiedOrderStatus.PENDING,    # OrderMerchantStatus.M_RECEIVED
                7: UnifiedOrderStatus.CANCELLED,  # OrderMerchantStatus.M_OUT_OF_SERVICE
                8: UnifiedOrderStatus.CANCELLED,  # OrderMerchantStatus.CANCELLED
                9: UnifiedOrderStatus.CANCELLED,  # OrderMerchantStatus.M_TIMEOUT
            }
            return numeric_map.get(code)

        s = str(raw_status).strip().upper()
        string_map = {
            "PENDING": UnifiedOrderStatus.PENDING,
            "NEW": UnifiedOrderStatus.PENDING,
            "CREATED": UnifiedOrderStatus.PENDING,
            "CONFIRMED": UnifiedOrderStatus.ACCEPTED,
            "ACCEPTED": UnifiedOrderStatus.ACCEPTED,
            "PREPARING": UnifiedOrderStatus.PREPARING,
            "PROCESSING": UnifiedOrderStatus.PREPARING,
            "COOKING": UnifiedOrderStatus.PREPARING,
            "READY": UnifiedOrderStatus.READY,
            "READY_FOR_PICKUP": UnifiedOrderStatus.READY,
            "PACKED": UnifiedOrderStatus.READY,
            "PICKED_UP": UnifiedOrderStatus.PICKED_UP,
            "SHIPPED": UnifiedOrderStatus.PICKED_UP,
            "DELIVERING": UnifiedOrderStatus.PICKED_UP,
            "DELIVERED": UnifiedOrderStatus.DELIVERED,
            "COMPLETED": UnifiedOrderStatus.DELIVERED,
            "DONE": UnifiedOrderStatus.DELIVERED,
            "CANCELLED": UnifiedOrderStatus.CANCELLED,
            "CANCELED": UnifiedOrderStatus.CANCELLED,
            "CANCEL": UnifiedOrderStatus.CANCELLED,
            "OUT_OF_SERVICE": UnifiedOrderStatus.CANCELLED,
            "REJECTED": UnifiedOrderStatus.CANCELLED,
            "FAILED": UnifiedOrderStatus.CANCELLED,
        }
        return string_map.get(s)

    async def handle_order_webhook(
        self,
        headers: Dict[str, str],
        raw_body: bytes,
        db: AsyncSession,
        verified_order_details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Process incoming ShopeeFood order or update_order webhook.
        Checks if 'order_webhook_enabled' is active.
        If 'auto_confirm_enabled' is active, auto-transitions new order to ACCEPTED and notifies Foody.
        When existing order is updated (created is False), updates driver info and transitions status.
        When `verified_order_details` is supplied (S2S Proof-of-Order), hydrates full order items & totals.
        """
        enabled = await channel_service.is_feature_enabled(self.channel_code, "order_webhook_enabled", db)
        if not enabled:
            logger.warning("Nhận webhook đơn hàng ShopeeFood nhưng tính năng 'Nhận đơn qua Webhook' đang TẮT.")
            return {
                "result": "failed",
                "error": "Tính năng nhận webhook đơn hàng ShopeeFood hiện đang tạm dừng bởi Quản trị viên."
            }

        payload = json.loads(raw_body.decode("utf-8"))
        auto_confirm = await channel_service.is_feature_enabled(self.channel_code, "auto_confirm_enabled", db)

        if verified_order_details is not None:
            details = verified_order_details if isinstance(verified_order_details, dict) else {}
            details_rest = details.get("restaurant") if isinstance(details.get("restaurant"), dict) else {}

            partner_restaurant_id = str(
                details_rest.get("partner_restaurant_id") or details_rest.get("id") or ""
            ).strip()
            if not partner_restaurant_id:
                raise ValueError("Verified S2S ShopeeFood order details missing restaurant identifier.")

            channel_order_id = str(
                details.get("code") or details.get("order_code") or details.get("order_id") or ""
            ).strip()
            raw_status = details.get("status") if details.get("status") is not None else details.get("order_status")
            mapped_status = self._map_webhook_status(raw_status)

            if mapped_status in (
                UnifiedOrderStatus.CANCELLED,
                UnifiedOrderStatus.DELIVERED,
                UnifiedOrderStatus.PICKED_UP,
                UnifiedOrderStatus.READY,
                UnifiedOrderStatus.PREPARING,
            ):
                initial_status = mapped_status
            else:
                initial_status = UnifiedOrderStatus.ACCEPTED if auto_confirm else (mapped_status or UnifiedOrderStatus.PENDING)

            deliver_addr = details.get("deliver_address") if isinstance(details.get("deliver_address"), dict) else {}
            customer_obj = details.get("customer") if isinstance(details.get("customer"), dict) else {}
            customer_name = customer_obj.get("name") or deliver_addr.get("name")
            customer_phone = customer_obj.get("phone") or deliver_addr.get("phone")
            delivery_address = deliver_addr.get("address") or customer_obj.get("address")

            driver_info = details.get("assignee") if isinstance(details.get("assignee"), dict) else {}
            driver_name = driver_info.get("name")
            driver_phone = driver_info.get("phone")
            driver_plate = (
                driver_info.get("license_plate")
                or driver_info.get("plate")
                or driver_info.get("vehicle_number")
            )

            customer_bill = details.get("customer_bill") if isinstance(details.get("customer_bill"), dict) else {}
            subtotal_amount = _extract_money_val(details.get("order_value"))
            discount_amount = _extract_money_val(details.get("total_merchant_discount"))
            delivery_fee = _extract_money_val(customer_bill.get("shipping_fee"))
            total_amount = _extract_money_val(details.get("total_value"))

            order_data = {
                "display_order_id": channel_order_id[-6:] if channel_order_id else None,
                "initial_status": initial_status,
                "subtotal_amount": subtotal_amount,
                "discount_amount": discount_amount,
                "delivery_fee": delivery_fee,
                "total_amount": total_amount,
                "customer_name": customer_name,
                "customer_phone": customer_phone,
                "delivery_address": delivery_address,
                "driver_name": driver_name,
                "driver_phone": driver_phone,
                "driver_license_plate": driver_plate,
                "raw_payload": {"webhook": payload, "s2s_order_details": details},
            }
            items_data = _extract_s2s_items(details.get("dish_groups"))
            status_note = (
                details.get("cancel_reason")
                or details.get("cancellation_reason")
                or details.get("note")
                or details.get("message")
                or "ShopeeFood S2S verified update"
            )
        else:
            partner_restaurant_id = str(
                payload.get("partner_restaurant_id")
                or payload.get("restaurant_id")
                or "10001"
            )
            channel_order_id = str(
                payload.get("order_id") or payload.get("order_code") or ""
            )

            raw_status = (
                payload.get("order_status")
                if payload.get("order_status") is not None
                else payload.get("status")
            )
            mapped_status = self._map_webhook_status(raw_status)

            if mapped_status in (
                UnifiedOrderStatus.CANCELLED,
                UnifiedOrderStatus.DELIVERED,
                UnifiedOrderStatus.PICKED_UP,
                UnifiedOrderStatus.READY,
                UnifiedOrderStatus.PREPARING,
            ):
                initial_status = mapped_status
            else:
                initial_status = UnifiedOrderStatus.ACCEPTED if auto_confirm else (mapped_status or UnifiedOrderStatus.PENDING)

            customer_info = payload.get("customer") or {}
            driver_info = payload.get("driver") or payload.get("shipper") or {}

            driver_name = (
                (driver_info.get("name") if isinstance(driver_info, dict) else None)
                or payload.get("driver_name")
                or payload.get("shipper_name")
            )
            driver_phone = (
                (driver_info.get("phone") if isinstance(driver_info, dict) else None)
                or payload.get("driver_phone")
                or payload.get("shipper_phone")
            )
            driver_plate = (
                (
                    driver_info.get("license_plate")
                    or driver_info.get("plate")
                    or driver_info.get("vehicle_number")
                    if isinstance(driver_info, dict)
                    else None
                )
                or payload.get("driver_license_plate")
                or payload.get("license_plate")
            )

            subtotal_amount = float(payload["subtotal"]) if payload.get("subtotal") is not None else 0.0
            discount_amount = float(payload["discount"]) if payload.get("discount") is not None else 0.0
            delivery_fee = float(payload["shipping_fee"]) if payload.get("shipping_fee") is not None else 0.0
            total_amount = float(payload["total_amount"]) if payload.get("total_amount") is not None else 0.0

            order_data = {
                "display_order_id": payload.get("display_order_id") or (channel_order_id[-6:] if channel_order_id else None),
                "initial_status": initial_status,
                "subtotal_amount": subtotal_amount,
                "discount_amount": discount_amount,
                "delivery_fee": delivery_fee,
                "total_amount": total_amount,
                "customer_name": customer_info.get("name") if isinstance(customer_info, dict) else None,
                "customer_phone": customer_info.get("phone") if isinstance(customer_info, dict) else None,
                "delivery_address": customer_info.get("address") if isinstance(customer_info, dict) else None,
                "driver_name": driver_name,
                "driver_phone": driver_phone,
                "driver_license_plate": driver_plate,
                "raw_payload": payload,
            }

            items_data = []
            for itm in (payload.get("items") or []):
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
            status_note = (
                payload.get("cancel_reason")
                or payload.get("cancellation_reason")
                or payload.get("note")
                or payload.get("message")
                or "ShopeeFood webhook update"
            )

        order, created = await OrderService.create_or_get_inbound_order(
            channel_code=self.channel_code,
            partner_store_id=partner_restaurant_id,
            channel_order_id=channel_order_id,
            order_data=order_data,
            items_data=items_data,
            db=db,
        )

        # If order already existed (e.g. /webhooks/update_order), reconcile item edits (update_type=1), driver info, and status
        if not created:
            is_order_item_update = str(payload.get("update_type") or "").strip() == "1"
            if is_order_item_update and order.status != UnifiedOrderStatus.CANCELLED:
                reconciled_snapshot = None
                if verified_order_details is not None and items_data:
                    reconciled_snapshot = (order_data, items_data)
                else:
                    try:
                        details_resp = await shopeefood_client.get_order_details(
                            order_code=channel_order_id,
                            partner_restaurant_id=partner_restaurant_id,
                            mark_as_received=0,
                        )
                        reply_dict = details_resp.get("reply") if isinstance(details_resp, dict) else None
                        if isinstance(reply_dict, dict) and reply_dict:
                            reconciled_snapshot = self._extract_s2s_order_snapshot(reply_dict, payload)
                    except Exception as det_ex:
                        logger.warning(f"Could not pull ShopeeFood get_order_details on update_type=1 for {channel_order_id}: {det_ex}")

                if reconciled_snapshot and reconciled_snapshot[1]:
                    order = await OrderService.reconcile_edited_inbound_order(
                        order_id=order.id,
                        order_data=reconciled_snapshot[0],
                        items_data=reconciled_snapshot[1],
                        db=db,
                        changed_by="SHOPEEFOOD_WEBHOOK",
                        note="Đồng bộ chỉnh sửa món/tổng tiền từ ShopeeFood (update_type=1)",
                    )

            driver_updated = False
            if driver_name and getattr(order, "driver_name", None) != driver_name:
                order.driver_name = driver_name
                driver_updated = True
            if driver_phone and getattr(order, "driver_phone", None) != driver_phone:
                order.driver_phone = driver_phone
                driver_updated = True
            if driver_plate and getattr(order, "driver_license_plate", None) != driver_plate:
                order.driver_license_plate = driver_plate
                driver_updated = True

            if mapped_status is not None and mapped_status != order.status:
                updated_order = await OrderService.update_order_status(
                    db=db,
                    order_id=order.id,
                    payload=OrderStatusUpdateSchema(
                        new_status=mapped_status,
                        note=str(status_note),
                        changed_by="SHOPEEFOOD_WEBHOOK",
                        cancellation_reason=str(status_note) if mapped_status == UnifiedOrderStatus.CANCELLED else None,
                    ),
                    sync_to_channel=False,
                    trigger_channel_sync=False,
                )
                if updated_order is not None:
                    order = updated_order
            elif driver_updated and db is not None:
                await db.commit()

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
                "status": order.status.value if hasattr(order.status, "value") else str(order.status),
                "auto_confirmed": auto_confirm,
                "created": created,
            }
        }

    @staticmethod
    def _extract_s2s_order_snapshot(
        details: Dict[str, Any],
        webhook_payload: Optional[Dict[str, Any]] = None,
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Extract canonical items and monetary totals from Foody `/s2s/order/get_details` `reply` dict."""
        customer_bill = details.get("customer_bill") if isinstance(details.get("customer_bill"), dict) else {}
        s2s_order_data: Dict[str, Any] = {
            "subtotal_amount": _extract_money_val(details.get("order_value")),
            "discount_amount": _extract_money_val(details.get("total_merchant_discount")),
            "delivery_fee": _extract_money_val(customer_bill.get("shipping_fee")),
            "total_amount": _extract_money_val(details.get("total_value")),
            "raw_payload": (
                {"webhook": webhook_payload, "s2s_order_details": details}
                if webhook_payload is not None
                else {"s2s_order_details": details}
            ),
        }
        s2s_items_data = _extract_s2s_items(details.get("dish_groups"))
        return s2s_order_data, s2s_items_data

    async def refresh_order_from_channel(
        self,
        order: UnifiedOrder,
        db: AsyncSession,
    ) -> UnifiedOrder:
        """
        Pull authoritative order details via ShopeeFood S2S `/s2s/order/get_details`
        and reconcile line items, totals, driver info, and virtual stock delta.
        """
        partner_store_id = ""
        if order.channel_id and order.store_id:
            mapping = (
                await db.execute(
                    select(StoreChannelMapping).where(
                        StoreChannelMapping.channel_id == order.channel_id,
                        StoreChannelMapping.store_id == order.store_id,
                    )
                )
            ).scalars().first()
            if mapping and mapping.partner_store_id:
                partner_store_id = str(mapping.partner_store_id)
        if not partner_store_id and isinstance(order.raw_payload, dict):
            rest = order.raw_payload.get("s2s_order_details", {}).get("restaurant", {})
            partner_store_id = str(rest.get("partner_restaurant_id") or rest.get("id") or "")

        details_resp = await shopeefood_client.get_order_details(
            order_code=order.channel_order_id,
            partner_restaurant_id=partner_store_id or None,
            mark_as_received=0,
        )
        reply = details_resp.get("reply") if isinstance(details_resp, dict) else None
        if not isinstance(reply, dict) or not reply:
            raise ValidationError(
                f"Không lấy được chi tiết mới từ ShopeeFood S2S cho đơn {order.channel_order_id}."
            )

        prev_webhook = (
            order.raw_payload.get("webhook")
            if isinstance(order.raw_payload, dict) and isinstance(order.raw_payload.get("webhook"), dict)
            else None
        )
        s2s_order_data, s2s_items_data = self._extract_s2s_order_snapshot(reply, prev_webhook)

        driver_info = reply.get("assignee") if isinstance(reply.get("assignee"), dict) else {}
        if driver_info:
            if driver_info.get("name"):
                order.driver_name = driver_info.get("name")
            if driver_info.get("phone"):
                order.driver_phone = driver_info.get("phone")
            plate = driver_info.get("license_plate") or driver_info.get("plate") or driver_info.get("vehicle_number")
            if plate:
                order.driver_license_plate = plate

        updated = await OrderService.reconcile_edited_inbound_order(
            order_id=order.id,
            order_data=s2s_order_data,
            items_data=s2s_items_data,
            db=db,
            changed_by="SHOPEEFOOD_SYNC",
            note="Đồng bộ lại danh sách món và tổng tiền từ ShopeeFood S2S",
        )

        raw_status = reply.get("status") if reply.get("status") is not None else reply.get("order_status")
        mapped_status = self._map_webhook_status(raw_status)
        if mapped_status is not None and mapped_status != updated.status and updated.status not in (
            UnifiedOrderStatus.DELIVERED,
            UnifiedOrderStatus.CANCELLED,
        ):
            try:
                updated = await OrderService.update_order_status(
                    order_id=updated.id,
                    payload=OrderStatusUpdateSchema(
                        new_status=mapped_status,
                        note="Đồng bộ trạng thái từ ShopeeFood S2S",
                        changed_by="SHOPEEFOOD_SYNC",
                    ),
                    db=db,
                    sync_to_channel=False,
                )
            except Exception as st_ex:
                logger.warning(f"Bỏ qua đồng bộ trạng thái chéo ShopeeFood cho {updated.order_code}: {st_ex}")

        return updated

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
            if db is not None:
                try:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    await error_service.log_error(
                        db=db,
                        error_code="SHOPEEFOOD_SYNC_ORDER_STATUS_ERROR",
                        message=f"Lỗi gửi cập nhật trạng thái đơn {channel_order_id} sang '{new_status.value}' lên ShopeeFood: {str(ex)}",
                        severity=ErrorSeverity.ERROR,
                        module="SHOPEEFOOD",
                        stack_trace=traceback.format_exc(),
                        endpoint="/shopeefood/orders/status",
                        http_method="POST",
                        request_payload={"order_id": channel_order_id, "partner_store_id": partner_store_id, "new_status": new_status.value, "error": str(ex)},
                    )
                except Exception as log_ex:
                    logger.error(f"Failed to record operational error: {log_ex}")
            return False
