from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


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
    created_at: datetime
    updated_at: datetime
