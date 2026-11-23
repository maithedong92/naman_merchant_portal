import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager, suppress
from typing import Optional
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.datastructures import MutableHeaders

from app.api.v1.router import api_v1_router
from app.core.config import get_settings
from app.core.database import Base, engine, AsyncSessionLocal
from app.core.exceptions import AppException
from app.core.responses import APIResponse
from app.models.operational_error import ErrorSeverity
from app.modules.grabmart import GrabMartChannelAdapter, grabmart_webhook_router
from app.modules.shopeefood import ShopeeFoodChannelAdapter, shopeefood_webhook_router
from app.modules.shopeemart import ShopeeMartChannelAdapter, shopeemart_webhook_router
from app.services.channel_registry import channel_registry
from app.services.channel_service import channel_service
from app.services.auth_service import auth_service
from app.services.error_service import error_service


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

_BEARER_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*['\"]?(?:bearer|basic)\s+)([^\s'\",;}\]]+)"
)
_KV_EQUALS_QUOTED_PATTERN = re.compile(
    r"(?i)\b(access_token|refresh_token|partner_key|password|sign)\s*=\s*(['\"])([^'\"]+)\2"
)
_KV_EQUALS_UNQUOTED_PATTERN = re.compile(
    r"(?i)\b(access_token|refresh_token|partner_key|password|sign)\s*=\s*([^\s&'\",;}\]]+)"
)
_KV_COLON_PATTERN = re.compile(
    r"(?i)(['\"]?(?:access_token|refresh_token|partner_key|password|sign)['\"]?\s*:\s*['\"])([^'\"]+)(['\"])"
)


class _SecretRedactingFilter(logging.Filter):
    """Redact Bearer credentials and sensitive keys/tokens from log messages."""

    @staticmethod
    def redact(text_value: str) -> str:
        redacted = _BEARER_PATTERN.sub(r"\1[REDACTED]", text_value)
        redacted = _KV_EQUALS_QUOTED_PATTERN.sub(r"\1=\2[REDACTED]\2", redacted)
        redacted = _KV_EQUALS_UNQUOTED_PATTERN.sub(r"\1=[REDACTED]", redacted)
        redacted = _KV_COLON_PATTERN.sub(r"\1[REDACTED]\3", redacted)
        return redacted

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            formatted = record.getMessage()
            redacted = self.redact(formatted)
            if redacted != formatted or record.args:
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        if isinstance(record.exc_text, str):
            record.exc_text = self.redact(record.exc_text)
        return True


_secret_filter = _SecretRedactingFilter()
_root_logger = logging.getLogger()
_root_logger.addFilter(_secret_filter)
for _handler in _root_logger.handlers:
    _handler.addFilter(_secret_filter)

logger = logging.getLogger("naman_portal")
logger.addFilter(_secret_filter)
settings = get_settings()


async def _stop_worker(worker) -> None:
    """Stop a background sync worker and await its task completion."""
    if hasattr(worker, "stop_and_wait"):
        await worker.stop_and_wait()
        return
    worker.stop_background_worker()
    task = getattr(worker, "_background_task", None)
    if task is not None and not task.done():
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=5.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application Lifespan:
    1. Initialize Database Schema tables (in DEBUG) or verify DB connectivity.
    2. Seed initial SuperAdmin account if none exists.
    3. Register Channel Adapters (ShopeeFood, GrabMart, ShopeeMart) into Central Registry.
    4. Clean up on shutdown.
    """
    logger.info("=== Khởi động Nam An Unified Merchant Portal ===")

    # Skip startup fail-fast validation under pytest harness (ENVIRONMENT=test in os.environ)
    if (
        settings.ENVIRONMENT.lower() != "test"
        and os.environ.get("ENVIRONMENT", "").lower() != "test"
    ):
        settings.validate_production_security()

    # Verify DB connectivity (and auto-create tables / seed in DEBUG)
    try:
        if settings.DEBUG:
            logger.info("Đang khởi tạo cấu trúc cơ sở dữ liệu PostgreSQL...")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("✅ Đã sẵn sàng cơ sở dữ liệu PostgreSQL.")

            # Seed default SuperAdmin and Default Channels
            async with AsyncSessionLocal() as session:
                await auth_service.seed_initial_superadmin(session)
                await channel_service.ensure_default_channels(session)
        else:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            logger.info("✅ Kết nối cơ sở dữ liệu PostgreSQL sẵn sàng.")
            async with AsyncSessionLocal() as db:
                await channel_service.ensure_default_stores(db)
    except Exception as ex:
        if settings.ENVIRONMENT != "test":
            logger.error(f"Không thể kết nối tới PostgreSQL trong startup: {str(ex)}")
            raise
        logger.warning(f"Lưu ý: Không thể kết nối tới PostgreSQL trong startup ({str(ex)}). Tiếp tục khởi động (test)...")


    # Register decoupled channel adapters
    logger.info("Đang đăng ký các Channel Adapters vào Registry...")
    if settings.SHOPEEFOOD_ENABLED:
        channel_registry.register(ShopeeFoodChannelAdapter())
    if settings.GRABMART_ENABLED:
        channel_registry.register(GrabMartChannelAdapter())
    if settings.SHOPEEMART_ENABLED:
        channel_registry.register(ShopeeMartChannelAdapter())
        try:
            from app.modules.shopeemart.service import shopeemart_client
            async with AsyncSessionLocal() as session:
                await shopeemart_client.hydrate_from_db(session)
        except Exception as ex:
            logger.warning(f"Lưu ý: Không thể nạp trước ShopeeMart token từ DB ({ex})")

    logger.info(f"✅ Các kênh bán lẻ đã đăng ký: {channel_registry.list_channels()}")
    
    # Start Haravan & Poppies ERP Background Sync Workers
    from app.modules.haravan.sync_worker import haravan_sync_worker
    from app.services.arito_service import arito_service
    from app.services.file_service import file_service
    from app.services.haravan_order_service import haravan_order_service
    from app.services.poppies_service import poppies_service
    from app.services.poppies_sync_worker import poppies_sync_worker
    from app.services.hr_sync_worker import hr_sync_worker
    from app.services.shopeemart_sync_worker import shopeemart_sync_worker
    haravan_sync_worker.start_background_worker()
    poppies_sync_worker.start_background_worker()
    hr_sync_worker.start_background_worker()
    shopeemart_sync_worker.start_background_worker()

    yield
    
    logger.info("=== Đang tắt hệ thống Nam An Merchant Portal ===")
    await _stop_worker(shopeemart_sync_worker)
    await _stop_worker(hr_sync_worker)
    await _stop_worker(poppies_sync_worker)
    await _stop_worker(haravan_sync_worker)
    if settings.SHOPEEMART_ENABLED:
        from app.modules.shopeemart.service import shopeemart_client
        await shopeemart_client.close()
    auth_service.close()
    haravan_order_service.close()
    arito_service.close()
    await poppies_service.client.close()
    await file_service.close()
    await engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Hệ thống Cổng Quản Trị Đa Kênh & Đồng Bộ Đơn Hàng Hợp Nhất của Nam An Market (ShopeeFood, GrabMart, Shopee,...)",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
    lifespan=lifespan
)

class _PayloadTooLargeError(Exception):
    """Internal control-flow exception raised when streamed request body exceeds limit."""


class RequestBodySizeLimitMiddleware:
    """Pure ASGI middleware enforcing request body size limits on headers and streamed body."""

    DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
    UPLOAD_MAX_BYTES = 20 * 1024 * 1024  # 20 MB

    def __init__(self, asgi_app) -> None:
        self.asgi_app = asgi_app

    @classmethod
    def _max_bytes_for_path(cls, path: str) -> int:
        if path.rstrip("/").endswith("/files/upload"):
            return cls.UPLOAD_MAX_BYTES
        return cls.DEFAULT_MAX_BYTES

    @staticmethod
    def _get_content_length(scope) -> Optional[int]:
        for name, value in scope.get("headers", ()):
            if name.lower() == b"content-length":
                try:
                    return int(value.decode("ascii").strip())
                except (UnicodeDecodeError, ValueError):
                    return -1
        return None

    @staticmethod
    def _too_large_response(max_bytes: int) -> JSONResponse:
        max_mb = max_bytes // (1024 * 1024)
        return JSONResponse(
            status_code=413,
            content=APIResponse.fail(
                message=f"Kích thước dữ liệu yêu cầu vượt quá giới hạn cho phép ({max_mb} MB).",
                error_code="PAYLOAD_TOO_LARGE",
            ).model_dump(),
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.asgi_app(scope, receive, send)
            return

        path = scope.get("path", "")
        max_bytes = self._max_bytes_for_path(path)
        content_length = self._get_content_length(scope)

        if content_length is not None and content_length < 0:
            bad_req = JSONResponse(
                status_code=400,
                content=APIResponse.fail(
                    message="Tiêu đề Content-Length không hợp lệ.",
                    error_code="BAD_REQUEST",
                ).model_dump(),
            )
            await bad_req(scope, receive, send)
            return

        if content_length is not None and content_length > max_bytes:
            await self._too_large_response(max_bytes)(scope, receive, send)
            return

        total_bytes = 0
        limit_exceeded = False
        response_started = False

        async def limited_receive():
            nonlocal total_bytes, limit_exceeded
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"") or b""
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    limit_exceeded = True
                    raise _PayloadTooLargeError()
            return message

        async def guarded_send(message):
            nonlocal response_started
            if limit_exceeded and not response_started:
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.asgi_app(scope, limited_receive, guarded_send)
        except _PayloadTooLargeError:
            if not response_started:
                await self._too_large_response(max_bytes)(scope, receive, send)
            return

        if limit_exceeded and not response_started:
            await self._too_large_response(max_bytes)(scope, receive, send)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware applying standard HTTP security headers to all responses."""

    def __init__(self, asgi_app) -> None:
        self.asgi_app = asgi_app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.asgi_app(scope, receive, send)
            return

        path = scope.get("path", "")

        async def send_with_security_headers(message):
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers["Permissions-Policy"] = (
                    "camera=(), microphone=(), geolocation=(), payment=()"
                )
                if not settings.DEBUG:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
                if path.startswith("/api/v1/") and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.asgi_app(scope, receive, send_with_security_headers)


# Middlewares (last added runs outermost)
app.add_middleware(RequestBodySizeLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "X-Requested-With",
        "X-Poppies-Webhook-Key",
        "X-Event-Id",
        "Idempotency-Key",
    ],
)
app.add_middleware(SecurityHeadersMiddleware)


# Global Exception Handlers conforming to Development SOP
@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    """Handles domain application exceptions with standard JSON schema."""
    # If this is a 5xx error or an external channel failure, log to operational_error_logs
    if exc.status_code >= 500:
        try:
            async with AsyncSessionLocal() as session:
                from app.core.exceptions import ExternalChannelError
                module = "EXTERNAL_CHANNEL"
                if isinstance(exc, ExternalChannelError) and hasattr(exc, "details") and exc.details:
                    module = str(exc.details.get("channel", "EXTERNAL")).upper()
                await error_service.log_error(
                    db=session,
                    error_code=exc.error_code,
                    message=exc.message,
                    severity=ErrorSeverity.ERROR,
                    module=module,
                    endpoint=str(request.url.path),
                    http_method=request.method,
                    http_status_code=exc.status_code,
                    client_ip=request.client.host if request.client else None,
                )
        except Exception as log_ex:
            logger.warning(f"Không thể ghi nhật ký sự cố AppException: {str(log_ex)}")

    response_data = None if (exc.status_code >= 500 and not settings.DEBUG) else exc.details
    return JSONResponse(
        status_code=exc.status_code,
        content=APIResponse.fail(
            message=exc.message,
            error_code=exc.error_code,
            data=response_data
        ).model_dump()
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handles Pydantic request validation errors."""
    errors = []
    for err in exc.errors():
        loc = " -> ".join([str(x) for x in err.get("loc", [])])
        errors.append({"field": loc, "message": err.get("msg")})
    
    return JSONResponse(
        status_code=422,
        content=APIResponse.fail(
            message="Dữ liệu yêu cầu không hợp lệ.",
            error_code="VALIDATION_ERROR",
            data=errors
        ).model_dump()
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catches any unhandled internal server error and records it in OperationalErrorLog."""
    logger.error(f"Unhandled Exception at {request.url.path}: {str(exc)}", exc_info=True)
    import traceback
    stack = traceback.format_exc()
    try:
        async with AsyncSessionLocal() as session:
            await error_service.log_error(
                db=session,
                error_code="INTERNAL_SERVER_ERROR",
                message=str(exc) or "Lỗi hệ thống không xác định",
                severity=ErrorSeverity.CRITICAL,
                module="CORE",
                stack_trace=stack,
                endpoint=str(request.url.path),
                http_method=request.method,
                http_status_code=500,
                client_ip=request.client.host if request.client else None,
            )
    except Exception as log_ex:
        logger.warning(f"Không thể ghi nhật ký sự cố Unhandled Exception: {str(log_ex)}")

    return JSONResponse(
        status_code=500,
        content=APIResponse.fail(
            message="Đã xảy ra lỗi nội bộ trên máy chủ. Vui lòng liên hệ quản trị viên.",
            error_code="INTERNAL_SERVER_ERROR"
        ).model_dump()
    )



from app.web import web_router

# Mount Core API Routers
app.include_router(api_v1_router)

# Mount Channel Webhook Routers under /api/v1 and root
app.include_router(shopeefood_webhook_router, prefix="/api/v1")
app.include_router(shopeefood_webhook_router)  # Allows root /shopeefoodapi/{store_code}.json
app.include_router(grabmart_webhook_router, prefix="/api/v1")
app.include_router(grabmart_webhook_router)     # Allows root /grabmart/... endpoints
app.include_router(shopeemart_webhook_router, prefix="/api/v1")
app.include_router(shopeemart_webhook_router)     # Allows root /shopeemart/... endpoints


@app.post("/oauth/token", include_in_schema=False)
async def root_oauth_token(request: Request):
    from app.modules.grabmart.router import get_partner_oauth_token
    return await get_partner_oauth_token(request)


# 1x1 transparent PNG image for static image mock/placeholder responses
PLACEHOLDER_IMAGE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02"
    b"\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82"
)


app.mount(
    "/static/css",
    StaticFiles(directory="app/static/css", check_dir=False),
    name="static-css",
)
app.mount(
    "/static/vendor",
    StaticFiles(directory="app/static/vendor", check_dir=False),
    name="static-vendor",
)


@app.get("/static/images/{filename:path}", include_in_schema=False)
async def serve_static_images(filename: str):
    """Serve mock/placeholder image for GrabMart and other channel photo validators."""
    return Response(content=PLACEHOLDER_IMAGE_PNG, media_type="image/png")


# Mount Admin Portal & Web Pages (/admin, /system-status, /admin/login)
app.include_router(web_router)


@app.get("/api/info", tags=["Root"])
async def api_info():
    data: dict = {
        "portal": settings.APP_NAME,
    }
    if settings.DEBUG:
        data["version"] = settings.APP_VERSION
        data["channels"] = channel_registry.list_channels()
        data["docs"] = "/docs"
    return APIResponse.ok(
        data=data,
        message="Chào mừng bạn đến với Nam An Market Unified Merchant Portal API"
    )

