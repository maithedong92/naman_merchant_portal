"""
Background Worker for Periodic Poppies ERP Pricing, Channel Governance & CTTK Stock Synchronization.
Ensures NAM-UMP prices and availability stay automatically aligned with Poppies ERP and propagate
to GrabMart, ShopeeFood, and ShopeeMart even when Outbox Webhooks are delayed or unavailable.
"""

import asyncio
from datetime import datetime, timezone
import gc
import logging
from typing import Any, Dict, Optional
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.channel import Channel
from app.models.store import Store
from app.services.poppies_channel_push import (
    propagate_channel_menu_to_external_channel,
    propagate_skus_to_external_channel,
)
from app.services.poppies_channels import get_canonical_channel_meta
from app.services.poppies_service import poppies_service

logger = logging.getLogger("naman_portal.poppies.worker")

ADVISORY_LOCK_KEY = 20261107
FULL_RECONCILE_EVERY_N_CYCLES = 144


class PoppiesSyncWorker:
    """Periodic background worker pulling Poppies ERP price-books & CTTK stock."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._is_running: bool = False
        self._background_task: Optional[asyncio.Task] = None
        self._channels_reconciled: bool = False
        self._cycle_count: int = 0
        self.last_sync_time: Optional[datetime] = None
        self.last_summary: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    async def sync_once(self, force_full: bool = False) -> Dict[str, Any]:
        """Run one coordinated sync cycle under PostgreSQL advisory lock across Uvicorn workers."""
        async with AsyncSessionLocal() as db:
            # Ensure only 1 Uvicorn worker executes the periodic sync at a time
            lock_res = await db.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": ADVISORY_LOCK_KEY},
            )
            acquired = bool(lock_res.scalar())
            if not acquired:
                return {"success": True, "skipped": True, "reason": "another_worker_running"}

            try:
                self._cycle_count += 1
                is_full_cycle = force_full or (
                    self._cycle_count % FULL_RECONCILE_EVERY_N_CYCLES == 0
                )

                if not self._channels_reconciled:
                    await poppies_service.reconcile_channels_from_poppies(
                        db=db, sync_store_outlets=True
                    )
                    self._channels_reconciled = True

                target_channels = (
                    "STORE",
                    "GRAB",
                    "SHOPEEFOOD",
                    "SHOPEEMART",
                    "WEB",
                    "SHOPEE",
                    "LAZADA",
                    "TIKTOK",
                )
                ch_rows = (
                    await db.execute(
                        select(Channel).where(Channel.code.in_(target_channels))
                    )
                ).scalars().all()

                if not force_full:
                    store_ch = next((c for c in ch_rows if c.code == "STORE"), None)
                    last_at_raw = (store_ch.config or {}).get("last_snapshot_at") if store_ch else None
                    if isinstance(last_at_raw, str) and last_at_raw:
                        try:
                            last_dt = datetime.fromisoformat(last_at_raw.replace("Z", "+00:00"))
                            if last_dt.tzinfo is None:
                                last_dt = last_dt.replace(tzinfo=timezone.utc)
                            interval_sec = max(60, int(getattr(self.settings, "POPPIES_SYNC_INTERVAL_SECONDS", 300)))
                            cooldown_sec = min(240, max(30, interval_sec - 45))
                            age_sec = (datetime.now(timezone.utc) - last_dt).total_seconds()
                            if 0 <= age_sec < cooldown_sec:
                                return {
                                    "success": True,
                                    "skipped": True,
                                    "reason": "recently_synced_by_peer_worker",
                                    "age_seconds": round(age_sec, 1),
                                }
                        except Exception:
                            pass

                last_ver_by_code: Dict[str, Optional[str]] = {
                    c.code: (c.config or {}).get("last_snapshot_version")
                    for c in ch_rows
                }
                channel_by_code: Dict[str, Channel] = {c.code: c for c in ch_rows}

                branch_codes = list(
                    (
                        await db.execute(
                            select(Store.code).where(Store.is_active == True).order_by(Store.code)
                        )
                    ).scalars().all()
                )
                if not branch_codes:
                    logger.warning("Poppies sync skipped: no active branches found.")
                    return {"success": True, "skipped": True, "reason": "no_active_branches"}

                synced_channels = {}
                for ch_code in target_channels:
                    try:
                        since_ver = (
                            None
                            if is_full_cycle
                            else last_ver_by_code.get(ch_code)
                        )
                        ch_meta = get_canonical_channel_meta(ch_code) or {}
                        ch_obj = channel_by_code.get(ch_code)
                        ch_has_partner_pricing = bool(
                            (ch_obj.config or {}).get(
                                "has_partner_pricing",
                                ch_meta.get("has_partner_pricing", False),
                            )
                            if ch_obj is not None
                            else ch_meta.get("has_partner_pricing", False)
                        )
                        # Only prefer lightweight `/price-book` for STORE-inherited channels;
                        # partner-priced channels must pull full `/snapshots` to preserve channel overrides.
                        use_price_book = (since_ver is None) and (not ch_has_partner_pricing)
                        total_prices_updated = 0
                        total_blocked_skus = 0
                        for b_code in branch_codes:
                            res = await poppies_service.sync_channel_pricing_snapshot(
                                db=db,
                                branch_code=b_code,
                                channel_code=ch_code,
                                since_version=since_ver,
                                fmt="map",
                                auto_propagate=True,
                                prefer_price_book=use_price_book,
                            )
                            total_prices_updated += res.get("prices_updated", 0)
                            total_blocked_skus += res.get("blocked_skus_count", 0)
                        synced_channels[ch_code] = {
                            "mode": (
                                "delta"
                                if since_ver
                                else ("full_price_book" if use_price_book else "full_snapshot")
                            ),
                            "branches_synced": len(branch_codes),
                            "prices_updated": total_prices_updated,
                            "blocked_skus_count": total_blocked_skus,
                        }
                    except Exception as ch_ex:
                        logger.warning(f"Poppies background pricing sync skipped {ch_code}: {ch_ex}")
                        synced_channels[ch_code] = {"error": str(ch_ex)}
                    await asyncio.sleep(0.15)

                await db.commit()
                cttk_res = await poppies_service.sync_stock_cttk_realtime(
                    db=db, create_missing_inventories=False, batch_size=10000
                )
                flipped_skus = cttk_res.get("availability_changed_skus") or []
                if flipped_skus:
                    for ext_ch in ("GRAB", "SHOPEEFOOD", "SHOPEEMART"):
                        if len(flipped_skus) > 100:
                            await propagate_channel_menu_to_external_channel(
                                db=db, raw_channel_code=ext_ch
                            )
                        else:
                            await propagate_skus_to_external_channel(
                                db=db, raw_channel_code=ext_ch, skus=flipped_skus
                            )

                self.last_sync_time = datetime.now(timezone.utc)
                self.last_error = None
                summary = {
                    "success": True,
                    "cycle": self._cycle_count,
                    "sync_mode": "full_reconcile" if is_full_cycle else "delta_fast",
                    "channels": synced_channels,
                    "inventories_updated": cttk_res.get("inventories_updated", 0),
                    "availability_flipped_count": len(flipped_skus),
                    "timestamp": self.last_sync_time.isoformat(),
                }
                self.last_summary = summary
                logger.info(f"✅ Poppies Sync Worker hoàn tất: {summary}")
                return summary
            except Exception as ex:
                self.last_error = str(ex)
                logger.error(f"Lỗi trong chu kỳ Poppies Sync Worker: {ex}")
                return {"success": False, "error": str(ex)}
            finally:
                try:
                    await db.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": ADVISORY_LOCK_KEY},
                    )
                except Exception:
                    pass
                try:
                    db.expunge_all()
                except Exception:
                    pass
                gc.collect()

    async def _background_loop(self) -> None:
        interval = max(60, int(getattr(self.settings, "POPPIES_SYNC_INTERVAL_SECONDS", 300)))
        logger.info(f"🚀 Poppies Sync Worker đã kích hoạt (chu kỳ {interval}s).")
        self._is_running = True

        # Initial short delay so container startup & TestClient fixtures stay non-blocking
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            self._is_running = False
            return

        while self._is_running:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                break
            except Exception as ex:
                logger.error(f"Ngoại lệ trong Poppies Sync Worker: {ex}")

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

        self._is_running = False
        logger.info("Poppies Sync Worker đã dừng an toàn.")

    def start_background_worker(self) -> None:
        if not getattr(self.settings, "POPPIES_SYNC_ENABLED", True):
            logger.info("Cấu hình POPPIES_SYNC_ENABLED=False. Bỏ qua Poppies Sync Worker.")
            return
        if self._background_task is None or self._background_task.done():
            self._background_task = asyncio.create_task(self._background_loop())

    def stop_background_worker(self) -> None:
        self._is_running = False
        if self._background_task and not self._background_task.done():
            self._background_task.cancel()

    async def stop_and_wait(self, timeout: float = 5.0) -> None:
        """Stop the background worker and await task completion up to timeout seconds."""
        self._is_running = False
        task = self._background_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


poppies_sync_worker = PoppiesSyncWorker()
