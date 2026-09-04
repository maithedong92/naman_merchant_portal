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
    channel_item_id: Optional[str] = None
    channel_category_id: Optional[str] = None
    custom_price: Optional[float] = None
    is_enabled: bool = True


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
