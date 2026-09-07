import inspect
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import get_settings
from app.models.channel import Channel

logger = logging.getLogger("naman_portal.services.channel")
settings = get_settings()

DEFAULT_FEATURES = {
    "order_webhook_enabled": True,
    "auto_confirm_enabled": False,
    "stock_sync_enabled": True,
    "menu_sync_enabled": True,
}

INITIAL_CHANNELS = [
    {
        "code": "SHOPEEFOOD",
        "name": "ShopeeFood VN",
        "description": "Tích hợp Foody S2S API v7.0.2. Đồng bộ menu, tồn kho và tiếp nhận đơn hàng tức thì.",
        "base_url": settings.SHOPEEFOOD_BASE_URL,
        "is_active": settings.SHOPEEFOOD_ENABLED,
        "config": {
            "app_id": settings.SHOPEEFOOD_APP_ID,
            "order_webhook_enabled": True,
            "auto_confirm_enabled": False,
            "stock_sync_enabled": True,
            "menu_sync_enabled": True,
        }
    },
    {
        "code": "GRABMART",
        "name": "GrabMart VN",
        "description": "Tích hợp GrabMart Partner POS API v1.1.3 qua OAuth2. Đồng bộ danh mục, tồn kho và xử lý đơn hàng.",
        "base_url": settings.GRABMART_BASE_URL,
        "is_active": settings.GRABMART_ENABLED,
        "config": {
            "client_id": settings.GRABMART_CLIENT_ID,
            "order_webhook_enabled": True,
            "auto_confirm_enabled": False,
            "stock_sync_enabled": True,
            "menu_sync_enabled": True,
        }
    },
    {
        "code": "SHOPEEMART",
        "name": "ShopeeMart (Fresh)",
        "description": "Tích hợp sàn ShopeeMart Open Platform v2 theo danh mục thực phẩm tươi sống.",
        "base_url": "https://partner.shopeemobile.com",
        "is_active": settings.SHOPEEMART_ENABLED,
        "config": {
            "order_webhook_enabled": True,
            "auto_confirm_enabled": False,
            "stock_sync_enabled": True,
            "menu_sync_enabled": True,
        }
    }
]


class ChannelService:
    """Service to manage sales channels and their granular feature toggles."""

    @staticmethod
    def extract_features(channel: Channel) -> Dict[str, bool]:
        """Extract standardized feature toggle flags from a channel."""
        cfg = channel.config or {}
        return {
            "is_active": bool(channel.is_active),
            "order_webhook_enabled": bool(cfg.get("order_webhook_enabled", DEFAULT_FEATURES["order_webhook_enabled"])),
            "auto_confirm_enabled": bool(cfg.get("auto_confirm_enabled", DEFAULT_FEATURES["auto_confirm_enabled"])),
            "stock_sync_enabled": bool(cfg.get("stock_sync_enabled", DEFAULT_FEATURES["stock_sync_enabled"])),
            "menu_sync_enabled": bool(cfg.get("menu_sync_enabled", DEFAULT_FEATURES["menu_sync_enabled"])),
        }

    @classmethod
    async def ensure_default_channels(cls, db: AsyncSession) -> List[Channel]:
        """Seed initial channels into PostgreSQL if not already present."""
        channels: List[Channel] = []
        for ch_def in INITIAL_CHANNELS:
            stmt = select(Channel).where(Channel.code == ch_def["code"])
            res = await db.execute(stmt)
            if inspect.isawaitable(res):
                res = await res
            scalar_fn = getattr(res, "scalar_one_or_none", None)
            ch = scalar_fn() if callable(scalar_fn) else None
            if inspect.isawaitable(ch):
                ch = await ch

            if not ch:
                ch = Channel(
                    code=ch_def["code"],
                    name=ch_def["name"],
                    description=ch_def["description"],
                    base_url=ch_def["base_url"],
                    is_active=ch_def["is_active"],
                    config=ch_def["config"].copy()
                )
                db.add(ch)
                logger.info(f"Đã khởi tạo kênh bán hàng mặc định: {ch_def['code']}")
            channels.append(ch)
        await db.commit()
        for ch in channels:
            try:
                await db.refresh(ch)
            except Exception:
                pass
        return channels

    @classmethod
    async def get_channel_by_code(cls, channel_code: str, db: Optional[AsyncSession] = None) -> Optional[Channel]:
        """Lookup channel by uppercase code."""
        if db is None:
            return None
        stmt = select(Channel).where(Channel.code == channel_code.upper())
        res = await db.execute(stmt)
        if inspect.isawaitable(res):
            res = await res
        scalar_fn = getattr(res, "scalar_one_or_none", None)
        if callable(scalar_fn):
            val = scalar_fn()
            if inspect.isawaitable(val):
                val = await val
            if isinstance(val, Channel) or (hasattr(val, "is_active") and hasattr(val, "config")):
                return val
        return None

    @classmethod
    async def is_feature_enabled(
        cls,
        channel_code: str,
        feature_key: str,
        db: Optional[AsyncSession] = None
    ) -> bool:
        """
        Check if a specific channel capability is enabled.
        A feature is active ONLY IF master channel is_active == True
        AND the specific feature toggle is True.
        """
        channel = None
        if db is not None:
            try:
                channel = await cls.get_channel_by_code(channel_code, db)
                if inspect.isawaitable(channel):
                    channel = await channel
            except Exception:
                channel = None

        if channel is None or not hasattr(channel, "is_active"):
            # Fallback to default channel configuration
            for ch in INITIAL_CHANNELS:
                if ch["code"] == channel_code.upper():
                    if not ch.get("is_active", True):
                        return False
                    return ch.get("config", {}).get(feature_key, DEFAULT_FEATURES.get(feature_key, True))
            return DEFAULT_FEATURES.get(feature_key, True)

        if not getattr(channel, "is_active", True):
            return False

        cfg = channel.config or {}
        default_val = DEFAULT_FEATURES.get(feature_key, True)
        return bool(cfg.get(feature_key, default_val))

    @classmethod
    async def update_feature_flags(
        cls,
        channel_code: str,
        updates: Dict[str, Any],
        db: AsyncSession
    ) -> Optional[Channel]:
        """Update master is_active or specific feature flags in channel.config."""
        channel = await cls.get_channel_by_code(channel_code, db)
        if not channel:
            return None

        # Handle master toggle
        if "is_active" in updates and updates["is_active"] is not None:
            channel.is_active = bool(updates["is_active"])

        # Handle sub-feature toggles
        cfg = dict(channel.config or {})
        for key in ["order_webhook_enabled", "auto_confirm_enabled", "stock_sync_enabled", "menu_sync_enabled"]:
            if key in updates and updates[key] is not None:
                cfg[key] = bool(updates[key])

        channel.config = cfg
        flag_modified(channel, "config")

        await db.commit()
        try:
            await db.refresh(channel)
        except Exception:
            pass
        logger.info(f"Đã cập nhật tính năng cho kênh {channel_code}: {cls.extract_features(channel)}")
        return channel


channel_service = ChannelService()
