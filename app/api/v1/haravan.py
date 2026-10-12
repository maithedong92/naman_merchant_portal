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


# ==============================================================================
# QUY TRÌNH PHÂN TUYẾN & XỬ LÝ ĐƠN HÀNG HARAVAN (FOLLOW 100% PMDONHANG)
# ==============================================================================

class AssignOrderRequest(BaseModel):
    store_code: str = Field(..., description="Mã chi nhánh: 10001, 10004, 10005, 10006, 10008")
    reason: str = Field(..., description="Lý do: Gần cửa hàng, Cửa hàng có sẵn hàng, Shipper gần nhất, Khách yêu cầu, Khác")
    note: str = Field("", description="Ghi chú phân tuyến bổ sung")


class ReassignOrderRequest(BaseModel):
    reason: str = Field(..., description="Lý do: Cửa hàng không có hàng, Cửa hàng quá tải, Địa chỉ quá xa, Khách yêu cầu, Khác")
    note: str = Field("", description="Ghi chú bổ sung")


class ShipOrderRequest(BaseModel):
    tracking_code: str = Field(..., description="Mã vận đơn giao hàng")
    carrier: str = Field("Nam An Market", description="Đơn vị vận chuyển: Nam An Market, Ahamove, GHN, Grab Express, Khác")
    note: Optional[str] = Field(None, description="Ghi chú vận đơn")


class CompleteOrderRequest(BaseModel):
    actual_total: float = Field(..., description="Tổng tiền hàng thực tế sau cân/soạn")
    actual_shipping: float = Field(0.0, description="Phí ship thực tế")
    actual_carrier: str = Field("Nam An Market", description="Đơn vị vận chuyển thực tế")
    tracking_code: str = Field("", description="Mã vận đơn thực tế")
    note: str = Field("", description="Ghi chú hoàn thành đơn")


class CancelOrderRequest(BaseModel):
    reason: str = Field(..., description="Lý do hủy đơn")
    note: str = Field("", description="Ghi chú hủy đơn")


from app.services.haravan_order_service import haravan_order_service


@router.get(
    "/orders",
    response_model=APIResponse[Dict[str, Any]],
    summary="Danh sách đơn hàng Haravan Omnichannel",
    description="Tra cứu danh sách đơn hàng Haravan từ MongoDB. Áp dụng Store Isolation RBAC: Staff chỉ thấy đơn đã phân tuyến về chi nhánh mình. SuperAdmin thấy tất cả và có thể lọc đơn chưa phân tuyến.",
)
async def list_haravan_orders(
    status: Optional[str] = Query(None, description="Lọc trạng thái xử lý nội bộ"),
    store_code: Optional[str] = Query(None, description="Lọc theo mã chi nhánh"),
    source: Optional[str] = Query(None, description="Lọc theo nguồn: checkout_next, shopee, lazada, pos, manual"),
    search: Optional[str] = Query(None, description="Tìm theo mã đơn (#NA...), SĐT, tên khách hàng"),
    unassigned_only: bool = Query(False, description="Chỉ lấy các đơn CHƯA PHÂN TUYẾN (Cần tổng đài điều phối)"),
    incomplete_only: bool = Query(False, description="Chỉ lấy các đơn CHƯA HOÀN THÀNH (khác Hoàn thành và Đã hủy)"),
    page: int = Query(1, ge=1, description="Trang hiện tại"),
    page_size: int = Query(25, ge=1, le=100, description="Số đơn mỗi trang"),
    current_user: User = Depends(get_current_user),
):
    items, total = await haravan_order_service.list_orders(
        current_user=current_user,
        status=status,
        store_code=store_code,
        source=source,
        search=search,
        unassigned_only=unassigned_only,
        incomplete_only=incomplete_only,
        page=page,
        page_size=page_size,
    )
    return APIResponse.ok(
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size if total > 0 else 1,
        },
        message=f"Lấy {len(items)} đơn hàng Haravan thành công.",
    )


@router.get(
    "/incomplete-count",
    response_model=APIResponse[Dict[str, Any]],
    summary="Đếm số lượng đơn Haravan chưa hoàn thành",
    description="Đếm các đơn hàng Haravan có trạng thái chưa hoàn thành (ngoại trừ Hoàn thành và Đã hủy) theo phân quyền chi nhánh.",
)
async def get_incomplete_count(
    store_code: Optional[str] = Query(None, description="Lọc theo mã chi nhánh"),
    current_user: User = Depends(get_current_user),
):
    count = await haravan_order_service.count_incomplete_orders(
        current_user=current_user,
        store_code=store_code,
    )
    return APIResponse.ok(
        data={"count": count},
        message=f"Có {count} đơn hàng Haravan chưa hoàn thành.",
    )


@router.get(
    "/orders/{order_id}",
    response_model=APIResponse[Dict[str, Any]],
    summary="Chi tiết đơn hàng Haravan & Tồn kho Arito",
    description="Lấy chi tiết 1 đơn hàng Haravan kèm theo số lượng tồn kho Arito ERP thời gian thực của các SKU tại 4 chi nhánh.",
)
async def get_haravan_order_detail(
    order_id: str,
    current_user: User = Depends(get_current_user),
):
    order = await haravan_order_service.get_order_detail(order_id, current_user)
    return APIResponse.ok(
        data=order,
        message="Lấy chi tiết đơn hàng Haravan thành công.",
    )


@router.post(
    "/orders/{order_id}/assign",
    response_model=APIResponse[Dict[str, Any]],
    summary="Phân tuyến đơn hàng cho chi nhánh",
    description="Điều phối viên gán đơn hàng cho 1 trong 4 siêu thị Nam An (10001, 10004, 10005, 10006, 10008). Tự động chụp snapshot tồn kho Arito.",
)
async def assign_haravan_order(
    order_id: str,
    payload: AssignOrderRequest,
    current_user: User = Depends(require_store_manager),
):
    updated = await haravan_order_service.assign_order(
        order_id_or_sn=order_id,
        store_code=payload.store_code,
        reason=payload.reason,
        note=payload.note,
        current_user=current_user,
    )
    return APIResponse.ok(
        data=updated,
        message=f"Đã phân tuyến đơn hàng cho chi nhánh {payload.store_code} thành công.",
    )


@router.post(
    "/orders/{order_id}/reassign",
    response_model=APIResponse[Dict[str, Any]],
    summary="Yêu cầu phân tuyến lại đơn hàng",
    description="Nhân viên chi nhánh báo thiếu hàng/quá tải. Gỡ bỏ assigned_store = null, trả đơn về hàng đợi tổng đài để đổi chi nhánh khác.",
)
async def reassign_haravan_order(
    order_id: str,
    payload: ReassignOrderRequest,
    current_user: User = Depends(get_current_user),
):
    updated = await haravan_order_service.request_reassignment(
        order_id_or_sn=order_id,
        reason=payload.reason,
        note=payload.note,
        current_user=current_user,
    )
    return APIResponse.ok(
        data=updated,
        message="Đã gửi yêu cầu phân tuyến lại. Đơn hàng đã được trả về tổng đài điều phối.",
    )


@router.post(
    "/orders/{order_id}/pack",
    response_model=APIResponse[Dict[str, Any]],
    summary="Xác nhận đã đóng gói hàng",
    description="Nhân viên siêu thị hoàn tất gom hàng và đóng gói, chuyển trạng thái 'Đã đóng gói'.",
)
async def pack_haravan_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
):
    updated = await haravan_order_service.pack_order(
        order_id_or_sn=order_id,
        current_user=current_user,
    )
    return APIResponse.ok(
        data=updated,
        message="Đã cập nhật trạng thái đơn hàng sang 'Đã đóng gói'.",
    )


@router.post(
    "/orders/{order_id}/ship",
    response_model=APIResponse[Dict[str, Any]],
    summary="Xác nhận đã gửi vận đơn",
    description="Đã bàn giao đơn hàng cho đơn vị vận chuyển, lưu mã vận đơn và chuyển trạng thái 'Đã gửi vận đơn'.",
)
async def ship_haravan_order(
    order_id: str,
    payload: ShipOrderRequest,
    current_user: User = Depends(get_current_user),
):
    updated = await haravan_order_service.ship_order(
        order_id_or_sn=order_id,
        tracking_code=payload.tracking_code,
        carrier=payload.carrier,
        current_user=current_user,
        note=payload.note,
    )
    return APIResponse.ok(
        data=updated,
        message="Đã cập nhật trạng thái đơn hàng sang 'Đã gửi vận đơn'.",
    )


@router.post(
    "/orders/{order_id}/complete",
    response_model=APIResponse[Dict[str, Any]],
    summary="Xác nhận hoàn thành đơn hàng",
    description="Chốt tổng tiền thực tế, cước ship thực tế, đơn vị vận chuyển và hoàn tất đơn hàng theo đúng chuẩn pmdonhang.",
)
async def complete_haravan_order(
    order_id: str,
    payload: CompleteOrderRequest,
    current_user: User = Depends(get_current_user),
):
    updated = await haravan_order_service.complete_order(
        order_id_or_sn=order_id,
        actual_total=payload.actual_total,
        actual_shipping=payload.actual_shipping,
        actual_carrier=payload.actual_carrier,
        tracking_code=payload.tracking_code,
        note=payload.note,
        current_user=current_user,
    )
    return APIResponse.ok(
        data=updated,
        message="Đã cập nhật trạng thái đơn hàng sang 'Hoàn thành'.",
    )


@router.post(
    "/orders/{order_id}/cancel",
    response_model=APIResponse[Dict[str, Any]],
    summary="Hủy đơn hàng",
    description="Ghi nhận lý do hủy đơn và chuyển trạng thái 'Đã hủy'.",
)
async def cancel_haravan_order(
    order_id: str,
    payload: CancelOrderRequest,
    current_user: User = Depends(get_current_user),
):
    updated = await haravan_order_service.cancel_order(
        order_id_or_sn=order_id,
        reason=payload.reason,
        note=payload.note,
        current_user=current_user,
    )
    return APIResponse.ok(
        data=updated,
        message="Đã hủy đơn hàng thành công.",
    )


class AdminNoteRequest(BaseModel):
    note: str = Field(..., description="Ghi chú nội bộ admin")


@router.post(
    "/orders/{order_id}/admin-note",
    response_model=APIResponse[Dict[str, Any]],
    summary="Cập nhật ghi chú nội bộ admin cho đơn hàng",
    description="Lưu ghi chú nội bộ (admin_notes) và tự động ghi lịch sử thay đổi (Follow 100% pmdonhang).",
)
async def update_haravan_admin_note(
    order_id: str,
    payload: AdminNoteRequest,
    current_user: User = Depends(get_current_user),
):
    updated = await haravan_order_service.update_admin_note(
        order_id_or_sn=order_id,
        note=payload.note,
        current_user=current_user,
    )
    return APIResponse.ok(
        data=updated,
        message="Đã lưu ghi chú nội bộ thành công.",
    )
