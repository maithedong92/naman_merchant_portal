from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field
from app.models.order import UnifiedOrderStatus


class OrderItemBase(BaseModel):
    sku: str
    item_name: str
    quantity: int = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)
    total_price: float = Field(..., ge=0)
    notes: Optional[str] = None
    modifiers: Optional[dict] = None


class OrderItemResponse(OrderItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    order_id: str
    product_id: Optional[str] = None


class OrderStatusHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    from_status: Optional[UnifiedOrderStatus] = None
    to_status: UnifiedOrderStatus
    note: Optional[str] = None
    changed_by: str
    created_at: datetime


class UnifiedOrderBase(BaseModel):
    order_code: str
    channel_order_id: str
    display_order_id: Optional[str] = None
    status: UnifiedOrderStatus
    subtotal_amount: float
    discount_amount: float = 0.0
    delivery_fee: float = 0.0
    total_amount: float
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    delivery_address: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    order_time: Optional[datetime] = None
    estimated_ready_time: Optional[datetime] = None


class UnifiedOrderResponse(UnifiedOrderBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    channel_id: str
    store_id: str
    channel_code: Optional[str] = None
    channel_name: Optional[str] = None
    store_code: Optional[str] = None
    store_name: Optional[str] = None
    cancellation_reason: Optional[str] = None
    cancelled_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    items: List[OrderItemResponse] = []
    status_history: List[OrderStatusHistoryResponse] = []


class OrderStatusUpdateSchema(BaseModel):
    new_status: UnifiedOrderStatus = Field(..., description="Target status in unified state machine")
    note: Optional[str] = None
    changed_by: str = Field(default="PORTAL_STAFF", description="Staff username or system worker")
    estimated_ready_time: Optional[datetime] = None
    cancellation_reason: Optional[str] = None


class OrderFilterParams(BaseModel):
    store_id: Optional[str] = None
    store_code: Optional[str] = None
    channel_id: Optional[str] = None
    channel_code: Optional[str] = None
    status: Optional[UnifiedOrderStatus] = None
    from_date: Optional[datetime] = None
    to_date: Optional[datetime] = None
    search: Optional[str] = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class OrderSummaryResponse(BaseModel):
    total_orders: int = 0
    pending_count: int = 0
    preparing_count: int = 0
    ready_count: int = 0
    delivering_count: int = 0
    delivered_today_count: int = 0
    cancelled_today_count: int = 0
    revenue_today: float = 0.0
