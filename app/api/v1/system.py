import logging
import traceback
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_store_manager, require_super_admin
from app.core.database import get_db
from app.core.responses import APIResponse
from app.models.operational_error import ErrorSeverity, ErrorStatus
from app.models.user import User
from app.schemas.common import PaginatedResponse, PaginationMeta
from app.schemas.system import (
    OperationalErrorResolveRequest,
    OperationalErrorResponse,
    SystemHealthResponse,
)
from app.services.error_service import error_service

router = APIRouter(prefix="/system", tags=["System Health & Operations"])
logger = logging.getLogger("naman_portal.system")


@router.get(
    "/health",
    response_model=APIResponse[SystemHealthResponse],
    summary="Trạng thái sức khỏe hệ thống (Live System Status)",
    description="Kiểm tra kết nối PostgreSQL, trạng thái các kênh bán lẻ (ShopeeFood, GrabMart, ShopeeMart) và tổng kết lỗi vận hành.",
)
async def get_system_health(
    db: AsyncSession = Depends(get_db),
):
    health = await error_service.get_system_health(db)
    return APIResponse.ok(
        data=health,
        message=f"Hệ thống đang ở trạng thái: {health.status}"
    )


@router.get(
    "/errors",
    response_model=APIResponse[PaginatedResponse[OperationalErrorResponse]],
    summary="Danh sách sự cố & lỗi vận hành (Operational Error Logs)",
    description="Truy vấn toàn bộ lỗi xảy ra trong quá trình vận hành với bộ lọc mức độ nghiêm trọng, phân hệ, trạng thái xử lý.",
)
async def list_operational_errors(
    severity: Optional[ErrorSeverity] = Query(None, description="Lọc theo mức độ nghiêm trọng"),
    module: Optional[str] = Query(None, description="Lọc theo phân hệ (CORE, SHOPEEFOOD, GRABMART, SHOPEEMART, DATABASE, AUTH)"),
    status: Optional[ErrorStatus] = Query(None, description="Lọc theo trạng thái xử lý (OPEN, INVESTIGATING, RESOLVED, IGNORED)"),
    search: Optional[str] = Query(None, description="Tìm kiếm theo mã lỗi, thông điệp hoặc endpoint"),
    page: int = Query(1, ge=1, description="Trang hiện tại"),
    page_size: int = Query(50, ge=1, le=100, description="Số bản ghi mỗi trang"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    errors, total = await error_service.list_errors(
        db=db,
        severity=severity,
        module=module,
        status=status,
        search=search,
        page=page,
        page_size=page_size,
    )
    items = [OperationalErrorResponse.model_validate(e) for e in errors]
    paginated = PaginatedResponse(
        items=items,
        meta=PaginationMeta.create(page=page, page_size=page_size, total_items=total),
    )
    return APIResponse.ok(
        data=paginated,
        message="Lấy danh sách lỗi vận hành thành công."
    )


@router.get(
    "/errors/{error_id}",
    response_model=APIResponse[OperationalErrorResponse],
    summary="Chi tiết một sự cố vận hành",
    description="Xem toàn bộ thông tin lỗi gồm Python stack trace, endpoint, payload gửi lên và lịch sử xử lý.",
)
async def get_error_detail(
    error_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    error_log = await error_service.get_error_by_id(db, error_id)
    return APIResponse.ok(
        data=OperationalErrorResponse.model_validate(error_log),
        message="Lấy chi tiết sự cố thành công."
    )


@router.post(
    "/errors/{error_id}/resolve",
    response_model=APIResponse[OperationalErrorResponse],
    summary="Đánh dấu đã xử lý sự cố (Resolve Incident)",
    description="Cập nhật trạng thái sự cố sang RESOLVED kèm theo ghi chú giải pháp khắc phục.",
)
async def resolve_error_incident(
    error_id: str,
    payload: OperationalErrorResolveRequest,
    current_user: User = Depends(require_store_manager),
    db: AsyncSession = Depends(get_db),
):
    resolved_by = payload.resolved_by or current_user.full_name or current_user.username
    updated_log = await error_service.resolve_error(
        db=db,
        error_id=error_id,
        resolution_notes=payload.resolution_notes,
        resolved_by=resolved_by,
    )
    return APIResponse.ok(
        data=OperationalErrorResponse.model_validate(updated_log),
        message=f"Đã đánh dấu sự cố #{error_id} là ĐÃ XỬ LÝ (RESOLVED)."
    )


@router.post(
    "/errors/{error_id}/reopen",
    response_model=APIResponse[OperationalErrorResponse],
    summary="Mở lại sự cố để tiếp tục kiểm tra (Reopen Incident)",
    description="Chuyển trạng thái sự cố về INVESTIGATING.",
)
async def reopen_error_incident(
    error_id: str,
    current_user: User = Depends(require_store_manager),
    db: AsyncSession = Depends(get_db),
):
    updated_log = await error_service.reopen_error(db=db, error_id=error_id)
    return APIResponse.ok(
        data=OperationalErrorResponse.model_validate(updated_log),
        message=f"Đã mở lại sự cố #{error_id} (INVESTIGATING)."
    )


@router.post(
    "/test-error",
    response_model=APIResponse[OperationalErrorResponse],
    summary="Kích hoạt lỗi thử nghiệm (Trigger Test Error)",
    description="Endpoint mô phỏng phát sinh lỗi vận hành ngẫu nhiên để kiểm tra cơ chế ghi log và hiển thị trên trang system-status.",
)
async def trigger_test_error(
    req: Request,
    severity: ErrorSeverity = Query(ErrorSeverity.ERROR, description="Mức độ nghiêm trọng của lỗi thử nghiệm"),
    module: str = Query("SHOPEEFOOD", description="Phân hệ phát sinh lỗi"),
    message: str = Query("Lỗi đồng bộ menu: Đối tác ShopeeFood phản hồi timeout sau 15 giây", description="Mô tả lỗi"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        raise RuntimeError(f"Simulated operational error: {message}")
    except Exception as ex:
        stack = traceback.format_exc()
        client_ip = req.client.host if req.client else "127.0.0.1"
        error_log = await error_service.log_error(
            db=db,
            error_code="TEST_SIMULATED_INCIDENT",
            message=message,
            severity=severity,
            module=module,
            stack_trace=stack,
            endpoint=str(req.url.path),
            http_method=req.method,
            http_status_code=500,
            client_ip=client_ip,
            user_id=current_user.id,
            request_payload={"simulated": True, "triggered_by": current_user.username},
        )
        return APIResponse.ok(
            data=OperationalErrorResponse.model_validate(error_log),
            message="Đã kích hoạt và lưu vết lỗi thử nghiệm thành công."
        )
