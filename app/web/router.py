from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.config import get_settings

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
settings = get_settings()

web_router = APIRouter(include_in_schema=False)


@web_router.get("/", response_class=HTMLResponse)
async def home_redirect():
    """Redirect home route to the Admin Dashboard."""
    return RedirectResponse(url="/admin", status_code=302)


@web_router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    """Serve Admin Dashboard Overview page."""
    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            "request": request,
            "active_page": "dashboard",
            "app_name": settings.APP_NAME,
            "app_version": settings.APP_VERSION,
        }
    )


@web_router.get("/system-status", response_class=HTMLResponse)
@web_router.get("/admin/system-status", response_class=HTMLResponse)
async def system_status_page(request: Request):
    """Serve System Status & Operational Error Incident Management page."""
    return templates.TemplateResponse(
        request=request,
        name="system_status.html",
        context={
            "request": request,
            "active_page": "system_status",
            "app_name": settings.APP_NAME,
            "app_version": settings.APP_VERSION,
        }
    )


@web_router.get("/orders", response_class=HTMLResponse)
@web_router.get("/admin/orders", response_class=HTMLResponse)
async def orders_dispatch_page(request: Request):
    """Serve Unified Orders Dispatching & Management page."""
    return templates.TemplateResponse(
        request=request,
        name="orders.html",
        context={
            "request": request,
            "active_page": "orders",
            "app_name": settings.APP_NAME,
            "app_version": settings.APP_VERSION,
        },
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


@web_router.get("/reports", response_class=HTMLResponse)
@web_router.get("/admin/reports", response_class=HTMLResponse)
async def reports_analytics_page(request: Request):
    """Serve Multi-Channel Analytics, Reporting & Reconciliation page."""
    return templates.TemplateResponse(
        request=request,
        name="reports.html",
        context={
            "request": request,
            "active_page": "reports",
            "app_name": settings.APP_NAME,
            "app_version": settings.APP_VERSION,
        }
    )


@web_router.get("/inventory", response_class=HTMLResponse)
@web_router.get("/admin/inventory", response_class=HTMLResponse)
async def inventory_page(request: Request):
    """Serve Omni-Channel Real-time Inventory & Menu Management page."""
    return templates.TemplateResponse(
        request=request,
        name="inventory.html",
        context={
            "request": request,
            "active_page": "inventory",
            "app_name": settings.APP_NAME,
            "app_version": settings.APP_VERSION,
        }
    )


@web_router.get("/users", response_class=HTMLResponse)
@web_router.get("/admin/users", response_class=HTMLResponse)
async def users_management_page(request: Request):
    """Serve User Management & Store RBAC Administration page."""
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "request": request,
            "active_page": "users",
            "app_name": settings.APP_NAME,
            "app_version": settings.APP_VERSION,
        }
    )


@web_router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    """Serve Admin Login page."""
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "app_name": settings.APP_NAME,
            "app_version": settings.APP_VERSION,
        }
    )


# ==============================================================================
# Design Review & Mockup Preview Routes
# ==============================================================================
@web_router.get("/preview", response_class=HTMLResponse)
async def preview_index_page(request: Request):
    """Serve Design Options Comparison & Interactive Review Hub."""
    return templates.TemplateResponse(
        request=request,
        name="mockups/index.html",
        context={"request": request}
    )


@web_router.get("/preview/option1", response_class=HTMLResponse)
async def preview_option1_page(request: Request):
    """Serve Option 1: Modern SaaS (Linear & Vercel Ops) Mockup."""
    return templates.TemplateResponse(
        request=request,
        name="mockups/option1_modern_saas.html",
        context={"request": request}
    )


@web_router.get("/preview/option2", response_class=HTMLResponse)
async def preview_option2_page(request: Request):
    """Serve Option 2: Industrial Logistics & Warehouse Ops Mockup."""
    return templates.TemplateResponse(
        request=request,
        name="mockups/option2_industrial_ops.html",
        context={"request": request}
    )


@web_router.get("/preview/option3", response_class=HTMLResponse)
async def preview_option3_page(request: Request):
    """Serve Option 3: Apple Retail & Merchant Pro Mockup."""
    return templates.TemplateResponse(
        request=request,
        name="mockups/option3_apple_retail.html",
        context={"request": request}
    )

