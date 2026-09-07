from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class ChannelFeatureStatus(BaseModel):
    is_active: bool = True
    order_webhook_enabled: bool = True
    auto_confirm_enabled: bool = False
    stock_sync_enabled: bool = True
    menu_sync_enabled: bool = True


class ChannelFeatureUpdate(BaseModel):
    is_active: Optional[bool] = None
    order_webhook_enabled: Optional[bool] = None
    auto_confirm_enabled: Optional[bool] = None
    stock_sync_enabled: Optional[bool] = None
    menu_sync_enabled: Optional[bool] = None


class ChannelBase(BaseModel):
    code: str = Field(..., description="Unique channel code: SHOPEEFOOD, GRABMART, SHOPEE")
    name: str = Field(..., description="Channel name: ShopeeFood VN")
    description: Optional[str] = None
    is_active: bool = True
    base_url: Optional[str] = None
    config: Optional[dict] = None


class ChannelCreate(ChannelBase):
    webhook_secret: Optional[str] = None


class ChannelResponse(ChannelBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    features: Optional[ChannelFeatureStatus] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
