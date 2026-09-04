from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.order import UnifiedOrderStatus
from app.schemas.sync import SyncResult


class BaseChannelAdapter(ABC):
    """
    Abstract Port / Interface that EVERY external channel module
    (ShopeeFood, GrabMart, Shopee) MUST implement.
    This guarantees zero coupling between channel modules.
    """

    @property
    @abstractmethod
    def channel_code(self) -> str:
        """Returns the unique uppercase channel code, e.g. 'SHOPEEFOOD' or 'GRABMART'."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Returns human-friendly channel name."""
        pass

    @abstractmethod
    async def sync_menu(
        self,
        store_id: str,
        partner_store_id: str,
        db: AsyncSession
    ) -> SyncResult:
        """
        Synchronize the full store catalog/menu to the channel platform.
        """
        pass

    @abstractmethod
    async def sync_inventory(
        self,
        store_id: str,
        partner_store_id: str,
        stock_items: List[Dict[str, Any]],
        db: AsyncSession
    ) -> SyncResult:
        """
        Push real-time stock levels or toggle item availability (in-stock / out-of-stock).
        """
        pass

    @abstractmethod
    async def handle_order_webhook(
        self,
        headers: Dict[str, str],
        raw_body: bytes,
        db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Verify inbound signature, parse platform order payload,
        and dispatch into Core OrderService as a UnifiedOrder.
        """
        pass

    @abstractmethod
    async def update_order_status(
        self,
        channel_order_id: str,
        partner_store_id: str,
        new_status: UnifiedOrderStatus,
        db: AsyncSession,
        reason: Optional[str] = None
    ) -> bool:
        """
        Notify channel platform of order status transitions
        (e.g., ACCEPT, READY, CANCEL).
        """
        pass
