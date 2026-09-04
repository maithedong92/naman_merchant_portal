from typing import Any, Optional


class AppException(Exception):
    """Base application domain exception."""
    def __init__(
        self,
        message: str,
        error_code: str = "INTERNAL_SERVER_ERROR",
        status_code: int = 500,
        details: Optional[Any] = None
    ):
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.details = details
        super().__init__(message)


class NotFoundError(AppException):
    def __init__(self, resource: str, identifier: Any):
        super().__init__(
            message=f"{resource} với mã nhận diện '{identifier}' không tồn tại.",
            error_code="RESOURCE_NOT_FOUND",
            status_code=404,
            details={"resource": resource, "id": str(identifier)}
        )


class ConflictError(AppException):
    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(
            message=message,
            error_code="RESOURCE_CONFLICT",
            status_code=409,
            details=details
        )


class ValidationError(AppException):
    def __init__(self, message: str, details: Optional[Any] = None):
        super().__init__(
            message=message,
            error_code="VALIDATION_ERROR",
            status_code=422,
            details=details
        )


class ExternalChannelError(AppException):
    def __init__(self, channel: str, message: str, raw_response: Optional[Any] = None):
        super().__init__(
            message=f"Lỗi tương tác với kênh {channel}: {message}",
            error_code=f"{channel.upper()}_INTEGRATION_ERROR",
            status_code=502,
            details={"channel": channel, "raw_response": raw_response}
        )


class UnauthorizedError(AppException):
    def __init__(self, message: str = "Xác thực không hợp lệ hoặc phiên đăng nhập đã hết hạn.", details: Optional[Any] = None):
        super().__init__(
            message=message,
            error_code="UNAUTHORIZED",
            status_code=401,
            details=details
        )


class ForbiddenError(AppException):
    def __init__(self, message: str = "Bạn không có quyền thực hiện hành động này.", details: Optional[Any] = None):
        super().__init__(
            message=message,
            error_code="FORBIDDEN",
            status_code=403,
            details=details
        )
