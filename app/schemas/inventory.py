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


class InventoryItemOverview(BaseModel):
    """Product + Store Inventory consolidated view."""
    product_id: str
    sku: str
    name: str
    barcode: Optional[str] = None
    unit: str = "Cái"
    base_price: float = 0.0
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    image_url: Optional[str] = None
    
    store_id: str
    store_code: str
    store_name: str
    
    total_stock: float = 0.0
    reserved_stock: float = 0.0
    available_stock: float = 0.0
    safety_stock: float = 0.0
    is_out_of_stock: bool = False
    last_synced_at: Optional[datetime] = None


class InventoryOverviewSummary(BaseModel):
    """Summary counts for inventory header cards."""
    total_skus: int = 0
    in_stock_count: int = 0
    out_of_stock_count: int = 0
    low_stock_count: int = 0


class InventoryToggleStatusRequest(BaseModel):
    """Toggle in-stock / out-of-stock for an item at a store."""
    store_code: str = Field(..., description="Mã chi nhánh (10001, 10004, 10005, 10006)")
    sku: str = Field(..., description="Mã SKU sản phẩm")
    is_out_of_stock: bool = Field(..., description="True nếu tạm ngưng bán / hết hàng, False nếu còn hàng")
    available_stock: Optional[float] = Field(None, ge=0, description="Tồn kho khả dụng mới nếu bật bán lại")
    sync_to_channels: bool = Field(default=True, description="Tự động đồng bộ ngay sang ShopeeFood và GrabMart")


class InventoryQuickStockUpdateRequest(BaseModel):
    """Quick edit stock quantity for a single SKU."""
    store_code: str = Field(..., description="Mã chi nhánh")
    sku: str = Field(..., description="Mã SKU")
    new_stock: float = Field(..., ge=0, description="Số lượng tồn kho khả dụng mới")
    sync_to_channels: bool = Field(default=True, description="Đồng bộ sang sàn")


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
