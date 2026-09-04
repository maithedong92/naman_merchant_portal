from typing import List, Optional, Tuple
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ConflictError, NotFoundError
from app.models.product import Category, Product
from app.schemas.product import CategoryCreate, ProductCreate, ProductUpdate


class ProductService:
    """Core Service managing master catalog, products and categories."""

    @staticmethod
    async def create_category(payload: CategoryCreate, db: AsyncSession) -> Category:
        existing = await db.execute(select(Category).where(Category.code == payload.code))
        if existing.scalar_one_or_none():
            raise ConflictError(f"Danh mục với mã '{payload.code}' đã tồn tại.")

        category = Category(
            code=payload.code,
            name=payload.name,
            parent_id=payload.parent_id,
            sequence=payload.sequence
        )
        db.add(category)
        await db.commit()
        await db.refresh(category)
        return category

    @staticmethod
    async def list_categories(db: AsyncSession) -> List[Category]:
        res = await db.execute(select(Category).order_by(Category.sequence, Category.name))
        return list(res.scalars().all())

    @staticmethod
    async def create_product(payload: ProductCreate, db: AsyncSession) -> Product:
        existing = await db.execute(select(Product).where(Product.sku == payload.sku))
        if existing.scalar_one_or_none():
            raise ConflictError(f"Sản phẩm với SKU '{payload.sku}' đã tồn tại.")

        product = Product(
            sku=payload.sku,
            barcode=payload.barcode,
            name=payload.name,
            description=payload.description,
            unit=payload.unit,
            base_price=payload.base_price,
            image_url=payload.image_url,
            category_id=payload.category_id,
            is_active=payload.is_active,
            metadata_json=payload.metadata_json
        )
        db.add(product)
        await db.commit()
        await db.refresh(product)
        return product

    @staticmethod
    async def get_product_by_id(product_id: str, db: AsyncSession) -> Product:
        stmt = select(Product).where(Product.id == product_id).options(
            selectinload(Product.channel_mappings),
            selectinload(Product.category)
        )
        res = await db.execute(stmt)
        product = res.scalar_one_or_none()
        if not product:
            raise NotFoundError("Product", product_id)
        return product

    @staticmethod
    async def list_products(
        page: int,
        page_size: int,
        category_id: Optional[str],
        search: Optional[str],
        db: AsyncSession
    ) -> Tuple[List[Product], int]:
        query = select(Product).options(selectinload(Product.channel_mappings))

        if category_id:
            query = query.where(Product.category_id == category_id)
        if search:
            pattern = f"%{search}%"
            query = query.where(
                (Product.sku.ilike(pattern)) |
                (Product.name.ilike(pattern)) |
                (Product.barcode.ilike(pattern))
            )

        count_stmt = select(func.count()).select_from(query.subquery())
        total = (await db.execute(count_stmt)).scalar() or 0

        query = query.order_by(Product.name).offset((page - 1) * page_size).limit(page_size)
        res = await db.execute(query)
        return list(res.scalars().all()), total
