from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import check_store_access, get_current_user, require_store_manager
from app.core.database import get_db
from app.core.exceptions import NotFoundError
from app.core.responses import APIResponse
from app.models.inventory import StoreInventory
from app.models.store import Store
from app.models.user import User
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
    items, total, summary = await InventoryService.get_store_inventory_overview(
        store_code=store_code,
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
