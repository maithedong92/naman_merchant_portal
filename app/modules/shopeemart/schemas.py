from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ShopeeMartAuthPayload(BaseModel):
    partner_id: int
    shop_id: int
    access_token: str


class ShopeeMartItemUpdate(BaseModel):
    item_id: int
    seller_sku: str
    normal_stock: int
    price: Optional[float] = None


class ShopeeMartStockUpdateBatch(BaseModel):
    items: List[ShopeeMartItemUpdate]


class ShopeeMartOrderItem(BaseModel):
    item_id: int
    item_name: str
    item_sku: str
    model_quantity_purchased: int
    model_discounted_price: float


class ShopeeMartOrderWebhookPayload(BaseModel):
    order_sn: str = Field(..., description="Shopee Order Serial Number")
    status: str = Field(..., description="READY_TO_SHIP, SHIPPED, COMPLETED, CANCELLED")
    buyer_username: Optional[str] = None
    buyer_phone: Optional[str] = None
    recipient_address: Optional[Dict[str, Any]] = None
    total_amount: float = 0.0
    shipping_carrier: Optional[str] = None
    item_list: List[ShopeeMartOrderItem] = []
    create_time: int = 0
    update_time: int = 0
