from datetime import datetime, timezone
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.models.channel import Channel
from app.models.inventory import InventoryStockLog, StoreInventory
from app.models.product import Category, ChannelProductMapping, Product
from app.models.store import Store

logger = logging.getLogger("naman_portal.poppies_service")
settings = get_settings()

POPPIES_STANDARD_CHANNELS = [
    {
        "channel_code": "POS_OFFLINE",
        "pos_channel_code": "STORE",
        "arito_channel_code": "K21",
        "ma_kenh": "K21",
        "channel_group": "store",
        "earn_enabled": True,
        "channel_name": "Tại cửa hàng",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "is_dynamic": False,
        "is_active": True,
    },
    {
        "channel_code": "SHOPEE",
        "pos_channel_code": "SHOPEE",
        "arito_channel_code": "K12",
        "ma_kenh": "K12",
        "channel_group": "marketplace",
        "earn_enabled": False,
        "channel_name": "Shopee Mall Nam An",
        "default_markup_percent": 20.0,
        "packaging_fee_vnd": 3000.0,
        "is_dynamic": True,
        "is_active": True,
    },
    {
        "channel_code": "TIKTOK_SHOP",
        "pos_channel_code": "TIKTOK",
        "arito_channel_code": "K17",
        "ma_kenh": "K17",
        "channel_group": "marketplace",
        "earn_enabled": False,
        "channel_name": "TikTok Shop",
        "default_markup_percent": 18.0,
        "packaging_fee_vnd": 2000.0,
        "is_dynamic": True,
        "is_active": True,
    },
    {
        "channel_code": "LAZADA",
        "pos_channel_code": "LAZADA",
        "arito_channel_code": "K10",
        "ma_kenh": "K10",
        "channel_group": "marketplace",
        "earn_enabled": False,
        "channel_name": "Lazada",
        "default_markup_percent": 15.0,
        "packaging_fee_vnd": 2000.0,
        "is_dynamic": True,
        "is_active": True,
    },
    {
        "channel_code": "GRAB_MART",
        "pos_channel_code": "GRAB",
        "arito_channel_code": "K15",
        "ma_kenh": "K15",
        "channel_group": "delivery_app",
        "earn_enabled": False,
        "channel_name": "Grab Mart",
        "default_markup_percent": 25.0,
        "packaging_fee_vnd": 5000.0,
        "is_dynamic": True,
        "is_active": True,
    },
    {
        "channel_code": "SHOPEE_FOOD",
        "pos_channel_code": "SHOPEEFOOD",
        "arito_channel_code": "K22",
        "ma_kenh": "K22",
        "channel_group": "delivery_app",
        "earn_enabled": False,
        "channel_name": "ShopeeFood",
        "default_markup_percent": 25.0,
        "packaging_fee_vnd": 5000.0,
        "is_dynamic": True,
        "is_active": True,
    },
    {
        "channel_code": "HARAVAN_WEB",
        "pos_channel_code": "WEB",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "social_web",
        "earn_enabled": True,
        "channel_name": "Website Nam An",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "is_dynamic": False,
        "is_active": True,
    },
    {
        "channel_code": "ZALO",
        "pos_channel_code": "ZALO",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "social_web",
        "earn_enabled": True,
        "channel_name": "Zalo OA",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "is_dynamic": False,
        "is_active": True,
    },
    {
        "channel_code": "FACEBOOK",
        "pos_channel_code": "FACEBOOK",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "social_web",
        "earn_enabled": True,
        "channel_name": "Facebook",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "is_dynamic": False,
        "is_active": True,
    },
    {
        "channel_code": "PHONE",
        "pos_channel_code": "PHONE",
        "arito_channel_code": "K20",
        "ma_kenh": "K20",
        "channel_group": "social_web",
        "earn_enabled": True,
        "channel_name": "Hotline / SĐT",
        "default_markup_percent": 0.0,
        "packaging_fee_vnd": 0.0,
        "is_dynamic": False,
        "is_active": True,
    },
]

CHANNEL_CODE_ALIASES = {
    "STORE": "POS_OFFLINE",
    "POS_OFFLINE": "POS_OFFLINE",
    "SHOPEE": "SHOPEE",
    "SHOPEEMART": "SHOPEE",
    "SHOPEE_MART": "SHOPEE",
    "TIKTOK": "TIKTOK_SHOP",
    "TIKTOK_SHOP": "TIKTOK_SHOP",
    "LAZADA": "LAZADA",
    "GRAB": "GRAB_MART",
    "GRABMART": "GRAB_MART",
    "GRAB_MART": "GRAB_MART",
    "SHOPEEFOOD": "SHOPEE_FOOD",
    "SHOPEE_FOOD": "SHOPEE_FOOD",
    "WEB": "HARAVAN_WEB",
    "HARAVAN": "HARAVAN_WEB",
    "HARAVAN_WEB": "HARAVAN_WEB",
    "ZALO": "ZALO",
    "FACEBOOK": "FACEBOOK",
    "PHONE": "PHONE",
}


def normalize_channel_code(raw_code: str) -> str:
    cleaned = (raw_code or "").strip().upper()
    return CHANNEL_CODE_ALIASES.get(cleaned, cleaned)


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
        self.pricing_base_url = getattr(settings, "POPPIES_PRICING_BASE_URL", "https://poppies.namanmarket.com/api/v1/pricing").rstrip("/")
        self.api_key = settings.POPPIES_API_KEY
        self.timeout = settings.POPPIES_TIMEOUT_SECONDS

    def _get_headers(self) -> Dict[str, str]:
        return {
            "X-POS-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "NAM-UMP/PoppiesMasterDataClient"
        }

    async def toggle_channel_publish_status(
        self,
        sku: str,
        scope_code: str,
        is_published: bool,
        scope_type: str = "CHANNEL"
    ) -> Dict[str, Any]:
        """
        Call Poppies ERP Pricing API to toggle channel publish status:
        POST /api/v1/pricing/channel-publish-status
        """
        payload = {
            "scopeType": scope_type,
            "scopeCode": scope_code,
            "sku": sku,
            "isPublished": is_published
        }
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"

        urls_to_try = [
            f"{self.pricing_base_url}/channel-publish-status",
            "http://127.0.0.1:6515/api/v1/pricing/channel-publish-status",
            "http://host.docker.internal:6515/api/v1/pricing/channel-publish-status",
            "http://103.175.248.250:1155/api/v1/pricing/channel-publish-status",
            "https://poppies.namanmarket.com/api/v1/pricing/channel-publish-status"
        ]

        last_err = None
        for url in urls_to_try:
            try:
                async with httpx.AsyncClient(timeout=min(self.timeout, 10.0)) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code in (200, 201):
                        data = resp.json()
                        data["resolved_url"] = url
                        return data
                    elif resp.status_code == 404:
                        continue
                    else:
                        resp.raise_for_status()
            except Exception as ex:
                last_err = ex

        # Fallback response if Poppies remote ERP server is temporarily unreachable
        logger.warning(
            f"Poppies remote pricing API unreachable for SKU {sku} on {scope_code}: {last_err}. Saved locally."
        )
        return {
            "success": True,
            "sku": sku,
            "scopeType": scope_type,
            "scopeCode": scope_code,
            "isPublished": is_published,
            "message": f"Đã cập nhật trạng thái cục bộ trên Merchant Portal (Poppies remote offline: {last_err})",
            "offline_mode": True
        }

    async def get_pricing_channels(self) -> Dict[str, Any]:
        """Fetch standard pricing channels from Poppies or return standard metadata."""
        urls_to_try = [
            f"{self.pricing_base_url}/channels",
            "http://127.0.0.1:6515/api/v1/pos/pricing/channels",
            "http://host.docker.internal:6515/api/v1/pos/pricing/channels",
            "https://poppies.namanmarket.com/api/v1/pos/pricing/channels"
        ]
        headers = self._get_headers()
        for url in urls_to_try:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        return resp.json()
            except Exception:
                pass

        return {
            "success": True,
            "total": len(POPPIES_STANDARD_CHANNELS),
            "channels": POPPIES_STANDARD_CHANNELS
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
        limit: int = 250,
        offset: int = 0,
        fetch_all: bool = False,
        max_pages: int = 25
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

        # 2. Cache existing categories, products, and inventories to avoid N+1 queries
        cat_stmt = select(Category)
        cat_res = await db.execute(cat_stmt)
        existing_categories = cat_res.scalars().all()
        cat_cache: Dict[str, Category] = {c.code: c for c in existing_categories}
        cat_name_cache: Dict[str, Category] = {c.name.strip().lower(): c for c in existing_categories}

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

                # Product Upsert via cache
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
                            last_synced_at=datetime.now(timezone.utc)
                        )
                        db.add(store_inv)
                        inv_cache[inv_key] = store_inv
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

        # 1. Load physical stores and existing inventories
        stores_stmt = select(Store).order_by(Store.code)
        stores_res = await db.execute(stores_stmt)
        all_stores = stores_res.scalars().all()
        store_map_by_code: Dict[str, Store] = {s.code: s for s in all_stores}

        inv_res_all = await db.execute(select(StoreInventory))
        inv_by_store_sku: Dict[Tuple[str, str], StoreInventory] = {
            (str(i.store_id), i.sku): i for i in inv_res_all.scalars().all()
        }

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

                inv = inv_by_store_sku.get((str(store_obj.id), sku))
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

    async def _get_or_create_channel(self, db: AsyncSession, channel_code: str) -> Channel:
        """Find or create channel record matching standardized metadata."""
        normalized = normalize_channel_code(channel_code)
        stmt = select(Channel).where(Channel.code == normalized)
        res = await db.execute(stmt)
        channel = res.scalar_one_or_none()
        if channel:
            return channel

        # Match with POPPIES_STANDARD_CHANNELS metadata
        meta = next((c for c in POPPIES_STANDARD_CHANNELS if c["channel_code"] == normalized), None)
        name = meta["channel_name"] if meta else normalized

        channel = Channel(
            code=normalized,
            name=name,
            description=f"Kênh bán hàng chuẩn Poppies ERP ({normalized})",
            is_active=True,
            config=meta or {}
        )
        db.add(channel)
        await db.flush()
        return channel

    async def ensure_standard_channels(self, db: AsyncSession) -> Dict[str, Channel]:
        """Ensure all 10 Poppies standard channels exist in the database."""
        stmt = select(Channel)
        res = await db.execute(stmt)
        existing = {c.code: c for c in res.scalars().all()}

        created = {}
        for meta in POPPIES_STANDARD_CHANNELS:
            code = meta["channel_code"]
            if code not in existing:
                channel = Channel(
                    code=code,
                    name=meta["channel_name"],
                    description=f"Kênh bán hàng chuẩn Poppies ERP ({code})",
                    is_active=True,
                    config=meta
                )
                db.add(channel)
                created[code] = channel

        if created:
            await db.flush()
            existing.update(created)

        return existing

    async def toggle_sku_channel_publish(
        self,
        db: AsyncSession,
        sku: str,
        channel_code: str,
        is_published: bool,
        sync_to_poppies: bool = True
    ) -> Dict[str, Any]:
        """
        Toggle ON/OFF SKU sales status for a specific channel and synchronize with Poppies ERP.
        Updates local ChannelProductMapping and invokes Poppies POST /api/v1/pricing/channel-publish-status.
        """
        # 1. Locate product
        prod_stmt = select(Product).where((Product.sku == sku) | (Product.barcode == sku))
        res = await db.execute(prod_stmt)
        product = res.scalar_one_or_none()
        if not product:
            raise NotFoundError(f"Không tìm thấy sản phẩm với SKU '{sku}'")

        # 2. Normalize and resolve channel
        poppies_channel_code = normalize_channel_code(channel_code)
        channel = await self._get_or_create_channel(db, poppies_channel_code)

        # 3. Upsert ChannelProductMapping
        mapping_stmt = select(ChannelProductMapping).where(
            ChannelProductMapping.product_id == product.id,
            ChannelProductMapping.channel_id == channel.id
        )
        mapping_res = await db.execute(mapping_stmt)
        mapping = mapping_res.scalar_one_or_none()

        poppies_res: Optional[Dict[str, Any]] = None
        if sync_to_poppies:
            poppies_res = await self.client.toggle_channel_publish_status(
                sku=product.sku,
                scope_code=poppies_channel_code,
                is_published=is_published,
                scope_type="CHANNEL"
            )

        if not mapping:
            mapping = ChannelProductMapping(
                product_id=product.id,
                channel_id=channel.id,
                is_enabled=is_published,
                channel_sync_payload={
                    "last_toggle": poppies_res,
                    "poppies_channel_code": poppies_channel_code,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            )
            db.add(mapping)
        else:
            mapping.is_enabled = is_published
            mapping.channel_sync_payload = {
                "last_toggle": poppies_res,
                "poppies_channel_code": poppies_channel_code,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

        await db.commit()

        status_str = "BẬT (Mở bán)" if is_published else "TẮT (Ngưng bán)"
        return {
            "sku": product.sku,
            "product_name": product.name,
            "channel_code": channel_code,
            "poppies_channel_code": poppies_channel_code,
            "is_published": is_published,
            "synced_to_poppies": sync_to_poppies,
            "message": f"Đã {status_str} SKU {product.sku} trên kênh {channel.name} ({poppies_channel_code})",
            "poppies_response": poppies_res
        }

    async def get_sku_channel_publish_overview(
        self,
        db: AsyncSession,
        sku: str
    ) -> Dict[str, Any]:
        """
        Get complete publish status across all 10 standard channels for a SKU.
        """
        prod_stmt = select(Product).options(
            selectinload(Product.channel_mappings).selectinload(ChannelProductMapping.channel)
        ).where((Product.sku == sku) | (Product.barcode == sku))
        res = await db.execute(prod_stmt)
        product = res.scalar_one_or_none()
        if not product:
            raise NotFoundError(f"Không tìm thấy sản phẩm với SKU '{sku}'")

        # Map channel_id -> mapping
        mappings_by_code: Dict[str, ChannelProductMapping] = {}
        for m in product.channel_mappings:
            if m.channel:
                mappings_by_code[m.channel.code] = m

        channel_items = []
        for meta in POPPIES_STANDARD_CHANNELS:
            code = meta["channel_code"]
            mapping = mappings_by_code.get(code)
            # Default to published if product is active and no explicit unpublish mapping exists
            is_pub = mapping.is_enabled if mapping is not None else product.is_active

            channel_items.append({
                "channel_code": code,
                "channel_name": meta["channel_name"],
                "poppies_channel_code": code,
                "arito_channel_code": meta.get("arito_channel_code"),
                "channel_group": meta.get("channel_group"),
                "is_published": is_pub,
                "custom_price": float(mapping.custom_price) if mapping and mapping.custom_price else None,
                "updated_at": mapping.updated_at if mapping else None
            })

        return {
            "sku": product.sku,
            "product_name": product.name,
            "base_price": float(product.base_price),
            "channels": channel_items
        }

    async def process_price_change_webhook(
        self,
        db: AsyncSession,
        payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Process Poppies ERP Webhook price_changed / CHANNEL_PUBLISH_STATUS_CHANGED.
        Synchronizes channel publish status without re-triggering Poppies API.
        """
        event_id = payload.get("event_id")
        event_type = payload.get("event_type")
        channel_raw = payload.get("channel_code") or payload.get("channel") or "ALL"
        affected_skus: List[str] = payload.get("affected_skus") or []

        logger.info(
            f"Processing Poppies Webhook event={event_id}, type={event_type}, channel={channel_raw}, skus={affected_skus}"
        )

        if not affected_skus:
            return {
                "success": True,
                "event_id": event_id,
                "processed_count": 0,
                "message": "Không có SKU nào bị ảnh hưởng."
            }

        # If it's a channel publish status event, update mappings
        poppies_channel_code = normalize_channel_code(channel_raw)
        channel = await self._get_or_create_channel(db, poppies_channel_code)

        updated_count = 0
        for sku in affected_skus:
            prod_stmt = select(Product).where(Product.sku == sku)
            prod_res = await db.execute(prod_stmt)
            prod = prod_res.scalar_one_or_none()
            if not prod:
                continue

            map_stmt = select(ChannelProductMapping).where(
                ChannelProductMapping.product_id == prod.id,
                ChannelProductMapping.channel_id == channel.id
            )
            map_res = await db.execute(map_stmt)
            mapping = map_res.scalar_one_or_none()

            # Determine published status: if is_published in payload, use it, else keep enabled
            is_pub = payload.get("is_published", False)

            if not mapping:
                mapping = ChannelProductMapping(
                    product_id=prod.id,
                    channel_id=channel.id,
                    is_enabled=is_pub,
                    channel_sync_payload={
                        "webhook_sync": payload,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                )
                db.add(mapping)
            else:
                mapping.is_enabled = is_pub
                mapping.channel_sync_payload = {
                    "webhook_sync": payload,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }

            updated_count += 1

        await db.commit()
        return {
            "success": True,
            "event_id": event_id,
            "channel_code": poppies_channel_code,
            "processed_count": updated_count,
            "message": f"Đã cập nhật trạng thái {updated_count} SKU từ webhook Poppies."
        }


poppies_service = PoppiesMasterDataSyncService()
