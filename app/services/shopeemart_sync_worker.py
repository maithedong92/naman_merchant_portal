"""
Background Worker for ShopeeMart OAuth Token Keepalive & Periodic Order Polling.
Ensures Shopee Open Platform access/refresh tokens stay proactively refreshed across Uvicorn workers
and polls recent ShopeeMart orders as a fallback when webhooks are delayed or missed.
"""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import logging
import os
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.modules.shopeemart.adapter import ShopeeMartChannelAdapter
from app.modules.shopeemart.service import shopeemart_client

logger = logging.getLogger("naman_portal.shopeemart.worker")

SHOPEEMART_SYNC_ADVISORY_LOCK_KEY = 20261124
TOKEN_REFRESH_LEAD_SECONDS = 1500  # 25 minutes prior to expiry


class ShopeeMartSyncWorker:
    """Periodic background worker for ShopeeMart OAuth token keepalive and order polling."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._is_running: bool = False
        self._stop_event: Optional[asyncio.Event] = None
        self._background_task: Optional[asyncio.Task] = None
        self.last_sync_time: Optional[datetime] = None
        self.last_summary: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    async def sync_once(self, hours_back: int = 24) -> Dict[str, Any]:
        """Execute one ShopeeMart token-refresh + order-poll cycle under a PG advisory lock."""
        self.settings = get_settings()
        if not getattr(self.settings, "SHOPEEMART_ENABLED", True) or not getattr(
            self.settings, "SHOPEEMART_POLL_ENABLED", True
        ):
            return {"success": True, "skipped": True, "reason": "shopeemart_poll_disabled"}

        if not shopeemart_client.is_configured:
            return {"success": True, "skipped": True, "reason": "shopeemart_not_configured"}

        async with AsyncSessionLocal() as db:
            acquired_lock = False
            is_pg = db.bind is not None and db.bind.dialect.name == "postgresql"
            if is_pg:
                lock_res = await db.execute(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": SHOPEEMART_SYNC_ADVISORY_LOCK_KEY},
                )
                acquired_lock = bool(lock_res.scalar())
                if not acquired_lock:
                    return {"success": True, "skipped": True, "reason": "advisory_lock_busy"}

            try:
                await shopeemart_client.hydrate_from_db(db)
                now = time.time()
                refreshed_shops: List[str] = []
                refresh_errors: List[Dict[str, str]] = []

                for sid, entry in list(shopeemart_client._token_cache.items()):
                    if not isinstance(entry, dict) or entry.get("unlinked"):
                        continue
                    rt = str(entry.get("refresh_token") or "").strip()
                    if not rt:
                        continue
                    exp_ts = float(entry.get("expires_at") or 0.0)
                    if exp_ts <= (now + TOKEN_REFRESH_LEAD_SECONDS):
                        try:
                            await shopeemart_client.refresh_access_token(shop_id=str(sid))
                            refreshed_shops.append(str(sid))
                        except Exception as ex:
                            refresh_errors.append({"shop_id": str(sid), "error": str(ex)})
                            logger.warning(f"ShopeeMart keepalive refresh failed for shop {sid}: {ex}")

                valid_shops: List[str] = []
                now_after = time.time()
                for sid, entry in list(shopeemart_client._token_cache.items()):
                    if not isinstance(entry, dict) or entry.get("unlinked"):
                        continue
                    tok = str(entry.get("access_token") or "").strip()
                    exp_ts = float(entry.get("expires_at") or 0.0)
                    if tok and not tok.startswith("mock_") and exp_ts > now_after:
                        valid_shops.append(str(sid))

                poll_results: List[Dict[str, Any]] = []
                total_checked = 0
                newly_created = 0
                status_synced = 0
                if valid_shops:
                    adapter = ShopeeMartChannelAdapter()
                    for sid in valid_shops:
                        res = await adapter.poll_recent_orders(
                            db=db,
                            shop_id=sid,
                            hours_back=hours_back,
                        )
                        poll_results.append(res)
                        total_checked += int(res.get("total_checked", 0) or 0)
                        newly_created += int(res.get("newly_created", 0) or 0)
                        status_synced += int(res.get("status_synced", 0) or 0)

                await db.commit()
                self.last_sync_time = datetime.now(timezone.utc)
                self.last_error = None
                summary = {
                    "success": True,
                    "refreshed_shops": refreshed_shops,
                    "refresh_errors": refresh_errors,
                    "valid_shops": valid_shops,
                    "total_checked": total_checked,
                    "newly_created": newly_created,
                    "status_synced": status_synced,
                    "poll_results": poll_results,
                }
                self.last_summary = summary
                return summary
            except Exception as ex:
                try:
                    await db.rollback()
                except Exception:
                    pass
                self.last_sync_time = datetime.now(timezone.utc)
                self.last_error = str(ex)
                logger.error(f"ShopeeMartSyncWorker cycle failed: {ex}")
                return {"success": False, "error": str(ex)}
            finally:
                if is_pg and acquired_lock:
                    try:
                        await db.execute(
                            text("SELECT pg_advisory_unlock(:lock_id)"),
                            {"lock_id": SHOPEEMART_SYNC_ADVISORY_LOCK_KEY},
                        )
                    except Exception:
                        pass

    async def _run_loop(self) -> None:
        assert self._stop_event is not None
        while self._is_running and not self._stop_event.is_set():
            try:
                await self.sync_once(hours_back=24)
            except Exception as exc:
                self.last_error = str(exc)
                logger.error(f"ShopeeMartSyncWorker loop exception: {exc}")

            interval = max(
                60,
                int(getattr(self.settings, "SHOPEEMART_POLL_INTERVAL_SECONDS", 180) or 180),
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def start_background_worker(self) -> None:
        self.settings = get_settings()
        if (
            str(self.settings.ENVIRONMENT).lower() == "test"
            or os.environ.get("ENVIRONMENT", "").lower() == "test"
        ):
            return
        if not getattr(self.settings, "SHOPEEMART_ENABLED", True) or not getattr(
            self.settings, "SHOPEEMART_POLL_ENABLED", True
        ):
            logger.info("ShopeeMartSyncWorker disabled by configuration.")
            return
        if self._is_running and self._background_task and not self._background_task.done():
            return
        self._is_running = True
        self._stop_event = asyncio.Event()
        self._background_task = asyncio.create_task(self._run_loop())
        logger.info("Started ShopeeMartSyncWorker (token keepalive + fallback order poll).")

    def stop_background_worker(self) -> None:
        self._is_running = False
        if self._stop_event is not None:
            self._stop_event.set()

    async def stop_and_wait(self) -> None:
        self.stop_background_worker()
        task = self._background_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=5.0)
        self._background_task = None


shopeemart_sync_worker = ShopeeMartSyncWorker()
