from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.responses import APIResponse
from app.schemas.common import PaginatedResponse, PaginationMeta
from app.schemas.product import (
    CategoryCreate,
    CategoryResponse,
    ProductCreate,
    ProductResponse,
)
from app.services.product_service import ProductService

router = APIRouter(prefix="/products", tags=["Catalog & Products"])


@router.get("/categories", response_model=APIResponse[List[CategoryResponse]])
async def list_categories(db: AsyncSession = Depends(get_db)):
    """List all product categories in hierarchical order."""
    categories = await ProductService.list_categories(db)
    return APIResponse.ok(data=categories)


@router.post("/categories", response_model=APIResponse[CategoryResponse], status_code=201)
async def create_category(payload: CategoryCreate, db: AsyncSession = Depends(get_db)):
    """Create a new product category."""
    category = await ProductService.create_category(payload, db)
    return APIResponse.ok(data=category, message="Tạo danh mục thành công")


@router.get("", response_model=APIResponse[PaginatedResponse[ProductResponse]])
async def list_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """List products with pagination and category / keyword search."""
    items, total = await ProductService.list_products(
        page=page,
        page_size=page_size,
        category_id=category_id,
        search=search,
        db=db
    )
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    return APIResponse.ok(
        data=PaginatedResponse(
            items=items,
            meta=PaginationMeta(
                page=page,
                page_size=page_size,
                total_items=total,
                total_pages=total_pages
            )
        )
    )


@router.post("", response_model=APIResponse[ProductResponse], status_code=201)
async def create_product(payload: ProductCreate, db: AsyncSession = Depends(get_db)):
    """Create a new master product SKU."""
    product = await ProductService.create_product(payload, db)
    return APIResponse.ok(data=product, message="Tạo sản phẩm thành công")


@router.get("/{product_id}", response_model=APIResponse[ProductResponse])
async def get_product(product_id: str, db: AsyncSession = Depends(get_db)):
    """Get product details by UUID."""
    product = await ProductService.get_product_by_id(product_id, db)
    return APIResponse.ok(data=product)
