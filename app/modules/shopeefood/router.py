from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.shopeefood.adapter import ShopeeFoodChannelAdapter

router = APIRouter(prefix="/shopeefood/webhooks", tags=["ShopeeFood Webhooks"])
adapter = ShopeeFoodChannelAdapter()


@router.post("/order")
async def receive_shopeefood_order(
    request: Request,
    authorization: str = Header(None, alias="Authorization"),
    x_foody_app_id: str = Header(None, alias="X-Foody-App-Id"),
    db: AsyncSession = Depends(get_db)
):
    """
    Webhook endpoint receiving real-time orders from ShopeeFood (Foody S2S).
    Parses and ingests orders into Core Order Service.
    """
    raw_body = await request.body()
    headers = dict(request.headers)
    
    try:
        response = await adapter.handle_order_webhook(headers, raw_body, db)
        return response
    except Exception as ex:
        raise HTTPException(status_code=400, detail=str(ex))
