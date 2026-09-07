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
