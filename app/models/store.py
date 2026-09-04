from typing import List, Optional
from sqlalchemy import Boolean, ForeignKey, Index, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class Store(Base, TimestampMixin):
    """
    Nam An Market physical store / branch model.
    Examples:
    - 10001: Nam An Thảo Điền
    - 10004: Nam An An Phú
    """
    __tablename__ = "stores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    channel_mappings: Mapped[List["StoreChannelMapping"]] = relationship(
        "StoreChannelMapping", back_populates="store", cascade="all, delete-orphan"
    )
    inventories: Mapped[List["StoreInventory"]] = relationship(
        "StoreInventory", back_populates="store", cascade="all, delete-orphan"
    )
    orders: Mapped[List["UnifiedOrder"]] = relationship(
        "UnifiedOrder", back_populates="store"
    )


class StoreChannelMapping(Base, TimestampMixin):
    """
    Mapping between a Nam An store and an external delivery channel outlet.
    Example: Store 10001 maps to ShopeeFood partner_restaurant_id '10001'
    and GrabMart outlet 'GM-THAO-DIEN'.
    """
    __tablename__ = "store_channel_mappings"
    __table_args__ = (
        Index("ix_store_channel_unique", "store_id", "channel_id", unique=True),
        Index("ix_channel_partner_id", "channel_id", "partner_store_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    store_id: Mapped[str] = mapped_column(String(36), ForeignKey("stores.id"), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(36), ForeignKey("channels.id"), nullable=False)
    partner_store_id: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Channel-specific merchant / restaurant ID"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    channel_config: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True, comment="Channel-specific outlet config overrides"
    )

    # Relationships
    store: Mapped["Store"] = relationship("Store", back_populates="channel_mappings")
    channel: Mapped["Channel"] = relationship("Channel", back_populates="store_mappings")
