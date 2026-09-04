from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class StoreInventoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    store_id: str
    product_id: str
    sku: str
    total_stock: float
    reserved_stock: float
    available_stock: float
    safety_stock: float
    is_out_of_stock: bool
    last_synced_at: Optional[datetime] = None


class InventoryStockUpdateItem(BaseModel):
    sku: str = Field(..., description="Product SKU")
    total_stock: float = Field(..., ge=0, description="Physical inventory count")
    safety_stock: Optional[float] = Field(default=0.0, ge=0)


class InventoryBatchUpdateRequest(BaseModel):
    store_id: str = Field(..., description="Target Store ID or Code")
    items: List[InventoryStockUpdateItem]
    sync_to_channels: bool = Field(
        default=True,
        description="Whether to automatically trigger stock sync to external channels"
    )
