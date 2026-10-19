from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.models.inventory import InventoryStockLog, StoreInventory
from app.models.product import Category, Product
from app.models.store import Store

logger = logging.getLogger("naman_portal.poppies_service")
settings = get_settings()


def slugify_category_code(name: str) -> str:
    """Generate a clean uppercase alphanumeric category code."""
    if not name:
        return "GENERAL"
    cleaned = re.sub(r"[^\w\s-]", "", name, flags=re.UNICODE)
    slug = re.sub(r"[-\s]+", "_", cleaned).strip("_").upper()
    return slug[:50] or "GENERAL"


class PoppiesClient:
    """
    HTTP Client connecting to Poppies ERP POS Master Data Inbound Gateway
    conforming strictly to DOCS_API_POS_MASTER_DATA_SKU.md.
    """

    def __init__(self):
        self.base_url = settings.POPPIES_API_BASE_URL.rstrip("/")
        self.api_key = settings.POPPIES_API_KEY
        self.timeout = settings.POPPIES_TIMEOUT_SECONDS

    def _get_headers(self) -> Dict[str, str]:
        return {
            "X-POS-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "NAM-UMP/PoppiesMasterDataClient"
        }

    async def get_health(self) -> Dict[str, Any]:
        """Check Poppies ERP Master Data API health probe."""
        urls_to_try = [
            f"{self.base_url}/health",
            "http://127.0.0.1:6515/api/v1/pos/master-data/health",
            "http://host.docker.internal:6515/api/v1/pos/master-data/health",
            "http://103.175.248.250:1155/api/v1/pos/master-data/health",
        ]
        headers = self._get_headers()

        last_err = None
        for url in urls_to_try:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        data["resolved_url"] = url
                        return data
            except Exception as ex:
                last_err = ex

        return {
            "status": "error",
            "service": "Nam An Poppies POS Master Data Inbound Gateway",
            "error": str(last_err),
            "base_url": self.base_url
        }

    async def get_sku(self, sku_or_barcode: str, branch_code: Optional[str] = "10001") -> Optional[Dict[str, Any]]:
        """Fetch full Master Data object for 1 SKU / Barcode."""
        url = f"{self.base_url}/sku/{sku_or_barcode}"
        params = {}
        if branch_code:
            params["branch_code"] = branch_code

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params, headers=self._get_headers())
            if resp.status_code == 200:
                body = resp.json()
                return body.get("item")
            elif resp.status_code == 404:
                return None
            else:
                resp.raise_for_status()
                return None

    async def get_snapshot(
        self,
        branch_code: str = "10001",
        status_filter: str = "01",
        limit: int = 500,
        offset: int = 0
    ) -> Dict[str, Any]:
        """Fetch paginated master data SKU snapshot with pricing, TTHH & CTTK stock."""
        url = f"{self.base_url}/snapshot"
        params = {
            "branch_code": branch_code,
            "status_filter": status_filter,
            "limit": limit,
            "offset": offset
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params, headers=self._get_headers())
            resp.raise_for_status()
            return resp.json()

    async def get_cttk_records(
        self,
        store_code: Optional[str] = None,
        only_positive: bool = True,
        limit: int = 5000,
        offset: int = 0
    ) -> Dict[str, Any]:
        """Fetch instant stock inventory records across warehouses & stores."""
        url = f"{self.base_url}/cttk-records"
        params: Dict[str, Any] = {
            "only_positive": str(only_positive).lower(),
            "limit": limit,
            "offset": offset
        }
        if store_code:
            params["store_code"] = store_code

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params, headers=self._get_headers())
            resp.raise_for_status()
            return resp.json()


class PoppiesMasterDataSyncService:
    """
    Service responsible for pulling Poppies ERP Master Data (Products, Prices, Categories)
    and CTTK Multi-Store Inventory into NAM-UMP database.
    """

    def __init__(self):
        self.client = PoppiesClient()

    async def check_health(self) -> Dict[str, Any]:
        """Get live health status of Poppies Master Data Gateway."""
        return await self.client.get_health()

    async def sync_master_data_from_poppies(
        self,
        db: AsyncSession,
        branch_code: str = "10001",
        status_filter: str = "01",
        limit: int = 500,
        offset: int = 0,
        fetch_all: bool = False,
        max_pages: int = 20
    ) -> Dict[str, Any]:
        """
        Pull SKU snapshot from Poppies ERP and synchronize into local PostgreSQL database.
        Inserts/updates:
        1. Categories (categories)
        2. Master Products (products)
        3. Multi-Store Physical Inventories (store_inventories) for 10001, 10004, 10005, 10006
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f"Starting Poppies Master Data sync: branch={branch_code}, status={status_filter}, limit={limit}, offset={offset}, fetch_all={fetch_all}")

        # 1. Load all physical stores from database
        stores_stmt = select(Store).order_by(Store.code)
        stores_res = await db.execute(stores_stmt)
        all_stores = stores_res.scalars().all()
        store_map_by_code: Dict[str, Store] = {s.code: s for s in all_stores}

        # 2. Cache existing categories to avoid redundant queries
        cat_stmt = select(Category)
        cat_res = await db.execute(cat_stmt)
        existing_categories = cat_res.scalars().all()
        cat_cache: Dict[str, Category] = {c.code: c for c in existing_categories}
        cat_name_cache: Dict[str, Category] = {c.name.strip().lower(): c for c in existing_categories}

        total_fetched = 0
        products_created = 0
        products_updated = 0
        categories_created = 0
        inventories_updated = 0

        current_offset = offset
        page_count = 0

        while True:
            page_count += 1
            logger.info(f"Fetching Poppies snapshot page {page_count}: offset={current_offset}, limit={limit}")
            
            try:
                snap_res = await self.client.get_snapshot(
                    branch_code=branch_code,
                    status_filter=status_filter,
                    limit=limit,
                    offset=current_offset
                )
            except Exception as ex:
                logger.error(f"Failed to fetch Poppies snapshot at offset {current_offset}: {ex}")
                raise

            items: List[Dict[str, Any]] = snap_res.get("items", [])
            total_available = snap_res.get("total", 0)
            if not items:
                break

            total_fetched += len(items)

            # Process each item in this batch
            for item in items:
                sku_code = str(item.get("skuCode") or "").strip()
                if not sku_code:
                    continue

                product_name = str(item.get("productName") or item.get("shortName") or sku_code).strip()
                raw_barcode = str(item.get("barcode") or "")
                barcodes_list = item.get("barcodes", [])
                if not raw_barcode and barcodes_list and isinstance(barcodes_list, list):
                    raw_barcode = str(barcodes_list[0].get("barcode") or "")

                unit_name = str(item.get("unitName") or "Cái").strip() or "Cái"
                pricing = item.get("pricing") or {}
                base_price = float(pricing.get("baseSalePriceVat") or pricing.get("baseNetSalePrice") or 0.0)

                ecommerce = item.get("ecommerce") or {}
                image_url = ecommerce.get("imageUrl")

                is_active = (str(item.get("status") or "01").strip() == "01")
                is_fresh = bool(item.get("isFresh", False))

                # Category Resolution
                cat_name = str(item.get("categoryName") or "Bách Hóa Tổng Hợp").strip()
                cat_code = slugify_category_code(cat_name)
                category = cat_cache.get(cat_code) or cat_name_cache.get(cat_name.lower())

                if not category:
                    category = Category(
                        code=cat_code,
                        name=cat_name,
                        sequence=len(cat_cache) + 1
                    )
                    db.add(category)
                    await db.flush()
                    cat_cache[cat_code] = category
                    cat_name_cache[cat_name.lower()] = category
                    categories_created += 1

                # Product Upsert
                prod_stmt = select(Product).where(Product.sku == sku_code)
                prod_res = await db.execute(prod_stmt)
                product = prod_res.scalar_one_or_none()

                metadata = {
                    "posCode": item.get("posCode"),
                    "codeType": item.get("codeType"),
                    "isFresh": is_fresh,
                    "brandName": item.get("brandName"),
                    "subcategoryName": item.get("subcategoryName"),
                    "countryOrigin": item.get("countryOrigin"),
                    "weightVolume": item.get("weightVolume"),
                    "pricing": pricing,
                    "tthh": item.get("tthh"),
                    "ecommerce": ecommerce,
                    "synced_from_poppies_at": datetime.now(timezone.utc).isoformat()
                }

                if not product:
                    product = Product(
                        sku=sku_code,
                        barcode=raw_barcode or None,
                        name=product_name,
                        description=item.get("shortName") or product_name,
                        unit=unit_name,
                        base_price=base_price,
                        image_url=image_url,
                        category_id=category.id,
                        is_active=is_active,
                        metadata_json=metadata
                    )
                    db.add(product)
                    await db.flush()
                    products_created += 1
                else:
                    product.barcode = raw_barcode or product.barcode
                    product.name = product_name
                    product.unit = unit_name
                    product.base_price = base_price
                    if image_url:
                        product.image_url = image_url
                    product.category_id = category.id
                    product.is_active = is_active
                    product.metadata_json = metadata
                    products_updated += 1

                # Multi-Store Inventory Calculation
                # Parse inventoryCttk details
                inv_cttk = item.get("inventoryCttk") or {}
                cttk_details: List[Dict[str, Any]] = inv_cttk.get("details", [])
                
                # Sum slTon by store code
                store_stock_sums: Dict[str, float] = {}
                for d in cttk_details:
                    sc = str(d.get("storeCode") or "").strip()
                    sl = float(d.get("slTon") or 0.0)
                    store_stock_sums[sc] = store_stock_sums.get(sc, 0.0) + sl

                # Check store branch locks (tthh.khoaRieng)
                tthh_info = item.get("tthh") or {}
                khoa_rieng_dict = tthh_info.get("khoaRieng") or {}

                # Upsert StoreInventory for each of our physical stores
                for sc, store_obj in store_map_by_code.items():
                    qty = store_stock_sums.get(sc, 0.0)
                    is_branch_locked = bool(khoa_rieng_dict.get(sc, False))

                    # If locked specifically for this branch or inactive overall
                    avail_qty = max(0.0, qty) if (not is_branch_locked and is_active) else 0.0
                    is_oos = (avail_qty <= 0.0 or is_branch_locked or not is_active)

                    # Look up existing inventory
                    inv_stmt = select(StoreInventory).where(
                        StoreInventory.store_id == store_obj.id,
                        StoreInventory.product_id == product.id
                    )
                    inv_res = await db.execute(inv_stmt)
                    store_inv = inv_res.scalar_one_or_none()

                    if not store_inv:
                        store_inv = StoreInventory(
                            store_id=store_obj.id,
                            product_id=product.id,
                            sku=product.sku,
                            total_stock=max(0.0, qty),
                            reserved_stock=0.0,
                            available_stock=avail_qty,
                            safety_stock=2.0,
                            is_out_of_stock=is_oos,
                            last_synced_at=datetime.now(timezone.utc)
                        )
                        db.add(store_inv)
                    else:
                        store_inv.total_stock = max(0.0, qty)
                        store_inv.available_stock = avail_qty
                        store_inv.is_out_of_stock = is_oos
                        store_inv.last_synced_at = datetime.now(timezone.utc)

                    inventories_updated += 1

            # Commit batch to database
            await db.commit()
            logger.info(f"Committed Poppies batch: offset={current_offset}, items={len(items)}, total_fetched={total_fetched}/{total_available}")

            if not fetch_all or page_count >= max_pages or (current_offset + len(items)) >= total_available:
                break

            current_offset += len(items)

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        result_summary = {
            "success": True,
            "branch_code": branch_code,
            "status_filter": status_filter,
            "total_fetched": total_fetched,
            "products_created": products_created,
            "products_updated": products_updated,
            "categories_created": categories_created,
            "inventories_updated": inventories_updated,
            "elapsed_seconds": round(elapsed, 2),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        logger.info(f"Completed Poppies Master Data sync: {result_summary}")
        return result_summary

    async def sync_stock_cttk_realtime(
        self,
        db: AsyncSession,
        store_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fast synchronization of real-time CTTK inventory stock across branches.
        Calls /cttk-records?only_positive=false and updates store_inventories.
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f"Starting real-time CTTK stock sync for store={store_code}")

        # 1. Load physical stores
        stores_stmt = select(Store).order_by(Store.code)
        stores_res = await db.execute(stores_stmt)
        all_stores = stores_res.scalars().all()
        store_map_by_code: Dict[str, Store] = {s.code: s for s in all_stores}

        # 2. Fetch CTTK records in chunks of 5000
        limit = 5000
        offset = 0
        total_records_processed = 0
        stock_updated_count = 0

        while True:
            data = await self.client.get_cttk_records(
                store_code=store_code,
                only_positive=False,
                limit=limit,
                offset=offset
            )
            items = data.get("items", [])
            if not items:
                break

            total_records_processed += len(items)

            # Group by (sku, storeCode)
            batch_sums: Dict[Tuple[str, str], float] = {}
            for row in items:
                sku = str(row.get("sku") or "").strip()
                sc = str(row.get("storeCode") or "").strip()
                sl = float(row.get("slTon") or 0.0)
                if sku and sc:
                    key = (sku, sc)
                    batch_sums[key] = batch_sums.get(key, 0.0) + sl

            # Update store_inventories for matched SKUs
            for (sku, sc), total_sl in batch_sums.items():
                store_obj = store_map_by_code.get(sc)
                if not store_obj:
                    continue

                inv_stmt = select(StoreInventory).where(
                    StoreInventory.store_id == store_obj.id,
                    StoreInventory.sku == sku
                )
                inv_res = await db.execute(inv_stmt)
                inv = inv_res.scalar_one_or_none()

                if inv:
                    inv.total_stock = max(0.0, total_sl)
                    inv.available_stock = max(0.0, total_sl)
                    inv.is_out_of_stock = (inv.available_stock <= 0.0)
                    inv.last_synced_at = datetime.now(timezone.utc)
                    stock_updated_count += 1

            await db.commit()
            if len(items) < limit:
                break
            offset += len(items)

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        return {
            "success": True,
            "store_code": store_code,
            "total_cttk_records": total_records_processed,
            "inventories_updated": stock_updated_count,
            "elapsed_seconds": round(elapsed, 2)
        }


poppies_service = PoppiesMasterDataSyncService()
