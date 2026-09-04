from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.grabmart.adapter import GrabMartChannelAdapter

router = APIRouter(prefix="/grabmart/webhooks", tags=["GrabMart Webhooks"])
adapter = GrabMartChannelAdapter()


@router.post("/order")
async def receive_grabmart_order(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Webhook endpoint receiving real-time orders from GrabMart POS integration.
    """
    raw_body = await request.body()
    headers = dict(request.headers)
    
    try:
        response = await adapter.handle_order_webhook(headers, raw_body, db)
        return response
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex))
