from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.responses import APIResponse
from app.models.order import UnifiedOrderStatus
from app.schemas.common import PaginatedResponse, PaginationMeta
from app.schemas.order import (
    OrderFilterParams,
    OrderStatusUpdateSchema,
    UnifiedOrderResponse,
)
from app.services.order_service import OrderService

router = APIRouter(prefix="/orders", tags=["Unified Orders"])


@router.get("", response_model=APIResponse[PaginatedResponse[UnifiedOrderResponse]])
async def list_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    store_id: Optional[str] = Query(None, description="Filter by store UUID"),
    channel_id: Optional[str] = Query(None, description="Filter by channel UUID"),
    status: Optional[UnifiedOrderStatus] = Query(None, description="Filter by unified order status"),
    search: Optional[str] = Query(None, description="Search by order code, customer name, phone, etc."),
    from_date: Optional[datetime] = Query(None),
    to_date: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """
    Omnichannel Unified Order Feed.
    Returns real-time orders from all platforms (ShopeeFood, GrabMart, Shopee) in one single place.
    """
    params = OrderFilterParams(
        page=page,
        page_size=page_size,
        store_id=store_id,
        channel_id=channel_id,
        status=status,
        search=search,
        from_date=from_date,
        to_date=to_date
    )
    items, total = await OrderService.list_orders(params, db)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return APIResponse.ok(
        data=PaginatedResponse(
            items=items,
            meta=PaginationMeta(
                page=page,
                page_size=page_size,
                total_items=total,
                total_pages=total_pages
            )
        )
    )


@router.get("/{order_id}", response_model=APIResponse[UnifiedOrderResponse])
async def get_order_detail(order_id: str, db: AsyncSession = Depends(get_db)):
    """Get complete order details including items and state transition history."""
    order = await OrderService.get_order_by_id(order_id, db)
    return APIResponse.ok(data=order)


@router.patch("/{order_id}/status", response_model=APIResponse[UnifiedOrderResponse])
async def update_order_status(
    order_id: str,
    payload: OrderStatusUpdateSchema,
    db: AsyncSession = Depends(get_db)
):
    """
    Transition order status (e.g. ACCEPTED -> PREPARING -> READY -> PICKED_UP -> DELIVERED).
    Validates state machine transitions and automatically notifies the external channel.
    """
    updated_order = await OrderService.update_order_status(order_id, payload, db)
    return APIResponse.ok(
        data=updated_order,
        message=f"Đã cập nhật trạng thái đơn hàng sang: {payload.new_status.value}"
    )
