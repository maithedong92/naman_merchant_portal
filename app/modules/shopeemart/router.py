import logging
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_active_user
from app.core.database import get_db, get_optional_db
from app.core.responses import APIResponse
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.order import UnifiedOrder, UnifiedOrderStatus
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
    ShopeeMartCreateShippingDocRequest,
    ShopeeMartDisputeReturnRequest,
    ShopeeMartPublishOutletRequest,
    ShopeeMartShipOrderRequest,
    ShopeeMartTokenResponse,
    ShopeeMartUpdateItemRequest,
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
    redirect_url: str = Query(
        default="https://ump.namanmarket.com/shopeemart/auth/callback",
        description="URL chuyển hướng sau khi hoàn tất ủy quyền trên Shopee"
    ),
    current_user: User = Depends(get_current_active_user),
):
    auth_info = shopeemart_client.get_authorization_url(redirect_url=redirect_url)
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
    return APIResponse.ok(
        data=token_resp,
        message=f"Đã liên kết và cấp quyền thành công cho gian hàng Shopee #{shop_id}."
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
# 3. Inventory & Order Management Operations
# ==============================================================================

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
    partner_store_id = mapping.partner_store_id if mapping else "10001"

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


# ==============================================================================
# 4. Product Listing & Outlet Management Endpoints
# ==============================================================================

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
    result = await shopeemart_client.add_item(
        shop_id=shop_id or "10001",
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
    description="Liên kết và công bố sản phẩm từ Mart Shop sang 4 siêu thị chi nhánh.",
)
async def publish_item_to_outlets(
    payload: ShopeeMartPublishOutletRequest,
    shop_id: Optional[str] = Query(None, description="Mart Shop ID"),
    current_user: User = Depends(get_current_active_user),
):
    result = await shopeemart_client.publish_item_to_outlet_shop(
        shop_id=shop_id or "10001",
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
    result = await shopeemart_client.batch_update_outlet_stock(
        shop_id=shop_id or "10001",
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
    response_model=APIResponse[Dict[str, Any]],
    summary="Tải file in phiếu vận đơn AWB (v2.logistics.download_shipping_document)",
)
async def download_shopee_awb(
    order_sn: str,
    shop_id: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
):
    result = await shopeemart_client.download_shipping_document(order_sn=order_sn, shop_id=shop_id)
    return APIResponse.ok(
        data=result,
        message=f"Tải phiếu AWB cho đơn #{order_sn} thành công."
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

