from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class SyncLog(Base, TimestampMixin):
    """
    Log of synchronization actions between Nam An Portal and external channels.
    Types: MENU_SYNC, STOCK_SYNC, PRICE_SYNC, ORDER_SYNC.
    """
    __tablename__ = "sync_logs"
    __table_args__ = (
        Index("ix_sync_logs_channel_type", "channel_code", "sync_type"),
        Index("ix_sync_logs_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    channel_code: Mapped[str] = mapped_column(String(50), nullable=False)
    store_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    sync_type: Mapped[str] = mapped_column(String(50), nullable=False)  # MENU, STOCK, ORDER
    status: Mapped[str] = mapped_column(String(50), nullable=False)     # SUCCESS, FAILED, PARTIAL
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    success_items: Mapped[int] = mapped_column(Integer, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, default=0)
    request_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    response_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class WebhookAuditLog(Base, TimestampMixin):
    """
    Audit log for every raw inbound webhook received from delivery channels.
    Ensures idempotency and traceability for order push notifications.
    """
    __tablename__ = "webhook_audit_logs"
    __table_args__ = (
        Index("ix_webhook_idempotency", "channel_code", "idempotency_key", unique=True),
        Index("ix_webhook_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    channel_code: Mapped[str] = mapped_column(String(50), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    headers: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    response_status: Mapped[int] = mapped_column(Integer, default=200)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
