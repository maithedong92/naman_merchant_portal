import inspect
import ipaddress
from typing import TYPE_CHECKING, List, Optional, Tuple
from urllib.parse import urlparse
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import decode_token
from app.models.user import User, UserRole

if TYPE_CHECKING:
    from app.models.store import Store

settings = get_settings()

# OAuth2 scheme configured to point to /api/v1/auth/login or token
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/v1/auth/token",
    auto_error=False
)


def _is_trusted_proxy(ip_str: Optional[str]) -> bool:
    """Check whether the direct peer IP belongs to a configured trusted reverse proxy."""
    if not ip_str:
        return False
    try:
        peer_ip = ipaddress.ip_address(ip_str)
        for cidr in settings.TRUSTED_PROXY_CIDRS:
            if peer_ip in ipaddress.ip_network(cidr, strict=False):
                return True
    except ValueError:
        return False
    return False


def get_client_ip_and_ua(request: Request) -> Tuple[Optional[str], Optional[str]]:
    """Extract real client IP (only trusting X-Forwarded-For from trusted proxies) and User-Agent."""
    peer_ip = request.client.host if request.client else None
    ip = peer_ip

    if _is_trusted_proxy(peer_ip):
        x_forwarded_for = request.headers.get("x-forwarded-for")
        if x_forwarded_for:
            # Walk from right to left to find the rightmost valid untrusted client IP
            parts = [p.strip() for p in x_forwarded_for.split(",") if p.strip()]
            valid_parts: List[str] = []
            for candidate in parts:
                try:
                    ipaddress.ip_address(candidate)
                    valid_parts.append(candidate)
                except ValueError:
                    continue
            for candidate in reversed(valid_parts):
                if not _is_trusted_proxy(candidate):
                    ip = candidate
                    break
            else:
                if valid_parts:
                    ip = valid_parts[0]

    user_agent = request.headers.get("user-agent")
    return ip, user_agent


_CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _validate_cookie_csrf(request: Request) -> None:
    """Validate Origin or Referer on state-changing requests authenticated via cookie."""
    origin_or_referer = request.headers.get("origin") or request.headers.get("referer")
    if not origin_or_referer:
        raise ForbiddenError("Yêu cầu không hợp lệ: thiếu Origin/Referer cho xác thực bằng cookie.")

    parsed = urlparse(origin_or_referer.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ForbiddenError("Yêu cầu không hợp lệ: Origin/Referer không đúng định dạng.")

    candidate_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

    for allowed in settings.ALLOWED_ORIGINS:
        allowed_clean = (allowed or "").strip().rstrip("/").lower()
        if not allowed_clean:
            continue
        if candidate_origin == allowed_clean:
            return

    if settings.ENVIRONMENT.lower() != "production" and request.url.scheme and request.url.netloc:
        request_origin = f"{request.url.scheme.lower()}://{request.url.netloc.lower()}"
        if candidate_origin == request_origin:
            return

    raise ForbiddenError("Yêu cầu bị từ chối do Origin/Referer không nằm trong danh sách cho phép.")


async def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency that validates the Bearer token (or HttpOnly cookie) and returns the current authenticated User.
    Raises 401 UnauthorizedError if token is missing, invalid, expired, or user is disabled.
    """
    cookie_token = request.cookies.get("naman_access_token")
    effective_token = token or cookie_token
    if not effective_token:
        raise UnauthorizedError("Yêu cầu cần có Bearer Access Token trong tiêu đề Authorization.")

    if not token and cookie_token and request.method.upper() in _CSRF_UNSAFE_METHODS:
        _validate_cookie_csrf(request)

    payload = decode_token(effective_token)
    token_type = payload.get("type")
    if token_type != "access":
        raise UnauthorizedError("Token cung cấp không phải là Access Token hợp lệ.")

    user_id = payload.get("sub")
    if not user_id:
        raise UnauthorizedError("Payload token không chứa thông tin nhận diện người dùng.")

    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise UnauthorizedError("Người dùng liên kết với token này không tồn tại trong hệ thống.")

    if not user.is_active:
        raise UnauthorizedError("Tài khoản người dùng đã bị vô hiệu hóa.")

    if getattr(user, "portal_disabled", False):
        raise UnauthorizedError("Tài khoản đã bị vô hiệu hóa trên cổng Portal.")

    raw_ver = payload.get("ver", 0)
    try:
        token_ver = int(0 if raw_ver is None else raw_ver)
    except (TypeError, ValueError):
        raise UnauthorizedError("Token cung cấp không hợp lệ.")
    if token_ver != int(getattr(user, "token_version", 0) or 0):
        raise UnauthorizedError("Phiên đăng nhập đã bị thu hồi. Vui lòng đăng nhập lại.")

    return user


get_current_active_user = get_current_user


class RoleChecker:
    """RBAC Guard dependency enforcing permitted roles."""

    def __init__(self, allowed_roles: List[UserRole]):
        self.allowed_roles = [r.value for r in allowed_roles]

    async def __call__(self, current_user: User = Depends(get_current_user)) -> User:
        # Super admin always has full privileges
        if current_user.is_superuser or current_user.role == UserRole.SUPER_ADMIN.value:
            return current_user

        if current_user.role not in self.allowed_roles:
            raise ForbiddenError(
                f"Tài khoản '{current_user.username}' không có quyền hạn cần thiết để thực hiện thao tác này. (Yêu cầu: {', '.join(self.allowed_roles)})"
            )
        return current_user


def require_roles(*roles: UserRole) -> RoleChecker:
    """Factory helper to enforce one or more roles."""
    return RoleChecker(list(roles))


# Pre-defined convenience guards
require_super_admin = require_roles(UserRole.SUPER_ADMIN)
require_store_manager = require_roles(UserRole.SUPER_ADMIN, UserRole.STORE_MANAGER)
require_financial_reader = require_roles(
    UserRole.SUPER_ADMIN, UserRole.STORE_MANAGER, UserRole.HQ_AUDITOR
)
require_order_dispatcher = require_roles(UserRole.SUPER_ADMIN, UserRole.STORE_MANAGER, UserRole.CS)
require_order_operator = require_roles(UserRole.SUPER_ADMIN, UserRole.STORE_MANAGER, UserRole.CS, UserRole.STAFF)
require_staff = require_roles(UserRole.SUPER_ADMIN, UserRole.STORE_MANAGER, UserRole.STAFF)


def is_head_office_manager_or_admin(current_user: User) -> bool:
    """True for SUPER_ADMIN (or is_superuser) or head-office STORE_MANAGER (store_id=None)."""
    return bool(
        current_user.is_superuser
        or current_user.role == UserRole.SUPER_ADMIN.value
        or (current_user.role == UserRole.STORE_MANAGER.value and current_user.store_id is None)
    )


async def require_head_office_manager(
    current_user: User = Depends(require_store_manager),
) -> User:
    """Enforce SUPER_ADMIN or head-office STORE_MANAGER without single-branch assignment."""
    if is_head_office_manager_or_admin(current_user):
        return current_user
    raise ForbiddenError(
        "Thao tác toàn chuỗi chỉ dành cho quản trị viên hệ thống hoặc quản lý tổng bộ (Head Office)."
    )


def has_cross_store_read_access(current_user: User) -> bool:
    """
    True for SUPER_ADMIN, HQ_AUDITOR, CS dispatchers, or head-office STORE_MANAGER
    without a single-branch assignment. Unassigned READ_ONLY fails closed (False).
    """
    if is_head_office_manager_or_admin(current_user):
        return True
    return current_user.role in (UserRole.HQ_AUDITOR.value, UserRole.CS.value)


def has_financial_global_read_access(current_user: User) -> bool:
    """
    True for SUPER_ADMIN, HQ_AUDITOR, or head-office STORE_MANAGER (store_id=None).
    Excludes CS and unassigned READ_ONLY accounts.
    """
    if is_head_office_manager_or_admin(current_user):
        return True
    return current_user.role == UserRole.HQ_AUDITOR.value


def check_store_access(current_user: User, store_id: str) -> None:
    """
    Validates if user is authorized to perform operations on a specific store.
    SUPER_ADMIN and Head-Office STORE_MANAGER (store_id=None) have access to all stores.
    Branch managers/staff can only operate on their assigned store.
    """
    if current_user.is_superuser or current_user.role == UserRole.SUPER_ADMIN.value:
        return

    if current_user.role == UserRole.STORE_MANAGER.value and current_user.store_id is None:
        return

    if current_user.store_id and current_user.store_id == store_id:
        return

    raise ForbiddenError(
        f"Bạn không có quyền quản trị hoặc thao tác trên chi nhánh '{store_id}'."
    )


async def check_store_access_by_key(
    db: AsyncSession,
    current_user: User,
    store_key: str,
) -> Optional["Store"]:
    """
    Resolve Store by ID or code before enforcing check_store_access.
    Fails closed (403) for branch-scoped users when no store record is resolved,
    while preserving mock-DB fallback for head-office/admin users.
    """
    from app.models.store import Store

    res = await db.execute(
        select(Store).where((Store.id == store_key) | (Store.code == store_key))
    )
    store = res.scalar_one_or_none()
    if inspect.iscoroutine(store):
        store.close()
        store = None
    store_id = getattr(store, "id", None) if store is not None else None
    if isinstance(store_id, str):
        check_store_access(current_user, store_id)
        return store
    if not is_head_office_manager_or_admin(current_user):
        raise ForbiddenError(
            f"Bạn không có quyền quản trị hoặc thao tác trên chi nhánh '{store_key}'."
        )
    return None


def reject_non_channel_mutator(current_user: User) -> None:
    """Block READ_ONLY and CS (order-dispatcher-only) roles from channel partner mutations."""
    role_val = (
        current_user.role.value
        if hasattr(current_user.role, "value")
        else str(current_user.role)
    )
    if (
        role_val in (UserRole.READ_ONLY.value, UserRole.CS.value)
        and not getattr(current_user, "is_superuser", False)
    ):
        raise ForbiddenError("Vai trò hiện tại không có quyền thực hiện thao tác thay đổi trên kênh bán.")
