from typing import List, Optional
from sqlalchemy import Boolean, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class Channel(Base, TimestampMixin):
    """
    Sales and delivery channels integrated into Nam An Portal.
    Examples:
    - code: SHOPEEFOOD, name: 'ShopeeFood VN'
    - code: GRABMART, name: 'GrabMart VN'
    - code: SHOPEE, name: 'Shopee E-Commerce'
    """
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    base_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    webhook_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Relationships
    store_mappings: Mapped[List["StoreChannelMapping"]] = relationship(
        "StoreChannelMapping", back_populates="channel"
    )
    product_mappings: Mapped[List["ChannelProductMapping"]] = relationship(
        "ChannelProductMapping", back_populates="channel"
    )
    orders: Mapped[List["UnifiedOrder"]] = relationship(
        "UnifiedOrder", back_populates="channel"
    )
