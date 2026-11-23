from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


ALLOWED_PUBLIC_CHANNEL_CONFIG_KEYS = frozenset({
    "features",
    "arito_channel_code",
    "ma_kenh",
    "channel_group",
    "has_partner_pricing",
    "default_markup_percent",
    "packaging_fee_vnd",
    "inherited_from",
    "is_dynamic",
    "legacy_channel_code",
})


def sanitize_channel_config(cfg: Optional[dict]) -> Optional[dict]:
    """Allowlist only non-sensitive metadata keys in public ChannelResponse.config."""
    if cfg is None:
        return None
    if not isinstance(cfg, dict):
        return {}
    return {k: v for k, v in cfg.items() if k in ALLOWED_PUBLIC_CHANNEL_CONFIG_KEYS}


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

    @field_validator("config", mode="before")
    @classmethod
    def _filter_public_config(cls, value: Optional[dict]) -> Optional[dict]:
        return sanitize_channel_config(value)
