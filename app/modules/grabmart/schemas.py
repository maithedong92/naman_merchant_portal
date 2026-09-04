from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ==============================================================================
# 1. OAuth2 Authentication Schemas
# ==============================================================================

class GrabOAuthTokenRequest(BaseModel):
    client_id: str
    client_secret: str
    grant_type: str = "client_credentials"
    scope: Optional[str] = "grabmart.partner.pos"


class GrabOAuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600


# ==============================================================================
# 2. Menu Sync & Catalog Schemas (GrabMart Partner POS API v1.1.3)
# ==============================================================================

class GrabSellingPeriod(BaseModel):
    startTime: str = Field(default="06:00", description="HH:mm local time")
    endTime: str = Field(default="22:00", description="HH:mm local time")


class GrabDaySchedule(BaseModel):
    openPeriodType: str = Field(default="OpenPeriod", description="OpenPeriod or Closed")
    periods: List[GrabSellingPeriod] = Field(default_factory=lambda: [GrabSellingPeriod()])


class GrabServiceHours(BaseModel):
    mon: GrabDaySchedule = Field(default_factory=GrabDaySchedule)
    tue: GrabDaySchedule = Field(default_factory=GrabDaySchedule)
    wed: GrabDaySchedule = Field(default_factory=GrabDaySchedule)
    thu: GrabDaySchedule = Field(default_factory=GrabDaySchedule)
    fri: GrabDaySchedule = Field(default_factory=GrabDaySchedule)
    sat: GrabDaySchedule = Field(default_factory=GrabDaySchedule)
    sun: GrabDaySchedule = Field(default_factory=GrabDaySchedule)


class GrabSellingTime(BaseModel):
    id: str = "standard_schedule"
    name: str = "Nam An Daily Schedule"
    startTime: str = "2024-01-01 00:00:00"  # UTC
    endTime: str = "2030-12-31 23:59:59"    # UTC
    serviceHours: GrabServiceHours = Field(default_factory=GrabServiceHours)


class GrabAdvancedPricing(BaseModel):
    key: str = "Delivery_OnDemand_GrabApp"
    price: int


class GrabPurchasability(BaseModel):
    key: str = "Delivery_OnDemand_GrabApp"
    purchasable: bool = True


class GrabModifier(BaseModel):
    id: str
    name: str
    price: int = 0
    availableStatus: str = "AVAILABLE"
    sequence: int = 0


class GrabModifierGroup(BaseModel):
    id: str
    name: str
    selectionRangeMin: int = 0
    selectionRangeMax: int = 1
    modifiers: List[GrabModifier] = Field(default_factory=list)


class GrabMenuItem(BaseModel):
    id: str = Field(..., description="Partner SKU / Item ID")
    name: str
    price: int = Field(..., description="VND amount (minor unit exponent is 0 for VN)")
    availableStatus: str = Field(default="AVAILABLE", description="AVAILABLE | UNAVAILABLE")
    maxStock: int = Field(default=999, description="Stock count, must be 0 if UNAVAILABLE")
    photos: List[str] = Field(default_factory=list)
    barcodes: List[str] = Field(default_factory=list)
    description: Optional[str] = None
    sellingTimeID: Optional[str] = "standard_schedule"
    modifierGroups: List[GrabModifierGroup] = Field(default_factory=list)
    advancedPricings: List[GrabAdvancedPricing] = Field(default_factory=list)
    purchasabilities: List[GrabPurchasability] = Field(default_factory=list)


class GrabSubcategory(BaseModel):
    id: str
    name: str
    sequence: int = 0
    items: List[GrabMenuItem] = Field(default_factory=list)


class GrabCategory(BaseModel):
    id: str
    name: str
    sequence: int = 0
    subcategories: List[GrabSubcategory] = Field(default_factory=list)


class GrabCurrency(BaseModel):
    code: str = "VND"
    symbol: str = "₫"
    exponent: int = 0


class GrabMartMenuPayload(BaseModel):
    merchantID: str
    partnerMerchantID: Optional[str] = None
    currency: GrabCurrency = Field(default_factory=GrabCurrency)
    sellingTimes: List[GrabSellingTime] = Field(default_factory=list)
    categories: List[GrabCategory] = Field(default_factory=list)


# ==============================================================================
# 3. Real-Time Stock & Menu Record Updates
# ==============================================================================

class GrabUpdateItemRecord(BaseModel):
    merchantID: str
    field: str = "ITEM"
    id: str = Field(..., description="Partner SKU")
    price: Optional[int] = None
    availableStatus: str = Field(default="AVAILABLE", description="AVAILABLE | UNAVAILABLE")
    maxStock: int = Field(default=10, description="0 if UNAVAILABLE")


class GrabBatchUpdateMenuRecordsRequest(BaseModel):
    merchantID: str
    records: List[GrabUpdateItemRecord] = Field(default_factory=list)


# ==============================================================================
# 4. Inbound Order Webhook Schemas
# ==============================================================================

class GrabCoordinates(BaseModel):
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class GrabAddress(BaseModel):
    unitNumber: Optional[str] = None
    deliveryInstruction: Optional[str] = None
    address: Optional[str] = None
    postcode: Optional[str] = None
    coordinates: Optional[GrabCoordinates] = None


class GrabReceiver(BaseModel):
    name: Optional[str] = None
    phones: Optional[str] = None
    address: Optional[GrabAddress] = None


class GrabOrderItemModifier(BaseModel):
    id: str
    price: int = 0
    quantity: int = 1


class GrabOrderItem(BaseModel):
    id: str = Field(..., description="Partner Item SKU")
    grabItemID: Optional[str] = None
    name: Optional[str] = None
    quantity: int = 1
    price: int = 0
    tax: Optional[int] = 0
    specifications: Optional[str] = None
    modifiers: List[GrabOrderItemModifier] = Field(default_factory=list)


class GrabOrderPrice(BaseModel):
    subtotal: int = 0
    tax: int = 0
    deliveryFee: int = 0
    merchantFundPromo: int = 0
    grabFundPromo: int = 0
    total: int = 0


class GrabFeatureFlags(BaseModel):
    orderAcceptedType: str = "AUTO"  # AUTO | MANUAL
    orderType: str = "DeliveredByGrab"  # DeliveredByGrab | TakeAway | DineIn | DeliveredByStore
    isMexEditOrder: bool = False


class GrabSubmitOrderWebhook(BaseModel):
    orderID: str = Field(..., description="GrabMart order unique ID")
    shortOrderNumber: Optional[str] = Field(None, description="Human friendly short code, e.g. GM-102")
    merchantID: str = Field(..., description="GrabMart outlet ID")
    partnerMerchantID: Optional[str] = Field(None, description="Nam An Store ID / Code")
    paymentType: str = "CASHLESS"  # CASH | CASHLESS
    orderTime: str
    submitTime: Optional[str] = None
    scheduledTime: Optional[str] = None
    currency: GrabCurrency = Field(default_factory=GrabCurrency)
    featureFlags: Optional[GrabFeatureFlags] = None
    items: List[GrabOrderItem] = Field(default_factory=list)
    price: GrabOrderPrice = Field(default_factory=GrabOrderPrice)
    receiver: Optional[GrabReceiver] = None


# ==============================================================================
# 5. Push Order State Webhook Schema
# ==============================================================================

class GrabPushOrderStateWebhook(BaseModel):
    merchantID: str
    partnerMerchantID: Optional[str] = None
    orderID: str
    state: str = Field(..., description="ACCEPTED | DRIVER_ALLOCATED | DRIVER_ARRIVED | COLLECTED | DELIVERED | FAILED | CANCELLED")
    driverETA: Optional[int] = None
    code: Optional[str] = None
    message: Optional[str] = None


# ==============================================================================
# 6. Partner Outbound Order Action Schemas
# ==============================================================================

class GrabOrderPrepareRequest(BaseModel):
    orderID: str
    toState: str = Field(default="Accepted", description="Accepted | Rejected")


class GrabMarkOrderReadyRequest(BaseModel):
    orderID: str
    markStatus: int = Field(default=1, description="Always 1")


class GrabCancelOrderRequest(BaseModel):
    orderID: str
    merchantID: str
    cancelCode: int = Field(default=1001, description="1001: Items unavailable, 1002: Too busy, 1003: Shop closed, 1004: Closing soon")

