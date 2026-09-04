import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import AppException, ConflictError, NotFoundError, ValidationError
from app.models.channel import Channel
from app.models.order import OrderItem, OrderStatusHistory, UnifiedOrder, UnifiedOrderStatus
from app.models.store import Store, StoreChannelMapping
from app.schemas.order import OrderFilterParams, OrderStatusUpdateSchema
from app.services.channel_registry import channel_registry

logger = logging.getLogger("naman_portal.order_service")

# State machine allowed transitions
VALID_STATUS_TRANSITIONS: Dict[UnifiedOrderStatus, List[UnifiedOrderStatus]] = {
    UnifiedOrderStatus.PENDING: [UnifiedOrderStatus.ACCEPTED, UnifiedOrderStatus.CANCELLED],
    UnifiedOrderStatus.ACCEPTED: [UnifiedOrderStatus.PREPARING, UnifiedOrderStatus.CANCELLED],
    UnifiedOrderStatus.PREPARING: [UnifiedOrderStatus.READY, UnifiedOrderStatus.CANCELLED],
    UnifiedOrderStatus.READY: [UnifiedOrderStatus.PICKED_UP, UnifiedOrderStatus.CANCELLED],
    UnifiedOrderStatus.PICKED_UP: [UnifiedOrderStatus.DELIVERED],
    UnifiedOrderStatus.DELIVERED: [],
    UnifiedOrderStatus.CANCELLED: [],
}


class OrderService:
    """Core Service managing omnichannel Unified Orders and State Transitions."""

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
        # 1. Resolve Channel
        channel_stmt = select(Channel).where(Channel.code == channel_code.upper())
        channel_res = await db.execute(channel_stmt)
        channel = channel_res.scalar_one_or_none()
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

        # 3. Resolve Store by partner_store_id
        mapping_stmt = select(StoreChannelMapping).where(
            StoreChannelMapping.channel_id == channel.id,
            StoreChannelMapping.partner_store_id == str(partner_store_id)
        ).options(selectinload(StoreChannelMapping.store))
        mapping_res = await db.execute(mapping_stmt)
        mapping = mapping_res.scalar_one_or_none()
        
        if not mapping:
            # Fallback: Find any active store if not mapped yet
            fallback_stmt = select(Store).limit(1)
            fallback_res = await db.execute(fallback_stmt)
            store = fallback_res.scalar_one_or_none()
            if not store:
                raise AppException(f"Không tìm thấy Store tương ứng với partner ID: {partner_store_id}")
            store_id = store.id
        else:
            store_id = mapping.store_id

        # 4. Generate Internal Order Code: NAM-<CHANNEL_CODE>-YYYYMMDD-<COUNT>
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        order_code = f"NAM-{channel_code[:2]}-{today_str}-{channel_order_id[-6:]}"

        # 5. Construct Unified Order
        order = UnifiedOrder(
            order_code=order_code,
            channel_id=channel.id,
            store_id=store_id,
            channel_order_id=str(channel_order_id),
            display_order_id=order_data.get("display_order_id") or channel_order_id[-4:],
            status=order_data.get("initial_status", UnifiedOrderStatus.PENDING),
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

        # 6. Add Items
        for item in items_data:
            order_item = OrderItem(
                order_id=order.id,
                product_id=item.get("product_id"),
                sku=item.get("sku", "UNKNOWN"),
                item_name=item.get("item_name", "Unknown Item"),
                quantity=int(item.get("quantity", 1)),
                unit_price=float(item.get("unit_price", 0.0)),
                total_price=float(item.get("total_price", 0.0)),
                notes=item.get("notes"),
                modifiers=item.get("modifiers")
            )
            db.add(order_item)

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

        # Refresh order with relations
        return await OrderService.get_order_by_id(order.id, db), True

    @staticmethod
    async def get_order_by_id(order_id: str, db: AsyncSession) -> UnifiedOrder:
        """Fetch single order by internal UUID with full items and status history."""
        stmt = select(UnifiedOrder).where(UnifiedOrder.id == order_id).options(
            selectinload(UnifiedOrder.items),
            selectinload(UnifiedOrder.status_history),
            selectinload(UnifiedOrder.store),
            selectinload(UnifiedOrder.channel)
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
            selectinload(UnifiedOrder.status_history)
        )

        if params.store_id:
            query = query.where(UnifiedOrder.store_id == params.store_id)
        if params.channel_id:
            query = query.where(UnifiedOrder.channel_id == params.channel_id)
        if params.status:
            query = query.where(UnifiedOrder.status == params.status)
        if params.from_date:
            query = query.where(UnifiedOrder.created_at >= params.from_date)
        if params.to_date:
            query = query.where(UnifiedOrder.created_at <= params.to_date)
        if params.search:
            search_pattern = f"%{params.search}%"
            query = query.where(
                (UnifiedOrder.order_code.ilike(search_pattern)) |
                (UnifiedOrder.channel_order_id.ilike(search_pattern)) |
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
    async def update_order_status(
        order_id: str,
        update_data: OrderStatusUpdateSchema,
        db: AsyncSession
    ) -> UnifiedOrder:
        """
        Transition order status following the Unified State Machine rules.
        Also calls channel adapter to notify external partner (ShopeeFood / GrabMart).
        """
        order = await OrderService.get_order_by_id(order_id, db)
        current_status = order.status
        target_status = update_data.new_status

        # Validate transition
        allowed = VALID_STATUS_TRANSITIONS.get(current_status, [])
        if target_status not in allowed:
            raise ValidationError(
                f"Không thể chuyển trạng thái đơn hàng từ '{current_status.value}' sang '{target_status.value}'. "
                f"Các trạng thái hợp lệ tiếp theo: {[s.value for s in allowed]}"
            )

        # Update order entity
        order.status = target_status
        if update_data.estimated_ready_time:
            order.estimated_ready_time = update_data.estimated_ready_time
        if target_status == UnifiedOrderStatus.READY:
            order.completed_at = datetime.now(timezone.utc)
        elif target_status == UnifiedOrderStatus.PICKED_UP:
            order.picked_up_at = datetime.now(timezone.utc)
        elif target_status == UnifiedOrderStatus.CANCELLED:
            order.cancellation_reason = update_data.cancellation_reason or update_data.note
            order.cancelled_by = update_data.changed_by

        # Add history log
        history = OrderStatusHistory(
            order_id=order.id,
            from_status=current_status,
            to_status=target_status,
            note=update_data.note,
            changed_by=update_data.changed_by
        )
        db.add(history)
        await db.commit()

        # Notify external channel adapter if available
        channel_stmt = select(Channel).where(Channel.id == order.channel_id)
        channel_res = await db.execute(channel_stmt)
        channel = channel_res.scalar_one_or_none()

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
                    mapping = mapping_res.scalar_one_or_none()
                    partner_store_id = mapping.partner_store_id if mapping else ""

                    await adapter.update_order_status(
                        channel_order_id=order.channel_order_id,
                        partner_store_id=partner_store_id,
                        new_status=target_status,
                        db=db,
                        reason=update_data.cancellation_reason
                    )
                except Exception as ex:
                    logger.error(f"Lỗi khi gửi cập nhật trạng thái lên sàn {channel.code}: {str(ex)}")

        return await OrderService.get_order_by_id(order.id, db)
