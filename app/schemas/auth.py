from typing import Optional
from pydantic import BaseModel, Field

from app.schemas.user import UserResponse


class LoginRequest(BaseModel):
    """Schema for username/password authentication request."""
    username: str = Field(..., description="Tên đăng nhập hoặc địa chỉ email")
    password: str = Field(..., description="Mật khẩu tài khoản")


class TokenResponse(BaseModel):
    """Schema for JWT access token and refresh token response."""
    access_token: str = Field(..., description="JWT Access Token để đính kèm vào Authorization header")
    refresh_token: str = Field(..., description="JWT Refresh Token dùng để gia hạn khi access token hết hạn")
    token_type: str = Field(default="bearer", description="Loại token")
    expires_in: int = Field(..., description="Thời gian hiệu lực của access token tính theo giây")
    user: UserResponse = Field(..., description="Thông tin tài khoản vừa đăng nhập")


class RefreshTokenRequest(BaseModel):
    """Schema for requesting a new token pair using a valid refresh token."""
    refresh_token: str = Field(..., description="Chuỗi refresh token hợp lệ")


class ChangePasswordRequest(BaseModel):
    """Schema for changing account password."""
    current_password: str = Field(..., description="Mật khẩu hiện tại")
    new_password: str = Field(..., min_length=8, max_length=128, description="Mật khẩu mới (tối thiểu 8 ký tự)")


class LogoutRequest(BaseModel):
    """Optional schema for logging out and revoking a refresh token."""
    refresh_token: Optional[str] = Field(None, description="Chuỗi refresh token cần thu hồi")
