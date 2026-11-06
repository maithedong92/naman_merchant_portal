"""
Poppies ERP Multi-Channel Pricing Governance & Snapshot Sync (Fail-Closed).
Conforms strictly to Sections 2, 3, and 4 of INTEGRATION_REPORT_FOR_AI_POS_AND_UMP.md.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Set, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import NotFoundError
from app.models.channel import Channel
from app.models.product import ChannelProductMapping, Product
from app.models.sync_log import SyncLog
from app.services.poppies_channel_push import (
    propagate_channel_menu_to_external_channel,
    propagate_skus_to_external_channel,
)
from app.services.poppies_channels import (
    POPPIES_STANDARD_CHANNELS,
    get_canonical_channel_meta,
    get_channel_family_codes,
    normalize_channel_code,
)
from app.services.poppies_client import PoppiesClient, PoppiesUpstreamError
from app.services.poppies_webhook_handler import (
    PoppiesOutboxWebhookMixin,
    _safe_flag_modified,
)

logger = logging.getLogger("naman_portal.poppies_pricing")


def _extract_snapshot_prices_and_blocks(
    snap: Dict[str, Any]
) -> Tuple[Dict[str, float], Set[str]]:
    """
    Extract `{sku: price}` and `blocked_skus` from either `map` or `list` snapshot format
    (Section 4.1 & 4.2).
    """
    blocked_set: Set[str] = set()
    for key in ("exclusions", "blocked_skus", "blockedSkus"):
        raw_list = snap.get(key)
        if isinstance(raw_list, list):
            for s in raw_list:
                sku_str = str(s or "").strip()
                if sku_str:
                    blocked_set.add(sku_str)

    price_map: Dict[str, float] = {}
    raw_prices = snap.get("prices")
    if not isinstance(raw_prices, dict):
        raw_prices = snap.get("price_map")
    if isinstance(raw_prices, dict):
        for sku_key, val in raw_prices.items():
            sku_str = str(sku_key or "").strip()
            if not sku_str:
                continue
            if isinstance(val, (int, float)):
                price_map[sku_str] = float(val)
            elif isinstance(val, dict):
                if (
                    val.get("allowed") is False
                    or val.get("allowed_in_context") is False
                    or val.get("allowedInContext") is False
                    or val.get("is_published") is False
                    or val.get("is_listed") is False
                ):
                    blocked_set.add(sku_str)
                for p_key in ("effective_price", "effectivePrice", "listedPrice", "price"):
                    if val.get(p_key) is not None:
                        price_map[sku_str] = float(val[p_key])
                        break

    raw_items = snap.get("items")
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            sku_str = str(item.get("sku") or item.get("skuCode") or "").strip()
            if not sku_str:
                continue
            if (
                item.get("allowed") is False
                or item.get("allowed_in_context") is False
                or item.get("allowedInContext") is False
                or item.get("is_published") is False
                or item.get("is_listed") is False
            ):
                blocked_set.add(sku_str)
            for p_key in ("effective_price", "effectivePrice", "listedPrice", "price"):
                if item.get(p_key) is not None:
                    price_map[sku_str] = float(item[p_key])
                    break

    return price_map, blocked_set


class PoppiesPricingGovernanceMixin(PoppiesOutboxWebhookMixin):
    """
    Mixin providing Canonical Channel Resolution, Snapshot Sync (Fail-Closed),
    SKU Publish Toggle, and Idempotent Outbox Webhook processing.
    """

    client: PoppiesClient

    async def _get_or_create_channel(self, db: AsyncSession, channel_code: str) -> Channel:
        """Find or create Canonical channel record matching standardized 13-channel metadata."""
        normalized = normalize_channel_code(channel_code)
        stmt = select(Channel).where(Channel.code == normalized)
        res = await db.execute(stmt)
        channel = res.scalar_one_or_none()
        if channel:
            return channel

        meta = get_canonical_channel_meta(normalized)
        name = meta["channel_name"] if meta else normalized

        channel = Channel(
            code=normalized,
            name=name,
            description=f"Kênh bán hàng chuẩn Canonical Poppies ERP ({normalized})",
            is_active=True,
            config=dict(meta) if meta else {},
        )
        db.add(channel)
        await db.flush()
        return channel

    async def ensure_standard_channels(self, db: AsyncSession) -> Dict[str, Channel]:
        """Ensure all 13 Poppies Canonical channels exist in the database."""
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
                    description=f"Kênh bán hàng chuẩn Canonical Poppies ERP ({code})",
                    is_active=meta.get("is_active", True),
                    config=dict(meta),
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
        sync_to_poppies: bool = True,
    ) -> Dict[str, Any]:
        """
        Toggle ON/OFF SKU sales status for a specific channel using Canonical codes
        and synchronize with Poppies ERP (`POST /api/v1/pricing/channel-publish-status`).
        """
        prod_stmt = select(Product).where((Product.sku == sku) | (Product.barcode == sku))
        res = await db.execute(prod_stmt)
        product = res.scalar_one_or_none()
        if not product:
            raise NotFoundError("Sản phẩm", sku)

        poppies_channel_code = normalize_channel_code(channel_code)
        channel = await self._get_or_create_channel(db, poppies_channel_code)

        mapping_stmt = select(ChannelProductMapping).where(
            ChannelProductMapping.product_id == product.id,
            ChannelProductMapping.channel_id == channel.id,
        )
        mapping_res = await db.execute(mapping_stmt)
        mapping = mapping_res.scalar_one_or_none()

        poppies_res: Optional[Dict[str, Any]] = None
        if sync_to_poppies:
            poppies_res = await self.client.toggle_channel_publish_status(
                sku=product.sku,
                scope_code=poppies_channel_code,
                is_published=is_published,
                scope_type="CHANNEL",
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        sync_meta = {
            "last_toggle": poppies_res,
            "poppies_channel_code": poppies_channel_code,
            "manual_override": True,
            "blocked_by_snapshot": False,
            "source_event_timestamp": now_iso,
            "timestamp": now_iso,
        }

        if not mapping:
            mapping = ChannelProductMapping(
                product_id=product.id,
                channel_id=channel.id,
                is_enabled=is_published,
                channel_sync_payload=sync_meta,
            )
            db.add(mapping)
        else:
            mapping.is_enabled = is_published
            mapping.channel_sync_payload = sync_meta

        await db.commit()

        ext_sync = await propagate_skus_to_external_channel(
            db=db,
            raw_channel_code=poppies_channel_code,
            skus=[product.sku],
        )

        status_str = "BẬT (Mở bán)" if is_published else "TẮT (Ngưng bán)"
        return {
            "sku": product.sku,
            "product_name": product.name,
            "channel_code": channel_code,
            "poppies_channel_code": poppies_channel_code,
            "is_published": is_published,
            "synced_to_poppies": sync_to_poppies,
            "message": f"Đã {status_str} SKU {product.sku} trên kênh {channel.name} ({poppies_channel_code})",
            "poppies_response": poppies_res,
            "external_channel_sync": ext_sync,
        }

    async def get_sku_channel_publish_overview(
        self,
        db: AsyncSession,
        sku: str,
    ) -> Dict[str, Any]:
        """
        Get complete publish & pricing status across all 13 Canonical channels for a SKU.
        Enforces Section 2.2 (closed Canonical channel cannot be reopened by active alias)
        and Section 3.1 (`has_partner_pricing = False` inherits `STORE` base price).
        """
        prod_stmt = (
            select(Product)
            .options(
                selectinload(Product.channel_mappings).selectinload(
                    ChannelProductMapping.channel
                )
            )
            .where((Product.sku == sku) | (Product.barcode == sku))
        )
        res = await db.execute(prod_stmt)
        product = res.scalar_one_or_none()
        if not product:
            raise NotFoundError("Sản phẩm", sku)

        ch_res = await db.execute(select(Channel))
        ch_scalars = ch_res.scalars().all() if ch_res else []
        db_channels_by_code: Dict[str, Channel] = {
            c.code.upper(): c for c in ch_scalars if getattr(c, "code", None)
        }

        mappings_by_code: Dict[str, ChannelProductMapping] = {}
        for m in product.channel_mappings or []:
            if m.channel and m.channel.code:
                mappings_by_code[m.channel.code.upper()] = m

        base_price = float(product.base_price or 0.0)
        channel_items: List[Dict[str, Any]] = []

        for meta in POPPIES_STANDARD_CHANNELS:
            canon_code = meta["channel_code"]
            family_codes = get_channel_family_codes(canon_code)

            # Section 2.2: Canonical record holds supreme authority over aliases
            canon_ch = db_channels_by_code.get(canon_code)
            if canon_ch is not None:
                is_channel_active = bool(canon_ch.is_active)
                ch_cfg = canon_ch.config or {}
            else:
                alias_chs = [
                    db_channels_by_code[ac]
                    for ac in family_codes[1:]
                    if ac in db_channels_by_code
                ]
                is_channel_active = (
                    any(bool(ac.is_active) for ac in alias_chs)
                    if alias_chs
                    else bool(meta.get("is_active", True))
                )
                ch_cfg = alias_chs[0].config or {} if alias_chs else {}

            has_partner_pricing = bool(
                ch_cfg.get("has_partner_pricing", meta.get("has_partner_pricing", False))
            )
            inherited_from = None if has_partner_pricing else "STORE"

            mapping = mappings_by_code.get(canon_code)
            if mapping is None:
                for alias_code in family_codes[1:]:
                    if alias_code in mappings_by_code:
                        mapping = mappings_by_code[alias_code]
                        break

            raw_published = (
                bool(mapping.is_enabled) if mapping is not None else bool(product.is_active)
            )
            allowed_in_context = bool(
                product.is_active and is_channel_active and raw_published
            )

            custom_price = (
                float(mapping.custom_price)
                if (has_partner_pricing and mapping and mapping.custom_price is not None)
                else None
            )
            effective_price = (
                (custom_price if custom_price is not None else base_price)
                if allowed_in_context
                else 0.0
            )

            channel_items.append(
                {
                    "channel_code": canon_code,
                    "channel_name": meta["channel_name"],
                    "poppies_channel_code": canon_code,
                    "arito_channel_code": meta.get("arito_channel_code"),
                    "channel_group": meta.get("channel_group"),
                    "is_published": allowed_in_context,
                    "is_channel_active": is_channel_active,
                    "allowed_in_context": allowed_in_context,
                    "has_partner_pricing": has_partner_pricing,
                    "inherited_from": inherited_from,
                    "custom_price": custom_price,
                    "effective_price": effective_price,
                    "aliases": list(meta.get("aliases", [])),
                    "updated_at": mapping.updated_at if mapping else None,
                }
            )

        return {
            "sku": product.sku,
            "product_name": product.name,
            "base_price": base_price,
            "channels": channel_items,
        }

    async def sync_channel_pricing_snapshot(
        self,
        db: AsyncSession,
        branch_code: str = "10001",
        channel_code: str = "STORE",
        since_version: Optional[str] = None,
        fmt: str = "map",
        auto_propagate: bool = True,
    ) -> Dict[str, Any]:
        """
        Pull Pricing & Exclusions Snapshot from Poppies ERP (`GET /api/v1/pos/pricing/snapshots`).
        Enforces:
        - Section 3.1: `has_partner_pricing = False` inherits `STORE` without duplicating 92k prices.
        - Section 3.2: Preserves 100% channel-specific `exclusions` / `blocked_skus` and lifts expired ones.
        - Section 4.3: FAIL-CLOSED — On HTTP 5xx or timeout, rolls back and retains Last-Known-Good cache.
        """
        start_time = datetime.now(timezone.utc)
        canon_code = normalize_channel_code(channel_code)

        try:
            snap = await self.client.get_pricing_snapshot(
                branch_code=branch_code,
                channel_code=canon_code,
                since_version=since_version,
                fmt=fmt,
            )
        except PoppiesUpstreamError as up_err:
            # FAIL-CLOSED: Do not modify or wipe any existing ChannelProductMapping rows!
            await db.rollback()
            db.add(
                SyncLog(
                    channel_code=canon_code,
                    sync_type="PRICE_SYNC",
                    status="FAILED",
                    error_message=up_err.message,
                    request_payload={"branch_code": branch_code, "channel_code": canon_code},
                )
            )
            await db.commit()
            raise

        meta = get_canonical_channel_meta(canon_code) or {}
        snapshot_version = snap.get("version") or snap.get("snapshotVersion")
        is_channel_active = bool(snap.get("is_active", snap.get("isActive", True)))
        restrict_to_priced = bool(snap.get("restrict_to_priced_items", False))

        channel = await self._get_or_create_channel(db, canon_code)
        prev_channel_active = bool(channel.is_active)
        cfg = dict(channel.config or {})
        has_partner_pricing = bool(
            snap.get(
                "has_partner_pricing",
                snap.get(
                    "hasPartnerPricing",
                    cfg.get("has_partner_pricing", meta.get("has_partner_pricing", False)),
                ),
            )
        )
        inherited_from = (
            None
            if has_partner_pricing
            else (snap.get("inherited_from") or snap.get("inheritedFrom") or "STORE")
        )
        is_delta = bool(snap.get("is_delta", snap.get("isDelta", False)))

        price_map, blocked_set = _extract_snapshot_prices_and_blocks(snap)

        channel.is_active = is_channel_active
        cfg.update(
            {
                "has_partner_pricing": has_partner_pricing,
                "inherited_from": inherited_from,
                "restrict_to_priced_items": restrict_to_priced,
                "last_snapshot_version": snapshot_version,
                "last_snapshot_branch": branch_code,
                "last_snapshot_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        channel.config = cfg
        _safe_flag_modified(channel, "config")

        prod_res = await db.execute(select(Product))
        products_by_sku: Dict[str, Product] = {
            p.sku: p for p in prod_res.scalars().all()
        }
        id_to_sku: Dict[str, str] = {
            str(p.id): sku_k for sku_k, p in products_by_sku.items()
        }

        if restrict_to_priced and not is_delta and price_map:
            for local_sku in products_by_sku.keys():
                if local_sku not in price_map:
                    blocked_set.add(local_sku)

        map_res = await db.execute(
            select(ChannelProductMapping).where(
                ChannelProductMapping.channel_id == channel.id
            )
        )
        existing_mappings: Dict[str, ChannelProductMapping] = {
            str(m.product_id): m for m in map_res.scalars().all()
        }

        prices_updated = 0
        matched_skus_count = 0
        unmatched_skus_count = 0
        blocked_applied = 0
        unblocked_count = 0
        changed_skus: Set[str] = set()
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Store Inheritance (`has_partner_pricing = False`): clear stale custom prices
        if not has_partner_pricing:
            matched_skus_count = len(products_by_sku)
            for pid_str, m in existing_mappings.items():
                if m.custom_price is not None:
                    m.custom_price = None
                    prices_updated += 1
                    if pid_str in id_to_sku:
                        changed_skus.add(id_to_sku[pid_str])
        else:
            for sku_code, p_val in price_map.items():
                prod = products_by_sku.get(sku_code)
                if not prod:
                    unmatched_skus_count += 1
                    continue
                matched_skus_count += 1
                base_p = float(prod.base_price or 0.0)
                has_custom_diff = abs(p_val - base_p) >= 0.01
                m = existing_mappings.get(str(prod.id))
                if not m:
                    if not has_custom_diff:
                        continue
                    m = ChannelProductMapping(
                        product_id=prod.id,
                        channel_id=channel.id,
                        custom_price=p_val,
                        is_enabled=(sku_code not in blocked_set),
                        channel_sync_payload={
                            "snapshot_version": snapshot_version,
                            "blocked_by_snapshot": (sku_code in blocked_set),
                            "timestamp": now_iso,
                        },
                    )
                    db.add(m)
                    existing_mappings[str(prod.id)] = m
                    changed_skus.add(sku_code)
                    prices_updated += 1
                else:
                    target_custom = p_val if has_custom_diff else None
                    cur_custom = float(m.custom_price) if m.custom_price is not None else None
                    if cur_custom != target_custom:
                        m.custom_price = target_custom
                        changed_skus.add(sku_code)
                        prices_updated += 1

        # 2. Enforce Exclusions & Publication Deny (`blocked_set`) even on inherited channels
        for sku_code in blocked_set:
            prod = products_by_sku.get(sku_code)
            if not prod:
                continue
            m = existing_mappings.get(str(prod.id))
            payload_meta = {
                "snapshot_version": snapshot_version,
                "blocked_by_snapshot": True,
                "timestamp": now_iso,
            }
            if not m:
                raw_p = price_map.get(sku_code)
                has_diff = (
                    raw_p is not None
                    and has_partner_pricing
                    and abs(raw_p - float(prod.base_price or 0.0)) >= 0.01
                )
                m = ChannelProductMapping(
                    product_id=prod.id,
                    channel_id=channel.id,
                    custom_price=raw_p if has_diff else None,
                    is_enabled=False,
                    channel_sync_payload=payload_meta,
                )
                db.add(m)
                existing_mappings[str(prod.id)] = m
                changed_skus.add(sku_code)
            else:
                if m.is_enabled is not False:
                    changed_skus.add(sku_code)
                m.is_enabled = False
                prev_payload = dict(m.channel_sync_payload or {})
                prev_payload.update(payload_meta)
                m.channel_sync_payload = prev_payload
            blocked_applied += 1

        # 3. Full Snapshot Parity: lift expired snapshot blocks no longer in `blocked_set`
        if not is_delta:
            for prod_id_str, m in existing_mappings.items():
                sku_k = id_to_sku.get(prod_id_str)
                if not sku_k or sku_k in blocked_set:
                    continue
                meta_p = m.channel_sync_payload or {}
                if not m.is_enabled and meta_p.get("blocked_by_snapshot") is True:
                    m.is_enabled = True
                    updated_meta = dict(meta_p)
                    updated_meta["blocked_by_snapshot"] = False
                    updated_meta["snapshot_version"] = snapshot_version
                    updated_meta["unblocked_at"] = now_iso
                    m.channel_sync_payload = updated_meta
                    unblocked_count += 1
                    changed_skus.add(sku_k)

        await db.commit()

        # Propagate changes immediately to external platform adapter (GRAB / SHOPEEFOOD / SHOPEEMART)
        if not auto_propagate:
            ext_sync = []
        elif prev_channel_active != is_channel_active or len(changed_skus) > 200:
            ext_sync = await propagate_channel_menu_to_external_channel(
                db=db, raw_channel_code=canon_code, branch_code=branch_code
            )
        elif changed_skus:
            ext_sync = await propagate_skus_to_external_channel(
                db=db,
                raw_channel_code=canon_code,
                skus=list(changed_skus),
                branch_code=branch_code,
            )
        else:
            ext_sync = []

        elapsed = round((datetime.now(timezone.utc) - start_time).total_seconds(), 3)
        return {
            "success": True,
            "branch_code": branch_code,
            "channel_code": canon_code,
            "snapshot_version": snapshot_version,
            "is_channel_active": is_channel_active,
            "has_partner_pricing": has_partner_pricing,
            "inherited_from": inherited_from,
            "is_delta": is_delta,
            "matched_skus_count": matched_skus_count,
            "unmatched_skus_count": unmatched_skus_count,
            "prices_updated": prices_updated,
            "blocked_skus_count": len(blocked_set),
            "blocked_mappings_applied": blocked_applied,
            "unblocked_skus_count": unblocked_count,
            "elapsed_seconds": elapsed,
            "external_channel_sync": ext_sync,
        }
