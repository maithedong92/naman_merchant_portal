import logging
from fastapi import APIRouter, Depends, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_client_ip_and_ua, get_current_user
from app.core.database import get_db
from app.core.responses import APIResponse
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    TokenResponse,
)
from app.schemas.user import UserResponse
from app.services.auth_service import auth_service

router = APIRouter(prefix="/auth", tags=["Core - Authentication & Security"])
logger = logging.getLogger("naman_portal.auth")


@router.post(
    "/login",
    response_model=APIResponse[TokenResponse],
    summary="Đăng nhập tài khoản quản trị (JSON)",
    description="Xác thực qua username/email và mật khẩu. Trả về Access Token và Refresh Token.",
)
async def login(
    req: Request,
    credentials: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    ip, ua = get_client_ip_and_ua(req)
    _, token_response = await auth_service.authenticate_user(
        db=db,
        username_or_email=credentials.username,
        password=credentials.password,
        ip_address=ip,
        user_agent=ua,
    )
    return APIResponse.ok(
        data=token_response,
        message="Đăng nhập thành công."
    )


@router.post(
    "/token",
    summary="OAuth2 Password Form Token (Swagger Docs)",
    description="Endpoint hỗ trợ form login chuẩn OAuth2 cho nút Authorize trên giao diện Swagger UI /docs.",
)
async def oauth2_login(
    req: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    ip, ua = get_client_ip_and_ua(req)
    _, token_response = await auth_service.authenticate_user(
        db=db,
        username_or_email=form_data.username,
        password=form_data.password,
        ip_address=ip,
        user_agent=ua,
    )
    # Return raw dict conforming to OAuth2 spec for Swagger UI
    return {
        "access_token": token_response.access_token,
        "token_type": "bearer",
        "refresh_token": token_response.refresh_token,
        "expires_in": token_response.expires_in,
    }


@router.post(
    "/refresh",
    response_model=APIResponse[TokenResponse],
    summary="Làm mới Access Token (Token Rotation)",
    description="Gia hạn cặp Access/Refresh Token bằng Refresh Token hợp lệ.",
)
async def refresh_token(
    req: Request,
    payload: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    ip, ua = get_client_ip_and_ua(req)
    token_response = await auth_service.refresh_access_token(
        db=db,
        refresh_token_str=payload.refresh_token,
        ip_address=ip,
        user_agent=ua,
    )
    return APIResponse.ok(
        data=token_response,
        message="Làm mới token thành công."
    )


@router.post(
    "/logout",
    response_model=APIResponse[dict],
    summary="Đăng xuất và thu hồi Refresh Token",
    description="Thu hồi phiên đăng nhập hiện tại hoặc toàn bộ phiên của người dùng.",
)
async def logout(
    req: Request,
    payload: LogoutRequest = LogoutRequest(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ip, ua = get_client_ip_and_ua(req)
    await auth_service.logout_user(
        db=db,
        user=current_user,
        refresh_token_str=payload.refresh_token,
        ip_address=ip,
        user_agent=ua,
    )
    return APIResponse.ok(
        data={"revoked": True},
        message="Đăng xuất thành công."
    )


@router.get(
    "/me",
    response_model=APIResponse[UserResponse],
    summary="Lấy thông tin tài khoản hiện tại",
    description="Trả về thông tin chi tiết, vai trò và chi nhánh phụ trách của tài khoản đang đăng nhập.",
)
async def get_me(
    current_user: User = Depends(get_current_user),
):
    return APIResponse.ok(
        data=UserResponse.model_validate(current_user),
        message="Lấy thông tin tài khoản thành công."
    )


@router.post(
    "/change-password",
    response_model=APIResponse[dict],
    summary="Đổi mật khẩu tài khoản",
    description="Thay đổi mật khẩu cá nhân. Toàn bộ phiên đăng nhập cũ sẽ bị thu hồi.",
)
async def change_password(
    req: Request,
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ip, ua = get_client_ip_and_ua(req)
    await auth_service.change_password(
        db=db,
        user=current_user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        ip_address=ip,
        user_agent=ua,
    )
    return APIResponse.ok(
        data={"updated": True},
        message="Đổi mật khẩu thành công. Vui lòng đăng nhập lại với mật khẩu mới."
    )
