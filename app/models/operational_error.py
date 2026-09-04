from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from sqlalchemy import DateTime, Index, Integer, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class ErrorSeverity(str, Enum):
    """Severity levels for operational system errors."""
    CRITICAL = "CRITICAL"    # Hệ thống ngưng trệ, DB mất kết nối, webhook sàn lỗi toàn bộ
    ERROR = "ERROR"          # Lỗi xử lý đơn hàng cụ thể, lỗi API ngoài, unhandled exception 500
    WARNING = "WARNING"      # Cảnh báo đồng bộ tồn kho chậm, webhook payload thiếu trường phụ
    INFO = "INFO"            # Thông tin theo dõi bảo trì, khởi động lại adapter


class ErrorStatus(str, Enum):
    """Resolution lifecycle status of an operational incident."""
    OPEN = "OPEN"                      # Mới phát sinh, chưa được xử lý
    INVESTIGATING = "INVESTIGATING"    # Đang kiểm tra, truy vết nguyên nhân
    RESOLVED = "RESOLVED"              # Đã xử lý / khắc phục thành công
    IGNORED = "IGNORED"                # Đã xác nhận là cảnh báo vô hại / bỏ qua


class OperationalErrorLog(Base, TimestampMixin):
    """
    Central repository of operational errors, unhandled exceptions, and channel failures
    for live monitoring and incident management in the Nam An Merchant Portal.
    """
    __tablename__ = "operational_error_logs"
    __table_args__ = (
        Index("ix_error_logs_severity_status", "severity", "resolution_status"),
        Index("ix_error_logs_module", "module"),
        Index("ix_error_logs_created_at", "created_at"),
        Index("ix_error_logs_hash", "error_hash"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    error_code: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True,
        comment="Mã định danh lỗi (ví dụ: DATABASE_CONNECTION_ERROR, SHOPEEFOOD_SYNC_FAILED)"
    )
    severity: Mapped[str] = mapped_column(
        String(20), default=ErrorSeverity.ERROR.value, nullable=False,
        comment="Mức độ nghiêm trọng (CRITICAL, ERROR, WARNING, INFO)"
    )
    module: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="Phân hệ phát sinh lỗi (CORE, SHOPEEFOOD, GRABMART, SHOPEEMART, DATABASE, AUTH, ORDER_ENGINE)"
    )
    message: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Thông báo lỗi tóm tắt người đọc hiểu được"
    )
    stack_trace: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Chi tiết Python exception traceback"
    )
    endpoint: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True,
        comment="Đường dẫn HTTP endpoint hoặc tác vụ nền"
    )
    http_method: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True,
        comment="Phương thức HTTP (GET, POST, PATCH, PUT, DELETE)"
    )
    http_status_code: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="Mã trạng thái HTTP trả về client"
    )
    client_ip: Mapped[Optional[str]] = mapped_column(
        String(45), nullable=True,
        comment="Địa chỉ IP của client gửi yêu cầu"
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True,
        comment="User ID của người dùng nếu đã đăng nhập"
    )
    request_payload: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True,
        comment="Payload dữ liệu đầu vào lúc xảy ra lỗi (đã che mật khẩu/secrets)"
    )
    resolution_status: Mapped[str] = mapped_column(
        String(30), default=ErrorStatus.OPEN.value, nullable=False,
        comment="Trạng thái xử lý (OPEN, INVESTIGATING, RESOLVED, IGNORED)"
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True,
        comment="Tên người quản trị đã đánh dấu xử lý xong"
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Thời điểm đánh dấu xử lý xong"
    )
    resolution_notes: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Ghi chú giải pháp hoặc nguyên nhân đã được khắc phục"
    )
    occurrence_count: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False,
        comment="Số lần lỗi tương tự lặp lại"
    )
    error_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="Hash tổng hợp để gom nhóm và đếm tần suất trùng lặp lỗi"
    )
    last_occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        comment="Thời điểm xảy ra gần nhất của lỗi này"
    )
