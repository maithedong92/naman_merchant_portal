from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class CategoryBase(BaseModel):
    code: str
    name: str
    parent_id: Optional[str] = None
    sequence: int = 0


class CategoryCreate(CategoryBase):
    pass


class CategoryResponse(CategoryBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    updated_at: datetime


class ChannelProductMappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    channel_id: str
    channel_code: Optional[str] = None
    channel_name: Optional[str] = None
    channel_item_id: Optional[str] = None
    channel_category_id: Optional[str] = None
    custom_price: Optional[float] = None
    is_enabled: bool = True


class ChannelPublishStatusItem(BaseModel):
    channel_code: str
    channel_name: str
    poppies_channel_code: str
    arito_channel_code: Optional[str] = None
    channel_group: Optional[str] = None
    is_published: bool
    is_channel_active: bool = True
    allowed_in_context: bool = True
    has_partner_pricing: bool = False
    inherited_from: Optional[str] = None
    custom_price: Optional[float] = None
    effective_price: Optional[float] = None
    aliases: List[str] = Field(default_factory=list)
    updated_at: Optional[datetime] = None


class ProductChannelPublishOverviewResponse(BaseModel):
    sku: str
    product_name: str
    base_price: float
    channels: List[ChannelPublishStatusItem]


class ProductChannelPublishToggleRequest(BaseModel):
    channel_code: Optional[str] = Field(default=None, description="Canonical or alias channel code (e.g. GRAB, SHOPEEFOOD, WEB)")
    is_published: bool = Field(..., description="True to publish/open sale, False to unpublish/disable")


class PoppiesChannelPublishStatusRequest(BaseModel):
    scopeType: str = Field(default="CHANNEL", description="CHANNEL or STORE")
    scopeCode: str = Field(..., description="Canonical or alias channel code (e.g. GRAB, SHOPEEFOOD, WEB)")
    sku: str = Field(..., description="Target SKU")
    isPublished: bool = Field(..., description="True to publish, False to unpublish")


class ChannelPublishToggleResult(BaseModel):
    sku: str
    product_name: str
    channel_code: str
    poppies_channel_code: str
    is_published: bool
    synced_to_poppies: bool
    message: str
    poppies_response: Optional[dict] = None
    external_channel_sync: Optional[List[dict]] = None


class PricingSnapshotSyncResponse(BaseModel):
    success: bool
    branch_code: str
    channel_code: str
    snapshot_version: Optional[str] = None
    is_channel_active: bool = True
    has_partner_pricing: bool = False
    inherited_from: Optional[str] = None
    is_delta: bool = False
    matched_skus_count: int = 0
    unmatched_skus_count: int = 0
    prices_updated: int = 0
    blocked_skus_count: int = 0
    unblocked_skus_count: int = 0
    elapsed_seconds: float = 0.0
    external_channel_sync: Optional[List[dict]] = None


class ProductBase(BaseModel):
    sku: str = Field(..., description="Unique Stock Keeping Unit (e.g. 010201020103)")
    barcode: Optional[str] = None
    name: str = Field(..., description="Product name")
    description: Optional[str] = None
    unit: str = "Cái"
    base_price: float = Field(..., ge=0, description="Base selling price in VND")
    image_url: Optional[str] = None
    category_id: Optional[str] = None
    is_active: bool = True
    metadata_json: Optional[dict] = None


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    barcode: Optional[str] = None
    description: Optional[str] = None
    unit: Optional[str] = None
    base_price: Optional[float] = None
    image_url: Optional[str] = None
    category_id: Optional[str] = None
    is_active: Optional[bool] = None


class ProductResponse(ProductBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    updated_at: datetime
    channel_mappings: List[ChannelProductMappingResponse] = []
