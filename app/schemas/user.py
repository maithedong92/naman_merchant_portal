from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole


class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, description="Tên đăng nhập")
    email: EmailStr = Field(..., description="Email người dùng")
    full_name: str = Field(..., min_length=2, max_length=100, description="Họ và tên")
    phone_number: Optional[str] = Field(None, max_length=20, description="Số điện thoại")
    role: UserRole = Field(default=UserRole.STAFF, description="Vai trò người dùng trong hệ thống")
    store_id: Optional[str] = Field(None, description="Mã chi nhánh được phân công (nếu có)")


class UserCreate(UserBase):
    password: str = Field(..., min_length=8, max_length=128, description="Mật khẩu (tối thiểu 8 ký tự)")


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = Field(None, description="Email người dùng")
    full_name: Optional[str] = Field(None, min_length=2, max_length=100)
    phone_number: Optional[str] = Field(None, max_length=20)
    role: Optional[UserRole] = None
    store_id: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(None, min_length=8, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    email: str
    full_name: str
    phone_number: Optional[str] = None
    role: UserRole
    store_id: Optional[str] = None
    is_active: bool
    is_superuser: bool
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AuditSecurityLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: Optional[str] = None
    username: Optional[str] = None
    event_type: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    details: Optional[str] = None
    created_at: Optional[datetime] = None

