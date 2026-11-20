import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

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
from app.services.auth_service import auth_service
from app.services.error_service import error_service


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("naman_portal")
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
                from app.services.channel_service import channel_service
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
    haravan_sync_worker.start_background_worker()
    poppies_sync_worker.start_background_worker()
    hr_sync_worker.start_background_worker()

    yield
    
    logger.info("=== Đang tắt hệ thống Nam An Merchant Portal ===")
    await _stop_worker(hr_sync_worker)
    await _stop_worker(poppies_sync_worker)
    await _stop_worker(haravan_sync_worker)
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

# CORS Middleware (explicit methods & headers)
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

    return JSONResponse(
        status_code=exc.status_code,
        content=APIResponse.fail(
            message=exc.message,
            error_code=exc.error_code,
            data=exc.details
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
    logger.error(f"Unhandled Exception at {request.url}: {str(exc)}", exc_info=True)
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


@app.get("/static/images/{filename:path}", include_in_schema=False)
async def serve_static_images(filename: str):
    """Serve mock/placeholder image for GrabMart and other channel photo validators."""
    return Response(content=PLACEHOLDER_IMAGE_PNG, media_type="image/png")


# Mount Admin Portal & Web Pages (/admin, /system-status, /admin/login)
app.include_router(web_router)


@app.get("/api/info", tags=["Root"])
async def api_info():
    data = {
        "portal": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "channels": channel_registry.list_channels(),
    }
    if settings.DEBUG:
        data["docs"] = "/docs"
    return APIResponse.ok(
        data=data,
        message="Chào mừng bạn đến với Nam An Market Unified Merchant Portal API"
    )

