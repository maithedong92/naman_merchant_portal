from collections import OrderedDict, deque
import logging
import threading
import time
from typing import Deque, Optional
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import _is_trusted_proxy, get_client_ip_and_ua, get_current_user
from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import UnauthorizedError
from app.core.responses import APIResponse
from app.models.user import User

settings = get_settings()


def _is_https_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    peer_ip = request.client.host if request.client else None
    if _is_trusted_proxy(peer_ip):
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return proto == "https"
    return False


def _set_auth_cookie(
    response: Response,
    request: Request,
    access_token: str,
    expires_in: int,
    refresh_token: Optional[str] = None,
) -> None:
    is_https = _is_https_request(request)
    response.set_cookie(
        key="naman_access_token",
        value=access_token,
        max_age=expires_in,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )
    if refresh_token:
        response.set_cookie(
            key="naman_refresh_token",
            value=refresh_token,
            max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=is_https,
            path="/api/v1/auth",
        )


def _clear_auth_cookies(response: Response, request: Request) -> None:
    is_https = _is_https_request(request)
    response.delete_cookie(
        key="naman_access_token",
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_https,
    )
    response.delete_cookie(
        key="naman_refresh_token",
        path="/api/v1/auth",
        httponly=True,
        samesite="lax",
        secure=is_https,
    )
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


class _SlidingWindowRateLimiter:
    """Thread-safe in-memory sliding-window rate limiter for auth endpoints."""

    def __init__(self, max_keys: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._hits: "OrderedDict[str, Deque[float]]" = OrderedDict()

    def check(self, key: str, max_requests: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            q = self._hits.get(key)
            if q is not None:
                while q and q[0] <= cutoff:
                    q.popleft()
                if not q:
                    del self._hits[key]
                    q = None
            if q is not None and len(q) >= max_requests:
                self._hits.move_to_end(key)
                return False
            if q is None:
                while len(self._hits) >= self._max_keys:
                    self._hits.popitem(last=False)
                q = deque()
                self._hits[key] = q
            else:
                self._hits.move_to_end(key)
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_auth_rate_limiter = _SlidingWindowRateLimiter()


def reset_auth_rate_limiter() -> None:
    """Clear in-memory auth rate limit counters (for tests)."""
    _auth_rate_limiter.reset()


def _enforce_rate_limit(req: Request, bucket: str, max_requests: int = 10, window_seconds: int = 60) -> None:
    if settings.ENVIRONMENT.lower() == "test":
        return
    ip, _ = get_client_ip_and_ua(req)
    key = f"{bucket}:{ip or 'unknown'}"
    if not _auth_rate_limiter.check(key, max_requests=max_requests, window_seconds=window_seconds):
        raise HTTPException(
            status_code=429,
            detail="Quá nhiều yêu cầu xác thực. Vui lòng thử lại sau ít phút.",
        )


@router.post(
    "/login",
    response_model=APIResponse[TokenResponse],
    summary="Đăng nhập tài khoản quản trị (JSON)",
    description="Xác thực qua tên đăng nhập (username) và mật khẩu. Trả về Access Token và Refresh Token.",
)
async def login(
    req: Request,
    response: Response,
    credentials: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    _enforce_rate_limit(req, "login", max_requests=10, window_seconds=60)
    ip, ua = get_client_ip_and_ua(req)
    _, token_response = await auth_service.authenticate_user(
        db=db,
        username_or_email=credentials.username,
        password=credentials.password,
        ip_address=ip,
        user_agent=ua,
    )
    _set_auth_cookie(
        response,
        req,
        token_response.access_token,
        token_response.expires_in,
        refresh_token=token_response.refresh_token,
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
    _enforce_rate_limit(req, "token", max_requests=10, window_seconds=60)
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
    response: Response,
    payload: Optional[RefreshTokenRequest] = Body(default=None),
    db: AsyncSession = Depends(get_db),
):
    _enforce_rate_limit(req, "refresh", max_requests=20, window_seconds=60)
    raw_refresh_token = (payload.refresh_token if payload else None) or req.cookies.get("naman_refresh_token")
    if not raw_refresh_token:
        raise UnauthorizedError("Thiếu Refresh Token.")
    ip, ua = get_client_ip_and_ua(req)
    token_response = await auth_service.refresh_access_token(
        db=db,
        refresh_token_str=raw_refresh_token,
        ip_address=ip,
        user_agent=ua,
    )
    _set_auth_cookie(
        response,
        req,
        token_response.access_token,
        token_response.expires_in,
        refresh_token=token_response.refresh_token,
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
    response: Response,
    payload: LogoutRequest = LogoutRequest(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ip, ua = get_client_ip_and_ua(req)
    await auth_service.logout_user(
        db=db,
        user=current_user,
        refresh_token_str=payload.refresh_token or req.cookies.get("naman_refresh_token"),
        ip_address=ip,
        user_agent=ua,
    )
    _clear_auth_cookies(response, req)
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
    response: Response,
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _enforce_rate_limit(req, "change_password", max_requests=10, window_seconds=60)
    ip, ua = get_client_ip_and_ua(req)
    await auth_service.change_password(
        db=db,
        user=current_user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        ip_address=ip,
        user_agent=ua,
    )
    _clear_auth_cookies(response, req)
    return APIResponse.ok(
        data={"updated": True},
        message="Đổi mật khẩu thành công. Vui lòng đăng nhập lại với mật khẩu mới."
    )
