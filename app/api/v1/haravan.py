import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Query, Body
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, require_super_admin, require_store_manager
from app.core.responses import APIResponse
from app.models.user import User
from app.modules.haravan.sync_worker import haravan_sync_worker
from app.services.arito_service import arito_service

router = APIRouter(prefix="/haravan", tags=["Haravan Omnichannel & Arito ERP"])
logger = logging.getLogger("naman_portal.haravan.api")


class RecommendStoreRequest(BaseModel):
    items: List[Dict[str, Any]] = Field(
        ...,
        description="Danh sách sản phẩm trong đơn hàng, ví dụ: [{'sku': '001020', 'quantity': 2}]",
    )


@router.get(
    "/status",
    response_model=APIResponse[Dict[str, Any]],
    summary="Trạng thái hệ thống đồng bộ Haravan",
    description="Xem tình trạng kết nối tới Haravan Omnichannel API và trạng thái background sync worker.",
)
async def get_haravan_status(
    current_user: User = Depends(require_store_manager),
):
    conn_info = await haravan_sync_worker.client.check_connection()
    worker_info = haravan_sync_worker.get_status()
    data = {
        "connection": conn_info,
        "worker": worker_info,
    }
    return APIResponse.ok(
        data=data,
        message="Lấy trạng thái Haravan Omnichannel thành công.",
    )


@router.post(
    "/sync",
    response_model=APIResponse[Dict[str, Any]],
    summary="Kích hoạt đồng bộ đơn hàng Haravan thủ công",
    description="Kích hoạt lấy ngay các đơn hàng mới nhất từ Haravan API về MongoDB (Chỉ SuperAdmin hoặc Cửa hàng trưởng).",
)
async def trigger_haravan_sync(
    limit: Optional[int] = Query(50, ge=1, le=200, description="Số lượng đơn hàng cần đồng bộ"),
    current_user: User = Depends(require_store_manager),
):
    result = await haravan_sync_worker.sync_once(limit=limit)
    if not result.get("success"):
        return APIResponse.error(
            message=result.get("error", "Lỗi trong quá trình đồng bộ đơn hàng."),
            error_code="HARAVAN_SYNC_ERROR",
        )
    return APIResponse.ok(
        data=result,
        message=f"Đồng bộ thành công {result.get('processed', 0)} đơn hàng từ Haravan.",
    )


@router.get(
    "/inventory-check",
    response_model=APIResponse[Dict[str, Any]],
    summary="Tra cứu tồn kho thực tế Arito ERP",
    description="Tra cứu số lượng tồn kho của danh sách SKU trên 4 siêu thị Nam An Market (10001, 10004, 10005, 10006).",
)
async def check_arito_inventory(
    skus: str = Query(..., description="Danh sách SKU cách nhau bởi dấu phẩy, ví dụ: '001020,001021'"),
    current_user: User = Depends(get_current_user),
):
    sku_list = [s.strip() for s in skus.split(",") if s.strip()]
    stock_data = await arito_service.get_inventory_by_skus(sku_list)
    return APIResponse.ok(
        data=stock_data,
        message=f"Tra cứu tồn kho thành công cho {len(stock_data)} SKU.",
    )


@router.post(
    "/recommend-store",
    response_model=APIResponse[Dict[str, Any]],
    summary="Gợi ý chi nhánh tối ưu để phân tuyến đơn hàng",
    description="Phân tích tồn kho Arito của các món trong đơn hàng và đề xuất chi nhánh có đủ hàng nhất để phân tuyến.",
)
async def recommend_store_for_order(
    payload: RecommendStoreRequest,
    current_user: User = Depends(require_store_manager),
):
    recommendation = await arito_service.recommend_best_store_for_order(payload.items)
    return APIResponse.ok(
        data=recommendation,
        message="Đề xuất chi nhánh phân tuyến thành công dựa trên tồn kho Arito.",
    )


@router.get(
    "/arito-status",
    response_model=APIResponse[Dict[str, Any]],
    summary="Kiểm tra kết nối Arito ERP",
    description="Kiểm tra trạng thái kết nối tới cơ sở dữ liệu Arito MongoDB và tổng số bản ghi tồn kho.",
)
async def get_arito_status(
    current_user: User = Depends(require_super_admin),
):
    status_info = arito_service.check_connection()
    return APIResponse.ok(
        data=status_info,
        message=status_info.get("message", "Kiểm tra Arito hoàn tất."),
    )
