from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.responses import APIResponse
from app.models.user import User
from app.schemas.common import PaginatedResponse, PaginationMeta
from app.schemas.report import FullAnalyticsReport, ReconciliationOrderItem
from app.services.report_service import report_service

router = APIRouter(prefix="/reports", tags=["Reports & Reconciliation"])


@router.get(
    "/analytics",
    response_model=APIResponse[FullAnalyticsReport],
    summary="Báo cáo phân tích doanh thu & KPI đa kênh",
    description="Tổng hợp doanh thu gộp, doanh thu thực nhận, số lượng đơn, tỷ lệ hủy, phân bố theo sàn và theo từng chi nhánh Nam An.",
)
async def get_analytics_report(
    from_date: Optional[date] = Query(None, description="Từ ngày (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, description="Đến ngày (YYYY-MM-DD)"),
    channel_code: Optional[str] = Query(None, description="Lọc theo kênh (SHOPEEFOOD, GRABMART, SHOPEEMART)"),
    store_code: Optional[str] = Query(None, description="Lọc theo chi nhánh (10001, 10004, 10005, 10006)"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    analytics = await report_service.get_full_analytics(
        db=db,
        from_date=from_date,
        to_date=to_date,
        channel_code=channel_code,
        store_code=store_code,
    )
    return APIResponse.ok(
        data=analytics,
        message="Lấy dữ liệu phân tích doanh thu đa kênh thành công."
    )


@router.get(
    "/reconciliation",
    response_model=APIResponse[PaginatedResponse[ReconciliationOrderItem]],
    summary="Danh sách đối soát chi tiết dòng tiền đơn hàng",
    description="Truy vấn danh sách đơn hàng có bóc tách tiền hàng, phí ship, chiết khấu và doanh thu ròng cho phòng kế toán.",
)
async def get_reconciliation_orders(
    from_date: Optional[date] = Query(None, description="Từ ngày (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, description="Đến ngày (YYYY-MM-DD)"),
    channel_code: Optional[str] = Query(None, description="Lọc theo kênh bán hàng"),
    store_code: Optional[str] = Query(None, description="Lọc theo chi nhánh"),
    search: Optional[str] = Query(None, description="Tìm theo mã đơn hoặc tên khách hàng"),
    page: int = Query(1, ge=1, description="Số trang hiện tại"),
    page_size: int = Query(20, ge=1, le=100, description="Số bản ghi trên mỗi trang"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items, total = await report_service.get_reconciliation_orders(
        db=db,
        from_date=from_date,
        to_date=to_date,
        channel_code=channel_code,
        store_code=store_code,
        search=search,
        page=page,
        page_size=page_size,
    )
    paginated = PaginatedResponse(
        items=items,
        meta=PaginationMeta.create(page=page, page_size=page_size, total_items=total),
    )
    return APIResponse.ok(
        data=paginated,
        message="Lấy danh sách đối soát đơn hàng thành công."
    )


@router.get(
    "/export",
    summary="Xuất báo cáo đối soát tài chính ra định dạng CSV/Excel",
    description="Tải xuống tệp CSV định dạng chuẩn UTF-8 BOM, tương thích hoàn toàn với Microsoft Excel.",
)
async def export_reconciliation_csv(
    from_date: Optional[date] = Query(None, description="Từ ngày (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, description="Đến ngày (YYYY-MM-DD)"),
    channel_code: Optional[str] = Query(None, description="Lọc theo kênh bán hàng"),
    store_code: Optional[str] = Query(None, description="Lọc theo chi nhánh"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    csv_content = await report_service.export_reconciliation_csv(
        db=db,
        from_date=from_date,
        to_date=to_date,
        channel_code=channel_code,
        store_code=store_code,
    )

    filename_date = date.today().strftime("%Y%m%d")
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=naman_doi_soat_{filename_date}.csv"
        },
    )
