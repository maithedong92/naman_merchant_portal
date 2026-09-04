from datetime import datetime, timezone
from typing import Any, Generic, Optional, TypeVar
from pydantic import BaseModel, Field

DataT = TypeVar("DataT")


class APIResponse(BaseModel, Generic[DataT]):
    """Standard unified response wrapper for all portal endpoints."""
    success: bool = Field(default=True, description="True if operation succeeded")
    message: str = Field(default="Thao tác thành công", description="User-friendly status message")
    data: Optional[DataT] = Field(default=None, description="Response payload")
    error_code: Optional[str] = Field(default=None, description="Machine-readable error code on failure")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="UTC ISO-8601 timestamp"
    )

    @classmethod
    def ok(cls, data: Optional[DataT] = None, message: str = "Thao tác thành công") -> "APIResponse[DataT]":
        return cls(success=True, message=message, data=data, error_code=None)

    @classmethod
    def fail(cls, message: str, error_code: str = "ERROR", data: Optional[Any] = None) -> "APIResponse[None]":
        return cls(success=False, message=message, data=data, error_code=error_code)
