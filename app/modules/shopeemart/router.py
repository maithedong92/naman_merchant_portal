import logging
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_active_user
from app.core.database import get_db, get_optional_db
from app.core.responses import APIResponse
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.product import Product
from app.models.store import Store, StoreChannelMapping
from app.models.user import User
from app.modules.shopeemart.adapter import ShopeeMartChannelAdapter
from app.modules.shopeemart.schemas import (
    ShopeeMartAddItemRequest,
    ShopeeMartAuthUrlResponse,
    ShopeeMartBatchOutletStockUpdate,
    ShopeeMartBuyerCancellationRequest,
    ShopeeMartCancelOrderRequest,
    ShopeeMartConfirmReturnRequest,
    ShopeeMartConnectionStatus,
    ShopeeMartCredentialsUpdateRequest,
    ShopeeMartCreateShippingDocRequest,
    ShopeeMartDisputeReturnRequest,
    ShopeeMartPublishOutletRequest,
    ShopeeMartPushSkuRequest,
    ShopeeMartShipOrderRequest,
    ShopeeMartTokenResponse,
    ShopeeMartUpdateItemRequest,
    ShopeeMartCommentListResponse,
    ShopeeMartReplyCommentRequest,
)
from app.modules.shopeemart.service import shopeemart_client
from app.schemas.order import OrderStatusUpdateSchema
from app.services.order_service import OrderService

logger = logging.getLogger("naman_portal.modules.shopeemart.router")

router = APIRouter(prefix="/shopeemart", tags=["Channel - ShopeeMart VN"])
adapter = ShopeeMartChannelAdapter()


# ==============================================================================
# 1. Inbound Webhooks from Shopee Open Platform Push Notification
# ==============================================================================

@router.post(
    "/webhooks/order",
    summary="ShopeeMart Inbound Order Push Webhook",
    description="Receives real-time push events (new order, status update, cancellation) from Shopee Open Platform v2.",
)
async def receive_shopeemart_order_webhook(
    request: Request,
    authorization: Optional[str] = Header(None),
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    raw_body = await request.body()
    headers = dict(request.headers)

    # Optional signature verification
    full_url = str(request.url)
    if authorization and not shopeemart_client.verify_webhook_signature(full_url, raw_body, authorization):
        logger.warning(f"ShopeeMart webhook signature invalid for url: {full_url}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ShopeeMart webhook signature verification failed"
        )

    try:
        if db is not None:
            response = await adapter.handle_order_webhook(headers, raw_body, db)
            return response
        else:
            logger.warning("ShopeeMart order received in offline DB mode.")
            return {"code": 0, "message": "simulated_accepted_offline"}
    except Exception as ex:
        logger.error(f"Error handling ShopeeMart webhook: {ex}", exc_info=True)
        return {"code": 400, "message": f"Webhook processing error: {str(ex)}"}


# ==============================================================================
# 2. OAuth 2.0 Authorization & Token Lifecycle Endpoints
# ==============================================================================

@router.get(
    "/auth/url",
    response_model=APIResponse[ShopeeMartAuthUrlResponse],
    summary="Lấy link ủy quyền gian hàng Shopee (Shopee Shop Authorization URL)",
    description="Sinh đường dẫn để Quản trị viên/Chủ shop Nam An đăng nhập vào Shopee Partner Platform và cấp quyền truy cập.",
)
async def get_shopee_auth_url(
    request: Request,
    redirect_url: Optional[str] = Query(
        default=None,
        description="URL chuyển hướng sau khi hoàn tất ủy quyền trên Shopee (Mặc định tự sinh từ máy chủ hiện tại)"
    ),
    current_user: User = Depends(get_current_active_user),
):
    base_host = str(request.base_url).rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_proto == "https" or "ump.namanmarket.com" in base_host:
        base_host = base_host.replace("http://", "https://")
    final_redirect = redirect_url or f"{base_host}/api/v1/shopeemart/auth/callback"
    auth_info = shopeemart_client.get_authorization_url(redirect_url=final_redirect)
    return APIResponse.ok(
        data=auth_info,
        message="Đã tạo liên kết ủy quyền gian hàng Shopee thành công."
    )


@router.get(
    "/auth/callback",
    response_model=APIResponse[ShopeeMartTokenResponse],
    summary="Tiếp nhận mã xác thực OAuth Callback từ Shopee",
    description="Endpoint chuyển tiếp sau khi shop owner bấm đồng ý cấp quyền trên Shopee để tự động đổi code lấy access_token.",
)
async def handle_shopee_auth_callback(
    code: str = Query(..., description="Mã ủy quyền một lần (Authorization Code)"),
    shop_id: str = Query(..., description="Mã gian hàng Shopee (Shop ID)"),
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    token_resp = await shopeemart_client.exchange_code_for_token(code=code, shop_id=shop_id)
    if shopeemart_client.is_configured and shop_id:
        shopeemart_client.save_credentials(
            partner_id=shopeemart_client.partner_id,
            partner_key=shopeemart_client.partner_key,
            shop_id=str(shop_id),
            base_url=shopeemart_client.base_url,
        )
    if db is not None:
        try:
            ch_res = await db.execute(select(Channel).where(Channel.code == "SHOPEEMART"))
            ch_obj = ch_res.scalar_one_or_none()
            if ch_obj:
                cfg = dict(ch_obj.config or {})
                tokens_map = dict(cfg.get("shop_tokens") or {})
                tokens_map[str(shop_id)] = shopeemart_client._token_cache.get(str(shop_id), {})
                cfg["shop_tokens"] = tokens_map
                cfg["default_shop_id"] = str(shop_id)
                ch_obj.config = cfg
                await db.commit()
        except Exception as db_ex:
            logger.warning(f"Could not save Shopee token into Channel.config: {db_ex}")
    return APIResponse.ok(
        data=token_resp,
        message=f"Đã liên kết và cấp quyền thành công cho gian hàng Shopee #{shop_id}."
    )


@router.post(
    "/credentials",
    response_model=APIResponse[ShopeeMartConnectionStatus],
    summary="Cập nhật Live Partner ID & Partner Key từ Shopee Open Platform",
    description="Lưu thông tin xác thực Live (Partner ID, Partner Key, Shop ID) ngay lập tức không cần khởi động lại container.",
)
async def update_shopee_live_credentials(
    payload: ShopeeMartCredentialsUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    shopeemart_client.save_credentials(
        partner_id=payload.partner_id,
        partner_key=payload.partner_key,
        shop_id=payload.shop_id,
        base_url=payload.base_url,
    )
    ch_res = await db.execute(select(Channel).where(Channel.code == "SHOPEEMART"))
    ch_obj = ch_res.scalar_one_or_none()
    if ch_obj:
        cfg = dict(ch_obj.config or {})
        cfg["partner_id"] = str(payload.partner_id).strip()
        if payload.shop_id:
            cfg["default_shop_id"] = str(payload.shop_id).strip()
        ch_obj.config = cfg
        await db.commit()
    status_info = shopeemart_client.get_connection_status(shop_id=payload.shop_id)
    return APIResponse.ok(
        data=status_info,
        message="Đã cập nhật thông tin xác thực Live Shopee Open Platform thành công."
    )


@router.post(
    "/auth/refresh",
    response_model=APIResponse[ShopeeMartTokenResponse],
    summary="Làm mới Token Shopee (Refresh Access Token)",
    description="Chủ động gia hạn Access Token của Shopee trước khi hết hạn sử dụng.",
)
async def refresh_shopee_token(
    shop_id: Optional[str] = Query(None, description="Shop ID cần làm mới token"),
    current_user: User = Depends(get_current_active_user),
):
    token_resp = await shopeemart_client.refresh_access_token(shop_id=shop_id)
    return APIResponse.ok(
        data=token_resp,
        message=f"Đã làm mới Access Token thành công cho Shopee shop #{token_resp.shop_id}."
    )


@router.get(
    "/connection-status",
    response_model=APIResponse[ShopeeMartConnectionStatus],
    summary="Trạng thái kết nối & Xác thực ShopeeMart",
    description="Kiểm tra thông số cấu hình Partner ID, Shop ID, trạng thái còn hạn của Token và chế độ Sandbox.",
)
async def get_shopee_connection_status(
    shop_id: Optional[str] = Query(None, description="Mã shop cần kiểm tra"),
):
    status_info = shopeemart_client.get_connection_status(shop_id=shop_id)
    return APIResponse.ok(
        data=status_info,
        message=f"Trạng thái kết nối ShopeeMart: {'Đã cấu hình' if status_info.is_configured else 'Chế độ mô phỏng (Sandbox)'}"
    )


# ==============================================================================
# 3. Inventory, Menu & Order Management Operations
# ==============================================================================

@router.post(
    "/sync/menu/{store_id}",
    summary="Đồng bộ danh mục sản phẩm (Menu/Catalog) lên gian hàng ShopeeMart",
    description="Đồng bộ danh sách sản phẩm từ Poppies ERP và ánh xạ item_id của gian hàng ShopeeMart tương ứng với chi nhánh.",
)
async def sync_store_menu_to_shopee(
    store_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    store_stmt = select(Store).where((Store.id == store_id) | (Store.code == store_id))
    store_obj = (await db.execute(store_stmt)).scalar_one_or_none()
    effective_store_id = store_obj.id if store_obj else store_id

    stmt = (
        select(StoreChannelMapping)
        .join(Channel)
        .where(
            Channel.code == "SHOPEEMART",
            StoreChannelMapping.store_id == effective_store_id,
        )
    )
    res = await db.execute(stmt)
    mapping = res.scalar_one_or_none()
    partner_store_id = (
        mapping.partner_store_id
        if (mapping and mapping.partner_store_id)
        else (store_obj.code if store_obj else store_id)
    )

    sync_result = await adapter.sync_menu(
        store_id=effective_store_id,
        partner_store_id=partner_store_id,
        db=db,
    )
    return APIResponse.ok(
        data=sync_result.model_dump(),
        message=sync_result.message,
    )


@router.post(
    "/sync/inventory/{store_id}",
    summary="Đồng bộ tồn kho cửa hàng lên gian hàng ShopeeMart",
    description="Quét toàn bộ tồn kho khả dụng tại siêu thị và đồng bộ theo lô SKU sang Outlet Shop tương ứng trên Shopee.",
)
async def sync_store_inventory_to_shopee(
    store_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    inv_stmt = (
        select(StoreInventory)
        .where(StoreInventory.store_id == store_id)
        .options(selectinload(StoreInventory.product))
    )
    inv_res = await db.execute(inv_stmt)
    inventories = inv_res.scalars().all()

    stock_items = []
    for inv in inventories:
        stock_items.append({
            "sku": inv.product.sku if inv.product else inv.product_id,
            "is_out_of_stock": inv.is_out_of_stock,
            "available_stock": inv.available_stock,
            "price": int(inv.product.base_price) if inv.product else None,
        })

    # Resolve Partner Store ID
    stmt = (
        select(StoreChannelMapping)
        .join(Channel)
        .where(
            Channel.code == "SHOPEEMART",
            StoreChannelMapping.store_id == store_id,
        )
    )
    res = await db.execute(stmt)
    mapping = res.scalar_one_or_none()
    if not mapping or not mapping.partner_store_id:
        raise HTTPException(
            status_code=400,
            detail=f"Cửa hàng (store_id: {store_id}) chưa được ánh xạ với mã Outlet Shop nào của ShopeeMart. Vui lòng thiết lập liên kết cửa hàng trong trang Quản Trị Đa Kênh."
        )
    partner_store_id = mapping.partner_store_id

    sync_result = await adapter.sync_inventory(
        store_id=store_id,
        partner_store_id=partner_store_id,
        stock_items=stock_items,
        db=db,
    )

    return APIResponse.ok(
        data=sync_result.model_dump(),
        message=sync_result.message,
    )


@router.post(
    "/orders/{order_id}/ready",
    response_model=APIResponse[Dict[str, Any]],
    summary="Báo đơn hàng sẵn sàng giao (Ready to Ship)",
    description="Nhân viên quầy báo đơn ShopeeMart đã đóng gói xong, gọi Shopee API chuyển giao cho đơn vị vận chuyển.",
)
async def mark_shopee_order_ready(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    order_stmt = select(UnifiedOrder).where(UnifiedOrder.id == order_id)
    order_res = await db.execute(order_stmt)
    order = order_res.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    updated_order = await OrderService.update_order_status(
        order_id=order.id,
        payload=OrderStatusUpdateSchema(
            new_status=UnifiedOrderStatus.READY,
            note="Đơn hàng ShopeeMart đã đóng gói sẵn sàng chuyển giao cho shipper",
            changed_by=current_user.username,
        ),
        db=db,
        sync_to_channel=True,
    )

    return APIResponse.ok(
        data={"order_id": order.id, "status": updated_order.status},
        message="Đã cập nhật trạng thái sẵn sàng giao cho đơn ShopeeMart.",
    )


@router.post(
    "/orders/{order_id}/cancel",
    response_model=APIResponse[Dict[str, Any]],
    summary="Hủy đơn hàng ShopeeMart với lý do cụ thể",
    description="Hủy đơn hàng ShopeeMart do hết tồn kho hoặc sự cố vận hành.",
)
async def cancel_shopee_order(
    order_id: str,
    payload: ShopeeMartCancelOrderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    order_stmt = select(UnifiedOrder).where(UnifiedOrder.id == order_id)
    order_res = await db.execute(order_stmt)
    order = order_res.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    updated_order = await OrderService.update_order_status(
        order_id=order.id,
        payload=OrderStatusUpdateSchema(
            new_status=UnifiedOrderStatus.CANCELLED,
            note=f"Hủy đơn ShopeeMart: {payload.cancel_reason}",
            cancellation_reason=payload.cancel_reason,
            changed_by=current_user.username,
        ),
        db=db,
        sync_to_channel=True,
    )

    return APIResponse.ok(
        data={"order_id": order.id, "status": updated_order.status},
        message=f"Đơn hàng ShopeeMart đã được hủy ({payload.cancel_reason}).",
    )


@router.post(
    "/orders/poll",
    response_model=APIResponse[Dict[str, Any]],
    summary="Quét & Đối soát đơn hàng ShopeeMart mới (Fallback Polling v2.order.get_order_list)",
    description="Chủ động truy vấn danh sách đơn hàng ShopeeMart trong 24 giờ gần nhất và tự động đồng bộ các đơn bị sót hoặc thay đổi trạng thái.",
)
async def poll_shopeemart_orders(
    shop_id: Optional[str] = Query(None, description="Shop ID cần quét (để trống để quét tất cả các gian hàng đang liên kết)"),
    hours_back: int = Query(24, ge=1, le=336, description="Khoảng thời gian quét ngược (giờ)"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    result = await adapter.poll_recent_orders(db=db, shop_id=shop_id, hours_back=hours_back)
    return APIResponse.ok(
        data=result,
        message=f"Đã quét {result.get('total_checked', 0)} đơn ShopeeMart (Tạo mới: {result.get('newly_created', 0)}, Cập nhật: {result.get('status_synced', 0)})."
    )


# ==============================================================================
# 4. Product Listing & Outlet Management Endpoints
# ==============================================================================

@router.post(
    "/product/push-sku/{sku}",
    response_model=APIResponse[Dict[str, Any]],
    summary="Đẩy trực tiếp 1 sản phẩm Poppies ERP từ kho NAM-UMP lên ShopeeMart",
    description=(
        "Tự động lấy thông tin món hàng từ PostgreSQL (đồng bộ từ Poppies ERP), upload ảnh lên Shopee Media Space, "
        "tự gợi ý danh mục lá Shopee, lấy kênh vận chuyển tích hợp và gọi v2.product.add_item."
    ),
)
async def push_erp_sku_to_shopee(
    sku: str,
    payload: Optional[ShopeeMartPushSkuRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    req = payload or ShopeeMartPushSkuRequest()
    prod_res = await db.execute(select(Product).where(Product.sku == sku))
    product = prod_res.scalar_one_or_none()
    if not product:
        raise HTTPException(
            status_code=404,
            detail=f"Không tìm thấy sản phẩm có SKU '{sku}' trong cơ sở dữ liệu NAM-UMP."
        )

    store_res = await db.execute(
        select(Store).where((Store.code == req.store_code) | (Store.id == req.store_code))
    )
    store_obj = store_res.scalar_one_or_none()

    stock_qty = 0
    if store_obj:
        inv_res = await db.execute(
            select(StoreInventory).where(
                StoreInventory.store_id == store_obj.id,
                StoreInventory.product_id == product.id,
            )
        )
        inv_obj = inv_res.scalar_one_or_none()
        if inv_obj and not inv_obj.is_out_of_stock:
            stock_qty = max(0, int(inv_obj.available_stock or 0))

    meta = dict(product.metadata_json or {})
    unit_name = meta.get("unit") or product.unit if hasattr(product, "unit") else meta.get("unit")
    barcode_val = meta.get("barcode") or getattr(product, "barcode", None)
    net_weight_raw = meta.get("net_weight") or meta.get("weight") or getattr(product, "weight", None)
    net_weight_val: Optional[float] = None
    if net_weight_raw is not None:
        try:
            net_weight_val = float(net_weight_raw)
        except (ValueError, TypeError):
            net_weight_val = None

    push_result = await shopeemart_client.push_erp_product_to_shopee(
        sku=product.sku,
        name=product.name,
        price=float(product.base_price or 0),
        stock=stock_qty,
        unit=unit_name,
        barcode=barcode_val,
        description=product.description,
        image_url=product.image_url,
        shop_id=req.shop_id,
        category_id=req.category_id,
        net_weight_kg=net_weight_val,
    )

    shopee_resp = push_result.get("shopee_response") or {}
    created_item_id = (shopee_resp.get("response") or {}).get("item_id")
    if created_item_id:
        meta["shopee_item_id"] = int(created_item_id)
        product.metadata_json = meta
        await db.commit()

        if req.outlet_shop_id_list:
            outlet_resp = await shopeemart_client.publish_item_to_outlet_shop(
                shop_id=str(push_result.get("shop_id") or ""),
                item_id=int(created_item_id),
                outlet_shop_id_list=req.outlet_shop_id_list,
            )
            push_result["outlet_publish_response"] = outlet_resp

    mode_str = push_result.get("mode")
    msg = (
        f"Đã đăng thành công '{product.name}' (SKU {product.sku}) lên ShopeeMart Live (item_id={created_item_id})."
        if mode_str == "LIVE"
        else f"Đã kiểm thử đẩy '{product.name}' (SKU {product.sku}) ở chế độ Sandbox (chưa nhập Live Partner ID/Key trên máy chủ)."
    )
    return APIResponse.ok(data=push_result, message=msg)


@router.post(
    "/product/add-item",
    response_model=APIResponse[Dict[str, Any]],
    summary="Đăng món mới lên Mart Shop (v2.product.add_item)",
    description="Tạo SKU gốc trên gian hàng Mart chính trước khi đẩy sang các Outlet.",
)
async def add_shopee_product(
    payload: ShopeeMartAddItemRequest,
    shop_id: Optional[str] = Query(None, description="Mart Shop ID"),
    current_user: User = Depends(get_current_active_user),
):
    target_shop = shop_id or shopeemart_client.default_shop_id
    if not target_shop:
        raise HTTPException(
            status_code=400,
            detail="Vui lòng cung cấp tham số shop_id hoặc cấu hình biến môi trường SHOPEEMART_SHOP_ID."
        )
    result = await shopeemart_client.add_item(
        shop_id=target_shop,
        item_data=payload.model_dump(exclude_none=True)
    )
    return APIResponse.ok(
        data=result,
        message=f"Đã gửi yêu cầu đăng sản phẩm '{payload.item_name}' lên ShopeeMart."
    )


@router.post(
    "/product/publish-outlet",
    response_model=APIResponse[Dict[str, Any]],
    summary="Đẩy SKU sang các chi nhánh Outlet (v2.product.publish_item_to_outlet_shop)",
    description="Liên kết và công bố sản phẩm từ Mart Shop sang các chi nhánh Outlet.",
)
async def publish_item_to_outlets(
    payload: ShopeeMartPublishOutletRequest,
    shop_id: Optional[str] = Query(None, description="Mart Shop ID"),
    current_user: User = Depends(get_current_active_user),
):
    target_shop = shop_id or shopeemart_client.default_shop_id
    if not target_shop:
        raise HTTPException(
            status_code=400,
            detail="Vui lòng cung cấp tham số shop_id hoặc cấu hình biến môi trường SHOPEEMART_SHOP_ID."
        )
    result = await shopeemart_client.publish_item_to_outlet_shop(
        shop_id=target_shop,
        item_id=payload.item_id,
        outlet_shop_id_list=payload.outlet_shop_id_list,
        price_list=payload.price_list,
        stock_list=payload.stock_list
    )
    return APIResponse.ok(
        data=result,
        message=f"Đã gửi yêu cầu publish sản phẩm #{payload.item_id} sang {len(payload.outlet_shop_id_list)} chi nhánh."
    )


@router.post(
    "/product/batch-outlet-stock",
    response_model=APIResponse[Dict[str, Any]],
    summary="Cập nhật tồn kho hàng loạt theo chi nhánh (v2.product.batch_update_outlet_stock)",
)
async def batch_update_outlet_stock_endpoint(
    payload: ShopeeMartBatchOutletStockUpdate,
    shop_id: Optional[str] = Query(None, description="Shop ID"),
    current_user: User = Depends(get_current_active_user),
):
    target_shop = shop_id or shopeemart_client.default_shop_id
    if not target_shop:
        raise HTTPException(
            status_code=400,
            detail="Vui lòng cung cấp tham số shop_id hoặc cấu hình biến môi trường SHOPEEMART_SHOP_ID."
        )
    result = await shopeemart_client.batch_update_outlet_stock(
        shop_id=target_shop,
        outlet_stock_list=[item.model_dump() for item in payload.outlet_stock_list]
    )
    return APIResponse.ok(
        data=result,
        message="Đã cập nhật tồn kho đa chi nhánh ShopeeMart thành công."
    )


# ==============================================================================
# 5. Logistics, Tracking & Airway Bill (AWB) Printing Endpoints
# ==============================================================================

@router.get(
    "/orders/{order_sn}/tracking",
    response_model=APIResponse[Dict[str, Any]],
    summary="Lấy mã vận đơn Tracking Number (v2.logistics.get_tracking_number)",
)
async def get_shopee_tracking(
    order_sn: str,
    shop_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
):
    result = await shopeemart_client.get_tracking_number(order_sn=order_sn, shop_id=shop_id)
    return APIResponse.ok(
        data=result,
        message=f"Đã truy vấn mã vận đơn đơn hàng #{order_sn}."
    )


@router.post(
    "/orders/{order_sn}/create-awb",
    response_model=APIResponse[Dict[str, Any]],
    summary="Tạo phiếu gửi hàng Airway Bill AWB (v2.logistics.create_shipping_document)",
)
async def create_shopee_awb(
    order_sn: str,
    payload: Optional[ShopeeMartCreateShippingDocRequest] = None,
    shop_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
):
    doc_type = payload.document_type if payload else "THERMAL_AIR_WAYBILL"
    result = await shopeemart_client.create_shipping_document(
        order_sn=order_sn,
        document_type=doc_type,
        shop_id=shop_id
    )
    return APIResponse.ok(
        data=result,
        message=f"Đã tạo lệnh in phiếu vận đơn AWB cho đơn #{order_sn}."
    )


@router.get(
    "/orders/{order_sn}/download-awb",
    summary="Tải file in phiếu vận đơn AWB dạng PDF binary (v2.logistics.download_shipping_document)",
)
async def download_shopee_awb(
    order_sn: str,
    shop_id: Optional[str] = Query(None),
    document_type: str = Query("THERMAL_AIR_WAYBILL", description="THERMAL_AIR_WAYBILL hoặc NORMAL_AIR_WAYBILL"),
):
    pdf_bytes = await shopeemart_client.download_shipping_document_bytes(
        order_sn=order_sn,
        document_type=document_type,
        shop_id=shop_id,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=Shopee_AWB_{order_sn}.pdf"},
    )


# ==============================================================================
# 6. Buyer Cancellation & Return/Refund Management Endpoints
# ==============================================================================

@router.post(
    "/orders/{order_sn}/buyer-cancellation",
    response_model=APIResponse[Dict[str, Any]],
    summary="Xử lý yêu cầu hủy đơn từ khách (v2.order.handle_buyer_cancellation)",
)
async def handle_buyer_cancel(
    order_sn: str,
    payload: ShopeeMartBuyerCancellationRequest,
    shop_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
):
    result = await shopeemart_client.handle_buyer_cancellation(
        order_sn=order_sn,
        operation=payload.operation,
        shop_id=shop_id
    )
    return APIResponse.ok(
        data=result,
        message=f"Đã gửi phản hồi {payload.operation} cho yêu cầu hủy đơn #{order_sn}."
    )


@router.get(
    "/returns",
    response_model=APIResponse[Dict[str, Any]],
    summary="Danh sách yêu cầu Đổi trả / Hoàn tiền (v2.returns.get_return_list)",
)
async def get_shopee_returns(
    page_no: int = Query(0, ge=0),
    page_size: int = Query(20, ge=1, le=50),
    shop_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
):
    result = await shopeemart_client.get_return_list(page_no=page_no, page_size=page_size, shop_id=shop_id)
    return APIResponse.ok(
        data=result,
        message="Lấy danh sách yêu cầu đổi trả thành công."
    )


@router.get(
    "/returns/{return_sn}",
    response_model=APIResponse[Dict[str, Any]],
    summary="Chi tiết một yêu cầu Đổi trả / Hoàn tiền (v2.returns.get_return_detail)",
)
async def get_shopee_return_detail(
    return_sn: str,
    shop_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
):
    result = await shopeemart_client.get_return_detail(return_sn=return_sn, shop_id=shop_id)
    return APIResponse.ok(
        data=result,
        message=f"Lấy chi tiết yêu cầu đổi trả #{return_sn} thành công."
    )


@router.post(
    "/returns/{return_sn}/confirm",
    response_model=APIResponse[Dict[str, Any]],
    summary="Chấp thuận hoàn tiền cho người mua (v2.returns.confirm)",
)
async def confirm_shopee_return(
    return_sn: str,
    shop_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
):
    result = await shopeemart_client.confirm_return(return_sn=return_sn, shop_id=shop_id)
    return APIResponse.ok(
        data=result,
        message=f"Đã chấp thuận yêu cầu hoàn tiền #{return_sn}."
    )


@router.post(
    "/returns/{return_sn}/dispute",
    response_model=APIResponse[Dict[str, Any]],
    summary="Khiếu nại yêu cầu hoàn tiền lên Shopee (v2.returns.dispute)",
)
async def dispute_shopee_return(
    return_sn: str,
    payload: ShopeeMartDisputeReturnRequest,
    shop_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
):
    result = await shopeemart_client.dispute_return(
        return_sn=return_sn,
        dispute_reason=payload.dispute_reason,
        dispute_text_reason=payload.dispute_text_reason or "",
        email=payload.email or "",
        shop_id=shop_id
    )
    return APIResponse.ok(
        data=result,
        message=f"Đã gửi khiếu nại tranh chấp cho yêu cầu #{return_sn} lên Shopee."
    )


# ==============================================================================
# 7. Financials & Escrow Settlement Endpoints
# ==============================================================================

@router.get(
    "/orders/{order_sn}/escrow",
    response_model=APIResponse[Dict[str, Any]],
    summary="Chi tiết quyết toán đơn hàng Escrow (v2.payment.get_escrow_detail)",
)
async def get_shopee_escrow(
    order_sn: str,
    shop_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
):
    result = await shopeemart_client.get_escrow_detail(order_sn=order_sn, shop_id=shop_id)
    return APIResponse.ok(
        data=result,
        message=f"Lấy bảng quyết toán tài chính Shopee cho đơn #{order_sn} thành công."
    )


# ==============================================================================
# 8. Customer Reviews & Ratings (v2.product.get_comment & reply_comment)
# ==============================================================================

@router.get(
    "/reviews",
    response_model=APIResponse[ShopeeMartCommentListResponse],
    summary="Kéo danh sách đánh giá nhận xét của khách hàng (v2.product.get_comment)",
    description=(
        "Truy vấn trực tiếp danh sách review/rating của khách hàng từ Shopee Live API. "
        "Bao gồm số sao (1-5 sao), nội dung nhận xét, hình ảnh/video review của khách, phân loại sản phẩm và phản hồi của shop."
    ),
)
async def get_shopee_customer_reviews(
    shop_id: Optional[str] = Query(None, description="Shopee Shop ID (mặc định lấy shop chính đang liên kết)"),
    item_id: Optional[int] = Query(None, description="Lọc theo ID sản phẩm Shopee cụ thể"),
    comment_id: Optional[int] = Query(None, description="Lọc theo ID đánh giá cụ thể"),
    cursor: str = Query("", description="Con trỏ phân trang (cursor để lấy trang tiếp theo)"),
    page_size: int = Query(20, ge=1, le=100, description="Số lượng đánh giá mỗi trang (tối đa 100)"),
    current_user: User = Depends(get_current_active_user),
):
    raw_res = await shopeemart_client.get_comments(
        shop_id=shop_id,
        item_id=item_id,
        comment_id=comment_id,
        cursor=cursor,
        page_size=page_size,
    )
    resp_data = raw_res.get("response") or {}
    parsed_result = ShopeeMartCommentListResponse(
        item_comment_list=resp_data.get("item_comment_list") or [],
        more=bool(resp_data.get("more", False)),
        next_cursor=str(resp_data.get("next_cursor") or ""),
    )
    total_found = len(parsed_result.item_comment_list)
    return APIResponse.ok(
        data=parsed_result,
        message=f"Đã tải thành công {total_found} đánh giá từ Shopee."
    )


@router.post(
    "/reviews/{comment_id}/reply",
    response_model=APIResponse[Dict[str, Any]],
    summary="Phản hồi đánh giá của khách hàng (v2.product.reply_comment)",
    description="Gửi phản hồi của shop cho một đánh giá/nhận xét cụ thể của khách hàng trên Shopee.",
)
async def reply_shopee_customer_review(
    comment_id: int,
    payload: ShopeeMartReplyCommentRequest,
    shop_id: Optional[str] = Query(None, description="Shopee Shop ID"),
    current_user: User = Depends(get_current_active_user),
):
    result = await shopeemart_client.reply_comment(
        comment_id=comment_id,
        comment=payload.comment,
        shop_id=shop_id,
    )
    return APIResponse.ok(
        data=result,
        message=f"Đã gửi phản hồi cho đánh giá #{comment_id} thành công lên Shopee."
    )


