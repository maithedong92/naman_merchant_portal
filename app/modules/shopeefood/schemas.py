from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class FoodyHeaderModel(BaseModel):
    app_id: str = Field(..., alias="X-Foody-App-Id")
    api_version: str = Field("1", alias="X-Foody-Api-Version")
    request_id: str = Field(..., alias="X-Foody-Request-Id")
    country: str = Field("VN", alias="X-Foody-Country")
    language: Optional[str] = Field("vi", alias="X-Foody-Language")


class ShopeeFoodMenuItem(BaseModel):
    id: str
    name: str
    price: float
    description: Optional[str] = None
    availableStatus: str = "AVAILABLE"  # AVAILABLE | UNAVAILABLE
    photos: List[str] = []
    sequence: int = 0


class ShopeeFoodCategory(BaseModel):
    id: str
    name: str
    sequence: int = 0
    sort_type: int = 1
    availableStatus: str = "AVAILABLE"
    items: List[ShopeeFoodMenuItem] = []


class ShopeeFoodMenuSection(BaseModel):
    id: int = 1
    name: str = "Regular Menu"
    serviceHours: Optional[dict] = None
    categories: List[ShopeeFoodCategory] = []


class ShopeeFoodMenuPayload(BaseModel):
    merchantID: Optional[str] = None
    partnerMerchantID: str
    currency: dict = {"code": "VND", "symbol": "₫", "exponent": 0}
    sections: List[ShopeeFoodMenuSection] = []


class ShopeeFoodOrderWebhookPayload(BaseModel):
    partner_restaurant_id: str
    order_id: str
    display_order_id: Optional[str] = None
    order_status: int = 1
    subtotal: float = 0.0
    discount: float = 0.0
    shipping_fee: float = 0.0
    total_amount: float = 0.0
    customer: Optional[Dict[str, Any]] = None
    driver: Optional[Dict[str, Any]] = None
    items: List[Dict[str, Any]] = []
