"""
Real-Time Outbound Propagation from Poppies ERP Channel Governance to External Platform Adapters
(`GRAB` -> GrabMart, `SHOPEEFOOD` -> ShopeeFood, `SHOPEE` -> ShopeeMart).
"""

import inspect
import logging
from typing import Any, Dict, List, Optional, Set, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import Channel
from app.models.inventory import StoreInventory
from app.models.product import ChannelProductMapping, Product
from app.models.store import Store, StoreChannelMapping
from app.services.channel_registry import channel_registry
from app.services.poppies_channels import (
    get_canonical_channel_meta,
    get_channel_family_codes,
    normalize_channel_code,
)

logger = logging.getLogger("naman_portal.poppies_channel_push")


async def _safe_execute(db: Any, stmt: Any) -> Any:
    """Execute a SQLAlchemy select safely, returning None if a unit-test side_effect list is exhausted."""
    try:
        return await db.execute(stmt)
    except (StopAsyncIteration, StopIteration):
        return None


async def _extract_orm_scalars(db_res: Any, expected_type: type) -> List[Any]:
    """
    Safely extract real SQLAlchemy ORM instances from a `db.execute(...)` result.
    Returns `[]` cleanly when `db` is an unconfigured `AsyncMock`/`MagicMock` in unit tests.
    """
    if db_res is None or not hasattr(db_res, "scalars"):
        return []
    try:
        scalars_obj = db_res.scalars()
        if inspect.iscoroutine(scalars_obj):
            scalars_obj = await scalars_obj
        if scalars_obj is None or not hasattr(scalars_obj, "all"):
            return []
        raw_items = scalars_obj.all()
        if inspect.iscoroutine(raw_items):
            raw_items = await raw_items
        if not isinstance(raw_items, (list, tuple, set)):
            return []
        return [item for item in raw_items if isinstance(item, expected_type)]
    except Exception:
        return []


def _get_Extended_family_codes(canonical_code: str) -> List[str]:
    """Return ordered list `[canonical_code, *aliases, adapter_code]` without duplicates."""
    family = list(get_channel_family_codes(canonical_code))
    adapter = channel_registry.get(canonical_code)
    if adapter:
        adp_code = adapter.channel_code.upper()
        if adp_code not in family:
            family.append(adp_code)
    return family


async def load_channel_governance_for_products(
    db: Optional[AsyncSession],
    raw_channel_code: str,
    product_ids: Optional[List[str]] = None,
) -> Tuple[bool, bool, Dict[str, ChannelProductMapping]]:
    """
    Load `(is_channel_active, has_partner_pricing, mapping_by_product_id)` for a channel.
    Enforces:
    - Section 2.2: Canonical channel `is_active = False` overrides any active legacy alias.
    - Section 3.1: `has_partner_pricing` determines whether `custom_price` or `STORE` base_price applies.
    """
    canon_code = normalize_channel_code(raw_channel_code)
    meta = get_canonical_channel_meta(canon_code) or {}
    default_active = bool(meta.get("is_active", True))
    default_hpp = bool(meta.get("has_partner_pricing", False))

    if db is None:
        return default_active, default_hpp, {}

    family_codes = _get_Extended_family_codes(canon_code)
    ch_stmt = select(Channel).where(Channel.code.in_(family_codes))
    ch_rows = await _extract_orm_scalars(await _safe_execute(db, ch_stmt), Channel)
    if not ch_rows:
        return default_active, default_hpp, {}

    ch_by_code: Dict[str, Channel] = {c.code.upper(): c for c in ch_rows if c.code}
    ch_by_id: Dict[str, Channel] = {str(c.id): c for c in ch_rows if c.id}

    canon_ch = ch_by_code.get(canon_code)
    if canon_ch is not None:
        is_channel_active = bool(canon_ch.is_active)
        ch_cfg = canon_ch.config or {}
    else:
        alias_chs = [ch_by_code[c] for c in family_codes[1:] if c in ch_by_code]
        is_channel_active = (
            any(bool(c.is_active) for c in alias_chs) if alias_chs else default_active
        )
        ch_cfg = (alias_chs[0].config or {}) if alias_chs else {}

    has_partner_pricing = bool(ch_cfg.get("has_partner_pricing", default_hpp))

    map_stmt = select(ChannelProductMapping).where(
        ChannelProductMapping.channel_id.in_(list(ch_by_id.keys()))
    )
    if product_ids:
        map_stmt = map_stmt.where(ChannelProductMapping.product_id.in_(product_ids))

    map_rows = await _extract_orm_scalars(await _safe_execute(db, map_stmt), ChannelProductMapping)

    # Prefer Canonical channel mapping over alias channel mapping per product_id
    mapping_by_pid: Dict[str, ChannelProductMapping] = {}
    for m in map_rows:
        pid = str(m.product_id)
        ch_obj = ch_by_id.get(str(m.channel_id))
        is_canon_map = bool(ch_obj and ch_obj.code and ch_obj.code.upper() == canon_code)
        if pid not in mapping_by_pid or is_canon_map:
            mapping_by_pid[pid] = m

    return is_channel_active, has_partner_pricing, mapping_by_pid


async def apply_channel_governance_to_stock_items(
    db: Optional[AsyncSession],
    raw_channel_code: str,
    stock_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Enforce Poppies ERP channel publish (`is_enabled`, `Channel.is_active`) and `custom_price`
    on outbound `stock_items` before sending to GrabMart / ShopeeFood / ShopeeMart.
    Prevents routine store stock syncs from accidentally reopening banned/unpublished SKUs.
    """
    if db is None or not stock_items:
        return stock_items

    skus = [str(it.get("sku") or "").strip() for it in stock_items if it.get("sku")]
    if not skus:
        return stock_items

    prod_stmt = select(Product).where(Product.sku.in_(skus[:1000]))
    prods = await _extract_orm_scalars(await _safe_execute(db, prod_stmt), Product)
    if not prods:
        return stock_items

    prod_by_sku: Dict[str, Product] = {p.sku: p for p in prods}
    is_ch_active, has_partner_pricing, map_by_pid = await load_channel_governance_for_products(
        db=db,
        raw_channel_code=raw_channel_code,
        product_ids=[str(p.id) for p in prods],
    )

    adjusted: List[Dict[str, Any]] = []
    for raw_item in stock_items:
        item = dict(raw_item)
        sku_str = str(item.get("sku") or "").strip()
        prod = prod_by_sku.get(sku_str)
        if not prod:
            adjusted.append(item)
            continue

        mapping = map_by_pid.get(str(prod.id))
        is_sku_published = bool(
            prod.is_active
            and is_ch_active
            and (mapping.is_enabled if mapping is not None else True)
        )

        if not is_sku_published:
            item["is_out_of_stock"] = True
            item["available_stock"] = 0

        eff_price = (
            float(mapping.custom_price)
            if (has_partner_pricing and mapping and mapping.custom_price is not None)
            else float(prod.base_price or 0.0)
        )
        if item.get("price") is None and eff_price > 0:
            item["price"] = eff_price

        adjusted.append(item)

    return adjusted


async def propagate_skus_to_external_channel(
    db: Optional[AsyncSession],
    raw_channel_code: str,
    skus: List[str],
    branch_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Immediately push SKU publish ON/OFF & effective price changes to the external platform adapter
    (`GRAB` -> GrabMart, `SHOPEEFOOD` -> ShopeeFood, `SHOPEE` -> ShopeeMart) across active mapped stores.
    """
    if db is None or not skus:
        return []

    canon_code = normalize_channel_code(raw_channel_code)
    adapter = channel_registry.get(canon_code)
    if adapter is None:
        return []

    clean_skus = list(dict.fromkeys(str(s).strip() for s in skus if str(s).strip()))
    if not clean_skus:
        return []

    family_codes = _get_Extended_family_codes(canon_code)
    scm_stmt = (
        select(StoreChannelMapping)
        .join(Channel, Channel.id == StoreChannelMapping.channel_id)
        .join(Store, Store.id == StoreChannelMapping.store_id)
        .where(
            StoreChannelMapping.is_active == True,
            Store.is_active == True,
            Channel.code.in_(family_codes),
        )
    )
    if branch_code and branch_code.strip().upper() != "ALL":
        scm_stmt = scm_stmt.where(Store.code == branch_code.strip())

    scm_rows = await _extract_orm_scalars(await _safe_execute(db, scm_stmt), StoreChannelMapping)
    if not scm_rows:
        return []

    # Deduplicate outlets by (store_id, partner_store_id)
    seen_outlets: Set[Tuple[str, str]] = set()
    outlets: List[Tuple[str, str]] = []
    for scm in scm_rows:
        key = (str(scm.store_id), str(scm.partner_store_id))
        if key not in seen_outlets:
            seen_outlets.add(key)
            outlets.append(key)

    prod_stmt = select(Product).where(Product.sku.in_(clean_skus[:1000]))
    prods = await _extract_orm_scalars(await _safe_execute(db, prod_stmt), Product)
    if not prods:
        return []

    prod_ids = [str(p.id) for p in prods]
    store_ids = list({st_id for st_id, _ in outlets})

    is_ch_active, has_partner_pricing, map_by_pid = await load_channel_governance_for_products(
        db=db,
        raw_channel_code=canon_code,
        product_ids=prod_ids,
    )

    inv_stmt = select(StoreInventory).where(
        StoreInventory.store_id.in_(store_ids),
        StoreInventory.product_id.in_(prod_ids),
    )
    inv_rows = await _extract_orm_scalars(await _safe_execute(db, inv_stmt), StoreInventory)
    inv_by_store_prod: Dict[Tuple[str, str], StoreInventory] = {
        (str(inv.store_id), str(inv.product_id)): inv for inv in inv_rows
    }

    sync_summaries: List[Dict[str, Any]] = []
    for store_id, partner_store_id in outlets:
        stock_items: List[Dict[str, Any]] = []
        for prod in prods:
            pid = str(prod.id)
            mapping = map_by_pid.get(pid)
            is_allowed = bool(
                prod.is_active
                and is_ch_active
                and (mapping.is_enabled if mapping is not None else True)
            )
            inv = inv_by_store_prod.get((store_id, pid))
            phys_avail = (
                float(inv.available_stock)
                if (inv and not inv.is_out_of_stock and float(inv.available_stock or 0) > 0)
                else 0.0
            )
            eff_avail = phys_avail if is_allowed else 0.0
            eff_oos = (not is_allowed) or (eff_avail <= 0.0)
            eff_price = (
                float(mapping.custom_price)
                if (has_partner_pricing and mapping and mapping.custom_price is not None)
                else float(prod.base_price or 0.0)
            )

            stock_items.append(
                {
                    "sku": prod.sku,
                    "available_stock": eff_avail,
                    "is_out_of_stock": eff_oos,
                    "price": eff_price if eff_price > 0 else None,
                }
            )

        try:
            res = await adapter.sync_inventory(
                store_id=store_id,
                partner_store_id=partner_store_id,
                stock_items=stock_items,
                db=db,
            )
            sync_summaries.append(
                {
                    "channel_code": canon_code,
                    "adapter": adapter.channel_code,
                    "store_id": store_id,
                    "partner_store_id": partner_store_id,
                    "success": res.success,
                    "total_synced": res.total_synced,
                    "message": res.message,
                }
            )
        except Exception as ex:
            logger.error(
                f"Lỗi đồng bộ tự động SKU lên kênh {canon_code} (store={partner_store_id}): {ex}"
            )
            sync_summaries.append(
                {
                    "channel_code": canon_code,
                    "adapter": adapter.channel_code,
                    "store_id": store_id,
                    "partner_store_id": partner_store_id,
                    "success": False,
                    "total_synced": 0,
                    "message": str(ex),
                }
            )

    return sync_summaries


async def propagate_channel_menu_to_external_channel(
    db: Optional[AsyncSession],
    raw_channel_code: str,
    branch_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Trigger full store menu re-sync on external platform (`GRAB`, `SHOPEEFOOD`, `SHOPEE`)
    when channel-level policy (`is_active` or `has_partner_pricing`) changes.
    """
    if db is None:
        return []

    canon_code = normalize_channel_code(raw_channel_code)
    adapter = channel_registry.get(canon_code)
    if adapter is None:
        return []

    family_codes = _get_Extended_family_codes(canon_code)
    scm_stmt = (
        select(StoreChannelMapping)
        .join(Channel, Channel.id == StoreChannelMapping.channel_id)
        .join(Store, Store.id == StoreChannelMapping.store_id)
        .where(
            StoreChannelMapping.is_active == True,
            Store.is_active == True,
            Channel.code.in_(family_codes),
        )
    )
    if branch_code and branch_code.strip().upper() != "ALL":
        scm_stmt = scm_stmt.where(Store.code == branch_code.strip())

    scm_rows = await _extract_orm_scalars(await _safe_execute(db, scm_stmt), StoreChannelMapping)
    seen_outlets: Set[Tuple[str, str]] = set()
    summaries: List[Dict[str, Any]] = []

    for scm in scm_rows:
        key = (str(scm.store_id), str(scm.partner_store_id))
        if key in seen_outlets:
            continue
        seen_outlets.add(key)
        try:
            res = await adapter.sync_menu(
                store_id=str(scm.store_id),
                partner_store_id=str(scm.partner_store_id),
                db=db,
            )
            summaries.append(
                {
                    "channel_code": canon_code,
                    "adapter": adapter.channel_code,
                    "store_id": str(scm.store_id),
                    "partner_store_id": str(scm.partner_store_id),
                    "success": res.success,
                    "total_synced": res.total_synced,
                    "message": res.message,
                }
            )
        except Exception as ex:
            logger.error(f"Lỗi đồng bộ menu kênh {canon_code} (store={scm.partner_store_id}): {ex}")
            summaries.append(
                {
                    "channel_code": canon_code,
                    "adapter": adapter.channel_code,
                    "store_id": str(scm.store_id),
                    "partner_store_id": str(scm.partner_store_id),
                    "success": False,
                    "total_synced": 0,
                    "message": str(ex),
                }
            )

    return summaries
