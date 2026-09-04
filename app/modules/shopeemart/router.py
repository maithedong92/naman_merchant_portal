from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.shopeemart.adapter import ShopeeMartChannelAdapter

router = APIRouter(prefix="/shopeemart/webhooks", tags=["ShopeeMart Webhooks"])
adapter = ShopeeMartChannelAdapter()


@router.post("/order")
async def receive_shopeemart_order(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Webhook receiver for real-time order notifications from ShopeeMart / Shopee Open Platform.
    """
    raw_body = await request.body()
    headers = dict(request.headers)

    try:
        response = await adapter.handle_order_webhook(headers, raw_body, db)
        return response
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex))
