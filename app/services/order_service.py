import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.exceptions import AppException, ConflictError, NotFoundError, ValidationError
from app.models.channel import Channel
from app.models.inventory import InventoryStockLog, StoreInventory
from app.models.operational_error import ErrorSeverity
from app.models.order import OrderItem, OrderStatusHistory, UnifiedOrder, UnifiedOrderStatus
from app.models.product import Product
from app.models.store import Store, StoreChannelMapping
from app.schemas.order import OrderEditRequest, OrderFilterParams, OrderStatusUpdateSchema
from app.services.channel_registry import channel_registry
from app.services.error_service import error_service
from app.services.inventory_service import InventoryService
from app.services.poppies_channels import (
    get_channel_family_codes,
    resolve_channel_from_db_result,
)

logger = logging.getLogger("naman_portal.order_service")

VN_TZ = timezone(timedelta(hours=7))

# State machine allowed transitions (including partner webhook catch-up transitions)
VALID_STATUS_TRANSITIONS: Dict[UnifiedOrderStatus, List[UnifiedOrderStatus]] = {
    UnifiedOrderStatus.PENDING: [
        UnifiedOrderStatus.ACCEPTED,
        UnifiedOrderStatus.PREPARING,
        UnifiedOrderStatus.READY,
        UnifiedOrderStatus.PICKED_UP,
        UnifiedOrderStatus.DELIVERED,
        UnifiedOrderStatus.CANCELLED,
    ],
    UnifiedOrderStatus.ACCEPTED: [
        UnifiedOrderStatus.PREPARING,
        UnifiedOrderStatus.READY,
        UnifiedOrderStatus.PICKED_UP,
        UnifiedOrderStatus.DELIVERED,
        UnifiedOrderStatus.CANCELLED,
    ],
    UnifiedOrderStatus.PREPARING: [
        UnifiedOrderStatus.READY,
        UnifiedOrderStatus.PICKED_UP,
        UnifiedOrderStatus.DELIVERED,
        UnifiedOrderStatus.CANCELLED,
    ],
    UnifiedOrderStatus.READY: [
        UnifiedOrderStatus.PICKED_UP,
        UnifiedOrderStatus.DELIVERED,
        UnifiedOrderStatus.CANCELLED,
    ],
    UnifiedOrderStatus.PICKED_UP: [
        UnifiedOrderStatus.DELIVERED,
        UnifiedOrderStatus.CANCELLED,
    ],
    UnifiedOrderStatus.DELIVERED: [],
    UnifiedOrderStatus.CANCELLED: [],
}


class OrderService:
    """Core Service managing omnichannel Unified Orders and State Transitions."""

    @staticmethod
    async def _lock_inventories_sorted(
        store_id: str,
        sorted_skus: List[str],
        db: AsyncSession,
    ) -> Dict[str, StoreInventory]:
        """Lock StoreInventory rows for a store in lexicographical SKU order to prevent deadlocks."""
        if not sorted_skus:
            return {}
        inv_res = await db.execute(
            select(StoreInventory)
            .where(
                StoreInventory.store_id == store_id,
                StoreInventory.sku.in_(sorted_skus),
            )
            .order_by(StoreInventory.sku.asc())
            .with_for_update()
        )
        return {inv.sku: inv for inv in inv_res.scalars().all()}

    @staticmethod
    async def create_or_get_inbound_order(
        channel_code: str,
        partner_store_id: str,
        channel_order_id: str,
        order_data: Dict[str, Any],
        items_data: List[Dict[str, Any]],
        db: AsyncSession
    ) -> Tuple[UnifiedOrder, bool]:
        """
        Create a new Unified Order from incoming webhook or return existing (Idempotency).
        Returns: (order, created)
        """
        # 1. Resolve Channel (Canonical + Legacy Alias aware)
        channel_stmt = select(Channel).where(
            Channel.code.in_(get_channel_family_codes(channel_code))
        )
        channel_res = await db.execute(channel_stmt)
        channel = await resolve_channel_from_db_result(channel_res, channel_code)
        if not channel:
            raise NotFoundError("Channel", channel_code)

        # 2. Check Idempotency: Does order already exist?
        existing_stmt = select(UnifiedOrder).where(
            UnifiedOrder.channel_id == channel.id,
            UnifiedOrder.channel_order_id == str(channel_order_id)
        ).options(selectinload(UnifiedOrder.items), selectinload(UnifiedOrder.status_history))
        existing_res = await db.execute(existing_stmt)
        existing_order = existing_res.scalar_one_or_none()
        if existing_order:
            logger.info(f"Order {channel_order_id} already exists in channel {channel_code} (Idempotent response)")
            return existing_order, False

        # 3. Resolve Store by partner_store_id (or raw_payload.merchantID)
        raw_payload_dict = order_data.get("raw_payload") if isinstance(order_data, dict) else None
        candidate_partner_ids = [str(partner_store_id)]
        if isinstance(raw_payload_dict, dict):
            for k in ("merchantID", "partnerMerchantID"):
                v = str(raw_payload_dict.get(k) or "").strip()
                if v and v not in candidate_partner_ids:
                    candidate_partner_ids.append(v)

        mapping_stmt = select(StoreChannelMapping).where(
            StoreChannelMapping.channel_id == channel.id,
            StoreChannelMapping.partner_store_id.in_(candidate_partner_ids)
        ).options(selectinload(StoreChannelMapping.store))
        mapping_res = await db.execute(mapping_stmt)
        mapping = mapping_res.scalars().first()
        
        if not mapping:
            # Check by Store code (e.g. 10001, 10004, 10005, 10006)
            store_stmt = select(Store).where(Store.code.in_(candidate_partner_ids))
            store_res = await db.execute(store_stmt)
            store = store_res.scalars().first()
            matched_exact_store = store is not None

            if not store:
                msg = (
                    f"Không thể định tuyến đơn hàng {channel_order_id} ({channel_code}): "
                    f"partner_store_id '{partner_store_id}' không khớp StoreChannelMapping hay Store.code nào."
                )
                logger.critical(msg)
                try:
                    await error_service.log_error(
                        db=db,
                        error_code="UNMAPPED_STORE_ORDER",
                        message=msg,
                        severity=ErrorSeverity.CRITICAL,
                        module=str(channel_code).upper(),
                        request_payload={
                            "channel_code": channel_code,
                            "channel_order_id": str(channel_order_id),
                            "partner_store_id": str(partner_store_id),
                            "candidate_partner_ids": candidate_partner_ids,
                        },
                    )
                except Exception as log_ex:
                    logger.error(f"Failed to record OperationalErrorLog for unmapped store order: {log_ex}")
                raise ValidationError(
                    msg,
                    details={
                        "channel_code": channel_code,
                        "channel_order_id": str(channel_order_id),
                        "partner_store_id": str(partner_store_id),
                    },
                )

            store_id = store.id
            # Create mapping for future orders ONLY if store matched explicitly by code (never bind unknown partner_store_id to fallback store)
            if matched_exact_store:
                try:
                    existing_scm_res = await db.execute(
                        select(StoreChannelMapping).where(
                            StoreChannelMapping.store_id == store_id,
                            StoreChannelMapping.channel_id == channel.id,
                        )
                    )
                    if existing_scm_res.scalars().first() is None:
                        preferred_id = (
                            str(raw_payload_dict.get("merchantID")).strip()
                            if (isinstance(raw_payload_dict, dict) and raw_payload_dict.get("merchantID"))
                            else str(partner_store_id)
                        )
                        new_map = StoreChannelMapping(
                            store_id=store_id,
                            channel_id=channel.id,
                            partner_store_id=preferred_id,
                            is_active=True
                        )
                        if isinstance(db, AsyncSession):
                            async with db.begin_nested():
                                db.add(new_map)
                                await db.flush()
                        else:
                            db.add(new_map)
                            await db.flush()
                except Exception:
                    pass
        else:
            store_id = mapping.store_id

        # 4. Generate Internal Order Code: NAM-<CHANNEL_PREFIX>-YYYYMMDD-<SUFFIX>
        prefix_map = {
            "SHOPEEFOOD": "SF",
            "SHOPEEMART": "SM",
            "GRABMART": "GM",
            "GRAB": "GM",
        }
        ch_prefix = prefix_map.get(channel_code.upper(), channel_code[:3].upper())
        today_str = datetime.now(VN_TZ).strftime("%Y%m%d")
        raw_oid = str(channel_order_id)
        if len(raw_oid) > 6:
            oid_hash = hashlib.sha256(raw_oid.encode("utf-8")).hexdigest()[:4].upper()
            order_suffix = f"{raw_oid[-6:]}-{oid_hash}"
        else:
            order_suffix = raw_oid
        order_code = f"NAM-{ch_prefix}-{today_str}-{order_suffix}"

        # 5. Construct Unified Order & Reserve Virtual Stock atomically
        depleted_stock_items: List[Dict[str, Any]] = []
        stock_warnings: List[Dict[str, Any]] = []
        try:
            order = UnifiedOrder(
                order_code=order_code,
                channel_id=channel.id,
                store_id=store_id,
                channel_order_id=str(channel_order_id),
                display_order_id=order_data.get("display_order_id") or channel_order_id[-4:],
                status=order_data.get("initial_status", UnifiedOrderStatus.PENDING),
                stock_reservation_status="NONE",
                subtotal_amount=order_data.get("subtotal_amount", 0.0),
                discount_amount=order_data.get("discount_amount", 0.0),
                delivery_fee=order_data.get("delivery_fee", 0.0),
                total_amount=order_data.get("total_amount", 0.0),
                customer_name=order_data.get("customer_name"),
                customer_phone=order_data.get("customer_phone"),
                delivery_address=order_data.get("delivery_address"),
                driver_name=order_data.get("driver_name"),
                driver_phone=order_data.get("driver_phone"),
                driver_license_plate=order_data.get("driver_license_plate"),
                order_time=order_data.get("order_time", datetime.now(timezone.utc)),
                estimated_ready_time=order_data.get("estimated_ready_time"),
                raw_payload=order_data.get("raw_payload")
            )
            db.add(order)
            await db.flush()

            # 6. Add Items & Aggregate SKUs for Virtual Stock Reservation
            sku_qty_map: Dict[str, int] = {}
            for item in items_data:
                sku_val = str(item.get("sku") or "UNKNOWN").strip()
                qty_val = int(item.get("quantity", 1))
                prod_id_val = item.get("product_id")

                if not prod_id_val and sku_val and sku_val != "UNKNOWN":
                    p_res = await db.execute(select(Product).where(Product.sku == sku_val))
                    p_obj = p_res.scalar_one_or_none()
                    if p_obj:
                        prod_id_val = p_obj.id

                order_item = OrderItem(
                    order_id=order.id,
                    product_id=prod_id_val,
                    sku=sku_val,
                    item_name=item.get("item_name", "Unknown Item"),
                    quantity=qty_val,
                    unit_price=float(item.get("unit_price", 0.0)),
                    total_price=float(item.get("total_price", 0.0)),
                    notes=item.get("notes"),
                    modifiers=item.get("modifiers")
                )
                db.add(order_item)

                if sku_val and sku_val != "UNKNOWN" and qty_val > 0:
                    sku_qty_map[sku_val] = sku_qty_map.get(sku_val, 0) + qty_val

            # Reserve virtual inventory for active non-cancelled orders (idempotent via stock_reservation_status)
            if order.status != UnifiedOrderStatus.CANCELLED and (order.stock_reservation_status or "NONE") == "NONE":
                if sku_qty_map:
                    sorted_skus = sorted(sku_qty_map.keys())
                    inv_by_sku = await OrderService._lock_inventories_sorted(store_id, sorted_skus, db)
                    settings = get_settings()

                    for sku_val in sorted_skus:
                        qty_needed = float(sku_qty_map[sku_val])
                        inv_obj = inv_by_sku.get(sku_val)

                        if inv_obj is None:
                            msg = (
                                f"Không tìm thấy tồn kho cửa hàng cho SKU '{sku_val}' "
                                f"khi giữ tồn đơn {order.order_code} (store_id={store_id})."
                            )
                            if settings.VIRTUAL_STOCK_STRICT_MODE:
                                await db.rollback()
                                raise ValidationError(
                                    msg,
                                    details={
                                        "sku": sku_val,
                                        "store_id": store_id,
                                        "required_qty": qty_needed,
                                        "order_code": order.order_code,
                                    },
                                )
                            stock_warnings.append({
                                "error_code": "MISSING_STORE_INVENTORY",
                                "message": msg,
                                "payload": {
                                    "sku": sku_val,
                                    "store_id": store_id,
                                    "required_qty": qty_needed,
                                    "order_code": order.order_code,
                                    "channel_code": channel_code,
                                    "channel_order_id": str(channel_order_id),
                                },
                            })
                            continue

                        avail_before = float(inv_obj.available_stock or 0.0)
                        if avail_before < qty_needed:
                            msg = (
                                f"Tồn kho khả dụng không đủ cho SKU '{sku_val}' "
                                f"khi giữ tồn đơn {order.order_code}: cần {qty_needed}, còn {avail_before}."
                            )
                            if settings.VIRTUAL_STOCK_STRICT_MODE:
                                await db.rollback()
                                raise ValidationError(
                                    msg,
                                    details={
                                        "sku": sku_val,
                                        "store_id": store_id,
                                        "required_qty": qty_needed,
                                        "available_stock": avail_before,
                                        "order_code": order.order_code,
                                    },
                                )
                            stock_warnings.append({
                                "error_code": "INSUFFICIENT_VIRTUAL_STOCK",
                                "message": msg,
                                "payload": {
                                    "sku": sku_val,
                                    "store_id": store_id,
                                    "required_qty": qty_needed,
                                    "available_stock": avail_before,
                                    "order_code": order.order_code,
                                    "channel_code": channel_code,
                                    "channel_order_id": str(channel_order_id),
                                },
                            })

                        inv_obj.reserved_stock = float(inv_obj.reserved_stock or 0.0) + qty_needed
                        inv_obj.available_stock = InventoryService.compute_available_stock(
                            float(inv_obj.total_stock or 0.0),
                            float(inv_obj.reserved_stock),
                            float(inv_obj.safety_stock or 0.0),
                        )
                        inv_obj.is_out_of_stock = inv_obj.available_stock <= 0.0
                        if inv_obj.is_out_of_stock:
                            depleted_stock_items.append({
                                "sku": sku_val,
                                "available_stock": 0,
                                "is_out_of_stock": True
                            })
                        db.add(
                            InventoryStockLog(
                                store_id=store_id,
                                sku=sku_val,
                                change_type="ORDER_RESERVE",
                                quantity_before=avail_before,
                                quantity_delta=-qty_needed,
                                quantity_after=float(inv_obj.available_stock),
                                reference_id=order.order_code,
                                note=f"Giữ tồn ảo đơn {channel_code} #{channel_order_id}",
                            )
                        )

                order.stock_reservation_status = "RESERVED"

            # 7. Add Initial History Log
            history = OrderStatusHistory(
                order_id=order.id,
                from_status=None,
                to_status=order.status,
                note=f"Đơn hàng được tạo từ Webhook {channel_code}",
                changed_by="WEBHOOK"
            )
            db.add(history)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            dup_res = await db.execute(existing_stmt)
            dup_order = dup_res.scalar_one_or_none()
            if dup_order:
                logger.info(
                    f"Concurrent duplicate order {channel_order_id} in channel {channel_code} caught by IntegrityError (Idempotent response)"
                )
                return dup_order, False
            raise

        # Log non-strict virtual stock warnings after commit so error_service's commit/rollback never interferes with the order txn
        for warn in stock_warnings:
            try:
                await error_service.log_error(
                    db=db,
                    error_code=warn["error_code"],
                    message=warn["message"],
                    severity=ErrorSeverity.WARNING,
                    module="INVENTORY",
                    request_payload=warn["payload"],
                )
            except Exception as log_ex:
                logger.warning(f"Failed to record OperationalErrorLog ({warn['error_code']}): {log_ex}")

        # Propagate out-of-stock signal across channels to prevent overselling
        if depleted_stock_items:
            try:
                mappings_res = await db.execute(
                    select(StoreChannelMapping)
                    .options(selectinload(StoreChannelMapping.channel))
                    .where(StoreChannelMapping.store_id == store_id, StoreChannelMapping.is_active == True)
                )
                for m in mappings_res.scalars().all():
                    if m.channel and m.partner_store_id:
                        ch_adapter = channel_registry.get(m.channel.code)
                        if ch_adapter:
                            await ch_adapter.sync_inventory(
                                store_id=store_id,
                                partner_store_id=m.partner_store_id,
                                stock_items=depleted_stock_items,
                                db=db
                            )
            except Exception as sync_ex:
                logger.warning(f"Could not propagate out-of-stock signal after order {order.order_code}: {sync_ex}")

        # Refresh order with relations
        return await OrderService.get_order_by_id(order.id, db), True

    @staticmethod
    async def get_order_by_id(order_id: str, db: AsyncSession) -> UnifiedOrder:
        """Fetch single order by internal UUID with full items and status history."""
        stmt = (
            select(UnifiedOrder)
            .where(UnifiedOrder.id == order_id)
            .options(
                selectinload(UnifiedOrder.items),
                selectinload(UnifiedOrder.status_history),
                selectinload(UnifiedOrder.store),
                selectinload(UnifiedOrder.channel),
            )
            .execution_options(populate_existing=True)
        )
        res = await db.execute(stmt)
        order = res.scalar_one_or_none()
        if not order:
            raise NotFoundError("Order", order_id)
        return order

    @staticmethod
    async def list_orders(
        params: OrderFilterParams,
        db: AsyncSession
    ) -> Tuple[List[UnifiedOrder], int]:
        """Fetch paginated list of unified orders with filters."""
        query = select(UnifiedOrder).options(
            selectinload(UnifiedOrder.items),
            selectinload(UnifiedOrder.status_history),
            selectinload(UnifiedOrder.channel),
            selectinload(UnifiedOrder.store),
        )

        if params.store_id:
            query = query.where(UnifiedOrder.store_id == params.store_id)
        if params.store_code:
            query = query.join(UnifiedOrder.store).where(Store.code == params.store_code)
        if params.channel_id:
            query = query.where(UnifiedOrder.channel_id == params.channel_id)
        if params.channel_code:
            family_codes = get_channel_family_codes(params.channel_code)
            if family_codes:
                query = query.join(UnifiedOrder.channel).where(Channel.code.in_(family_codes))
            else:
                query = query.join(UnifiedOrder.channel).where(Channel.code == params.channel_code.upper())
        if params.status:
            if params.status.upper() in ("INCOMPLETE", "CHUA_HOAN_THANH") or params.status == "Chưa hoàn thành":
                query = query.where(
                    UnifiedOrder.status.not_in([UnifiedOrderStatus.DELIVERED, UnifiedOrderStatus.CANCELLED])
                )
            else:
                try:
                    target_status = UnifiedOrderStatus(params.status)
                    query = query.where(UnifiedOrder.status == target_status)
                except ValueError:
                    pass
        if params.from_date:
            query = query.where(UnifiedOrder.created_at >= params.from_date)
        if params.to_date:
            query = query.where(UnifiedOrder.created_at <= params.to_date)
        if params.search:
            search_pattern = f"%{params.search}%"
            query = query.where(
                (UnifiedOrder.order_code.ilike(search_pattern)) |
                (UnifiedOrder.channel_order_id.ilike(search_pattern)) |
                (UnifiedOrder.display_order_id.ilike(search_pattern)) |
                (UnifiedOrder.customer_name.ilike(search_pattern)) |
                (UnifiedOrder.customer_phone.ilike(search_pattern))
            )

        # Count total
        count_stmt = select(func.count()).select_from(query.subquery())
        total_count = (await db.execute(count_stmt)).scalar() or 0

        # Pagination & sorting
        offset = (params.page - 1) * params.page_size
        query = query.order_by(desc(UnifiedOrder.created_at)).offset(offset).limit(params.page_size)

        res = await db.execute(query)
        items = list(res.scalars().all())
        return items, total_count

    @staticmethod
    async def get_order_summary(
        store_id: Optional[str],
        db: AsyncSession
    ) -> Dict[str, Any]:
        """Calculate order metric counters for dashboard KPI tiles (Vietnam UTC+7 day boundary)."""
        now_vn = datetime.now(VN_TZ)
        start_of_day = now_vn.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

        # Total
        total_stmt = select(func.count(UnifiedOrder.id))
        if store_id:
            total_stmt = total_stmt.where(UnifiedOrder.store_id == store_id)
        total_orders = (await db.execute(total_stmt)).scalar() or 0

        # Pending
        pending_stmt = select(func.count(UnifiedOrder.id)).where(UnifiedOrder.status == UnifiedOrderStatus.PENDING)
        if store_id:
            pending_stmt = pending_stmt.where(UnifiedOrder.store_id == store_id)
        pending_count = (await db.execute(pending_stmt)).scalar() or 0

        # Preparing (ACCEPTED or PREPARING)
        prep_stmt = select(func.count(UnifiedOrder.id)).where(
            UnifiedOrder.status.in_([UnifiedOrderStatus.ACCEPTED, UnifiedOrderStatus.PREPARING])
        )
        if store_id:
            prep_stmt = prep_stmt.where(UnifiedOrder.store_id == store_id)
        preparing_count = (await db.execute(prep_stmt)).scalar() or 0

        # Ready
        ready_stmt = select(func.count(UnifiedOrder.id)).where(UnifiedOrder.status == UnifiedOrderStatus.READY)
        if store_id:
            ready_stmt = ready_stmt.where(UnifiedOrder.store_id == store_id)
        ready_count = (await db.execute(ready_stmt)).scalar() or 0

        # Delivering (PICKED_UP)
        delivering_stmt = select(func.count(UnifiedOrder.id)).where(UnifiedOrder.status == UnifiedOrderStatus.PICKED_UP)
        if store_id:
            delivering_stmt = delivering_stmt.where(UnifiedOrder.store_id == store_id)
        delivering_count = (await db.execute(delivering_stmt)).scalar() or 0

        # Delivered today
        delivered_stmt = select(func.count(UnifiedOrder.id)).where(
            UnifiedOrder.status == UnifiedOrderStatus.DELIVERED,
            UnifiedOrder.created_at >= start_of_day
        )
        if store_id:
            delivered_stmt = delivered_stmt.where(UnifiedOrder.store_id == store_id)
        delivered_today_count = (await db.execute(delivered_stmt)).scalar() or 0

        # Cancelled today
        cancelled_stmt = select(func.count(UnifiedOrder.id)).where(
            UnifiedOrder.status == UnifiedOrderStatus.CANCELLED,
            UnifiedOrder.created_at >= start_of_day
        )
        if store_id:
            cancelled_stmt = cancelled_stmt.where(UnifiedOrder.store_id == store_id)
        cancelled_today_count = (await db.execute(cancelled_stmt)).scalar() or 0

        # Revenue today (exclude CANCELLED and unaccepted PENDING orders)
        revenue_stmt = select(func.coalesce(func.sum(UnifiedOrder.total_amount), 0.0)).where(
            UnifiedOrder.status.not_in([UnifiedOrderStatus.CANCELLED, UnifiedOrderStatus.PENDING]),
            UnifiedOrder.created_at >= start_of_day
        )
        if store_id:
            revenue_stmt = revenue_stmt.where(UnifiedOrder.store_id == store_id)
        revenue_today = float((await db.execute(revenue_stmt)).scalar() or 0.0)

        return {
            "total_orders": total_orders,
            "pending_count": pending_count,
            "preparing_count": preparing_count,
            "ready_count": ready_count,
            "delivering_count": delivering_count,
            "delivered_today_count": delivered_today_count,
            "cancelled_today_count": cancelled_today_count,
            "revenue_today": revenue_today,
        }

    @staticmethod
    async def update_order_status(
        order_id: str,
        update_data: Optional[OrderStatusUpdateSchema] = None,
        db: Optional[AsyncSession] = None,
        payload: Optional[OrderStatusUpdateSchema] = None,
        sync_to_channel: bool = True,
        **kwargs
    ) -> UnifiedOrder:
        """
        Transition order status following the Unified State Machine rules.
        Also calls channel adapter to notify external partner (ShopeeFood / GrabMart) if sync_to_channel is True.
        Supports both 'update_data' and 'payload' keyword arguments for seamless adapter compatibility.
        """
        data = update_data or payload
        if not data:
            raise ValidationError("Dữ liệu cập nhật trạng thái (update_data/payload) không được để trống.")
        if db is None:
            raise ValidationError("Session cơ sở dữ liệu (db) không được để trống.")

        order_stmt = (
            select(UnifiedOrder)
            .where(UnifiedOrder.id == order_id)
            .options(
                selectinload(UnifiedOrder.items),
                selectinload(UnifiedOrder.status_history),
                selectinload(UnifiedOrder.store),
                selectinload(UnifiedOrder.channel),
            )
            .with_for_update()
        )
        order = (await db.execute(order_stmt)).scalar_one_or_none()
        if not order:
            raise NotFoundError("Order", order_id)

        current_status = order.status
        target_status = data.new_status

        # Validate transition
        allowed = VALID_STATUS_TRANSITIONS.get(current_status, [])
        if target_status not in allowed:
            raise ValidationError(
                f"Không thể chuyển trạng thái đơn hàng từ '{current_status.value}' sang '{target_status.value}'. "
                f"Các trạng thái hợp lệ tiếp theo: {[s.value for s in allowed]}"
            )

        # Update order entity
        order.status = target_status
        now_utc = datetime.now(timezone.utc)
        if data.estimated_ready_time:
            order.estimated_ready_time = data.estimated_ready_time
        if target_status == UnifiedOrderStatus.READY:
            if order.completed_at is None:
                order.completed_at = now_utc
        elif target_status == UnifiedOrderStatus.PICKED_UP:
            order.picked_up_at = now_utc
        elif target_status == UnifiedOrderStatus.DELIVERED:
            if order.picked_up_at is None:
                order.picked_up_at = now_utc
            order.completed_at = now_utc

        restocked_items: List[Dict[str, Any]] = []

        sku_qty_map: Dict[str, int] = {}
        for itm in (order.items or []):
            if itm.sku and itm.sku != "UNKNOWN":
                q = int(itm.quantity or 1)
                if q > 0:
                    sku_qty_map[itm.sku] = sku_qty_map.get(itm.sku, 0) + q
        sorted_skus = sorted(sku_qty_map.keys())

        # Release virtual reservation on PICKED_UP / DELIVERED (NEVER touch total_stock; Poppies CTTK is sole physical source)
        if target_status in (UnifiedOrderStatus.PICKED_UP, UnifiedOrderStatus.DELIVERED) and current_status not in (
            UnifiedOrderStatus.PICKED_UP,
            UnifiedOrderStatus.DELIVERED,
            UnifiedOrderStatus.CANCELLED,
        ):
            if order.stock_reservation_status == "RESERVED":
                if sorted_skus:
                    inv_by_sku = await OrderService._lock_inventories_sorted(order.store_id, sorted_skus, db)
                    for sku_val in sorted_skus:
                        inv_obj = inv_by_sku.get(sku_val)
                        if inv_obj:
                            qty_val = float(sku_qty_map[sku_val])
                            reserved_before = float(inv_obj.reserved_stock or 0.0)
                            inv_obj.reserved_stock = max(0.0, reserved_before - qty_val)
                            inv_obj.available_stock = InventoryService.compute_available_stock(
                                float(inv_obj.total_stock or 0.0),
                                float(inv_obj.reserved_stock),
                                float(inv_obj.safety_stock or 0.0),
                            )
                            inv_obj.is_out_of_stock = inv_obj.available_stock <= 0.0
                            db.add(
                                InventoryStockLog(
                                    store_id=order.store_id,
                                    sku=sku_val,
                                    change_type="ORDER_COMPLETE",
                                    quantity_before=reserved_before,
                                    quantity_delta=-qty_val,
                                    quantity_after=float(inv_obj.reserved_stock),
                                    reference_id=order.order_code,
                                    note=f"Giải phóng giữ tồn ảo hoàn tất đơn {order.order_code}",
                                )
                            )
                order.stock_reservation_status = "CONSUMED"
        elif target_status == UnifiedOrderStatus.CANCELLED and current_status != UnifiedOrderStatus.CANCELLED:
            order.cancellation_reason = data.cancellation_reason or data.note
            order.cancelled_by = data.changed_by
            # Only auto-release virtual reservation if order was NOT already picked up and is still RESERVED
            if current_status != UnifiedOrderStatus.PICKED_UP and order.stock_reservation_status == "RESERVED":
                if sorted_skus:
                    inv_by_sku = await OrderService._lock_inventories_sorted(order.store_id, sorted_skus, db)
                    for sku_val in sorted_skus:
                        inv_obj = inv_by_sku.get(sku_val)
                        if inv_obj:
                            qty_val = float(sku_qty_map[sku_val])
                            qty_before = float(inv_obj.available_stock or 0.0)
                            was_oos = bool(inv_obj.is_out_of_stock)
                            inv_obj.reserved_stock = max(0.0, float(inv_obj.reserved_stock or 0.0) - qty_val)
                            inv_obj.available_stock = InventoryService.compute_available_stock(
                                float(inv_obj.total_stock or 0.0),
                                float(inv_obj.reserved_stock),
                                float(inv_obj.safety_stock or 0.0),
                            )
                            inv_obj.is_out_of_stock = inv_obj.available_stock <= 0.0
                            db.add(
                                InventoryStockLog(
                                    store_id=order.store_id,
                                    sku=sku_val,
                                    change_type="ORDER_CANCEL",
                                    quantity_before=qty_before,
                                    quantity_delta=qty_val,
                                    quantity_after=float(inv_obj.available_stock),
                                    reference_id=order.order_code,
                                    note=f"Hoàn tồn ảo do hủy đơn {order.order_code}",
                                )
                            )
                            if was_oos and not inv_obj.is_out_of_stock:
                                restocked_items.append({
                                    "sku": sku_val,
                                    "available_stock": int(inv_obj.available_stock),
                                    "is_out_of_stock": False,
                                })
                order.stock_reservation_status = "RELEASED"

        # Add history log
        history = OrderStatusHistory(
            order_id=order.id,
            from_status=current_status,
            to_status=target_status,
            note=data.note,
            changed_by=data.changed_by
        )
        db.add(history)
        await db.commit()

        # Propagate restocked items back to external channels if any SKU flipped from OOS to available
        if restocked_items:
            try:
                mappings_res = await db.execute(
                    select(StoreChannelMapping)
                    .options(selectinload(StoreChannelMapping.channel))
                    .where(StoreChannelMapping.store_id == order.store_id, StoreChannelMapping.is_active == True)
                )
                for m in mappings_res.scalars().all():
                    if m.channel and m.partner_store_id:
                        ch_adapter = channel_registry.get(m.channel.code)
                        if ch_adapter:
                            await ch_adapter.sync_inventory(
                                store_id=order.store_id,
                                partner_store_id=m.partner_store_id,
                                stock_items=restocked_items,
                                db=db,
                            )
            except Exception as sync_ex:
                logger.warning(f"Could not propagate restocked items after cancelling order {order.order_code}: {sync_ex}")

        # Notify external channel adapter if enabled and requested
        if sync_to_channel:
            channel_stmt = select(Channel).where(Channel.id == order.channel_id)
            channel_res = await db.execute(channel_stmt)
            channel = channel_res.scalars().first()

            if channel:
                adapter = channel_registry.get(channel.code)
                if adapter:
                    try:
                        # Look up partner_store_id
                        mapping_stmt = select(StoreChannelMapping).where(
                            StoreChannelMapping.channel_id == channel.id,
                            StoreChannelMapping.store_id == order.store_id
                        )
                        mapping_res = await db.execute(mapping_stmt)
                        mapping = mapping_res.scalars().first()
                        partner_store_id = mapping.partner_store_id if mapping else ""

                        await adapter.update_order_status(
                            channel_order_id=order.channel_order_id,
                            partner_store_id=partner_store_id,
                            new_status=target_status,
                            db=db,
                            reason=data.cancellation_reason
                        )
                    except Exception as ex:
                        logger.error(f"Lỗi khi gửi cập nhật trạng thái lên sàn {channel.code}: {str(ex)}")

        return await OrderService.get_order_by_id(order.id, db)

    @staticmethod
    async def reconcile_edited_inbound_order(
        order_id: str,
        order_data: Dict[str, Any],
        items_data: List[Dict[str, Any]],
        db: AsyncSession,
        changed_by: str = "CHANNEL_WEBHOOK",
        note: str = "Cập nhật danh sách món và tổng tiền từ sàn",
    ) -> UnifiedOrder:
        """
        Idempotently reconcile an edited order's line items, financials, and virtual stock
        reservation delta (`new_qty - old_qty`) under row locks.
        """
        stmt = (
            select(UnifiedOrder)
            .where(UnifiedOrder.id == order_id)
            .options(
                selectinload(UnifiedOrder.items),
                selectinload(UnifiedOrder.status_history),
                selectinload(UnifiedOrder.store),
                selectinload(UnifiedOrder.channel),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        order = (await db.execute(stmt)).scalar_one_or_none()
        if not order:
            raise NotFoundError("Order", order_id)

        if order.status == UnifiedOrderStatus.CANCELLED:
            logger.info(f"Bỏ qua đồng bộ sửa món cho đơn đã hủy {order.order_code}.")
            return order

        old_sku_qty: Dict[str, int] = {}
        for old_itm in (order.items or []):
            sku_key = str(old_itm.sku or "").strip()
            if sku_key and sku_key != "UNKNOWN":
                q = int(old_itm.quantity or 0)
                if q > 0:
                    old_sku_qty[sku_key] = old_sku_qty.get(sku_key, 0) + q

        new_sku_qty: Dict[str, int] = {}
        for new_itm in (items_data or []):
            sku_key = str(new_itm.get("sku") or "").strip()
            if sku_key and sku_key != "UNKNOWN":
                q = int(new_itm.get("quantity") or 0)
                if q > 0:
                    new_sku_qty[sku_key] = new_sku_qty.get(sku_key, 0) + q

        stock_sync_items: List[Dict[str, Any]] = []
        all_skus = sorted(set(old_sku_qty) | set(new_sku_qty))

        if order.stock_reservation_status == "RESERVED" and all_skus:
            delta_skus = [s for s in all_skus if (new_sku_qty.get(s, 0) - old_sku_qty.get(s, 0)) != 0]
            if delta_skus:
                inv_by_sku = await OrderService._lock_inventories_sorted(order.store_id, sorted(delta_skus), db)
                settings = get_settings()
                for sku_val in sorted(delta_skus):
                    delta = float(new_sku_qty.get(sku_val, 0) - old_sku_qty.get(sku_val, 0))
                    inv_obj = inv_by_sku.get(sku_val)
                    if inv_obj is None:
                        continue
                    avail_before = float(inv_obj.available_stock or 0.0)
                    was_oos = bool(inv_obj.is_out_of_stock)
                    if delta > 0 and settings.VIRTUAL_STOCK_STRICT_MODE and avail_before < delta:
                        raise ValidationError(
                            f"Tồn kho khả dụng không đủ để tăng/thêm SKU '{sku_val}' cho đơn {order.order_code}: "
                            f"cần thêm {delta}, còn {avail_before}."
                        )
                    reserved_before = float(inv_obj.reserved_stock or 0.0)
                    inv_obj.reserved_stock = max(0.0, reserved_before + delta)
                    inv_obj.available_stock = InventoryService.compute_available_stock(
                        float(inv_obj.total_stock or 0.0),
                        float(inv_obj.reserved_stock),
                        float(inv_obj.safety_stock or 0.0),
                    )
                    inv_obj.is_out_of_stock = inv_obj.available_stock <= 0.0
                    db.add(
                        InventoryStockLog(
                            store_id=order.store_id,
                            sku=sku_val,
                            change_type="ORDER_EDIT",
                            quantity_before=avail_before,
                            quantity_delta=-delta,
                            quantity_after=float(inv_obj.available_stock),
                            reference_id=order.order_code,
                            note=f"Điều chỉnh giữ tồn ảo do sửa đơn {order.order_code} (delta={int(delta):+d})",
                        )
                    )
                    if (not was_oos and inv_obj.is_out_of_stock) or (was_oos and not inv_obj.is_out_of_stock):
                        stock_sync_items.append(
                            {
                                "sku": sku_val,
                                "available_stock": max(0, int(inv_obj.available_stock)),
                                "is_out_of_stock": inv_obj.is_out_of_stock,
                            }
                        )

        # Replace OrderItems safely when items_data is supplied
        if items_data:
            order.items.clear()
            await db.flush()

            sku_list = list({str(it.get("sku") or "").strip() for it in items_data if it.get("sku") and it.get("sku") != "UNKNOWN"})
            prod_id_map: Dict[str, str] = {}
            if sku_list:
                p_rows = (await db.execute(select(Product).where(Product.sku.in_(sku_list)))).scalars().all()
                prod_id_map = {p.sku: p.id for p in p_rows}

            for item in items_data:
                sku_val = str(item.get("sku") or "UNKNOWN").strip()
                qty_val = max(1, int(item.get("quantity", 1)))
                unit_p = float(item.get("unit_price", 0.0))
                total_p = float(item.get("total_price", unit_p * qty_val))
                order.items.append(
                    OrderItem(
                        order_id=order.id,
                        product_id=item.get("product_id") or prod_id_map.get(sku_val),
                        sku=sku_val,
                        item_name=item.get("item_name") or "Unknown Item",
                        quantity=qty_val,
                        unit_price=unit_p,
                        total_price=total_p,
                        notes=item.get("notes"),
                        modifiers=item.get("modifiers"),
                    )
                )

        for fld in ("subtotal_amount", "discount_amount", "delivery_fee", "total_amount"):
            if fld in order_data and order_data[fld] is not None:
                setattr(order, fld, float(order_data[fld]))

        if isinstance(order_data.get("raw_payload"), dict):
            merged_raw = dict(order_data["raw_payload"])
            prev_raw = order.raw_payload if isinstance(order.raw_payload, dict) else {}
            if prev_raw.get("_ump_edited") and not merged_raw.get("_ump_edited"):
                merged_raw["_ump_edited"] = True
            order.raw_payload = merged_raw

        db.add(
            OrderStatusHistory(
                order_id=order.id,
                from_status=order.status,
                to_status=order.status,
                note=note,
                changed_by=changed_by,
            )
        )
        await db.commit()

        if stock_sync_items:
            try:
                mappings_res = await db.execute(
                    select(StoreChannelMapping)
                    .options(selectinload(StoreChannelMapping.channel))
                    .where(StoreChannelMapping.store_id == order.store_id, StoreChannelMapping.is_active == True)
                )
                for m in mappings_res.scalars().all():
                    if m.channel and m.partner_store_id:
                        ch_adapter = channel_registry.get(m.channel.code)
                        if ch_adapter:
                            await ch_adapter.sync_inventory(
                                store_id=order.store_id,
                                partner_store_id=m.partner_store_id,
                                stock_items=stock_sync_items,
                                db=db,
                            )
            except Exception as sync_ex:
                logger.warning(f"Could not propagate inventory delta after editing order {order.order_code}: {sync_ex}")

        return await OrderService.get_order_by_id(order.id, db)

    @staticmethod
    async def edit_order(
        order_id: str,
        payload: OrderEditRequest,
        changed_by: str,
        db: AsyncSession,
    ) -> UnifiedOrder:
        """
        Validate, push outbound edit request to supported channel (e.g., GrabMart),
        and update local UnifiedOrder items & virtual stock delta.
        """
        if not payload.customer_confirmed:
            raise ValidationError("Bắt buộc phải xác nhận đã trao đổi và được khách hàng đồng ý trước khi sửa đơn.")

        order = await OrderService.get_order_by_id(order_id, db)
        editable_statuses = (
            UnifiedOrderStatus.PENDING,
            UnifiedOrderStatus.ACCEPTED,
            UnifiedOrderStatus.PREPARING,
        )
        if order.status not in editable_statuses:
            raise ValidationError(
                f"Chỉ được phép chỉnh sửa đơn ở trạng thái PENDING / ACCEPTED / PREPARING (hiện tại: {order.status.value})."
            )

        active_items = [it for it in payload.items if it.status != "DELETED" and it.quantity > 0]
        if not active_items:
            raise ValidationError("Đơn hàng sau chỉnh sửa phải còn ít nhất 1 món có số lượng >= 1.")

        channel = (
            await db.execute(select(Channel).where(Channel.id == order.channel_id))
        ).scalars().first()
        if not channel:
            raise NotFoundError("Channel", order.channel_id)

        adapter = channel_registry.get(channel.code)
        if not adapter:
            raise ValidationError(f"Không tìm thấy Channel Adapter cho kênh {channel.code}.")

        mapping = (
            await db.execute(
                select(StoreChannelMapping).where(
                    StoreChannelMapping.channel_id == channel.id,
                    StoreChannelMapping.store_id == order.store_id,
                )
            )
        ).scalars().first()
        partner_store_id = mapping.partner_store_id if mapping else ""

        raw_payload = dict(order.raw_payload) if isinstance(order.raw_payload, dict) else {}
        raw_items_list = list(raw_payload.get("items") or []) if isinstance(raw_payload.get("items"), list) else []
        raw_by_sku = {
            str(it.get("id") or "").strip(): it
            for it in raw_items_list
            if isinstance(it, dict) and str(it.get("id") or "").strip()
        }
        existing_by_sku = {
            str(it.sku or "").strip(): it
            for it in (order.items or [])
            if str(it.sku or "").strip()
        }

        channel_items_payload: List[Dict[str, Any]] = []
        added_payloads: List[Dict[str, Any]] = []
        for it in payload.items:
            sku_str = it.sku.strip()
            raw_meta = raw_by_sku.get(sku_str, {})
            sold_by_weight = bool(raw_meta.get("soldByWeight", False))
            if it.status != "ADDED" and (it.adjusted_price_in_min is not None or it.adjusted_weight is not None):
                if not sold_by_weight:
                    raise ValidationError(f"Món '{sku_str}' không phải hàng cân ký (soldByWeight=true), không thể sửa giá/trọng lượng trực tiếp.")

            entry = {
                "sku": sku_str,
                "grab_item_id": it.grab_item_id or raw_meta.get("grabItemID") or sku_str,
                "status": it.status,
                "quantity": 0 if it.status == "DELETED" else max(1, int(it.quantity)),
                "adjusted_price_in_min": it.adjusted_price_in_min,
                "adjusted_weight": it.adjusted_weight,
                "sold_by_weight": sold_by_weight,
                "is_external_item_id": (it.status == "ADDED") or not bool(raw_meta.get("grabItemID") or it.grab_item_id),
            }
            if it.status == "ADDED":
                added_payloads.append(entry)
            else:
                channel_items_payload.append(entry)

        # GrabMart spec requires ADDED replacement items at the end of the array
        ordered_channel_items = [*channel_items_payload, *added_payloads]

        try:
            await adapter.edit_order(
                channel_order_id=order.channel_order_id,
                partner_store_id=partner_store_id,
                items=ordered_channel_items,
                db=db,
                only_recalculate=payload.only_recalculate,
            )
        except NotImplementedError as nie:
            raise ValidationError(str(nie))

        if payload.only_recalculate:
            return order

        # Build updated local OrderItems & estimated totals (authoritative recalculation will also arrive via webhook)
        added_skus = [it.sku.strip() for it in active_items if it.sku.strip() not in existing_by_sku]
        prod_map: Dict[str, Product] = {}
        if added_skus:
            p_rows = (await db.execute(select(Product).where(Product.sku.in_(added_skus)))).scalars().all()
            prod_map = {p.sku: p for p in p_rows}

        currency_obj = raw_payload.get("currency") if isinstance(raw_payload.get("currency"), dict) else {}
        try:
            exp_val = max(0, int(currency_obj.get("exponent") or 0))
        except (TypeError, ValueError):
            exp_val = 0
        divisor = float(10 ** exp_val) if exp_val > 0 else 1.0

        updated_items_data: List[Dict[str, Any]] = []
        new_subtotal = 0.0
        for it in active_items:
            sku_str = it.sku.strip()
            existing_itm = existing_by_sku.get(sku_str)
            prod_obj = prod_map.get(sku_str)
            if existing_itm is not None:
                unit_p = (
                    (float(it.adjusted_price_in_min) / divisor)
                    if it.adjusted_price_in_min is not None
                    else float(it.unit_price if it.unit_price is not None else existing_itm.unit_price)
                )
                itm_name = it.item_name or existing_itm.item_name
                mods = existing_itm.modifiers
                notes = existing_itm.notes
                pid = existing_itm.product_id
            else:
                unit_p = float(
                    it.unit_price
                    if it.unit_price is not None
                    else (prod_obj.base_price if prod_obj and prod_obj.base_price is not None else 0.0)
                )
                itm_name = it.item_name or (prod_obj.name if prod_obj else f"SKU {sku_str}")
                mods = None
                notes = "Món thay thế theo xác nhận khách"
                pid = prod_obj.id if prod_obj else None

            qty = max(1, int(it.quantity))
            line_total = round(unit_p * qty, 2)
            new_subtotal += line_total
            updated_items_data.append(
                {
                    "product_id": pid,
                    "sku": sku_str,
                    "item_name": itm_name,
                    "quantity": qty,
                    "unit_price": unit_p,
                    "total_price": line_total,
                    "notes": notes,
                    "modifiers": mods,
                }
            )

        discount_amt = float(order.discount_amount or 0.0)
        delivery_fee = float(order.delivery_fee or 0.0)
        new_total = max(0.0, round(new_subtotal - discount_amt + delivery_fee, 2))

        raw_payload["_ump_edited"] = True
        raw_payload["_ump_edit_meta"] = {
            "edited_by": changed_by,
            "reason": payload.edit_reason,
            "edited_at": datetime.now(timezone.utc).isoformat(),
        }
        updated_raw_items = []
        for it in active_items:
            sku_str = it.sku.strip()
            prev_raw_item = dict(raw_by_sku.get(sku_str) or {})
            if prev_raw_item:
                prev_raw_item["quantity"] = int(it.quantity)
                if it.adjusted_price_in_min is not None:
                    prev_raw_item["price"] = int(it.adjusted_price_in_min)
                if it.adjusted_weight is not None:
                    prev_raw_item["weight"] = float(it.adjusted_weight)
                updated_raw_items.append(prev_raw_item)
            else:
                matched_new = next((x for x in updated_items_data if x["sku"] == sku_str), {})
                updated_raw_items.append(
                    {
                        "id": sku_str,
                        "name": matched_new.get("item_name") or sku_str,
                        "quantity": int(it.quantity),
                        "price": int(round(float(matched_new.get("unit_price", 0.0)) * divisor)),
                    }
                )
        raw_payload["items"] = updated_raw_items

        order_data = {
            "subtotal_amount": round(new_subtotal, 2),
            "discount_amount": discount_amt,
            "delivery_fee": delivery_fee,
            "total_amount": new_total,
            "raw_payload": raw_payload,
        }

        return await OrderService.reconcile_edited_inbound_order(
            order_id=order.id,
            order_data=order_data,
            items_data=updated_items_data,
            db=db,
            changed_by=changed_by,
            note=f"Sửa đơn ({payload.edit_reason})",
        )
