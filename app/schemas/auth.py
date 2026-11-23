from typing import Optional
from pydantic import BaseModel, Field, field_validator

from app.schemas.user import UserResponse, validate_password_policy


class LoginRequest(BaseModel):
    """Schema for username/password authentication request."""
    username: str = Field(..., min_length=1, max_length=100, description="Tên đăng nhập (username) hoặc mã nhân viên")
    password: str = Field(..., min_length=1, max_length=128, description="Mật khẩu tài khoản")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Strip whitespace and block control/null characters in login identifier."""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Tên đăng nhập không được để trống.")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
            raise ValueError("Tên đăng nhập chứa ký tự điều khiển không hợp lệ.")
        return cleaned


class TokenResponse(BaseModel):
    """Schema for JWT access token and refresh token response."""
    access_token: str = Field(..., description="JWT Access Token để đính kèm vào Authorization header")
    refresh_token: str = Field(..., description="JWT Refresh Token dùng để gia hạn khi access token hết hạn")
    token_type: str = Field(default="bearer", description="Loại token")
    expires_in: int = Field(..., description="Thời gian hiệu lực của access token tính theo giây")
    user: UserResponse = Field(..., description="Thông tin tài khoản vừa đăng nhập")


class RefreshTokenRequest(BaseModel):
    """Schema for requesting a new token pair using a valid refresh token."""
    refresh_token: Optional[str] = Field(None, description="Chuỗi refresh token hợp lệ")


class ChangePasswordRequest(BaseModel):
    """Schema for changing account password."""
    current_password: str = Field(..., description="Mật khẩu hiện tại")
    new_password: str = Field(..., min_length=8, max_length=72, description="Mật khẩu mới (tối thiểu 8 ký tự)")

    @field_validator("new_password")
    @classmethod
    def check_new_password_policy(cls, v: str) -> str:
        return validate_password_policy(v)


class LogoutRequest(BaseModel):
    """Optional schema for logging out and revoking a refresh token."""
    refresh_token: Optional[str] = Field(None, description="Chuỗi refresh token cần thu hồi")
