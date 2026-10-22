from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, get_current_user, require_store_manager
from app.core.database import get_db
from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.responses import APIResponse
from app.models.inventory import StoreInventory
from app.models.store import Store
from app.models.user import User, UserRole
from app.schemas.common import PaginatedResponse, PaginationMeta
from app.schemas.inventory import (
    InventoryBatchUpdateRequest,
    InventoryItemOverview,
    InventoryOverviewSummary,
    InventoryQuickStockUpdateRequest,
    InventoryToggleStatusRequest,
    StoreInventoryResponse,
)
from app.services.inventory_service import InventoryService
from app.services.poppies_service import poppies_service

router = APIRouter(prefix="/inventory", tags=["Multi-Store Inventory"])


@router.get(
    "/overview",
    summary="Tổng quan tồn kho & trạng thái thực đơn theo chi nhánh",
    description="Truy vấn toàn bộ mặt hàng kèm số lượng tồn kho khả dụng, trạng thái Còn hàng / Tạm hết hàng và phân loại danh mục.",
)
async def get_inventory_overview(
    store_code: str = Query("10001", description="Mã chi nhánh Nam An (10001, 10004, 10005, 10006)"),
    category_id: Optional[str] = Query(None, description="Lọc theo ID danh mục"),
    status: Optional[str] = Query(None, description="Lọc trạng thái: ALL, IN_STOCK, OUT_OF_STOCK, LOW_STOCK"),
    search: Optional[str] = Query(None, description="Tìm theo tên sản phẩm, mã SKU, mã vạch"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    effective_store_code = store_code
    if not (current_user.is_superuser or current_user.role == UserRole.SUPER_ADMIN.value):
        if not current_user.store_code:
            return APIResponse.ok(
                data={
                    "paginated": PaginatedResponse(items=[], meta=PaginationMeta.create(page=page, page_size=page_size, total_items=0)),
                    "summary": {
                        "store_code": "",
                        "store_name": "",
                        "total_skus": 0,
                        "in_stock_skus": 0,
                        "out_of_stock_skus": 0,
                        "low_stock_skus": 0,
                        "categories_count": 0,
                    }
                },
                message="Người dùng chưa được phân công chi nhánh."
            )
        effective_store_code = current_user.store_code

    items, total, summary = await InventoryService.get_store_inventory_overview(
        store_code=effective_store_code,
        category_id=category_id,
        status=status,
        search=search,
        page=page,
        page_size=page_size,
        db=db,
    )
    paginated = PaginatedResponse(
        items=items,
        meta=PaginationMeta.create(page=page, page_size=page_size, total_items=total),
    )
    return APIResponse.ok(
        data={"paginated": paginated, "summary": summary},
        message="Lấy tổng quan tồn kho chi nhánh thành công."
    )


@router.patch(
    "/toggle",
    summary="Bật / Tắt món tức thì (In-Stock / Out-of-Stock) 1-chạm",
    description="Cập nhật trạng thái tạm hết hàng hoặc còn hàng cho 1 SKU và tự động đẩy tín hiệu tồn kho sang GrabMart và ShopeeFood.",
)
async def toggle_item_status(
    payload: InventoryToggleStatusRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not (current_user.is_superuser or current_user.role == UserRole.SUPER_ADMIN.value):
        if not current_user.store_code or current_user.store_code != payload.store_code:
            raise ForbiddenError(f"Bạn không có quyền thao tác trên chi nhánh '{payload.store_code}'.")

    inv, sync_results = await InventoryService.toggle_item_status(payload, db)
    status_text = "HẾT HÀNG (Tạm ngưng nhận món)" if payload.is_out_of_stock else "CÒN HÀNG (Đang mở bán)"
    return APIResponse.ok(
        data={"inventory": StoreInventoryResponse.model_validate(inv), "sync_results": sync_results},
        message=f"Đã chuyển SKU {payload.sku} sang trạng thái '{status_text}' và đồng bộ lên các sàn."
    )


@router.patch(
    "/quick-update",
    summary="Cập nhật nhanh số lượng tồn kho khả dụng",
    description="Điều chỉnh số lượng tồn kho khả dụng của một SKU tại một chi nhánh cụ thể.",
)
async def quick_update_stock(
    payload: InventoryQuickStockUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not (current_user.is_superuser or current_user.role == UserRole.SUPER_ADMIN.value):
        if not current_user.store_code or current_user.store_code != payload.store_code:
            raise ForbiddenError(f"Bạn không có quyền thao tác trên chi nhánh '{payload.store_code}'.")

    inv, sync_results = await InventoryService.quick_update_stock(
        store_code=payload.store_code,
        sku=payload.sku,
        new_stock=payload.new_stock,
        sync_to_channels=payload.sync_to_channels,
        db=db,
    )
    return APIResponse.ok(
        data={"inventory": StoreInventoryResponse.model_validate(inv), "sync_results": sync_results},
        message=f"Đã cập nhật tồn kho SKU {payload.sku} thành {payload.new_stock}."
    )


@router.post(
    "/sync-all",
    summary="Đồng bộ toàn bộ tồn kho chi nhánh sang ShopeeFood & GrabMart",
    description="Kích hoạt lệnh đẩy toàn bộ tồn kho của tất cả SKU thuộc chi nhánh sang các sàn đối tác đang kết nối.",
)
async def sync_all_inventory(
    store_code: str = Query(..., description="Mã chi nhánh cần đồng bộ"),
    current_user: User = Depends(require_store_manager),
    db: AsyncSession = Depends(get_db),
):
    if not (current_user.is_superuser or current_user.role == UserRole.SUPER_ADMIN.value):
        if not current_user.store_code or current_user.store_code != store_code:
            raise ForbiddenError(f"Bạn không có quyền thao tác trên chi nhánh '{store_code}'.")

    sync_results = await InventoryService.sync_all_store_inventory(store_code, db)
    return APIResponse.ok(
        data={"sync_results": sync_results},
        message=f"Đã kích hoạt đồng bộ toàn bộ tồn kho chi nhánh {store_code} sang các sàn."
    )


@router.post(
    "/seed",
    summary="Khởi tạo danh mục & sản phẩm mẫu Nam An Market",
    description="Khởi tạo 5 danh mục hàng hóa, 16 sản phẩm tươi sống cao cấp và tồn kho cho 4 chi nhánh.",
)
async def seed_inventory_catalog(
    current_user: User = Depends(require_store_manager),
    db: AsyncSession = Depends(get_db),
):
    result = await InventoryService.seed_sample_catalog(db)
    return APIResponse.ok(
        data=result,
        message="Đã khởi tạo danh mục sản phẩm và tồn kho mẫu cho Nam An Market thành công."
    )


@router.get("/stores/{store_id}", response_model=APIResponse[List[StoreInventoryResponse]])
async def get_store_inventory(
    store_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Retrieve all SKU inventory records for a given store."""
    store_stmt = select(Store).where((Store.id == store_id) | (Store.code == store_id))
    store = (await db.execute(store_stmt)).scalar_one_or_none()
    if not store:
        raise NotFoundError("Store", store_id)

    check_store_access(current_user, store.id)

    stmt = select(StoreInventory).where(StoreInventory.store_id == store.id).order_by(StoreInventory.sku)
    res = await db.execute(stmt)
    inventories = res.scalars().all()
    return APIResponse.ok(data=inventories)


@router.post("/batch-update", response_model=APIResponse[List[StoreInventoryResponse]])
async def batch_update_inventory(
    payload: InventoryBatchUpdateRequest,
    current_user: User = Depends(require_store_manager),
    db: AsyncSession = Depends(get_db)
):
    """Batch update physical stock counts per store and optionally trigger real-time stock sync."""
    check_store_access(current_user, payload.store_id)
    updated = await InventoryService.update_store_inventory(payload, db)
    return APIResponse.ok(
        data=updated,
        message=f"Đã cập nhật tồn kho cho {len(updated)} SKU và kích hoạt đồng bộ đa kênh."
    )


@router.get(
    "/poppies/health",
    summary="Kiểm tra kết nối Poppies ERP POS Master Data Gateway",
    description="Kiểm tra trạng thái sẵn sàng của Poppies ERP Master Data Gateway và tổng số SKU/CTTK tồn kho khả dụng.",
)
async def get_poppies_health(
    current_user: User = Depends(get_current_user),
):
    health_data = await poppies_service.check_health()
    return APIResponse.ok(
        data=health_data,
        message="Kiểm tra trạng thái Poppies ERP Gateway thành công."
    )


@router.post(
    "/poppies/sync",
    summary="Đồng bộ Master Data (Hàng hóa, Danh mục, Tồn kho) từ Poppies ERP",
    description="Kéo danh mục, sản phẩm, giá bán VAT và tồn kho đa chi nhánh (10001, 10004, 10005, 10006) từ Poppies ERP POS Gateway vào cơ sở dữ liệu NAM-UMP.",
)
async def sync_poppies_master_data(
    branch_code: str = Query("10001", description="Mã chi nhánh đối chiếu giá bán"),
    status_filter: str = Query("01", description="Trạng thái mặt hàng: 01 (Đang kinh doanh), 02 (Ngừng kinh doanh)"),
    limit: int = Query(250, ge=1, le=1000, description="Số lượng SKU mỗi trang"),
    offset: int = Query(0, ge=0, description="Vị trí bắt đầu"),
    fetch_all: bool = Query(True, description="Đồng bộ toàn bộ danh mục kinh doanh (tất cả các trang)"),
    current_user: User = Depends(require_store_manager),
    db: AsyncSession = Depends(get_db),
):
    result = await poppies_service.sync_master_data_from_poppies(
        db=db,
        branch_code=branch_code,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
        fetch_all=fetch_all,
    )
    return APIResponse.ok(
        data=result,
        message=f"Đã hoàn thành đồng bộ {result.get('total_fetched', 0)} SKU từ Poppies ERP."
    )


@router.post(
    "/poppies/sync-stock",
    summary="Đồng bộ nhanh tồn kho tức thời CTTK từ Poppies ERP",
    description="Truy vấn dữ liệu tồn kho tức thời từ Poppies CTTK (/cttk-records) và cập nhật số lượng tồn cho tất cả các chi nhánh.",
)
async def sync_poppies_stock_realtime(
    store_code: Optional[str] = Query(None, description="Mã chi nhánh cần đồng bộ tồn kho (để trống nếu đồng bộ toàn bộ)"),
    current_user: User = Depends(require_store_manager),
    db: AsyncSession = Depends(get_db),
):
    result = await poppies_service.sync_stock_cttk_realtime(
        db=db,
        store_code=store_code
    )
    return APIResponse.ok(
        data=result,
        message=f"Đã cập nhật tồn kho tức thời cho {result.get('inventories_updated', 0)} bản ghi tồn kho."
    )

