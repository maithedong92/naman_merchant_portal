import logging
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import get_settings
from app.core.database import get_db
from app.core.responses import APIResponse
from app.services.channel_registry import channel_registry

router = APIRouter(tags=["System & Health"])
settings = get_settings()
logger = logging.getLogger("naman_portal.health")


@router.get("/health", response_model=APIResponse[dict])
async def health_check():
    """Liveness probe: verifies the FastAPI application is running."""
    return APIResponse.ok(
        data={
            "app_name": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT,
            "registered_channels": channel_registry.list_channels(),
        },
        message="Hệ thống Nam An Merchant Portal đang hoạt động bình thường."
    )


@router.get("/health/db", response_model=APIResponse[dict])
async def database_health_check(db: AsyncSession = Depends(get_db)):
    """Readiness probe: verifies connection to PostgreSQL database."""
    try:
        result = await db.execute(text("SELECT 1"))
        val = result.scalar()
        return APIResponse.ok(
            data={"database": "PostgreSQL", "status": "CONNECTED", "check": val},
            message="Kết nối cơ sở dữ liệu PostgreSQL thành công."
        )
    except Exception as ex:
        logger.error(f"Database readiness check failed: {ex}")
        return JSONResponse(
            status_code=503,
            content=APIResponse.fail(
                message="Không thể kết nối cơ sở dữ liệu.",
                error_code="DATABASE_CONNECTION_ERROR",
            ).model_dump(),
        )
