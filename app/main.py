import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application Lifespan:
    1. Initialize Database Schema tables.
    2. Seed initial SuperAdmin account if none exists.
    3. Register Channel Adapters (ShopeeFood, GrabMart, ShopeeMart) into Central Registry.
    4. Clean up on shutdown.
    """
    logger.info("=== Khởi động Nam An Unified Merchant Portal ===")
    
    # Auto-create tables in development and seed initial admin
    if settings.DEBUG:
        logger.info("Đang khởi tạo cấu trúc cơ sở dữ liệu PostgreSQL...")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("✅ Đã sẵn sàng cơ sở dữ liệu PostgreSQL.")

            # Seed default SuperAdmin
            async with AsyncSessionLocal() as session:
                await auth_service.seed_initial_superadmin(session)
        except Exception as ex:
            logger.warning(f"Lưu ý: Không thể kết nối tới PostgreSQL trong startup ({str(ex)}). Tiếp tục khởi động...")


    # Register decoupled channel adapters
    logger.info("Đang đăng ký các Channel Adapters vào Registry...")
    if settings.SHOPEEFOOD_ENABLED:
        channel_registry.register(ShopeeFoodChannelAdapter())
    if settings.GRABMART_ENABLED:
        channel_registry.register(GrabMartChannelAdapter())
    if settings.SHOPEEMART_ENABLED:
        channel_registry.register(ShopeeMartChannelAdapter())

    logger.info(f"✅ Các kênh bán lẻ đã đăng ký: {channel_registry.list_channels()}")
    
    yield
    
    logger.info("=== Đang tắt hệ thống Nam An Merchant Portal ===")
    await engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Hệ thống Cổng Quản Trị Đa Kênh & Đồng Bộ Đơn Hàng Hợp Nhất của Nam An Market (ShopeeFood, GrabMart, Shopee,...)",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

# Mount Channel Webhook Routers under /api/v1
app.include_router(shopeefood_webhook_router, prefix="/api/v1")
app.include_router(grabmart_webhook_router, prefix="/api/v1")
app.include_router(shopeemart_webhook_router, prefix="/api/v1")

# Mount Admin Portal & Web Pages (/admin, /system-status, /admin/login)
app.include_router(web_router)


@app.get("/api/info", tags=["Root"])
async def api_info():
    return APIResponse.ok(
        data={
            "portal": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "docs": "/docs",
            "channels": channel_registry.list_channels()
        },
        message="Chào mừng bạn đến với Nam An Market Unified Merchant Portal API"
    )

