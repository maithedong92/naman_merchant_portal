from datetime import datetime
from enum import Enum
from typing import Optional, List
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class UserRole(str, Enum):
    """User Roles for RBAC authorization in Nam An Merchant Portal."""
    SUPER_ADMIN = "SUPER_ADMIN"        # Toàn quyền hệ thống & cấu hình kênh
    STORE_MANAGER = "STORE_MANAGER"    # Quản lý kho, tồn kho, giá và đơn hàng tại chi nhánh
    STAFF = "STAFF"                    # Nhân viên chi nhánh, xem & chuẩn bị đơn hàng
    READ_ONLY = "READ_ONLY"            # Chỉ xem báo cáo, tra cứu dữ liệu


class User(Base, TimestampMixin):
    """User entity representing an internal merchant portal operator or admin."""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    username: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False,
        comment="Tên đăng nhập duy nhất"
    )
    email: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False,
        comment="Email liên lạc của người dùng"
    )
    hashed_password: Mapped[str] = mapped_column(
        String(255), nullable=False,
        comment="Bcrypt hash của mật khẩu"
    )
    full_name: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="Họ và tên đầy đủ"
    )
    phone_number: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True,
        comment="Số điện thoại liên hệ"
    )
    role: Mapped[str] = mapped_column(
        String(30), default=UserRole.STAFF.value, nullable=False, index=True,
        comment="Vai trò RBAC (SUPER_ADMIN, STORE_MANAGER, STAFF, READ_ONLY)"
    )
    store_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True,
        comment="Chi nhánh Nam An được phân quyền phụ trách (nếu null và SUPER_ADMIN thì toàn quyền)"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        comment="Trạng thái kích hoạt tài khoản"
    )
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Đánh dấu tài khoản quản trị tối cao"
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Thời điểm đăng nhập thành công gần nhất"
    )
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
        comment="Số lần đăng nhập thất bại liên tiếp"
    )
    locked_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Thời điểm khóa tài khoản tạm thời nếu đăng nhập sai quá số lần quy định"
    )

    # Relationships
    store = relationship("Store", lazy="selectin")
    refresh_tokens = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class RefreshToken(Base, TimestampMixin):
    """Store hashed refresh tokens to allow revocation and rotation."""
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
        comment="Khóa ngoại liên kết tới User"
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False,
        comment="SHA-256 hash của chuỗi refresh token"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        comment="Thời hạn hết hạn của refresh token"
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Thời điểm token bị thu hồi (khi logout hoặc rotate)"
    )
    user_agent: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True,
        comment="User Agent của trình duyệt/thiết bị đăng nhập"
    )
    ip_address: Mapped[Optional[str]] = mapped_column(
        String(45), nullable=True,
        comment="Địa chỉ IP gửi yêu cầu tạo token"
    )

    # Relationship
    user = relationship("User", back_populates="refresh_tokens")


class AuditSecurityLog(Base, TimestampMixin):
    """Security audit logs recording all authentication events."""
    __tablename__ = "audit_security_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True,
        comment="User ID liên quan (nếu đã xác định được)"
    )
    username: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, index=True,
        comment="Tên người dùng thực hiện yêu cầu"
    )
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True,
        comment="Loại sự kiện (LOGIN_SUCCESS, LOGIN_FAILED, LOGOUT, REFRESH_TOKEN, PASSWORD_CHANGED, ACCOUNT_LOCKED)"
    )
    ip_address: Mapped[Optional[str]] = mapped_column(
        String(45), nullable=True,
        comment="IP của client"
    )
    user_agent: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True,
        comment="Trình duyệt / Thiết bị"
    )
    details: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Thông tin chi tiết bổ sung (JSON format nếu có)"
    )
