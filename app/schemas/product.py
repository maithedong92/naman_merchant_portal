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
    custom_price: Optional[float] = None
    updated_at: Optional[datetime] = None


class ProductChannelPublishOverviewResponse(BaseModel):
    sku: str
    product_name: str
    base_price: float
    channels: List[ChannelPublishStatusItem]


class ProductChannelPublishToggleRequest(BaseModel):
    channel_code: str = Field(..., description="Sales channel code (e.g. GRAB_MART, SHOPEE_FOOD, HARAVAN_WEB)")
    is_published: bool = Field(..., description="True to publish/open sale, False to unpublish/disable")


class PoppiesChannelPublishStatusRequest(BaseModel):
    scopeType: str = Field(default="CHANNEL", description="CHANNEL or STORE")
    scopeCode: str = Field(..., description="Channel code (e.g. GRAB_MART)")
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
