from datetime import datetime
from decimal import Decimal
from typing import Any, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.models.order import UnifiedOrderStatus


class OrderItemBase(BaseModel):
    sku: str
    item_name: str
    quantity: int = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)
    total_price: float = Field(..., ge=0)
    notes: Optional[str] = None
    modifiers: Optional[Any] = None


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
    subtotal_amount: Union[float, Decimal] = 0.0
    discount_amount: Union[float, Decimal] = 0.0
    delivery_fee: Union[float, Decimal] = 0.0
    total_amount: Union[float, Decimal] = 0.0
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    delivery_address: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    order_time: Optional[datetime] = None
    estimated_ready_time: Optional[datetime] = None

    @field_validator("subtotal_amount", "discount_amount", "delivery_fee", "total_amount", mode="before")
    @classmethod
    def _coerce_financial_float(cls, v: Any) -> float:
        if v is None:
            return 0.0
        return float(v)


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
    virtual_contact: Optional[dict] = None
    is_edited: bool = False
    grab_edit_context: Optional[dict] = None


class OrderEditItemInput(BaseModel):
    sku: str = Field(..., min_length=1, max_length=100, description="Nam An SKU or item code")
    grab_item_id: Optional[str] = Field(None, description="GrabMart grabItemID / item.id for original items")
    status: Literal["UNCHANGED", "UPDATED", "DELETED", "ADDED"]
    quantity: int = Field(1, ge=0, description="Target quantity (0 allowed when status=DELETED)")
    adjusted_price_in_min: Optional[int] = Field(None, ge=0, description="Weighted item adjusted price in minor unit")
    adjusted_weight: Optional[float] = Field(None, gt=0, description="Weighted item adjusted weight (soldByWeight only)")
    item_name: Optional[str] = Field(None, max_length=255)
    unit_price: Optional[float] = Field(None, ge=0)


class OrderEditRequest(BaseModel):
    items: List[OrderEditItemInput] = Field(..., min_length=1)
    customer_confirmed: bool = Field(..., description="Operator confirms customer agreed to changes")
    edit_reason: str = Field(..., min_length=2, max_length=300, description="Reason for editing the order")
    only_recalculate: bool = Field(default=False, description="GrabMart test simulation flag")


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
    status: Optional[str] = None
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
