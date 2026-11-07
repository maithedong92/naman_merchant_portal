"""
Poppies ERP Master Data & CTTK Multi-Store Inventory Synchronization Service.
Combines Master Data / CTTK pull sync with Canonical 13-Channel Pricing Governance.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.models.inventory import StoreInventory
from app.models.product import Category, Product
from app.models.store import Store
from app.services.poppies_channels import (
    CANONICAL_CHANNEL_META_MAP,
    CHANNEL_CODE_ALIASES,
    POPPIES_STANDARD_CHANNELS,
    consolidate_channel_records,
    get_canonical_channel_meta,
    get_channel_family_codes,
    normalize_channel_code,
    slugify_category_code,
)
from app.services.poppies_client import PoppiesClient, PoppiesUpstreamError
from app.services.poppies_matching import PoppiesMatchingMixin
from app.services.poppies_pricing_sync import PoppiesPricingGovernanceMixin

logger = logging.getLogger("naman_portal.poppies_service")

__all__ = [
    "POPPIES_STANDARD_CHANNELS",
    "CHANNEL_CODE_ALIASES",
    "CANONICAL_CHANNEL_META_MAP",
    "normalize_channel_code",
    "get_canonical_channel_meta",
    "get_channel_family_codes",
    "consolidate_channel_records",
    "slugify_category_code",
    "PoppiesClient",
    "PoppiesUpstreamError",
    "PoppiesMasterDataSyncService",
    "poppies_service",
]


class PoppiesMasterDataSyncService(PoppiesMatchingMixin, PoppiesPricingGovernanceMixin):
    """
    Service responsible for pulling Poppies ERP Master Data (Products, Prices, Categories),
    CTTK Multi-Store Inventory, and Multi-Channel Pricing Snapshots into NAM-UMP.
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
        limit: int = 250,
        offset: int = 0,
        fetch_all: bool = False,
        max_pages: int = 25,
    ) -> Dict[str, Any]:
        """
        Pull SKU snapshot from Poppies ERP and synchronize into local PostgreSQL database.
        Inserts/updates:
        1. Categories (categories)
        2. Master Products (products)
        3. Multi-Store Physical Inventories (store_inventories) for 10001, 10004, 10005, 10006
        """
        start_time = datetime.now(timezone.utc)
        logger.info(
            f"Starting Poppies Master Data sync: branch={branch_code}, status={status_filter}, "
            f"limit={limit}, offset={offset}, fetch_all={fetch_all}"
        )

        # 1. Load all physical stores from database
        stores_stmt = select(Store).order_by(Store.code)
        stores_res = await db.execute(stores_stmt)
        all_stores = stores_res.scalars().all()
        store_map_by_code: Dict[str, Store] = {s.code: s for s in all_stores}

        # 2. Cache existing categories, products, and inventories to avoid N+1 queries
        cat_stmt = select(Category)
        cat_res = await db.execute(cat_stmt)
        existing_categories = cat_res.scalars().all()
        cat_cache: Dict[str, Category] = {c.code: c for c in existing_categories}
        cat_name_cache: Dict[str, Category] = {
            c.name.strip().lower(): c for c in existing_categories
        }

        prod_res_all = await db.execute(select(Product))
        prod_cache: Dict[str, Product] = {p.sku: p for p in prod_res_all.scalars().all()}

        inv_res_all = await db.execute(select(StoreInventory))
        inv_cache: Dict[Tuple[str, str], StoreInventory] = {
            (str(i.store_id), str(i.product_id)): i for i in inv_res_all.scalars().all()
        }

        total_fetched = 0
        products_created = 0
        products_updated = 0
        categories_created = 0
        inventories_updated = 0

        current_offset = offset
        page_count = 0

        while True:
            page_count += 1
            logger.info(
                f"Fetching Poppies snapshot page {page_count}: offset={current_offset}, limit={limit}"
            )

            try:
                snap_res = await self.client.get_snapshot(
                    branch_code=branch_code,
                    status_filter=status_filter,
                    limit=limit,
                    offset=current_offset,
                )
            except Exception as ex:
                if page_count == 1:
                    logger.warning(
                        f"Poppies master-data/snapshot unavailable ({ex}); "
                        "falling back to STORE pricing snapshot + CTTK records."
                    )
                    return await self.sync_catalog_from_store_pricing_snapshot(
                        db=db, branch_code=branch_code
                    )
                logger.error(f"Failed to fetch Poppies snapshot at offset {current_offset}: {ex}")
                raise

            items: List[Dict[str, Any]] = snap_res.get("items", [])
            total_available = snap_res.get("total", 0)
            if not items:
                break

            total_fetched += len(items)

            for item in items:
                sku_code = str(item.get("skuCode") or "").strip()
                if not sku_code:
                    continue

                product_name = str(
                    item.get("productName") or item.get("shortName") or sku_code
                ).strip()
                raw_barcode = str(item.get("barcode") or "").strip()
                barcodes_list = item.get("barcodes", [])
                if not raw_barcode and barcodes_list and isinstance(barcodes_list, list):
                    raw_barcode = str(barcodes_list[0].get("barcode") or "").strip()

                unit_name = str(item.get("unitName") or "Cái").strip() or "Cái"
                pricing = item.get("pricing") or {}
                base_price = float(
                    pricing.get("baseSalePriceVat") or pricing.get("baseNetSalePrice") or 0.0
                )

                ecommerce = item.get("ecommerce") or {}
                image_url = ecommerce.get("imageUrl")

                is_active = str(item.get("status") or "01").strip() == "01"
                is_fresh = bool(item.get("isFresh", False))

                # Category Resolution
                cat_name = str(item.get("categoryName") or "Bách Hóa Tổng Hợp").strip()
                cat_code = slugify_category_code(cat_name)
                category = cat_cache.get(cat_code) or cat_name_cache.get(cat_name.lower())

                if not category:
                    category = Category(
                        code=cat_code,
                        name=cat_name,
                        sequence=len(cat_cache) + 1,
                    )
                    db.add(category)
                    await db.flush()
                    cat_cache[cat_code] = category
                    cat_name_cache[cat_name.lower()] = category
                    categories_created += 1

                product = prod_cache.get(sku_code)

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
                    "synced_from_poppies_at": datetime.now(timezone.utc).isoformat(),
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
                        metadata_json=metadata,
                    )
                    db.add(product)
                    await db.flush()
                    prod_cache[sku_code] = product
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

                # Multi-Store Inventory Calculation (respecting tthh.khoaRieng & status=="01")
                inv_cttk = item.get("inventoryCttk") or {}
                cttk_details: List[Dict[str, Any]] = inv_cttk.get("details", [])

                store_stock_sums: Dict[str, float] = {}
                for d in cttk_details:
                    sc = str(d.get("storeCode") or "").strip()
                    sl = float(d.get("slTon") or 0.0)
                    store_stock_sums[sc] = store_stock_sums.get(sc, 0.0) + sl

                tthh_info = item.get("tthh") or {}
                khoa_rieng_dict = tthh_info.get("khoaRieng") or {}

                for sc, store_obj in store_map_by_code.items():
                    qty = store_stock_sums.get(sc, 0.0)
                    is_branch_locked = bool(khoa_rieng_dict.get(sc, False))

                    avail_qty = max(0.0, qty) if (not is_branch_locked and is_active) else 0.0
                    is_oos = avail_qty <= 0.0 or is_branch_locked or not is_active

                    inv_key = (str(store_obj.id), str(product.id))
                    store_inv = inv_cache.get(inv_key)

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
                            last_synced_at=datetime.now(timezone.utc),
                        )
                        db.add(store_inv)
                        inv_cache[inv_key] = store_inv
                    else:
                        store_inv.total_stock = max(0.0, qty)
                        store_inv.available_stock = avail_qty
                        store_inv.is_out_of_stock = is_oos
                        store_inv.last_synced_at = datetime.now(timezone.utc)

                    inventories_updated += 1

            await db.commit()
            logger.info(
                f"Committed Poppies batch: offset={current_offset}, items={len(items)}, "
                f"total_fetched={total_fetched}/{total_available}"
            )

            if (
                not fetch_all
                or page_count >= max_pages
                or (current_offset + len(items)) >= total_available
            ):
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(f"Completed Poppies Master Data sync: {result_summary}")
        return result_summary

    async def sync_stock_cttk_realtime(
        self,
        db: AsyncSession,
        store_code: Optional[str] = None,
        create_missing_inventories: bool = False,
        batch_size: int = 5000,
    ) -> Dict[str, Any]:
        """
        Fast synchronization of real-time CTTK inventory stock across branches.
        Calls /cttk-records?only_positive=false and updates store_inventories
        only for rows whose stock or availability actually changed.
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f"Starting real-time CTTK stock sync for store={store_code}")

        stores_stmt = select(Store).order_by(Store.code)
        stores_res = await db.execute(stores_stmt)
        all_stores = stores_res.scalars().all()
        store_map_by_code: Dict[str, Store] = {s.code: s for s in all_stores}

        prod_by_sku: Dict[str, Product] = {}
        if create_missing_inventories:
            prod_res = await db.execute(
                select(Product).options(
                    defer(Product.metadata_json),
                    defer(Product.description),
                    defer(Product.image_url),
                )
            )
            prod_by_sku = {p.sku: p for p in prod_res.scalars().all()}

        inv_res_all = await db.execute(select(StoreInventory))
        inv_by_store_sku: Dict[Tuple[str, str], StoreInventory] = {
            (str(i.store_id), i.sku): i for i in inv_res_all.scalars().all()
        }

        limit = max(100, int(batch_size or 5000))
        offset = 0
        total_records_processed = 0
        stock_updated_count = 0
        units_updated_count = 0
        global_sums: Dict[Tuple[str, str], float] = {}
        unit_by_sku: Dict[str, str] = {}

        while True:
            data = await self.client.get_cttk_records(
                store_code=store_code,
                only_positive=False,
                limit=limit,
                offset=offset,
            )
            items = data.get("items", [])
            if not items:
                break

            total_records_processed += len(items)

            for row in items:
                sku = str(row.get("sku") or "").strip()
                sc = str(row.get("storeCode") or "").strip()
                sl = float(row.get("slTon") or 0.0)
                u_name = str(row.get("unitName") or "").strip()
                if sku and u_name and sku not in unit_by_sku:
                    unit_by_sku[sku] = u_name
                if sku and sc:
                    key = (sku, sc)
                    global_sums[key] = global_sums.get(key, 0.0) + sl

            if len(items) < limit:
                break
            offset += len(items)

        if prod_by_sku and unit_by_sku:
            for sku_k, u_val in unit_by_sku.items():
                p_obj = prod_by_sku.get(sku_k)
                if p_obj and u_val and p_obj.unit in ("Cái", ""):
                    p_obj.unit = u_val[:50]
                    units_updated_count += 1

        now_utc = datetime.now(timezone.utc)
        availability_changed_skus: List[str] = []
        seen_changed_skus = set()
        for (sku, sc), total_sl in global_sums.items():
            store_obj = store_map_by_code.get(sc)
            if not store_obj:
                continue

            inv = inv_by_store_sku.get((str(store_obj.id), sku))
            if inv:
                prev_oos = bool(inv.is_out_of_stock)
                new_total = max(0.0, total_sl)
                new_avail = max(0.0, total_sl - float(inv.reserved_stock or 0.0))
                new_oos = new_avail <= 0.0
                if (
                    abs(new_total - float(inv.total_stock or 0.0)) >= 0.001
                    or abs(new_avail - float(inv.available_stock or 0.0)) >= 0.001
                    or new_oos != prev_oos
                ):
                    inv.total_stock = new_total
                    inv.available_stock = new_avail
                    inv.is_out_of_stock = new_oos
                    inv.last_synced_at = now_utc
                    stock_updated_count += 1
                    if new_oos != prev_oos and sku not in seen_changed_skus:
                        seen_changed_skus.add(sku)
                        availability_changed_skus.append(sku)
            elif create_missing_inventories and sku in prod_by_sku:
                p_obj = prod_by_sku[sku]
                avail = max(0.0, total_sl)
                new_inv = StoreInventory(
                    store_id=store_obj.id,
                    product_id=p_obj.id,
                    sku=sku,
                    total_stock=avail,
                    reserved_stock=0.0,
                    available_stock=avail,
                    safety_stock=2.0,
                    is_out_of_stock=(avail <= 0.0),
                    last_synced_at=now_utc,
                )
                db.add(new_inv)
                inv_by_store_sku[(str(store_obj.id), sku)] = new_inv
                stock_updated_count += 1

        if stock_updated_count > 0 or units_updated_count > 0:
            await db.commit()

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        return {
            "success": True,
            "store_code": store_code,
            "total_cttk_records": total_records_processed,
            "inventories_updated": stock_updated_count,
            "availability_changed_skus": availability_changed_skus,
            "elapsed_seconds": round(elapsed, 2),
        }


poppies_service = PoppiesMasterDataSyncService()
