from typing import List, Optional
from sqlalchemy import Boolean, ForeignKey, Index, Numeric, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class Category(Base, TimestampMixin):
    """Product master category hierarchy."""
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("categories.id"), nullable=True)
    sequence: Mapped[int] = mapped_column(default=0)

    # Relationships
    parent: Mapped[Optional["Category"]] = relationship("Category", remote_side=[id])
    products: Mapped[List["Product"]] = relationship("Product", back_populates="category")


class Product(Base, TimestampMixin):
    """
    Nam An Market Master Product (SKU).
    Single Source of Truth for items synchronized to ShopeeFood, GrabMart, etc.
    """
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    sku: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    barcode: Mapped[Optional[str]] = mapped_column(String(100), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    unit: Mapped[str] = mapped_column(String(50), default="Cái", nullable=False)
    base_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    image_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    category_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("categories.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Relationships
    category: Mapped[Optional["Category"]] = relationship("Category", back_populates="products")
    channel_mappings: Mapped[List["ChannelProductMapping"]] = relationship(
        "ChannelProductMapping", back_populates="product", cascade="all, delete-orphan"
    )
    inventories: Mapped[List["StoreInventory"]] = relationship(
        "StoreInventory", back_populates="product", cascade="all, delete-orphan"
    )


class ChannelProductMapping(Base, TimestampMixin):
    """
    Mapping and override configurations for a product on an external channel.
    Allows channel-specific pricing, custom dish ID, or channel availability.
    """
    __tablename__ = "channel_product_mappings"
    __table_args__ = (
        Index("ix_product_channel_unique", "product_id", "channel_id", unique=True),
        Index("ix_channel_item_id", "channel_id", "channel_item_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    product_id: Mapped[str] = mapped_column(String(36), ForeignKey("products.id"), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(36), ForeignKey("channels.id"), nullable=False)
    channel_item_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, comment="Channel ID (dish_id on ShopeeFood, item_id on Grab)"
    )
    channel_category_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    custom_price: Mapped[Optional[float]] = mapped_column(
        Numeric(14, 2), nullable=True, comment="Channel-specific price override if any"
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    channel_sync_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Relationships
    product: Mapped["Product"] = relationship("Product", back_populates="channel_mappings")
    channel: Mapped["Channel"] = relationship("Channel", back_populates="product_mappings")
