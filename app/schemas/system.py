from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from app.models.operational_error import ErrorSeverity, ErrorStatus


class OperationalErrorResponse(BaseModel):
    """Schema for individual operational error log entries."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    error_code: str
    severity: ErrorSeverity
    module: str
    message: str
    stack_trace: Optional[str] = None
    endpoint: Optional[str] = None
    http_method: Optional[str] = None
    http_status_code: Optional[int] = None
    client_ip: Optional[str] = None
    user_id: Optional[str] = None
    request_payload: Optional[Dict[str, Any]] = None
    resolution_status: ErrorStatus
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None
    occurrence_count: int = 1
    last_occurred_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class OperationalErrorResolveRequest(BaseModel):
    """Schema for marking an error incident as resolved."""
    resolution_notes: str = Field(..., min_length=3, description="Ghi chú nguyên nhân và giải pháp đã thực hiện")
    resolved_by: Optional[str] = Field(None, description="Tên hoặc mã người xử lý sự cố")


class SystemErrorSummary(BaseModel):
    """Summary metrics of operational errors."""
    total_errors: int = 0
    open_errors: int = 0
    investigating_errors: int = 0
    resolved_errors: int = 0
    critical_errors: int = 0
    errors_today: int = 0


class ComponentHealth(BaseModel):
    """Health indicator for an individual sub-system or integration."""
    name: str
    status: str  # "UP", "DEGRADED", "DOWN", "CONFIG_REQUIRED"
    latency_ms: Optional[float] = None
    details: Optional[str] = None


class SystemHealthResponse(BaseModel):
    """Overall system health and operations overview."""
    status: str  # "HEALTHY", "WARNING", "CRITICAL"
    uptime_seconds: float
    app_version: str
    environment: str
    components: List[ComponentHealth]
    error_summary: SystemErrorSummary
    timestamp: datetime
