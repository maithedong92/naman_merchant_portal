"""
Poppies ERP <-> NAM-UMP Channel, Store Outlet, Master SKU & Pricing Matching Engine.

Provides:
1. Canonical Channel Reconciliation & Safe Legacy Alias Migration (e.g. GRABMART -> GRAB, POS_OFFLINE -> STORE)
   without losing StoreChannelMapping, ChannelProductMapping, UnifiedOrder history, or Admin feature flags.
2. Resilient Master SKU Catalog Fallback via STORE Pricing Snapshot + CTTK Records when
   `/api/v1/pos/master-data/snapshot` is unavailable.
3. Full 1-Click End-to-End Synchronization & Matching Report (`sync_and_match_all_from_poppies`).
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.order import UnifiedOrder
from app.models.product import Category, ChannelProductMapping, Product
from app.models.store import Store, StoreChannelMapping
from app.services.channel_registry import channel_registry
from app.services.channel_service import DEFAULT_FEATURES, DEFAULT_STORES
from app.services.poppies_channel_push import propagate_channel_menu_to_external_channel
from app.services.poppies_channels import (
    get_channel_family_codes,
    normalize_channel_code,
    slugify_category_code,
)
from app.services.poppies_client import PoppiesClient, PoppiesUpstreamError
from app.services.poppies_webhook_handler import _safe_flag_modified

logger = logging.getLogger("naman_portal.poppies_matching")
settings = get_settings()

ADAPTER_DEFAULT_CONFIGS: Dict[str, Dict[str, Any]] = {
    "SHOPEEFOOD": {
        "base_url": settings.SHOPEEFOOD_BASE_URL,
        "app_id": settings.SHOPEEFOOD_APP_ID,
    },
    "GRAB": {
        "base_url": settings.GRABMART_BASE_URL,
        "client_id": settings.GRABMART_CLIENT_ID,
    },
    "SHOPEEMART": {
        "base_url": settings.SHOPEEMART_BASE_URL,
    },
}


def _classify_integration_mode(canon_code: str, has_adapter: bool) -> str:
    if has_adapter:
        return "S2S_ADAPTER"
    if canon_code == "STORE":
        return "POS_BASE"
    return "OMNICHANNEL_ORDER_FEED"


class PoppiesMatchingMixin:
    """
    Mixin providing Poppies ERP Channel Matching, Legacy Alias Consolidation,
    Resilient Catalog Sync, and End-to-End Reconciliation Reporting.
    """

    client: PoppiesClient

    async def _merge_alias_channel_into_canonical(
        self,
        db: AsyncSession,
        alias_ch: Channel,
        canon_ch: Channel,
    ) -> Dict[str, int]:
        """
        Safely migrate all foreign-key references (StoreChannelMapping, ChannelProductMapping, UnifiedOrder)
        from a legacy alias Channel row (e.g. GRABMART) into the Canonical Channel row (e.g. GRAB),
        preserving Admin feature toggles and avoiding unique index collisions.
        """
        stats = {
            "store_mappings_migrated": 0,
            "product_mappings_migrated": 0,
            "orders_migrated": 0,
        }

        # 1. Preserve Admin feature toggles / credentials on canonical config if not set yet
        canon_cfg = dict(canon_ch.config or {})
        alias_cfg = dict(alias_ch.config or {})
        for preserve_key in (
            "order_webhook_enabled",
            "auto_confirm_enabled",
            "stock_sync_enabled",
            "menu_sync_enabled",
            "app_id",
            "client_id",
        ):
            if preserve_key in alias_cfg and preserve_key not in canon_cfg:
                canon_cfg[preserve_key] = alias_cfg[preserve_key]
        canon_ch.config = canon_cfg
        _safe_flag_modified(canon_ch, "config")
        if not canon_ch.base_url and alias_ch.base_url:
            canon_ch.base_url = alias_ch.base_url
        if not canon_ch.webhook_secret and alias_ch.webhook_secret:
            canon_ch.webhook_secret = alias_ch.webhook_secret

        # 2. Migrate StoreChannelMapping rows (unique on store_id + channel_id)
        canon_sm_res = await db.execute(
            select(StoreChannelMapping).where(StoreChannelMapping.channel_id == canon_ch.id)
        )
        canon_sm_by_store = {str(m.store_id): m for m in canon_sm_res.scalars().all()}

        alias_sm_res = await db.execute(
            select(StoreChannelMapping).where(StoreChannelMapping.channel_id == alias_ch.id)
        )
        for a_sm in alias_sm_res.scalars().all():
            existing_c_sm = canon_sm_by_store.get(str(a_sm.store_id))
            if existing_c_sm is None:
                a_sm.channel_id = canon_ch.id
                canon_sm_by_store[str(a_sm.store_id)] = a_sm
                stats["store_mappings_migrated"] += 1
            else:
                # Keep custom partner_store_id if alias had a real partner ID and canonical only had store_code
                if a_sm.partner_store_id and not existing_c_sm.partner_store_id:
                    existing_c_sm.partner_store_id = a_sm.partner_store_id
                await db.delete(a_sm)

        # 3. Migrate ChannelProductMapping rows (unique on product_id + channel_id)
        canon_pm_res = await db.execute(
            select(ChannelProductMapping).where(ChannelProductMapping.channel_id == canon_ch.id)
        )
        canon_pm_by_prod = {str(m.product_id): m for m in canon_pm_res.scalars().all()}

        alias_pm_res = await db.execute(
            select(ChannelProductMapping).where(ChannelProductMapping.channel_id == alias_ch.id)
        )
        for a_pm in alias_pm_res.scalars().all():
            existing_c_pm = canon_pm_by_prod.get(str(a_pm.product_id))
            if existing_c_pm is None:
                a_pm.channel_id = canon_ch.id
                canon_pm_by_prod[str(a_pm.product_id)] = a_pm
                stats["product_mappings_migrated"] += 1
            else:
                if not existing_c_pm.channel_item_id and a_pm.channel_item_id:
                    existing_c_pm.channel_item_id = a_pm.channel_item_id
                await db.delete(a_pm)

        # 4. Migrate UnifiedOrder rows
        ord_res = await db.execute(
            update(UnifiedOrder)
            .where(UnifiedOrder.channel_id == alias_ch.id)
            .values(channel_id=canon_ch.id)
        )
        stats["orders_migrated"] = int(getattr(ord_res, "rowcount", 0) or 0)

        await db.flush()
        await db.delete(alias_ch)
        await db.flush()
        return stats

    async def reconcile_channels_from_poppies(
        self,
        db: AsyncSession,
        sync_store_outlets: bool = True,
    ) -> Dict[str, Any]:
        """
        Pull live Canonical channels from Poppies ERP (`GET /api/v1/pos/pricing/channels`)
        and accurately reconcile NAM-UMP's `channels` and `store_channel_mappings` tables.
        """
        poppies_resp = await self.client.get_pricing_channels()
        poppies_channels: List[Dict[str, Any]] = poppies_resp.get("channels") or []
        upstream_source = poppies_resp.get("source", "poppies_live")
        resolved_url = poppies_resp.get("resolved_url")

        ch_res = await db.execute(select(Channel))
        all_local_channels = list(ch_res.scalars().all())
        by_code: Dict[str, Channel] = {str(c.code).upper(): c for c in all_local_channels if c.code}

        channels_created = 0
        channels_updated = 0
        legacy_merged: List[Dict[str, Any]] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        reconciled_by_code: Dict[str, Channel] = {}

        for ch_meta in poppies_channels:
            canon_code = normalize_channel_code(ch_meta.get("channel_code"))
            if not canon_code:
                continue

            family_codes = list(get_channel_family_codes(canon_code))
            leg_code = str(ch_meta.get("legacy_channel_code") or "").strip().upper()
            if leg_code and leg_code not in family_codes:
                family_codes.append(leg_code)

            alias_codes = [c for c in family_codes if c != canon_code]
            canon_ch = by_code.get(canon_code)
            found_aliases = [by_code[ac] for ac in alias_codes if ac in by_code]

            # Step A: Rename or merge legacy aliases into Canonical channel
            if canon_ch is None and found_aliases:
                primary_alias = found_aliases[0]
                old_code = primary_alias.code
                by_code.pop(old_code.upper(), None)
                primary_alias.code = canon_code
                await db.flush()
                canon_ch = primary_alias
                by_code[canon_code] = canon_ch
                legacy_merged.append(
                    {
                        "from_code": old_code,
                        "to_code": canon_code,
                        "action": "renamed_in_place",
                    }
                )
                for extra_alias in found_aliases[1:]:
                    old_extra = extra_alias.code
                    by_code.pop(old_extra.upper(), None)
                    m_stats = await self._merge_alias_channel_into_canonical(db, extra_alias, canon_ch)
                    legacy_merged.append(
                        {
                            "from_code": old_extra,
                            "to_code": canon_code,
                            "action": "merged_into_canonical",
                            **m_stats,
                        }
                    )
            elif canon_ch is not None and found_aliases:
                for alias_obj in found_aliases:
                    old_alias = alias_obj.code
                    by_code.pop(old_alias.upper(), None)
                    m_stats = await self._merge_alias_channel_into_canonical(db, alias_obj, canon_ch)
                    legacy_merged.append(
                        {
                            "from_code": old_alias,
                            "to_code": canon_code,
                            "action": "merged_into_canonical",
                            **m_stats,
                        }
                    )

            # Step B: Create or update Canonical Channel metadata from Poppies ERP
            adapter_defaults = ADAPTER_DEFAULT_CONFIGS.get(canon_code, {})
            has_partner_pricing = bool(ch_meta.get("has_partner_pricing", False))
            inherited_from = (
                None if has_partner_pricing else (ch_meta.get("inherited_from") or "STORE")
            )

            poppies_cfg_patch = {
                "channel_code": canon_code,
                "legacy_channel_code": ch_meta.get("legacy_channel_code"),
                "arito_channel_code": ch_meta.get("arito_channel_code") or ch_meta.get("ma_kenh"),
                "ma_kenh": ch_meta.get("arito_channel_code") or ch_meta.get("ma_kenh"),
                "channel_group": ch_meta.get("channel_group"),
                "earn_enabled": bool(ch_meta.get("earn_enabled", False)),
                "has_partner_pricing": has_partner_pricing,
                "inherited_from": inherited_from,
                "default_markup_percent": float(ch_meta.get("default_markup_percent") or 0.0),
                "packaging_fee_vnd": float(ch_meta.get("packaging_fee_vnd") or 0.0),
                "rounding_rule": ch_meta.get("rounding_rule"),
                "restrict_to_priced_items": bool(ch_meta.get("restrict_to_priced_items", False)),
                "aliases": alias_codes,
                "poppies_updated_at": ch_meta.get("updated_at"),
                "poppies_synced_at": now_iso,
            }

            if canon_ch is None:
                init_cfg = {**DEFAULT_FEATURES, **poppies_cfg_patch}
                if "app_id" in adapter_defaults:
                    init_cfg["app_id"] = adapter_defaults["app_id"]
                if "client_id" in adapter_defaults:
                    init_cfg["client_id"] = adapter_defaults["client_id"]

                canon_ch = Channel(
                    code=canon_code,
                    name=ch_meta.get("channel_name") or canon_code,
                    description=(
                        f"Đồng bộ chuẩn Canonical từ Poppies ERP ({canon_code} • "
                        f"Arito {poppies_cfg_patch['arito_channel_code'] or 'N/A'})"
                    ),
                    base_url=adapter_defaults.get("base_url"),
                    is_active=bool(ch_meta.get("is_active", True)),
                    config=init_cfg,
                )
                db.add(canon_ch)
                await db.flush()
                by_code[canon_code] = canon_ch
                channels_created += 1
            else:
                if ch_meta.get("channel_name"):
                    canon_ch.name = ch_meta["channel_name"]
                canon_ch.is_active = bool(ch_meta.get("is_active", canon_ch.is_active))
                if not canon_ch.base_url and adapter_defaults.get("base_url"):
                    canon_ch.base_url = adapter_defaults["base_url"]
                merged_cfg = {**DEFAULT_FEATURES, **(canon_ch.config or {}), **poppies_cfg_patch}
                canon_ch.config = merged_cfg
                _safe_flag_modified(canon_ch, "config")
                channels_updated += 1

            reconciled_by_code[canon_code] = canon_ch

        # Step C: Ensure 4 Nam An Market stores & S2S Adapter StoreChannelMappings
        store_mappings_created = 0
        if sync_store_outlets:
            st_res = await db.execute(select(Store).order_by(Store.code))
            stores_by_code = {s.code: s for s in st_res.scalars().all()}
            for st_def in DEFAULT_STORES:
                if st_def["code"] not in stores_by_code:
                    new_st = Store(
                        code=st_def["code"],
                        name=st_def["name"],
                        address=st_def["address"],
                        phone=st_def["phone"],
                        is_active=True,
                    )
                    db.add(new_st)
                    await db.flush()
                    stores_by_code[new_st.code] = new_st

            existing_sm_res = await db.execute(select(StoreChannelMapping))
            existing_sm_pairs = {
                (str(m.store_id), str(m.channel_id)) for m in existing_sm_res.scalars().all()
            }

            for canon_code, ch_obj in reconciled_by_code.items():
                adapter = channel_registry.get(canon_code)
                if not adapter:
                    continue
                for st_code, st_obj in stores_by_code.items():
                    if not st_obj.is_active:
                        continue
                    pair = (str(st_obj.id), str(ch_obj.id))
                    if pair not in existing_sm_pairs:
                        db.add(
                            StoreChannelMapping(
                                store_id=st_obj.id,
                                channel_id=ch_obj.id,
                                partner_store_id=st_code,
                                is_active=True,
                            )
                        )
                        existing_sm_pairs.add(pair)
                        store_mappings_created += 1

        await db.commit()

        matching_report = await self.get_poppies_channel_matching_report(
            db=db, preloaded_poppies=poppies_resp
        )
        return {
            "success": True,
            "source": upstream_source,
            "resolved_url": resolved_url,
            "total_poppies_channels": len(poppies_channels),
            "channels_created": channels_created,
            "channels_updated": channels_updated,
            "legacy_channels_merged": legacy_merged,
            "store_mappings_created": store_mappings_created,
            "synced_at": now_iso,
            "matching": matching_report,
        }

    async def get_poppies_channel_matching_report(
        self,
        db: AsyncSession,
        preloaded_poppies: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Compare live Poppies ERP Canonical channels against local UMP channels, adapters,
        store outlet mappings, and SKU pricing/exclusion overrides.
        """
        poppies_resp = preloaded_poppies or await self.client.get_pricing_channels()
        poppies_channels: List[Dict[str, Any]] = poppies_resp.get("channels") or []

        ch_res = await db.execute(select(Channel))
        local_channels = list(ch_res.scalars().all())
        by_code: Dict[str, Channel] = {str(c.code).upper(): c for c in local_channels if c.code}

        sm_res = await db.execute(select(StoreChannelMapping))
        sm_list = list(sm_res.scalars().all())
        sm_count_by_ch: Dict[str, int] = {}
        for m in sm_list:
            if m.is_active:
                cid = str(m.channel_id)
                sm_count_by_ch[cid] = sm_count_by_ch.get(cid, 0) + 1

        pm_res = await db.execute(select(ChannelProductMapping))
        pm_list = list(pm_res.scalars().all())
        override_count_by_ch: Dict[str, int] = {}
        blocked_count_by_ch: Dict[str, int] = {}
        for pm in pm_list:
            cid = str(pm.channel_id)
            if pm.custom_price is not None:
                override_count_by_ch[cid] = override_count_by_ch.get(cid, 0) + 1
            if not pm.is_enabled:
                blocked_count_by_ch[cid] = blocked_count_by_ch.get(cid, 0) + 1

        total_local_products = int(
            (await db.execute(select(func.count(Product.id)))).scalar_one() or 0
        )

        items: List[Dict[str, Any]] = []
        matched_count = 0
        needs_reconciliation = False

        for ch_meta in poppies_channels:
            canon_code = normalize_channel_code(ch_meta.get("channel_code"))
            family_codes = get_channel_family_codes(canon_code)
            alias_codes = [c for c in family_codes if c != canon_code]

            canon_ch = by_code.get(canon_code)
            legacy_present = [ac for ac in alias_codes if ac in by_code]
            if legacy_present or canon_ch is None:
                needs_reconciliation = True

            effective_ch = canon_ch or (by_code[legacy_present[0]] if legacy_present else None)
            adapter = channel_registry.get(canon_code)
            has_adapter = adapter is not None

            if canon_ch is not None and not legacy_present:
                match_status = "MATCHED"
                matched_count += 1
            elif effective_ch is not None:
                match_status = "ALIAS_NEEDS_MERGE"
            else:
                match_status = "MISSING_LOCAL"

            ch_id_str = str(effective_ch.id) if effective_ch else ""
            cfg = (effective_ch.config or {}) if effective_ch else {}

            items.append(
                {
                    "channel_code": canon_code,
                    "channel_name": ch_meta.get("channel_name") or canon_code,
                    "legacy_channel_code": ch_meta.get("legacy_channel_code"),
                    "aliases": alias_codes,
                    "legacy_rows_in_db": legacy_present,
                    "arito_channel_code": ch_meta.get("arito_channel_code") or ch_meta.get("ma_kenh"),
                    "channel_group": ch_meta.get("channel_group"),
                    "poppies_is_active": bool(ch_meta.get("is_active", True)),
                    "ump_is_active": bool(effective_ch.is_active) if effective_ch else False,
                    "has_partner_pricing": bool(ch_meta.get("has_partner_pricing", False)),
                    "inherited_from": (
                        None
                        if ch_meta.get("has_partner_pricing")
                        else (ch_meta.get("inherited_from") or "STORE")
                    ),
                    "default_markup_percent": float(ch_meta.get("default_markup_percent") or 0.0),
                    "packaging_fee_vnd": float(ch_meta.get("packaging_fee_vnd") or 0.0),
                    "rounding_rule": ch_meta.get("rounding_rule"),
                    "match_status": match_status,
                    "ump_channel_code": effective_ch.code if effective_ch else None,
                    "adapter_code": adapter.channel_code if adapter else None,
                    "adapter_name": adapter.display_name if adapter else None,
                    "integration_mode": _classify_integration_mode(canon_code, has_adapter),
                    "mapped_stores_count": sm_count_by_ch.get(ch_id_str, 0),
                    "custom_price_skus_count": override_count_by_ch.get(ch_id_str, 0),
                    "blocked_skus_count": blocked_count_by_ch.get(ch_id_str, 0),
                    "last_snapshot_version": cfg.get("last_snapshot_version"),
                    "last_snapshot_at": cfg.get("last_snapshot_at"),
                }
            )

        return {
            "source": poppies_resp.get("source", "poppies_live"),
            "total_poppies_channels": len(poppies_channels),
            "matched_channels_count": matched_count,
            "needs_reconciliation": needs_reconciliation,
            "total_local_products": total_local_products,
            "channels": items,
        }

    async def sync_catalog_from_store_pricing_snapshot(
        self,
        db: AsyncSession,
        branch_code: str = "10001",
    ) -> Dict[str, Any]:
        """
        Resilient Master SKU synchronization using Poppies `STORE` Pricing Snapshot
        (`GET /api/v1/pos/pricing/snapshots?channel_code=STORE&format=map`) + CTTK Stock.
        Guarantees 100% SKU matching even when `/api/v1/pos/master-data/snapshot` returns 500.
        """
        start_time = datetime.now(timezone.utc)
        snap = await self.client.get_pricing_snapshot(
            branch_code=branch_code,
            channel_code="STORE",
            fmt="map",
        )
        raw_prices = snap.get("prices") or snap.get("price_map") or {}

        cat_code = "POPPIES_MASTER"
        cat_res = await db.execute(select(Category).where(Category.code == cat_code))
        default_cat = cat_res.scalar_one_or_none()
        categories_created = 0
        if not default_cat:
            default_cat = Category(
                code=cat_code,
                name="Hàng Hóa Đồng Bộ Poppies ERP",
                sequence=1,
            )
            db.add(default_cat)
            await db.flush()
            categories_created = 1

        prod_res = await db.execute(select(Product))
        prod_cache: Dict[str, Product] = {p.sku: p for p in prod_res.scalars().all()}

        products_created = 0
        products_updated = 0
        total_fetched = 0
        batch_counter = 0

        for raw_sku, info in raw_prices.items():
            sku_code = str(raw_sku or "").strip()
            if not sku_code or not isinstance(info, dict):
                continue
            total_fetched += 1

            name = str(info.get("productName") or info.get("name") or sku_code).strip() or sku_code
            barcode_clean = str(info.get("barcode") or "").strip() or None
            base_price = float(
                info.get("base_price")
                if info.get("base_price") is not None
                else (info.get("effective_price") or 0.0)
            )
            is_active = bool(info.get("allowed_in_context", info.get("allowed", True)))
            unit_name = "Kg" if info.get("is_weight_item") else "Cái"

            existing = prod_cache.get(sku_code)
            if not existing:
                new_prod = Product(
                    sku=sku_code,
                    barcode=barcode_clean,
                    name=name[:255],
                    unit=unit_name,
                    base_price=base_price,
                    category_id=default_cat.id,
                    is_active=is_active,
                    metadata_json={
                        "is_weight_item": bool(info.get("is_weight_item", False)),
                        "tax_rate_pct": info.get("tax_rate_pct"),
                        "synced_via": "poppies_store_pricing_snapshot",
                    },
                )
                db.add(new_prod)
                prod_cache[sku_code] = new_prod
                products_created += 1
            else:
                existing.name = name[:255]
                if barcode_clean:
                    existing.barcode = barcode_clean
                existing.base_price = base_price
                existing.is_active = is_active
                products_updated += 1

            batch_counter += 1
            if batch_counter >= 2000:
                await db.commit()
                batch_counter = 0

        await db.commit()

        # Enrich unit names & multi-store physical inventory from CTTK records
        cttk_summary = await self.sync_stock_cttk_realtime(db=db, create_missing_inventories=True)
        elapsed = round((datetime.now(timezone.utc) - start_time).total_seconds(), 2)
        return {
            "success": True,
            "sync_mode": "store_pricing_snapshot_fallback",
            "branch_code": branch_code,
            "total_fetched": total_fetched,
            "products_created": products_created,
            "products_updated": products_updated,
            "categories_created": categories_created,
            "inventories_updated": cttk_summary.get("inventories_updated", 0),
            "elapsed_seconds": elapsed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def sync_and_match_all_from_poppies(
        self,
        db: AsyncSession,
        branch_code: str = "10001",
        sync_master_skus: bool = True,
        sync_stock: bool = True,
        pricing_channels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        1-Click Comprehensive Poppies ERP Sync & Matching Pipeline:
        1. Pull & reconcile all live Canonical channels + merge legacy aliases + bind 4-store outlets.
        2. Sync Master SKUs (with automatic fallback to STORE snapshot if master-data/snapshot returns 500).
        3. Sync CTTK multi-branch physical stock.
        4. Sync channel-specific Pricing & Exclusions snapshots for active marketplace/delivery channels
           and propagate menus to external adapters.
        """
        start_time = datetime.now(timezone.utc)

        # 1. Reconcile Channels & Store Outlet Mappings
        channel_rec = await self.reconcile_channels_from_poppies(db=db, sync_store_outlets=True)

        # 2. Master SKU Catalog Sync
        catalog_summary: Optional[Dict[str, Any]] = None
        stock_already_synced = False
        if sync_master_skus:
            catalog_summary = await self.sync_master_data_from_poppies(
                db=db,
                branch_code=branch_code,
                fetch_all=True,
            )
            stock_already_synced = True

        # 3. CTTK Stock Sync (if requested separately)
        stock_summary: Optional[Dict[str, Any]] = None
        if sync_stock and not stock_already_synced:
            stock_summary = await self.sync_stock_cttk_realtime(
                db=db, create_missing_inventories=True
            )

        # 4. Sync Pricing & Publish Exclusions Snapshots for target channels
        target_codes = pricing_channels
        if target_codes is None:
            # Default: active S2S adapter channels so GrabMart, ShopeeFood, ShopeeMart have 100% matched prices & exclusions
            target_codes = ["GRAB", "SHOPEEFOOD", "SHOPEEMART"]

        snapshot_results: List[Dict[str, Any]] = []
        snapshot_errors: List[Dict[str, Any]] = []
        for raw_code in target_codes:
            canon_code = normalize_channel_code(raw_code)
            try:
                snap_res = await self.sync_channel_pricing_snapshot(
                    db=db,
                    branch_code=branch_code,
                    channel_code=canon_code,
                    fmt="map",
                    auto_propagate=False,
                )
                snapshot_results.append(snap_res)
                if channel_registry.get(canon_code):
                    await propagate_channel_menu_to_external_channel(
                        db=db, raw_channel_code=canon_code, branch_code=branch_code
                    )
            except PoppiesUpstreamError as up_err:
                snapshot_errors.append(
                    {
                        "channel_code": canon_code,
                        "error": up_err.message,
                        "fail_closed": True,
                    }
                )

        final_matching = await self.get_poppies_channel_matching_report(db=db)
        elapsed = round((datetime.now(timezone.utc) - start_time).total_seconds(), 2)

        return {
            "success": True,
            "branch_code": branch_code,
            "channel_reconciliation": {
                "source": channel_rec.get("source"),
                "total_poppies_channels": channel_rec.get("total_poppies_channels"),
                "channels_created": channel_rec.get("channels_created"),
                "channels_updated": channel_rec.get("channels_updated"),
                "legacy_channels_merged": channel_rec.get("legacy_channels_merged"),
                "store_mappings_created": channel_rec.get("store_mappings_created"),
            },
            "catalog_sync": catalog_summary,
            "stock_sync": stock_summary,
            "pricing_snapshots_synced": snapshot_results,
            "pricing_snapshot_warnings": snapshot_errors,
            "matching": final_matching,
            "elapsed_seconds": elapsed,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
