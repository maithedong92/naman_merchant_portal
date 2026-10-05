from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class StoreChannelMappingBase(BaseModel):
    channel_id: str
    partner_store_id: str
    is_active: bool = True
    channel_config: Optional[dict] = None


class StoreChannelMappingCreate(StoreChannelMappingBase):
    pass


class StoreChannelMappingUpdate(BaseModel):
    partner_store_id: Optional[str] = None
    is_active: Optional[bool] = None
    channel_config: Optional[dict] = None


class StoreChannelMappingResponse(StoreChannelMappingBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    store_id: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class StoreChannelMappingDetail(StoreChannelMappingResponse):
    store_code: Optional[str] = None
    store_name: Optional[str] = None
    channel_code: Optional[str] = None
    channel_name: Optional[str] = None


class StoreBase(BaseModel):
    code: str = Field(..., description="Store code, e.g. '10001'")
    name: str = Field(..., description="Store name, e.g. 'Nam An Thảo Điền'")
    address: Optional[str] = None
    phone: Optional[str] = None
    is_active: bool = True


class StoreCreate(StoreBase):
    pass


class StoreUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None


class StoreResponse(StoreBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    updated_at: datetime
    channel_mappings: List[StoreChannelMappingResponse] = []
