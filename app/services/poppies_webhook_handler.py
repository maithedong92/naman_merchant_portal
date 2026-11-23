"""
Poppies ERP Outbox Webhook Processor (`PricingOutboxDispatcher` Consumer).
Guarantees Idempotency, Out-of-Order Timestamp Protection, and Fail-Closed Price Updates.
Conforms strictly to Sections 4.3, 5.2, and 6.1 of INTEGRATION_REPORT_FOR_AI_POS_AND_UMP.md.
"""

from datetime import datetime, timezone
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.exceptions import BadRequestError
from app.core.security import redact_sensitive_headers
from app.models.channel import Channel
from app.models.product import ChannelProductMapping, Product
from app.models.sync_log import WebhookAuditLog
from app.services.poppies_channel_push import (
    normalize_conversion_rate,
    propagate_channel_menu_to_external_channel,
    propagate_skus_to_external_channel,
)
from app.services.poppies_channels import normalize_channel_code
from app.services.poppies_client import PoppiesClient

logger = logging.getLogger("naman_portal.poppies_webhook")


def parse_iso_timestamp(raw_ts: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 timestamp safely into UTC-aware datetime for out-of-order checks."""
    if not raw_ts or not isinstance(raw_ts, str):
        return None
    try:
        cleaned = raw_ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _safe_flag_modified(instance: Any, attr_name: str) -> None:
    try:
        flag_modified(instance, attr_name)
    except Exception:
        pass


_SKU_REMOTE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_SKU_REMOTE_CACHE_TTL_SEC = 5.0


class PoppiesOutboxWebhookMixin:
    """Mixin handling real-time Outbox Webhook events pushed from Poppies ERP."""

    client: PoppiesClient

    async def _get_or_create_channel(self, db: AsyncSession, channel_code: str) -> Channel:
        raise NotImplementedError

    async def process_price_change_webhook(
        self,
        db: AsyncSession,
        payload: Dict[str, Any],
        event_id_header: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Process Poppies ERP Outbox Webhook events:
        - `CHANNEL_PUBLISH_STATUS_CHANGED`
        - `CHANNEL_PRICING_STATUS_CHANGED`
        - `price_changed`

        Guarantees:
        1. Idempotency via `event_id` / `X-Event-Id` (`WebhookAuditLog`).
        2. Out-of-order protection via original `timestamp` comparison.
        3. Safe price updates: pure `price_changed` events NEVER overwrite `is_enabled` to False.
        """
        headers = redact_sensitive_headers(headers or {})
        event_id = str(
            event_id_header or payload.get("event_id") or payload.get("eventId") or ""
        ).strip()
        event_type = str(
            payload.get("event_type") or payload.get("event") or "price_changed"
        ).strip()
        channel_raw = str(
            payload.get("channel_code") or payload.get("channel") or "ALL"
        ).strip()
        branch_code = str(
            payload.get("branch_code") or payload.get("store_code") or "ALL"
        ).strip()
        affected_skus: List[str] = payload.get("affected_skus") or []
        incoming_ts = parse_iso_timestamp(payload.get("timestamp"))
        if incoming_ts is not None and (incoming_ts - datetime.now(timezone.utc)).total_seconds() > 60:
            raise BadRequestError("Timestamp webhook Poppies không hợp lệ (ở tương lai quá 60 giây).")
        incoming_ts_iso = (
            incoming_ts.isoformat()
            if incoming_ts
            else datetime.now(timezone.utc).isoformat()
        )

        # 1. Idempotency Check against WebhookAuditLog
        if event_id:
            dup_stmt = select(WebhookAuditLog).where(
                WebhookAuditLog.channel_code == "POPPIES_ERP",
                WebhookAuditLog.idempotency_key == event_id,
            )
            dup_res = await db.execute(dup_stmt)
            existing_audit = dup_res.scalar_one_or_none()
            if existing_audit is not None:
                return {
                    "success": True,
                    "event_id": event_id,
                    "duplicate": True,
                    "processed_count": 0,
                    "message": f"Sự kiện {event_id} đã được xử lý trước đó (Idempotent).",
                }

        poppies_channel_code = (
            "ALL" if channel_raw.upper() == "ALL" else normalize_channel_code(channel_raw)
        )
        is_conversion_event = event_type.upper() == "CONVERSION_RATE_UPDATED"
        is_full_catalog = bool(
            payload.get("is_full_catalog", payload.get("isFullCatalog", False))
        )
        raw_conv_map = payload.get("conversion_rates")
        if not isinstance(raw_conv_map, dict):
            raw_conv_map = payload.get("conversionRates")
        conv_rates_map: Dict[str, Any] = raw_conv_map if isinstance(raw_conv_map, dict) else {}

        has_single_conv_rate = ("conversion_rate" in payload) or ("conversionRate" in payload)
        single_conv_rate = payload.get("conversion_rate", payload.get("conversionRate"))
        single_conv_source = (
            payload.get("conversion_rate_source") or payload.get("conversionRateSource")
        )

        clean_affected_skus = list(
            dict.fromkeys(str(s).strip() for s in affected_skus if str(s).strip())
        )
        if not clean_affected_skus and conv_rates_map:
            clean_affected_skus = list(
                dict.fromkeys(str(s).strip() for s in conv_rates_map.keys() if str(s).strip())
            )

        # Pull conversion rates from Poppies UMP API when a conversion event omits the map
        # (e.g. `is_full_catalog=True` or `len(affected_skus) > 200`).
        pulled_conv_map: Dict[str, Tuple[float, str]] = {}
        if (
            is_conversion_event
            and not conv_rates_map
            and not has_single_conv_rate
            and (is_full_catalog or clean_affected_skus)
        ):
            try:
                if len(clean_affected_skus) == 1 and not is_full_catalog:
                    one_res = await self.client.get_conversion_rate_by_sku(clean_affected_skus[0])
                    if isinstance(one_res, dict):
                        r_val = one_res.get("conversionRate", one_res.get("conversion_rate"))
                        r_src = str(
                            one_res.get("conversionRateSource")
                            or one_res.get("conversion_rate_source")
                            or "DEFAULT"
                        ).strip().upper()[:32] or "DEFAULT"
                        if r_val is not None:
                            pulled_conv_map[clean_affected_skus[0]] = (
                                normalize_conversion_rate(r_val),
                                r_src,
                            )
                else:
                    pull_skus = None if (is_full_catalog or len(clean_affected_skus) > 200) else clean_affected_skus
                    batch_res = await self.client.get_conversion_rates(
                        skus=pull_skus,
                        limit=1000,
                    )
                    items = batch_res.get("items") or batch_res.get("conversion_rates") or []
                    if isinstance(items, list):
                        for row in items:
                            if not isinstance(row, dict):
                                continue
                            s_code = str(row.get("sku") or row.get("skuCode") or "").strip()
                            if not s_code:
                                continue
                            r_val = row.get("conversionRate", row.get("conversion_rate"))
                            r_src = str(
                                row.get("conversionRateSource")
                                or row.get("conversion_rate_source")
                                or "DEFAULT"
                            ).strip().upper()[:32] or "DEFAULT"
                            if r_val is not None:
                                pulled_conv_map[s_code] = (
                                    normalize_conversion_rate(r_val),
                                    r_src,
                                )
                        if is_full_catalog and not clean_affected_skus:
                            clean_affected_skus = list(pulled_conv_map.keys())
            except Exception as conv_ex:
                logger.warning(
                    f"Fail-Closed: không thể pull conversion-rates từ Poppies UMP ({conv_ex})"
                )

        # 2. Handle Channel-Level Policy Change (`CHANNEL_PRICING_STATUS_CHANGED`, Section 6.1)
        if event_type.upper() == "CHANNEL_PRICING_STATUS_CHANGED" and poppies_channel_code != "ALL":
            channel = await self._get_or_create_channel(db, poppies_channel_code)
            cfg = dict(channel.config or {})
            prev_ts = parse_iso_timestamp(cfg.get("source_event_timestamp"))
            if incoming_ts and prev_ts and incoming_ts < prev_ts:
                return {
                    "success": True,
                    "event_id": event_id or None,
                    "channel_code": poppies_channel_code,
                    "skipped_out_of_order": True,
                    "processed_count": 0,
                    "message": "Bỏ qua sự kiện cũ hơn trạng thái hiện tại (Out-of-Order Protection).",
                }

            if "is_active" in payload or "isActive" in payload:
                channel.is_active = bool(
                    payload.get("is_active", payload.get("isActive"))
                )
            if "has_partner_pricing" in payload or "hasPartnerPricing" in payload:
                hpp = bool(
                    payload.get("has_partner_pricing", payload.get("hasPartnerPricing"))
                )
                cfg["has_partner_pricing"] = hpp
                cfg["inherited_from"] = None if hpp else "STORE"
            cfg["source_event_timestamp"] = incoming_ts_iso
            channel.config = cfg
            _safe_flag_modified(channel, "config")

            if event_id:
                db.add(
                    WebhookAuditLog(
                        channel_code="POPPIES_ERP",
                        event_type=event_type,
                        idempotency_key=event_id,
                        headers=headers,
                        payload=payload,
                        response_status=200,
                    )
                )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                logger.warning(
                    f"Concurrent channel webhook conflict resolved via rollback for event_id={event_id} channel={poppies_channel_code}"
                )
                return {
                    "success": True,
                    "event_id": event_id or None,
                    "duplicate": True,
                    "channel_code": poppies_channel_code,
                    "processed_count": 0,
                    "external_channel_sync": [],
                    "message": f"Sự kiện {event_id} đã được xử lý đồng thời (Idempotent).",
                }
            ext_sync = await propagate_channel_menu_to_external_channel(
                db=db,
                raw_channel_code=poppies_channel_code,
                branch_code=branch_code,
            )
            return {
                "success": True,
                "event_id": event_id or None,
                "channel_code": poppies_channel_code,
                "processed_count": 1,
                "external_channel_sync": ext_sync,
                "message": f"Đã cập nhật cấu hình kênh {poppies_channel_code} từ Poppies.",
            }

        has_conv_payload = is_conversion_event or bool(conv_rates_map) or has_single_conv_rate or bool(pulled_conv_map)

        if not clean_affected_skus or (poppies_channel_code == "ALL" and not has_conv_payload):
            if event_id:
                db.add(
                    WebhookAuditLog(
                        channel_code="POPPIES_ERP",
                        event_type=event_type,
                        idempotency_key=event_id,
                        headers=headers,
                        payload=payload,
                        response_status=200,
                    )
                )
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    return {
                        "success": True,
                        "event_id": event_id,
                        "duplicate": True,
                        "processed_count": 0,
                        "external_channel_sync": [],
                        "message": f"Sự kiện {event_id} đã được xử lý đồng thời (Idempotent).",
                    }
            return {
                "success": True,
                "event_id": event_id or None,
                "processed_count": 0,
                "message": "Không có SKU cụ thể cần cập nhật.",
            }

        # 2b. Handle `CONVERSION_RATE_UPDATED` (or conversion-rate-only update on channel=ALL)
        if is_conversion_event or poppies_channel_code == "ALL":
            updated_count = 0
            propagated_skus: List[str] = []
            for idx in range(0, len(clean_affected_skus), 1000):
                sku_chunk = clean_affected_skus[idx : idx + 1000]
                prod_res = await db.execute(select(Product).where(Product.sku.in_(sku_chunk)))
                for prod in prod_res.scalars().all():
                    sku_k = str(prod.sku)
                    raw_rate: Any = None
                    raw_src: Optional[str] = None
                    has_rate = False
                    if sku_k in conv_rates_map:
                        entry = conv_rates_map[sku_k]
                        if isinstance(entry, dict):
                            raw_rate = entry.get("conversion_rate", entry.get("conversionRate"))
                            raw_src = entry.get("conversion_rate_source") or entry.get("conversionRateSource")
                        else:
                            raw_rate = entry
                            raw_src = single_conv_source
                        has_rate = True
                    elif sku_k in pulled_conv_map:
                        raw_rate, raw_src = pulled_conv_map[sku_k]
                        has_rate = True
                    elif has_single_conv_rate and (len(clean_affected_skus) == 1 or is_conversion_event):
                        raw_rate = single_conv_rate
                        raw_src = single_conv_source
                        has_rate = True

                    if not has_rate:
                        continue

                    new_rate = normalize_conversion_rate(raw_rate)
                    new_src = (
                        str(raw_src or getattr(prod, "conversion_rate_source", None) or "DEFAULT")
                        .strip()
                        .upper()[:32]
                        or "DEFAULT"
                    )
                    cur_rate = normalize_conversion_rate(getattr(prod, "conversion_rate", 1.0))
                    cur_src = getattr(prod, "conversion_rate_source", "DEFAULT") or "DEFAULT"
                    if abs(cur_rate - new_rate) >= 0.0001 or cur_src != new_src:
                        prod.conversion_rate = new_rate
                        prod.conversion_rate_source = new_src
                    updated_count += 1
                    propagated_skus.append(sku_k)

            if event_id:
                db.add(
                    WebhookAuditLog(
                        channel_code="POPPIES_ERP",
                        event_type=event_type,
                        idempotency_key=event_id,
                        headers=headers,
                        payload=payload,
                        response_status=200,
                    )
                )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                logger.warning(
                    f"Concurrent conversion webhook conflict resolved via rollback for event_id={event_id}"
                )
                return {
                    "success": True,
                    "event_id": event_id or None,
                    "duplicate": True,
                    "processed_count": 0,
                    "external_channel_sync": [],
                    "message": f"Sự kiện {event_id} đã được xử lý đồng thời (Idempotent).",
                }

            ext_sync: List[Dict[str, Any]] = []
            if propagated_skus:
                target_channels = (
                    ("GRAB", "SHOPEEFOOD", "SHOPEEMART")
                    if poppies_channel_code == "ALL"
                    else (poppies_channel_code,)
                )
                for ch_target in target_channels:
                    if len(propagated_skus) > 200:
                        ext_sync.extend(
                            await propagate_channel_menu_to_external_channel(
                                db=db, raw_channel_code=ch_target, branch_code=branch_code
                            )
                        )
                    else:
                        ext_sync.extend(
                            await propagate_skus_to_external_channel(
                                db=db,
                                raw_channel_code=ch_target,
                                skus=propagated_skus,
                                branch_code=branch_code,
                            )
                        )

            return {
                "success": True,
                "event_id": event_id or None,
                "channel_code": poppies_channel_code,
                "processed_count": updated_count,
                "skipped_out_of_order": 0,
                "external_channel_sync": ext_sync,
                "message": f"Đã cập nhật tỉ lệ quy đổi cho {updated_count} SKU từ webhook Poppies.",
            }

        # 3. Handle SKU-Level Publish Status or Price Update
        channel = await self._get_or_create_channel(db, poppies_channel_code)
        has_explicit_pub = ("is_published" in payload) or ("isPublished" in payload)
        explicit_pub_val: Optional[bool] = (
            bool(payload.get("is_published", payload.get("isPublished")))
            if has_explicit_pub
            else None
        )
        inline_prices: Dict[str, Any] = (
            payload.get("prices") if isinstance(payload.get("prices"), dict) else {}
        )
        has_inline_price_payload = bool(inline_prices) or (payload.get("effective_price") is not None)

        updated_count = 0
        skipped_stale = 0
        propagated_skus: List[str] = []

        for sku in clean_affected_skus:
            prod_stmt = select(Product).where(Product.sku == sku)
            prod_res = await db.execute(prod_stmt)
            prod = prod_res.scalar_one_or_none()
            if not prod:
                continue

            map_stmt = select(ChannelProductMapping).where(
                ChannelProductMapping.product_id == prod.id,
                ChannelProductMapping.channel_id == channel.id,
            )
            map_res = await db.execute(map_stmt)
            mapping = map_res.scalar_one_or_none()

            # Out-of-order protection per SKU+channel
            if mapping and mapping.channel_sync_payload and incoming_ts:
                prev_ts = parse_iso_timestamp(
                    mapping.channel_sync_payload.get("source_event_timestamp")
                )
                if prev_ts and incoming_ts < prev_ts:
                    skipped_stale += 1
                    continue

            prev_enabled = bool(mapping.is_enabled) if mapping is not None else bool(prod.is_active)
            prev_price = float(mapping.custom_price) if (mapping is not None and mapping.custom_price is not None) else None

            # Resolve publish status safely (NEVER default to False on pure price_changed!)
            target_enabled = prev_enabled
            if explicit_pub_val is not None:
                target_enabled = explicit_pub_val

            # Resolve updated custom price if provided inline or via Fail-Closed SKU pull
            target_price = prev_price
            if sku in inline_prices and inline_prices[sku] is not None:
                target_price = float(inline_prices[sku])
            elif payload.get("effective_price") is not None and len(clean_affected_skus) == 1:
                target_price = float(payload["effective_price"])
            elif event_type.upper() != "CHANNEL_PUBLISH_STATUS_CHANGED":
                try:
                    now_mono = time.monotonic()
                    cached_entry = _SKU_REMOTE_CACHE.get(sku)
                    if cached_entry and (now_mono - cached_entry[0]) < _SKU_REMOTE_CACHE_TTL_SEC:
                        remote_item = cached_entry[1]
                    else:
                        remote_item = await self.client.get_sku(sku)
                        if isinstance(remote_item, dict):
                            if len(_SKU_REMOTE_CACHE) > 500:
                                _SKU_REMOTE_CACHE.clear()
                            _SKU_REMOTE_CACHE[sku] = (now_mono, remote_item)

                    if isinstance(remote_item, dict):
                        rem_pricing = remote_item.get("pricing") or {}
                        base_vat = rem_pricing.get("baseSalePriceVat") or rem_pricing.get("baseNetSalePrice")
                        if base_vat is not None and float(base_vat) > 0:
                            new_base = float(base_vat)
                            if abs(float(prod.base_price or 0.0) - new_base) >= 0.01:
                                prod.base_price = new_base
                        for ch_entry in rem_pricing.get("channels") or []:
                            ch_c = normalize_channel_code(ch_entry.get("channelCode"))
                            if ch_c == poppies_channel_code:
                                if ch_entry.get("listedPrice") is not None:
                                    target_price = float(ch_entry["listedPrice"])
                                if "isPublished" in ch_entry and explicit_pub_val is None:
                                    target_enabled = bool(ch_entry["isPublished"])
                                break
                except Exception as pull_ex:
                    logger.warning(
                        f"Fail-Closed: giữ nguyên giá & trạng thái hiện tại cho SKU {sku} ({pull_ex})"
                    )

            sync_meta = {
                "webhook_sync": payload,
                "event_id": event_id or None,
                "source_event_timestamp": incoming_ts_iso,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            was_new_mapping = mapping is None
            price_changed = (
                (prev_price is None and target_price is not None)
                or (prev_price is not None and target_price is not None and abs(prev_price - target_price) >= 0.01)
            )
            enabled_changed = target_enabled != prev_enabled

            if not mapping:
                mapping = ChannelProductMapping(
                    product_id=prod.id,
                    channel_id=channel.id,
                    custom_price=target_price,
                    is_enabled=target_enabled,
                    channel_sync_payload=sync_meta,
                )
                db.add(mapping)
            else:
                mapping.is_enabled = target_enabled
                if target_price is not None:
                    mapping.custom_price = target_price
                mapping.channel_sync_payload = sync_meta

            # Apply conversion_rate if provided in `conversion_rates` map or single `conversion_rate`
            conv_changed = False
            raw_conv_val: Any = None
            raw_conv_src: Optional[str] = None
            has_conv_for_sku = False
            if sku in conv_rates_map:
                c_entry = conv_rates_map[sku]
                if isinstance(c_entry, dict):
                    raw_conv_val = c_entry.get("conversion_rate", c_entry.get("conversionRate"))
                    raw_conv_src = c_entry.get("conversion_rate_source") or c_entry.get("conversionRateSource")
                else:
                    raw_conv_val = c_entry
                    raw_conv_src = single_conv_source
                has_conv_for_sku = True
            elif has_single_conv_rate and len(clean_affected_skus) == 1:
                raw_conv_val = single_conv_rate
                raw_conv_src = single_conv_source
                has_conv_for_sku = True

            if has_conv_for_sku:
                new_conv_rate = normalize_conversion_rate(raw_conv_val)
                new_conv_src = (
                    str(raw_conv_src or getattr(prod, "conversion_rate_source", None) or "DEFAULT")
                    .strip()
                    .upper()[:32]
                    or "DEFAULT"
                )
                cur_conv_rate = normalize_conversion_rate(getattr(prod, "conversion_rate", 1.0))
                cur_conv_src = getattr(prod, "conversion_rate_source", "DEFAULT") or "DEFAULT"
                if abs(cur_conv_rate - new_conv_rate) >= 0.0001 or cur_conv_src != new_conv_src:
                    prod.conversion_rate = new_conv_rate
                    prod.conversion_rate_source = new_conv_src
                    conv_changed = True

            updated_count += 1
            if (
                was_new_mapping
                or price_changed
                or enabled_changed
                or conv_changed
                or has_explicit_pub
                or has_inline_price_payload
            ):
                propagated_skus.append(str(prod.sku))

        if event_id:
            db.add(
                WebhookAuditLog(
                    channel_code="POPPIES_ERP",
                    event_type=event_type,
                    idempotency_key=event_id,
                    headers=headers,
                    payload=payload,
                    response_status=200,
                )
            )

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            logger.warning(
                f"Concurrent webhook upsert conflict resolved via rollback for event_id={event_id} channel={poppies_channel_code}"
            )
            return {
                "success": True,
                "event_id": event_id or None,
                "duplicate": True,
                "channel_code": poppies_channel_code,
                "processed_count": 0,
                "skipped_out_of_order": skipped_stale,
                "external_channel_sync": [],
                "message": f"Sự kiện {event_id} đã được xử lý đồng thời (Idempotent).",
            }

        ext_sync = (
            await propagate_skus_to_external_channel(
                db=db,
                raw_channel_code=poppies_channel_code,
                skus=propagated_skus,
                branch_code=branch_code,
            )
            if propagated_skus
            else []
        )

        return {
            "success": True,
            "event_id": event_id or None,
            "channel_code": poppies_channel_code,
            "processed_count": updated_count,
            "skipped_out_of_order": skipped_stale,
            "external_channel_sync": ext_sync,
            "message": f"Đã cập nhật {updated_count} SKU trên kênh {poppies_channel_code} từ webhook Poppies.",
        }
