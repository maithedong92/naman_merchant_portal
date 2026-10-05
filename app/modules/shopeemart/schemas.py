from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ==============================================================================
# 1. Authentication & OAuth Schemas
# ==============================================================================

class ShopeeMartAuthUrlResponse(BaseModel):
    """Response containing the Shopee shop authorization URL."""
    auth_url: str = Field(..., description="Shopee Partner Shop Authorization URL")
    partner_id: str = Field(..., description="Shopee Partner ID")
    redirect_url: str = Field(..., description="Configured Redirect URL after authorization")


class ShopeeMartTokenResponse(BaseModel):
    """Shopee Open Platform OAuth Token Response."""
    access_token: str = Field(..., description="Access token for API requests (valid for ~4 hours)")
    refresh_token: str = Field(..., description="Refresh token used to obtain new access token (valid for 30 days)")
    expire_in: int = Field(..., description="Expiration duration in seconds (usually 14400s)")
    shop_id: Optional[int] = Field(None, description="Shopee Shop ID associated with token")
    error: Optional[str] = Field(None, description="Shopee error code if failed")
    message: Optional[str] = Field(None, description="Shopee error message")


class ShopeeMartConnectionStatus(BaseModel):
    """Current connection and authorization health of ShopeeMart channel."""
    channel_code: str = "SHOPEEMART"
    display_name: str = "ShopeeMart / Shopee Fresh VN"
    is_configured: bool = Field(..., description="Whether partner_id and partner_key are set")
    has_valid_token: bool = Field(..., description="Whether an active unexpired access token exists")
    token_expires_at: Optional[str] = Field(None, description="ISO timestamp when token expires")
    is_sandbox_mode: bool = Field(..., description="True if running in mock/sandbox mode without real credentials")
    partner_id: Optional[str] = None
    shop_id: Optional[str] = None


# ==============================================================================
# 2. Product Listing, Mass Upload & Outlet Publish Schemas
# ==============================================================================

class ShopeeMartAddItemRequest(BaseModel):
    """Request to create new SKU on Mart Shop (v2.product.add_item)."""
    original_price: float = Field(..., gt=0, description="Base retail price in VND")
    description: str = Field(..., description="Product detailed description")
    item_name: str = Field(..., description="Product name")
    normal_stock: int = Field(..., ge=0, description="Initial stock")
    category_id: int = Field(..., description="Shopee category ID")
    seller_sku: Optional[str] = Field(None, description="Internal SKU from Nam An Market")
    image_id_list: Optional[List[str]] = Field(default_factory=list, description="List of image IDs hosted on Shopee")
    weight: Optional[float] = Field(0.5, description="Weight in kg")


class ShopeeMartPublishOutletRequest(BaseModel):
    """Request to publish item from Mart Shop to Outlet Shop(s)."""
    item_id: int = Field(..., description="Shopee Item ID created on Mart Shop")
    outlet_shop_id_list: List[int] = Field(..., description="List of Outlet Shop IDs (e.g. 10001, 10004, 10005, 10006)")
    price_list: Optional[List[Dict[str, Any]]] = None
    stock_list: Optional[List[Dict[str, Any]]] = None


class ShopeeMartBatchPublishResponse(BaseModel):
    """Result of batch publishing item to outlet shops."""
    item_id: int
    task_id: Optional[str] = None
    success_outlets: List[int] = []
    failed_outlets: List[Dict[str, Any]] = []


class ShopeeMartUpdateItemRequest(BaseModel):
    """Request to update existing product details (v2.product.update_item)."""
    item_id: int = Field(..., description="Shopee Item ID")
    item_name: Optional[str] = None
    description: Optional[str] = None
    category_id: Optional[int] = None
    weight: Optional[float] = None


class ShopeeMartStockItem(BaseModel):
    """Stock payload item for v2.product.update_stock."""
    seller_sku: Optional[str] = Field(None, description="SKU identifier from Nam An internal system")
    item_id: Optional[int] = Field(None, description="Shopee Item ID")
    model_id: Optional[int] = Field(None, description="Shopee Model/Variation ID if applicable")
    normal_stock: int = Field(..., ge=0, description="Available stock quantity (0 indicates out of stock)")


class ShopeeMartStockUpdateBatch(BaseModel):
    """Batch stock update payload."""
    stock_list: List[ShopeeMartStockItem] = Field(..., description="List of items to update stock")


class ShopeeMartOutletStockItem(BaseModel):
    """Payload item for v2.product.batch_update_outlet_stock."""
    outlet_shop_id: int
    item_id: int
    model_id: Optional[int] = 0
    normal_stock: int


class ShopeeMartBatchOutletStockUpdate(BaseModel):
    """Batch update outlet stock across multiple outlets."""
    outlet_stock_list: List[ShopeeMartOutletStockItem]


class ShopeeMartPriceItem(BaseModel):
    """Price payload item for v2.product.update_price or batch_update_outlet_price."""
    item_id: int = Field(..., description="Shopee Item ID")
    model_id: Optional[int] = Field(None, description="Shopee Model ID")
    original_price: float = Field(..., gt=0, description="Retail price in VND")


class ShopeeMartBatchPriceUpdate(BaseModel):
    """Batch price update payload for Outlet shops."""
    price_list: List[ShopeeMartPriceItem] = Field(..., description="List of items to update price")


# ==============================================================================
# 3. Order Management & Cancellation Schemas
# ==============================================================================

class ShopeeMartOrderItem(BaseModel):
    """Individual item line within an incoming ShopeeMart order."""
    item_id: Optional[int] = None
    item_name: Optional[str] = None
    item_sku: Optional[str] = None
    model_id: Optional[int] = None
    model_name: Optional[str] = None
    model_quantity_purchased: int = Field(1, ge=1)
    model_discounted_price: float = 0.0
    model_original_price: Optional[float] = 0.0


class ShopeeMartRecipientAddress(BaseModel):
    """Delivery address of the buyer."""
    name: Optional[str] = None
    phone: Optional[str] = None
    full_address: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    ward: Optional[str] = None
    zipcode: Optional[str] = None


class ShopeeMartOrderWebhookPayload(BaseModel):
    """
    Standard Shopee Open Platform Push Notification / Order Ingest Payload.
    Supports both direct order payloads and standard push notification wrapper.
    """
    order_sn: str = Field(..., description="Shopee Order Serial Number")
    status: str = Field(..., description="Shopee Order Status: UNPAID, READY_TO_SHIP, PROCESSED, SHIPPED, COMPLETED, CANCELLED")
    shop_id: Optional[int] = None
    buyer_username: Optional[str] = None
    recipient_address: Optional[ShopeeMartRecipientAddress] = None
    total_amount: float = 0.0
    actual_shipping_fee: Optional[float] = 0.0
    shipping_carrier: Optional[str] = None
    item_list: List[ShopeeMartOrderItem] = []
    create_time: Optional[int] = None
    update_time: Optional[int] = None
    cancel_reason: Optional[str] = None


class ShopeeMartCancelOrderRequest(BaseModel):
    """Request to cancel order on Shopee (v2.order.cancel_order)."""
    order_sn: str = Field(..., description="Shopee Order SN")
    cancel_reason: str = Field("OUT_OF_STOCK", description="OUT_OF_STOCK, CUSTOMER_REQUEST, UNDELIVERABLE_AREA")
    item_list: Optional[List[Dict[str, Any]]] = None


class ShopeeMartBuyerCancellationRequest(BaseModel):
    """Handle buyer cancellation request (v2.order.handle_buyer_cancellation)."""
    order_sn: str = Field(..., description="Shopee Order SN")
    operation: str = Field("ACCEPT", description="ACCEPT or REJECT")


# ==============================================================================
# 4. Logistics & Airway Bill (AWB / Shipping Document) Schemas
# ==============================================================================

class ShopeeMartShipOrderRequest(BaseModel):
    """Request to arrange shipment on Shopee (Ready to ship)."""
    order_sn: str = Field(..., description="Shopee Order SN")
    dropoff: Optional[Dict[str, Any]] = None
    pickup: Optional[Dict[str, Any]] = None


class ShopeeMartTrackingNumberResponse(BaseModel):
    """Tracking number information for order / 3PL."""
    order_sn: str
    tracking_number: Optional[str] = None
    package_number: Optional[str] = None


class ShopeeMartCreateShippingDocRequest(BaseModel):
    """Request to generate Airway Bill (AWB) document for printing."""
    order_sn: Optional[str] = Field(None, description="Shopee Order SN")
    document_type: str = Field("THERMAL_AIR_WAYBILL", description="THERMAL_AIR_WAYBILL or NORMAL_AIR_WAYBILL")


class ShopeeMartShippingDocResult(BaseModel):
    """Airway Bill status and download url/base64."""
    order_sn: str
    status: str = Field(..., description="READY, PROCESSING, FAILED")
    file_url: Optional[str] = None


# ==============================================================================
# 5. Return & Refund (RR) Management Schemas
# ==============================================================================

class ShopeeMartReturnItem(BaseModel):
    """Single Return & Refund case detail."""
    return_sn: str = Field(..., description="Shopee Return SN")
    order_sn: str = Field(..., description="Associated Order SN")
    status: str = Field(..., description="REQUESTED, ACCEPTED, CANCELLED, REFUND_PAID, CLOSED, PROCESSING")
    refund_amount: float = 0.0
    reason: Optional[str] = None
    text_reason: Optional[str] = None
    buyer_username: Optional[str] = None
    create_time: Optional[int] = None
    update_time: Optional[int] = None


class ShopeeMartConfirmReturnRequest(BaseModel):
    """Accept return/refund application from buyer (v2.returns.confirm)."""
    return_sn: str = Field(..., description="Shopee Return SN")


class ShopeeMartDisputeReturnRequest(BaseModel):
    """Dispute a return/refund request to Shopee (v2.returns.dispute)."""
    return_sn: str = Field(..., description="Shopee Return SN")
    dispute_reason: str = Field(..., description="Reason for disputing return")
    dispute_text_reason: Optional[str] = None
    email: Optional[str] = None


# ==============================================================================
# 6. Financials & Payment Schemas (Optional)
# ==============================================================================

class ShopeeMartEscrowDetail(BaseModel):
    """Shopee Escrow settlement information for an order."""
    order_sn: str
    buyer_total_amount: float = 0.0
    original_price: float = 0.0
    seller_discount: float = 0.0
    shopee_discount: float = 0.0
    commission_fee: float = 0.0
    service_fee: float = 0.0
    escrow_amount: float = 0.0
    order_chargeable_weight: Optional[float] = 0.0
