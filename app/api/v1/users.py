import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_super_admin
from app.core.database import get_db
from app.core.responses import APIResponse
from app.models.user import User, UserRole
from app.schemas.common import PaginatedResponse, PaginationMeta
from app.schemas.user import (
    AuditSecurityLogResponse,
    UserCreate,
    UserResponse,
    UserUpdate,
)
from app.services.auth_service import auth_service

router = APIRouter(prefix="/users", tags=["Core - User Management & RBAC"])
logger = logging.getLogger("naman_portal.users")


@router.get(
    "",
    response_model=APIResponse[PaginatedResponse[UserResponse]],
    summary="Danh sách người dùng hệ thống",
    description="Tra cứu danh sách nhân viên/quản trị viên với bộ lọc vai trò, chi nhánh (Chỉ SuperAdmin).",
)
async def list_users(
    role: Optional[UserRole] = Query(None, description="Lọc theo vai trò"),
    store_id: Optional[str] = Query(None, description="Lọc theo mã chi nhánh"),
    is_active: Optional[bool] = Query(None, description="Lọc theo trạng thái hoạt động"),
    page: int = Query(1, ge=1, description="Trang hiện tại"),
    page_size: int = Query(50, ge=1, le=100, description="Số lượng bản ghi mỗi trang"),
    current_admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    users, total = await auth_service.list_users(
        db=db,
        role=role,
        store_id=store_id,
        is_active=is_active,
        page=page,
        page_size=page_size,
    )
    user_responses = [UserResponse.model_validate(u) for u in users]
    paginated_data = PaginatedResponse(
        items=user_responses,
        meta=PaginationMeta.create(page=page, page_size=page_size, total_items=total),
    )

    return APIResponse.ok(
        data=paginated_data,
        message="Lấy danh sách người dùng thành công."
    )


@router.post(
    "",
    response_model=APIResponse[UserResponse],
    summary="Tạo người dùng mới",
    description="Tạo tài khoản người dùng hoặc nhân viên chi nhánh mới (Chỉ SuperAdmin).",
)
async def create_user(
    payload: UserCreate,
    current_admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await auth_service.create_user(db=db, user_in=payload)
    return APIResponse.ok(
        data=UserResponse.model_validate(user),
        message=f"Đã tạo người dùng '{user.username}' thành công."
    )


@router.get(
    "/security-logs",
    response_model=APIResponse[List[AuditSecurityLogResponse]],
    summary="Nhật ký kiểm toán bảo mật (Security Audit Logs)",
    description="Xem các sự kiện đăng nhập, đăng xuất, khóa tài khoản, đổi mật khẩu và cảnh báo bảo mật (Chỉ SuperAdmin).",
)
async def list_security_logs(
    user_id: Optional[str] = Query(None, description="Lọc theo mã người dùng"),
    event_type: Optional[str] = Query(None, description="Lọc theo loại sự kiện"),
    limit: int = Query(100, ge=1, le=500, description="Số lượng nhật ký tối đa"),
    current_admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    logs = await auth_service.list_security_logs(
        db=db,
        user_id=user_id,
        event_type=event_type,
        limit=limit,
    )
    log_responses = [AuditSecurityLogResponse.model_validate(l) for l in logs]
    return APIResponse.ok(
        data=log_responses,
        message="Lấy nhật ký kiểm toán bảo mật thành công."
    )


@router.get(
    "/{user_id}",
    response_model=APIResponse[UserResponse],
    summary="Chi tiết người dùng",
    description="Lấy thông tin chi tiết một tài khoản người dùng theo ID.",
)
async def get_user_detail(
    user_id: str,
    current_admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await auth_service.get_user_by_id(db=db, user_id=user_id)
    return APIResponse.ok(
        data=UserResponse.model_validate(user),
        message="Lấy chi tiết người dùng thành công."
    )


@router.put(
    "/{user_id}",
    response_model=APIResponse[UserResponse],
    summary="Cập nhật người dùng",
    description="Cập nhật vai trò, chi nhánh, trạng thái hoạt động hoặc đặt lại mật khẩu cho người dùng.",
)
async def update_user(
    user_id: str,
    payload: UserUpdate,
    current_admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await auth_service.update_user(db=db, user_id=user_id, user_in=payload)
    return APIResponse.ok(
        data=UserResponse.model_validate(user),
        message=f"Đã cập nhật tài khoản '{user.username}' thành công."
    )


@router.delete(
    "/{user_id}",
    response_model=APIResponse[dict],
    summary="Vô hiệu hóa người dùng",
    description="Đổi trạng thái tài khoản sang không hoạt động (is_active=False).",
)
async def deactivate_user(
    user_id: str,
    current_admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    await auth_service.update_user(
        db=db,
        user_id=user_id,
        user_in=UserUpdate(is_active=False),
    )
    return APIResponse.ok(
        data={"deactivated": True},
        message="Đã vô hiệu hóa tài khoản người dùng thành công."
    )
