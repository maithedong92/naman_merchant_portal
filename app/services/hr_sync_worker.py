"""
Scheduled Background Cronjob for Internal HR Sync (naman_internal_app.user -> PostgreSQL).
Runs twice daily in Vietnam Time (UTC+7), defaulting to 07:00 and 14:00.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional, Set, Tuple
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.services.auth_service import auth_service

logger = logging.getLogger("naman_portal.hr_sync.worker")

VN_TZ = timezone(timedelta(hours=7))
DEFAULT_SCHEDULE_SLOTS: List[Tuple[int, int]] = [(7, 0), (14, 0)]
ADVISORY_LOCK_KEY = 20261120
SLOT_GRACE_WINDOW_SECONDS = 600


def parse_schedule_slots(schedule_str: str) -> List[Tuple[int, int]]:
    """Parse comma-separated HH:MM slots (UTC+7), falling back to 07:00 and 14:00."""
    parsed: Set[Tuple[int, int]] = set()
    for token in (schedule_str or "").split(","):
        cleaned = token.strip()
        if not cleaned or ":" not in cleaned:
            continue
        parts = cleaned.split(":", 1)
        try:
            hour = int(parts[0].strip())
            minute = int(parts[1].strip())
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            parsed.add((hour, minute))
    return sorted(parsed) if parsed else list(DEFAULT_SCHEDULE_SLOTS)


def compute_next_run_utc(now_utc: datetime, slots: List[Tuple[int, int]]) -> datetime:
    """Compute the next scheduled UTC datetime given UTC+7 daily (hour, minute) slots."""
    effective_slots = slots or list(DEFAULT_SCHEDULE_SLOTS)
    now_vn = now_utc.astimezone(VN_TZ)
    for hour, minute in effective_slots:
        candidate_vn = now_vn.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate_vn > now_vn:
            return candidate_vn.astimezone(timezone.utc)
    first_h, first_m = effective_slots[0]
    tomorrow_vn = (now_vn + timedelta(days=1)).replace(
        hour=first_h, minute=first_m, second=0, microsecond=0
    )
    return tomorrow_vn.astimezone(timezone.utc)


def find_due_slot_key(
    now_utc: datetime,
    slots: List[Tuple[int, int]],
    executed_slots: Set[str],
    grace_seconds: int = SLOT_GRACE_WINDOW_SECONDS,
) -> Optional[str]:
    """
    Return the 'YYYY-MM-DD HH:MM' VN slot key if a scheduled slot is currently due
    within the grace window and has not yet been executed in this worker process.
    """
    now_vn = now_utc.astimezone(VN_TZ)
    for hour, minute in slots:
        slot_vn = now_vn.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (now_vn - slot_vn).total_seconds()
        if 0 <= delta < grace_seconds:
            slot_key = f"{slot_vn:%Y-%m-%d %H:%M}"
            if slot_key not in executed_slots:
                return slot_key
    return None


class HRSyncWorker:
    """2x/day scheduled cronjob syncing HR accounts from internal MongoDB."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._is_running: bool = False
        self._executed_slots: Set[str] = set()
        self._last_slot_key: Optional[str] = None
        self._last_run_at: Optional[datetime] = None
        self._last_result: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._total_runs: int = 0

    def get_slots(self) -> List[Tuple[int, int]]:
        self.settings = get_settings()
        return parse_schedule_slots(getattr(self.settings, "HR_SYNC_SCHEDULE_HOURS_VN", "07:00,14:00"))

    def start_background_worker(self) -> None:
        self.settings = get_settings()
        if not getattr(self.settings, "HR_SYNC_ENABLED", True):
            logger.info("⏸️ HRSyncWorker đã được tắt qua HR_SYNC_ENABLED=False.")
            return
        if not getattr(self.settings, "AUTH_MONGO_ENABLED", True):
            logger.info("⏸️ HRSyncWorker không khởi động vì AUTH_MONGO_ENABLED=False.")
            return
        if self._task is not None and not self._task.done():
            return

        slots = self.get_slots()
        slot_labels = ", ".join(f"{h:02d}:{m:02d}" for h, m in slots)
        self._stop_event = asyncio.Event()
        self._is_running = True
        self._task = asyncio.create_task(self._run_loop(), name="hr_sync_cronjob")
        logger.info(f"🚀 HRSyncWorker started (Cronjob 2 lần/ngày tại {slot_labels} UTC+7).")

    async def stop_and_wait(self, timeout: float = 5.0) -> None:
        self._is_running = False
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass
            finally:
                self._task = None

    def _record_run_success(self, trigger: str, summary: Dict[str, Any]) -> Dict[str, Any]:
        """Update worker tracking metrics after a completed HR sync run."""
        self._total_runs += 1
        self._last_run_at = datetime.now(timezone.utc)
        self._last_error = None
        self._last_result = {
            "success": True,
            "trigger": trigger,
            **summary,
        }
        return self._last_result

    def record_manual_run(self, summary: Dict[str, Any]) -> None:
        """Record a manual sync triggered via POST /api/v1/users/sync-mongo."""
        self._record_run_success("manual", summary)

    async def sync_once(
        self,
        trigger: str = "cron",
        purge_demo_accounts: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Run one HR synchronization cycle with a PostgreSQL advisory lock."""
        self.settings = get_settings()
        if not getattr(self.settings, "AUTH_MONGO_ENABLED", True):
            return {"success": True, "skipped": True, "reason": "auth_mongo_disabled"}

        effective_purge = (
            bool(getattr(self.settings, "HR_SYNC_PURGE_DEMO_ACCOUNTS", False))
            if purge_demo_accounts is None
            else bool(purge_demo_accounts)
        )

        async with AsyncSessionLocal() as db:
            acquired_lock = False
            is_pg = db.bind is not None and db.bind.dialect.name == "postgresql"
            if is_pg:
                lock_res = await db.execute(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": ADVISORY_LOCK_KEY},
                )
                acquired_lock = bool(lock_res.scalar())
                if not acquired_lock:
                    logger.info("⏭️ HRSyncWorker skipped: another worker holds advisory lock.")
                    return {"success": True, "skipped": True, "reason": "advisory_lock_busy"}

            try:
                summary = await auth_service.sync_users_from_mongodb(
                    db=db,
                    purge_demo_accounts=effective_purge,
                )
                result = self._record_run_success(trigger, summary)
                logger.info(
                    f"✅ HR Sync Cronjob ({trigger}) hoàn tất: "
                    f"{summary.get('mongo_active_users', 0)} nhân sự MongoDB, "
                    f"vô hiệu hóa {summary.get('stale_accounts_deactivated', 0)}."
                )
                return result
            except Exception as ex:
                try:
                    await db.rollback()
                except Exception:
                    pass
                self._last_run_at = datetime.now(timezone.utc)
                self._last_error = str(ex)
                logger.error(f"❌ HR Sync Cronjob ({trigger}) lỗi: {ex}")
                return {"success": False, "trigger": trigger, "error": str(ex)}
            finally:
                if is_pg and acquired_lock:
                    try:
                        await db.execute(
                            text("SELECT pg_advisory_unlock(:lock_id)"),
                            {"lock_id": ADVISORY_LOCK_KEY},
                        )
                    except Exception:
                        pass

    async def _run_loop(self) -> None:
        assert self._stop_event is not None
        while self._is_running and not self._stop_event.is_set():
            try:
                now_utc = datetime.now(timezone.utc)
                slots = self.get_slots()
                due_slot = find_due_slot_key(now_utc, slots, self._executed_slots)
                if due_slot is not None:
                    self._executed_slots.add(due_slot)
                    self._last_slot_key = due_slot
                    # Prune old day keys to keep memory bounded
                    today_prefix = now_utc.astimezone(VN_TZ).strftime("%Y-%m-%d")
                    self._executed_slots = {
                        k for k in self._executed_slots if k.startswith(today_prefix)
                    }
                    await self.sync_once(trigger=f"cron:{due_slot}")
            except Exception as exc:
                logger.error(f"HRSyncWorker loop exception: {exc}")
                self._last_error = str(exc)

            interval = max(5, int(getattr(self.settings, "HR_SYNC_CHECK_INTERVAL_SECONDS", 30) or 30))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def get_status(self) -> Dict[str, Any]:
        slots = self.get_slots()
        now_utc = datetime.now(timezone.utc)
        next_utc = compute_next_run_utc(now_utc, slots)
        next_vn = next_utc.astimezone(VN_TZ)
        return {
            "enabled": bool(getattr(self.settings, "HR_SYNC_ENABLED", True)),
            "is_running": self._is_running,
            "schedule_vn": ", ".join(f"{h:02d}:{m:02d}" for h, m in slots),
            "timezone": "Asia/Ho_Chi_Minh (UTC+7)",
            "runs_per_day": len(slots),
            "next_run_at": next_utc.isoformat(),
            "next_run_vn": next_vn.strftime("%Y-%m-%d %H:%M %Z"),
            "last_slot_key": self._last_slot_key,
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "last_result": self._last_result,
            "last_error": self._last_error,
            "total_runs": self._total_runs,
        }


hr_sync_worker = HRSyncWorker()
