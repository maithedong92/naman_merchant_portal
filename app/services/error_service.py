import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.models.operational_error import ErrorSeverity, ErrorStatus, OperationalErrorLog
from app.schemas.system import (
    ComponentHealth,
    SystemErrorSummary,
    SystemHealthResponse,
)

logger = logging.getLogger("naman_portal.error_service")
settings = get_settings()

START_TIME = time.time()


def sanitize_payload(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Remove sensitive credential keys before storing request payload in logs."""
    if not payload or not isinstance(payload, dict):
        return payload

    sensitive_keys = {
        "password", "token", "secret", "access_token", "refresh_token",
        "authorization", "api_key", "app_key", "client_secret", "partner_key"
    }

    clean = {}
    for k, v in payload.items():
        if any(s in k.lower() for s in sensitive_keys):
            clean[k] = "****** [REDACTED]"
        elif isinstance(v, dict):
            clean[k] = sanitize_payload(v)
        else:
            clean[k] = v
    return clean


class ErrorService:
    """Service handling operational error ingestion, deduplication, resolution, and health metrics."""

    async def log_error(
        self,
        db: AsyncSession,
        error_code: str,
        message: str,
        severity: ErrorSeverity = ErrorSeverity.ERROR,
        module: str = "CORE",
        stack_trace: Optional[str] = None,
        endpoint: Optional[str] = None,
        http_method: Optional[str] = None,
        http_status_code: Optional[int] = None,
        client_ip: Optional[str] = None,
        user_id: Optional[str] = None,
        request_payload: Optional[Dict[str, Any]] = None,
    ) -> OperationalErrorLog:
        """
        Record an operational error incident. Deduplicates identical open errors within 1 hour.
        """
        now = datetime.now(timezone.utc)
        error_hash = hashlib.sha256(
            f"{module}:{error_code}:{message[:150]}".encode("utf-8")
        ).hexdigest()

        cleaned_payload = sanitize_payload(request_payload)

        try:
            # Check for recent open duplicate error
            one_hour_ago = now - timedelta(hours=1)
            stmt = select(OperationalErrorLog).where(
                OperationalErrorLog.error_hash == error_hash,
                OperationalErrorLog.resolution_status.in_([ErrorStatus.OPEN.value, ErrorStatus.INVESTIGATING.value]),
                OperationalErrorLog.last_occurred_at >= one_hour_ago,
            )
            result = await db.execute(stmt)
            existing = result.scalars().first()

            if existing:
                existing.occurrence_count += 1
                existing.last_occurred_at = now
                if stack_trace and not existing.stack_trace:
                    existing.stack_trace = stack_trace
                if http_status_code:
                    existing.http_status_code = http_status_code
                await db.commit()
                await db.refresh(existing)
                return existing

            new_log = OperationalErrorLog(
                error_code=error_code,
                severity=severity.value if isinstance(severity, ErrorSeverity) else severity,
                module=module,
                message=message,
                stack_trace=stack_trace,
                endpoint=endpoint,
                http_method=http_method,
                http_status_code=http_status_code,
                client_ip=client_ip,
                user_id=user_id,
                request_payload=cleaned_payload,
                resolution_status=ErrorStatus.OPEN.value,
                occurrence_count=1,
                error_hash=error_hash,
                last_occurred_at=now,
            )
            db.add(new_log)
            await db.commit()
            await db.refresh(new_log)
            logger.error(
                f"[INCIDENT LOGGED] [{severity}] [{module}] {error_code}: {message} (id={new_log.id})"
            )
            return new_log
        except Exception as ex:
            logger.critical(f"Lỗi nghiêm trọng khi lưu OperationalErrorLog vào DB: {str(ex)}")
            await db.rollback()
            # Return dummy unsaved log so callers don't fail
            return OperationalErrorLog(
                error_code=error_code,
                severity=severity.value if isinstance(severity, ErrorSeverity) else severity,
                module=module,
                message=message,
            )

    async def list_errors(
        self,
        db: AsyncSession,
        severity: Optional[ErrorSeverity] = None,
        module: Optional[str] = None,
        status: Optional[ErrorStatus] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[OperationalErrorLog], int]:
        """Fetch operational incident logs with filtering and pagination."""
        query = select(OperationalErrorLog)

        if severity:
            query = query.where(OperationalErrorLog.severity == severity.value)
        if module:
            query = query.where(OperationalErrorLog.module == module.upper())
        if status:
            query = query.where(OperationalErrorLog.resolution_status == status.value)
        if search:
            kw = f"%{search.strip()}%"
            query = query.where(
                or_(
                    OperationalErrorLog.error_code.ilike(kw),
                    OperationalErrorLog.message.ilike(kw),
                    OperationalErrorLog.endpoint.ilike(kw),
                )
            )

        # Count total
        count_stmt = select(func.count()).select_from(query.subquery())
        total = (await db.execute(count_stmt)).scalar() or 0

        # Paginate ordered by last occurred time
        offset = (page - 1) * page_size
        stmt = query.order_by(desc(OperationalErrorLog.last_occurred_at)).offset(offset).limit(page_size)
        result = await db.execute(stmt)
        return list(result.scalars().all()), total

    async def get_error_by_id(self, db: AsyncSession, error_id: str) -> OperationalErrorLog:
        """Get details of a single incident."""
        stmt = select(OperationalErrorLog).where(OperationalErrorLog.id == error_id)
        result = await db.execute(stmt)
        error_log = result.scalar_one_or_none()
        if not error_log:
            raise NotFoundError("OperationalErrorLog", error_id)
        return error_log

    async def resolve_error(
        self,
        db: AsyncSession,
        error_id: str,
        resolution_notes: str,
        resolved_by: Optional[str] = None,
    ) -> OperationalErrorLog:
        """Mark an incident as RESOLVED with resolution notes."""
        error_log = await self.get_error_by_id(db, error_id)
        now = datetime.now(timezone.utc)

        error_log.resolution_status = ErrorStatus.RESOLVED.value
        error_log.resolution_notes = resolution_notes.strip()
        error_log.resolved_by = resolved_by or "Admin"
        error_log.resolved_at = now

        await db.commit()
        await db.refresh(error_log)
        logger.info(f"Đã xử lý xong sự cố #{error_id} bởi '{error_log.resolved_by}'")
        return error_log

    async def reopen_error(self, db: AsyncSession, error_id: str) -> OperationalErrorLog:
        """Reopen an incident back to INVESTIGATING status."""
        error_log = await self.get_error_by_id(db, error_id)
        error_log.resolution_status = ErrorStatus.INVESTIGATING.value
        await db.commit()
        await db.refresh(error_log)
        return error_log

    async def get_system_health(self, db: AsyncSession) -> SystemHealthResponse:
        """Check live connectivity to PostgreSQL, Channels, and summarize operational errors."""
        now = datetime.now(timezone.utc)
        components: List[ComponentHealth] = []
        overall_status = "HEALTHY"

        # 1. Check PostgreSQL Local DB
        db_start = time.perf_counter()
        try:
            await db.execute(text("SELECT 1"))
            db_latency = round((time.perf_counter() - db_start) * 1000, 2)
            components.append(
                ComponentHealth(
                    name="PostgreSQL Local Database",
                    status="UP",
                    latency_ms=db_latency,
                    details=f"Kết nối thành công (Host: 5432, Pool size: {settings.DB_POOL_SIZE})",
                )
            )
        except Exception as ex:
            overall_status = "CRITICAL"
            components.append(
                ComponentHealth(
                    name="PostgreSQL Local Database",
                    status="DOWN",
                    details=f"Không thể kết nối cơ sở dữ liệu: {str(ex)[:100]}",
                )
            )

        # 2. Check ShopeeFood S2S Module
        if settings.SHOPEEFOOD_ENABLED:
            components.append(
                ComponentHealth(
                    name="ShopeeFood Partner S2S API",
                    status="UP" if settings.SHOPEEFOOD_APP_ID else "CONFIG_REQUIRED",
                    details=f"Base URL: {settings.SHOPEEFOOD_BASE_URL} (App ID: {settings.SHOPEEFOOD_APP_ID})",
                )
            )

        # 3. Check GrabMart POS Module
        if settings.GRABMART_ENABLED:
            has_creds = bool(settings.GRABMART_CLIENT_ID and settings.GRABMART_CLIENT_SECRET)
            components.append(
                ComponentHealth(
                    name="GrabMart Partner POS API",
                    status="UP" if has_creds else "CONFIG_REQUIRED",
                    details=f"Base URL: {settings.GRABMART_BASE_URL} (Scope: {settings.GRABMART_SCOPE})",
                )
            )

        # 4. Check ShopeeMart Open Platform Module
        if settings.SHOPEEMART_ENABLED:
            has_shopee_creds = bool(settings.SHOPEEMART_PARTNER_ID and settings.SHOPEEMART_PARTNER_KEY)
            components.append(
                ComponentHealth(
                    name="ShopeeMart Open Platform API v2",
                    status="UP" if has_shopee_creds else "CONFIG_REQUIRED",
                    details=f"Base URL: {settings.SHOPEEMART_BASE_URL}",
                )
            )

        # 5. Aggregate Error Log Counts
        today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        try:
            total_stmt = select(func.count(OperationalErrorLog.id))
            open_stmt = select(func.count(OperationalErrorLog.id)).where(
                OperationalErrorLog.resolution_status == ErrorStatus.OPEN.value
            )
            investigating_stmt = select(func.count(OperationalErrorLog.id)).where(
                OperationalErrorLog.resolution_status == ErrorStatus.INVESTIGATING.value
            )
            resolved_stmt = select(func.count(OperationalErrorLog.id)).where(
                OperationalErrorLog.resolution_status == ErrorStatus.RESOLVED.value
            )
            critical_stmt = select(func.count(OperationalErrorLog.id)).where(
                OperationalErrorLog.severity == ErrorSeverity.CRITICAL.value,
                OperationalErrorLog.resolution_status.in_([ErrorStatus.OPEN.value, ErrorStatus.INVESTIGATING.value]),
            )
            today_stmt = select(func.count(OperationalErrorLog.id)).where(
                OperationalErrorLog.last_occurred_at >= today_start
            )

            total_errors = (await db.execute(total_stmt)).scalar() or 0
            open_errors = (await db.execute(open_stmt)).scalar() or 0
            investigating_errors = (await db.execute(investigating_stmt)).scalar() or 0
            resolved_errors = (await db.execute(resolved_stmt)).scalar() or 0
            critical_errors = (await db.execute(critical_stmt)).scalar() or 0
            errors_today = (await db.execute(today_stmt)).scalar() or 0

            error_summary = SystemErrorSummary(
                total_errors=total_errors,
                open_errors=open_errors,
                investigating_errors=investigating_errors,
                resolved_errors=resolved_errors,
                critical_errors=critical_errors,
                errors_today=errors_today,
            )

            if critical_errors > 0 and overall_status != "CRITICAL":
                overall_status = "WARNING"

        except Exception as ex:
            logger.warning(f"Không thể đếm số liệu sự cố: {str(ex)}")
            error_summary = SystemErrorSummary()

        uptime = round(time.time() - START_TIME, 1)

        return SystemHealthResponse(
            status=overall_status,
            uptime_seconds=uptime,
            app_version=settings.APP_VERSION,
            environment=settings.ENVIRONMENT,
            components=components,
            error_summary=error_summary,
            timestamp=now,
        )


error_service = ErrorService()
