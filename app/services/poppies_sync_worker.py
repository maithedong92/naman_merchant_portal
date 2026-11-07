"""
Background Worker for Periodic Poppies ERP Pricing, Channel Governance & CTTK Stock Synchronization.
Ensures NAM-UMP prices and availability stay automatically aligned with Poppies ERP and propagate
to GrabMart, ShopeeFood, and ShopeeMart even when Outbox Webhooks are delayed or unavailable.
"""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.services.poppies_channel_push import (
    propagate_channel_menu_to_external_channel,
    propagate_skus_to_external_channel,
)
from app.services.poppies_service import poppies_service

logger = logging.getLogger("naman_portal.poppies.worker")

ADVISORY_LOCK_KEY = 20261107


class PoppiesSyncWorker:
    """Periodic background worker pulling Poppies ERP price-books & CTTK stock."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._is_running: bool = False
        self._background_task: Optional[asyncio.Task] = None
        self._channels_reconciled: bool = False
        self.last_sync_time: Optional[datetime] = None
        self.last_summary: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    async def sync_once(self) -> Dict[str, Any]:
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
                if not self._channels_reconciled:
                    await poppies_service.reconcile_channels_from_poppies(
                        db=db, sync_store_outlets=True
                    )
                    self._channels_reconciled = True

                synced_channels = {}
                for ch_code in ("STORE", "GRAB", "SHOPEEFOOD", "SHOPEEMART"):
                    try:
                        res = await poppies_service.sync_channel_pricing_snapshot(
                            db=db,
                            branch_code="10001",
                            channel_code=ch_code,
                            fmt="map",
                            auto_propagate=True,
                        )
                        synced_channels[ch_code] = {
                            "prices_updated": res.get("prices_updated", 0),
                            "blocked_skus_count": res.get("blocked_skus_count", 0),
                        }
                    except Exception as ch_ex:
                        logger.warning(f"Poppies background pricing sync skipped {ch_code}: {ch_ex}")
                        synced_channels[ch_code] = {"error": str(ch_ex)}

                cttk_res = await poppies_service.sync_stock_cttk_realtime(
                    db=db, create_missing_inventories=False
                )
                flipped_skus = cttk_res.get("availability_changed_skus") or []
                if flipped_skus:
                    for ext_ch in ("GRAB", "SHOPEEFOOD"):
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


poppies_sync_worker = PoppiesSyncWorker()
