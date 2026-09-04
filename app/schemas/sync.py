from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class SyncTriggerRequest(BaseModel):
    store_id: str = Field(..., description="Target Store ID")
    channel_code: Optional[str] = Field(None, description="Optional channel code filter, or all if null")


class SyncResult(BaseModel):
    channel_code: str
    store_id: str
    sync_type: str  # MENU, STOCK
    success: bool
    total_synced: int = 0
    failed_count: int = 0
    message: str = "Đồng bộ hoàn tất"
    details: Optional[dict] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
