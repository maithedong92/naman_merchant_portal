from fastapi import APIRouter
from app.api.v1.auth import router as auth_router
from app.api.v1.channels import router as channels_router
from app.api.v1.health import router as health_router
from app.api.v1.inventory import router as inventory_router
from app.api.v1.orders import router as orders_router
from app.api.v1.products import router as products_router
from app.api.v1.stores import router as stores_router
from app.api.v1.system import router as system_router
from app.api.v1.users import router as users_router

api_v1_router = APIRouter(prefix="/api/v1")

# Register Core routers
api_v1_router.include_router(health_router)
api_v1_router.include_router(system_router)
api_v1_router.include_router(auth_router)
api_v1_router.include_router(users_router)
api_v1_router.include_router(stores_router)
api_v1_router.include_router(channels_router)
api_v1_router.include_router(products_router)
api_v1_router.include_router(inventory_router)
api_v1_router.include_router(orders_router)


