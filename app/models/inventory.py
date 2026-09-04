from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class StoreInventory(Base, TimestampMixin):
    """
    Inventory level for a product (SKU) at a specific physical store / warehouse.
    Used to calculate available stock for partner platforms (ShopeeFood, GrabMart).
    """
    __tablename__ = "store_inventories"
    __table_args__ = (
        Index("ix_store_product_unique", "store_id", "product_id", unique=True),
        Index("ix_store_sku", "store_id", "sku"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    store_id: Mapped[str] = mapped_column(String(36), ForeignKey("stores.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(String(36), ForeignKey("products.id"), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
    
    total_stock: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0, nullable=False)
    reserved_stock: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0, nullable=False)
    available_stock: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0, nullable=False)
    safety_stock: Mapped[float] = mapped_column(Numeric(12, 2), default=0.0, nullable=False)
    
    is_out_of_stock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    store: Mapped["Store"] = relationship("Store", back_populates="inventories")
    product: Mapped["Product"] = relationship("Product", back_populates="inventories")


class InventoryStockLog(Base, TimestampMixin):
    """Audit log for stock movement and adjustments."""
    __tablename__ = "inventory_stock_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    store_id: Mapped[str] = mapped_column(String(36), ForeignKey("stores.id"), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    change_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="SYNC, ORDER_RESERVE, ORDER_CANCEL, MANUAL"
    )
    quantity_before: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    quantity_delta: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    quantity_after: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    reference_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, comment="Order ID or Batch Sync ID"
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
