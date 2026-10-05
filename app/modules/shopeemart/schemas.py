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
# 2. Inventory & Price Sync Schemas (Mart & Outlet)
# ==============================================================================

class ShopeeMartStockItem(BaseModel):
    """Stock payload item for v2.product.update_stock."""
    seller_sku: Optional[str] = Field(None, description="SKU identifier from Nam An internal system")
    item_id: Optional[int] = Field(None, description="Shopee Item ID")
    model_id: Optional[int] = Field(None, description="Shopee Model/Variation ID if applicable")
    normal_stock: int = Field(..., ge=0, description="Available stock quantity (0 indicates out of stock)")


class ShopeeMartStockUpdateBatch(BaseModel):
    """Batch stock update payload."""
    stock_list: List[ShopeeMartStockItem] = Field(..., description="List of items to update stock")


class ShopeeMartPriceItem(BaseModel):
    """Price payload item for v2.product.update_price or batch_update_outlet_price."""
    item_id: int = Field(..., description="Shopee Item ID")
    model_id: Optional[int] = Field(None, description="Shopee Model ID")
    original_price: float = Field(..., gt=0, description="Retail price in VND")


class ShopeeMartBatchPriceUpdate(BaseModel):
    """Batch price update payload for Outlet shops."""
    price_list: List[ShopeeMartPriceItem] = Field(..., description="List of items to update price")


# ==============================================================================
# 3. Order Management Schemas
# ==============================================================================

class ShopeeMartOrderItem(BaseModel):
    """Individual item line within an incoming ShopeeMart order."""
    item_id: Optional[int] = None
    item_name: str = "Sản phẩm Shopee"
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


class ShopeeMartShipOrderRequest(BaseModel):
    """Request to arrange shipment on Shopee (Ready to ship)."""
    order_sn: str = Field(..., description="Shopee Order SN")
    dropoff: Optional[Dict[str, Any]] = None
    pickup: Optional[Dict[str, Any]] = None


class ShopeeMartCancelOrderRequest(BaseModel):
    """Request to cancel order on Shopee."""
    order_sn: str = Field(..., description="Shopee Order SN")
    cancel_reason: str = Field("OUT_OF_STOCK", description="OUT_OF_STOCK, CUSTOMER_REQUEST, UNDELIVERABLE_AREA")
    item_list: Optional[List[Dict[str, Any]]] = None
