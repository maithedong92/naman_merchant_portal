from typing import Callable, List, Optional, Tuple
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import decode_token
from app.models.user import User, UserRole

# OAuth2 scheme configured to point to /api/v1/auth/login or token
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/v1/auth/token",
    auto_error=False
)


def get_client_ip_and_ua(request: Request) -> Tuple[Optional[str], Optional[str]]:
    """Extract real client IP (considering reverse proxies) and User-Agent."""
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        # Take the first IP in the list
        ip = x_forwarded_for.split(",")[0].strip()
    elif request.client:
        ip = request.client.host
    else:
        ip = None

    user_agent = request.headers.get("user-agent")
    return ip, user_agent


async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency that validates the Bearer token and returns the current authenticated User.
    Raises 401 UnauthorizedError if token is missing, invalid, expired, or user is disabled.
    """
    if not token:
        raise UnauthorizedError("Yêu cầu cần có Bearer Access Token trong tiêu đề Authorization.")

    payload = decode_token(token)
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

    return user


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
require_staff = require_roles(UserRole.SUPER_ADMIN, UserRole.STORE_MANAGER, UserRole.STAFF)


def check_store_access(current_user: User, store_id: str) -> None:
    """
    Validates if user is authorized to perform operations on a specific store.
    SUPER_ADMIN has access to all stores.
    Store managers/staff can only operate on their assigned store.
    """
    if current_user.is_superuser or current_user.role == UserRole.SUPER_ADMIN.value:
        return

    if current_user.store_id and current_user.store_id == store_id:
        return

    raise ForbiddenError(
        f"Bạn không có quyền quản trị hoặc thao tác trên chi nhánh '{store_id}'."
    )
