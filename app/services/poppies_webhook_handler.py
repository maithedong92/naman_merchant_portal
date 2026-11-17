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

from app.core.security import redact_sensitive_headers
from app.models.channel import Channel
from app.models.product import ChannelProductMapping, Product
from app.models.sync_log import WebhookAuditLog
from app.services.poppies_channel_push import (
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
            await db.commit()
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

        if not affected_skus or poppies_channel_code == "ALL":
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
                await db.commit()
            return {
                "success": True,
                "event_id": event_id or None,
                "processed_count": 0,
                "message": "Không có SKU cụ thể cần cập nhật.",
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

        clean_affected_skus = list(
            dict.fromkeys(str(s).strip() for s in affected_skus if str(s).strip())
        )

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

            updated_count += 1
            if (
                was_new_mapping
                or price_changed
                or enabled_changed
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
