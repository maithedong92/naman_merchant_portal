import enum
from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class UnifiedOrderStatus(str, enum.Enum):
    """
    Standardized Order Lifecycle Status across all channels:
    ShopeeFood, GrabMart, Shopee E-Commerce.
    """
    PENDING = "PENDING"          # Newly created, awaiting store acceptance
    ACCEPTED = "ACCEPTED"        # Accepted by store
    PREPARING = "PREPARING"      # Items are being picked and packed
    READY = "READY"              # Packed and waiting for driver pickup
    PICKED_UP = "PICKED_UP"      # Driver picked up, delivering
    DELIVERED = "DELIVERED"      # Successfully delivered to customer
    CANCELLED = "CANCELLED"      # Cancelled by user, merchant or driver


class UnifiedOrder(Base, TimestampMixin):
    """
    Unified Order entity aggregating orders from all channels.
    """
    __tablename__ = "unified_orders"
    __table_args__ = (
        Index("ix_channel_order_unique", "channel_id", "channel_order_id", unique=True),
        Index("ix_orders_store_status", "store_id", "status"),
        Index("ix_orders_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    order_code: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False, comment="Internal order code"
    )
    channel_id: Mapped[str] = mapped_column(String(36), ForeignKey("channels.id"), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), ForeignKey("stores.id"), nullable=False)
    
    # Channel identifiers
    channel_order_id: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Platform order ID (e.g., Grab or ShopeeFood ID)"
    )
    display_order_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, comment="Short order number displayed to driver/customer"
    )

    # Status
    status: Mapped[UnifiedOrderStatus] = mapped_column(
        Enum(UnifiedOrderStatus), default=UnifiedOrderStatus.PENDING, nullable=False, index=True
    )

    # Financials
    subtotal_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0.0, nullable=False)
    discount_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0.0, nullable=False)
    delivery_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0.0, nullable=False)
    total_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0.0, nullable=False)

    # Customer info
    customer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    customer_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    delivery_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Driver info
    driver_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    driver_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    driver_license_plate: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Delivery timings
    order_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    estimated_ready_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    picked_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Cancellation & raw payload
    cancellation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancelled_by: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Relationships
    channel: Mapped["Channel"] = relationship("Channel", back_populates="orders")
    store: Mapped["Store"] = relationship("Store", back_populates="orders")
    items: Mapped[List["OrderItem"]] = relationship(
        "OrderItem", back_populates="order", cascade="all, delete-orphan"
    )
    status_history: Mapped[List["OrderStatusHistory"]] = relationship(
        "OrderStatusHistory", back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base, TimestampMixin):
    """Line item in a unified order."""
    __tablename__ = "order_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("unified_orders.id"), nullable=False)
    product_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("products.id"), nullable=True)
    
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
    item_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(default=1, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    total_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    modifiers: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Relationships
    order: Mapped["UnifiedOrder"] = relationship("UnifiedOrder", back_populates="items")


class OrderStatusHistory(Base, TimestampMixin):
    """Audit log tracking state transitions of an order."""
    __tablename__ = "order_status_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("unified_orders.id"), nullable=False)
    from_status: Mapped[Optional[UnifiedOrderStatus]] = mapped_column(Enum(UnifiedOrderStatus), nullable=True)
    to_status: Mapped[UnifiedOrderStatus] = mapped_column(Enum(UnifiedOrderStatus), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str] = mapped_column(String(100), default="SYSTEM", nullable=False)

    # Relationships
    order: Mapped["UnifiedOrder"] = relationship("UnifiedOrder", back_populates="status_history")
