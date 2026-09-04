from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class GrabOAuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


class GrabMenuItem(BaseModel):
    id: str
    name: str
    price: int  # Grab expects price in minor units or integer VND
    availableStatus: str = "AVAILABLE"  # AVAILABLE | UNAVAILABLE
    photos: List[str] = []


class GrabCategory(BaseModel):
    id: str
    name: str
    sequence: int = 0
    items: List[GrabMenuItem] = []


class GrabMenuPayload(BaseModel):
    merchantID: str
    currency: Dict[str, Any] = {"code": "VND", "symbol": "₫", "exponent": 0}
    categories: List[GrabCategory] = []


class GrabOrderWebhookPayload(BaseModel):
    orderID: str
    shortOrderNumber: Optional[str] = None
    merchantID: str
    partnerMerchantID: str
    orderTime: str
    paymentType: Optional[str] = None
    orderState: Optional[str] = None
    items: List[Dict[str, Any]] = []
    price: Optional[Dict[str, Any]] = None
