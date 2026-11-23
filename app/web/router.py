from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_optional_db
from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.core.security import decode_token
from app.models.user import User, UserRole

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
settings = get_settings()

web_router = APIRouter(include_in_schema=False)

NO_STORE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


async def _get_authenticated_web_user(
    request: Request,
    db: Optional[AsyncSession],
) -> Optional[User]:
    """Validate the HttpOnly `naman_access_token` cookie on the server before rendering any UI."""
    token = request.cookies.get("naman_access_token")
    if not token or db is None:
        return None
    try:
        payload = decode_token(token)
        if not payload or payload.get("type") != "access":
            return None
        user_id = payload.get("sub")
        if not user_id:
            return None

        raw_ver = payload.get("ver", 0)
        token_ver = int(0 if raw_ver is None else raw_ver)

        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
    except (UnauthorizedError, ValueError, TypeError, SQLAlchemyError):
        return None

    if not user or not user.is_active or getattr(user, "portal_disabled", False):
        return None
    current_ver = int(getattr(user, "token_version", 0) or 0)
    if token_ver != current_ver:
        return None
    return user


def _login_redirect(request: Request) -> RedirectResponse:
    """Redirect unauthenticated browser requests directly to /admin/login without rendering UI."""
    target = request.url.path or "/admin"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    login_url = f"/admin/login?redirect={quote(target, safe='/')}"
    return RedirectResponse(url=login_url, status_code=302, headers=NO_STORE_HEADERS)


def _is_super_admin(user: User) -> bool:
    return bool(user.is_superuser or user.role == UserRole.SUPER_ADMIN.value)


def _render_protected_page(
    request: Request,
    template_name: str,
    active_page: str,
    extra_context: Optional[dict] = None,
) -> HTMLResponse:
    ctx = {
        "request": request,
        "active_page": active_page,
        "app_name": settings.APP_NAME,
        "app_version": settings.APP_VERSION,
    }
    if extra_context:
        ctx.update(extra_context)
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context=ctx,
        headers=NO_STORE_HEADERS,
    )


@web_router.get("/", response_class=HTMLResponse)
async def home_redirect(
    request: Request,
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    """Redirect home route to Admin Dashboard if authenticated, otherwise to /admin/login."""
    user = await _get_authenticated_web_user(request, db)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302, headers=NO_STORE_HEADERS)
    return RedirectResponse(url="/admin", status_code=302, headers=NO_STORE_HEADERS)


@web_router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    """Serve Admin Dashboard Overview page (requires valid server-side session cookie)."""
    user = await _get_authenticated_web_user(request, db)
    if not user:
        return _login_redirect(request)
    if user.role in (UserRole.STAFF.value, UserRole.READ_ONLY.value):
        return RedirectResponse(url="/admin/orders", status_code=302, headers=NO_STORE_HEADERS)
    return _render_protected_page(
        request,
        "admin_dashboard.html",
        "dashboard",
        extra_context={"debug": settings.DEBUG},
    )


@web_router.get("/system-status", response_class=HTMLResponse)
@web_router.get("/admin/system-status", response_class=HTMLResponse)
async def system_status_page(
    request: Request,
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    """Serve System Status & Operational Error Incident Management page (SUPER_ADMIN only)."""
    user = await _get_authenticated_web_user(request, db)
    if not user:
        return _login_redirect(request)
    if not _is_super_admin(user):
        return RedirectResponse(url="/admin/orders", status_code=302, headers=NO_STORE_HEADERS)
    return _render_protected_page(request, "system_status.html", "system_status")


@web_router.get("/orders", response_class=HTMLResponse)
@web_router.get("/admin/orders", response_class=HTMLResponse)
async def orders_dispatch_page(
    request: Request,
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    """Serve Unified Orders Dispatching & Management page."""
    user = await _get_authenticated_web_user(request, db)
    if not user:
        return _login_redirect(request)
    return _render_protected_page(request, "orders.html", "orders")


@web_router.get("/reports", response_class=HTMLResponse)
@web_router.get("/admin/reports", response_class=HTMLResponse)
async def reports_analytics_page(
    request: Request,
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    """Serve Multi-Channel Analytics, Reporting & Reconciliation page."""
    user = await _get_authenticated_web_user(request, db)
    if not user:
        return _login_redirect(request)
    return _render_protected_page(request, "reports.html", "reports")


@web_router.get("/inventory", response_class=HTMLResponse)
@web_router.get("/admin/inventory", response_class=HTMLResponse)
async def inventory_page(
    request: Request,
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    """Serve Omni-Channel Real-time Inventory & Menu Management page."""
    user = await _get_authenticated_web_user(request, db)
    if not user:
        return _login_redirect(request)
    return _render_protected_page(request, "inventory.html", "inventory")


@web_router.get("/users", response_class=HTMLResponse)
@web_router.get("/admin/users", response_class=HTMLResponse)
async def users_management_page(
    request: Request,
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    """Serve User Management & Store RBAC Administration page (SUPER_ADMIN only)."""
    user = await _get_authenticated_web_user(request, db)
    if not user:
        return _login_redirect(request)
    if not _is_super_admin(user):
        return RedirectResponse(url="/admin/orders", status_code=302, headers=NO_STORE_HEADERS)
    return _render_protected_page(request, "users.html", "users")


@web_router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(
    request: Request,
    db: Optional[AsyncSession] = Depends(get_optional_db),
):
    """Serve Admin Login page (redirects to /admin if already authenticated)."""
    user = await _get_authenticated_web_user(request, db)
    if user:
        return RedirectResponse(url="/admin", status_code=302, headers=NO_STORE_HEADERS)
    resp = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "app_name": settings.APP_NAME,
            "app_version": settings.APP_VERSION,
        },
        headers=NO_STORE_HEADERS,
    )
    if request.cookies.get("naman_access_token"):
        resp.delete_cookie("naman_access_token", path="/")
    return resp


# ==============================================================================
# Design Review & Mockup Preview Routes (DEBUG only)
# ==============================================================================
def _ensure_debug_mode() -> None:
    if not settings.DEBUG:
        raise HTTPException(status_code=404, detail="Not Found")


@web_router.get("/preview", response_class=HTMLResponse)
async def preview_index_page(request: Request):
    """Serve Design Options Comparison & Interactive Review Hub (DEBUG only)."""
    _ensure_debug_mode()
    return templates.TemplateResponse(
        request=request,
        name="mockups/index.html",
        context={"request": request}
    )


@web_router.get("/preview/option1", response_class=HTMLResponse)
async def preview_option1_page(request: Request):
    """Serve Option 1: Modern SaaS (Linear & Vercel Ops) Mockup (DEBUG only)."""
    _ensure_debug_mode()
    return templates.TemplateResponse(
        request=request,
        name="mockups/option1_modern_saas.html",
        context={"request": request}
    )


@web_router.get("/preview/option2", response_class=HTMLResponse)
async def preview_option2_page(request: Request):
    """Serve Option 2: Industrial Logistics & Warehouse Ops Mockup (DEBUG only)."""
    _ensure_debug_mode()
    return templates.TemplateResponse(
        request=request,
        name="mockups/option2_industrial_ops.html",
        context={"request": request}
    )


@web_router.get("/preview/option3", response_class=HTMLResponse)
async def preview_option3_page(request: Request):
    """Serve Option 3: Apple Retail & Merchant Pro Mockup (DEBUG only)."""
    _ensure_debug_mode()
    return templates.TemplateResponse(
        request=request,
        name="mockups/option3_apple_retail.html",
        context={"request": request}
    )
