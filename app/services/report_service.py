import asyncio
import csv
import io
import logging
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.channel import Channel
from app.models.order import UnifiedOrder, UnifiedOrderStatus
from app.models.store import Store
from app.schemas.report import (
    ChannelAnalyticsItem,
    DailyTrendItem,
    FinancialSummary,
    FullAnalyticsReport,
    ReconciliationOrderItem,
    StoreAnalyticsItem,
)

logger = logging.getLogger("naman_portal.reports")

VN_TZ = timezone(timedelta(hours=7))

HARAVAN_STATUS_MAP: Dict[str, UnifiedOrderStatus] = {
    "Hoàn thành": UnifiedOrderStatus.DELIVERED,
    "Đã hủy": UnifiedOrderStatus.CANCELLED,
    "Đã gửi vận đơn": UnifiedOrderStatus.PICKED_UP,
    "Đã đóng gói": UnifiedOrderStatus.READY,
    "Đã phân tuyến": UnifiedOrderStatus.PREPARING,
    "Yêu cầu phân tuyến lại": UnifiedOrderStatus.ACCEPTED,
    "Đơn hàng mới": UnifiedOrderStatus.PENDING,
}

HARAVAN_STORE_NAMES: Dict[str, str] = {
    "10001": "Nam An Market - 21 Thảo Điền",
    "10004": "Nam An Market - 46 Nguyễn Lương Bằng",
    "10005": "Nam An Market - 17 Mai Chí Thọ",
    "10006": "Nam An Market - 303 Nguyễn Văn Trỗi",
    "10008": "Kho Online E-Commerce (10008)",
}


class ReportService:
    """Service handling multi-channel financial analytics and reconciliation for Nam An Market."""

    @staticmethod
    def _sanitize_csv_cell(val: Any) -> str:
        """Prevent Excel / spreadsheet formula injection by escaping leading formula triggers."""
        if val is None:
            return ""
        s = str(val)
        if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
            return f"'{s}"
        return s

    @staticmethod
    def _build_filter_criteria(
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        channel_code: Optional[str] = None,
        store_code: Optional[str] = None,
    ):
        """Construct SQLAlchemy filter clauses from Vietnam (+07:00) date ranges and channel/store codes."""
        criteria = []

        if from_date:
            from_dt = datetime.combine(from_date, time.min, tzinfo=VN_TZ).astimezone(timezone.utc)
            criteria.append(UnifiedOrder.order_time >= from_dt)

        if to_date:
            to_dt = datetime.combine(to_date, time.max, tzinfo=VN_TZ).astimezone(timezone.utc)
            criteria.append(UnifiedOrder.order_time <= to_dt)

        if channel_code:
            criteria.append(Channel.code == channel_code.upper())

        if store_code:
            criteria.append(Store.code == str(store_code))

        return criteria

    @staticmethod
    def _resolve_date_window(
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        default_days: int = 30,
    ) -> Tuple[date, date]:
        """Enforce a bounded date window (default 30 days) when from_date/to_date are omitted."""
        today_vn = datetime.now(VN_TZ).date()
        if from_date is None and to_date is None:
            resolved_to = today_vn
            resolved_from = today_vn - timedelta(days=default_days - 1)
        elif from_date is not None and to_date is None:
            resolved_from = from_date
            resolved_to = today_vn
        elif from_date is None and to_date is not None:
            resolved_to = to_date
            resolved_from = to_date - timedelta(days=default_days - 1)
        else:
            resolved_from = from_date  # type: ignore[assignment]
            resolved_to = to_date  # type: ignore[assignment]
        return resolved_from, resolved_to

    @classmethod
    def _fetch_haravan_orders_for_report(
        cls,
        db: Any,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        channel_code: Optional[str] = None,
        store_code: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        existing_keys: Optional[Set[str]] = None,
    ) -> List[Any]:
        """
        Resiliently fetch Haravan Omnichannel orders from MongoDB for unified reporting,
        deduplicating any orders already present in PostgreSQL unified_orders.
        """
        if not isinstance(db, AsyncSession):
            return []

        try:
            from app.modules.haravan.sync_worker import haravan_sync_worker

            col = haravan_sync_worker.get_mongo_collection()
            mongo_query: Dict[str, Any] = {}
            if store_code:
                mongo_query["internal.assigned_store"] = str(store_code)

            docs = list(col.find(mongo_query).sort("created_at", -1).limit(2000))
        except Exception as ex:
            logger.debug(f"Skipping Haravan MongoDB in report aggregation: {ex}")
            return []

        seen = set(existing_keys or set())
        from_dt = datetime.combine(from_date, time.min, tzinfo=VN_TZ).astimezone(timezone.utc) if from_date else None
        to_dt = datetime.combine(to_date, time.max, tzinfo=VN_TZ).astimezone(timezone.utc) if to_date else None
        search_kw = search.strip().lower() if search and search.strip() else None

        results: List[Any] = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue

            hrv_id = str(doc.get("id") or "").strip()
            order_number = str(doc.get("order_number") or doc.get("name") or hrv_id).strip()
            clean_number = order_number.lstrip("#")

            # Deduplicate against PostgreSQL unified_orders (e.g. Shopee orders synced to both)
            if (hrv_id and hrv_id in seen) or (order_number and order_number in seen) or (clean_number and clean_number in seen):
                continue

            internal = doc.get("internal") if isinstance(doc.get("internal"), dict) else {}
            proc_status = str(internal.get("order_processing_status") or "").strip()
            mapped_status = HARAVAN_STATUS_MAP.get(proc_status)
            if mapped_status is None:
                if str(doc.get("cancelled_status") or "").lower() == "cancelled":
                    mapped_status = UnifiedOrderStatus.CANCELLED
                elif str(doc.get("fulfillment_status") or "").lower() == "fulfilled":
                    mapped_status = UnifiedOrderStatus.DELIVERED
                else:
                    mapped_status = UnifiedOrderStatus.PENDING

            if status and mapped_status.value != status.upper():
                continue

            source_raw = str(doc.get("source") or doc.get("source_name") or "haravan").lower()
            if "shopee" in source_raw:
                ch_code = "SHOPEEMART"
                ch_name = "Shopee E-Commerce"
            else:
                ch_code = "HARAVAN"
                ch_name = "Haravan Omnichannel"

            if channel_code and ch_code != channel_code.upper():
                continue

            assigned_store = str(internal.get("assigned_store") or "").strip()
            if store_code and assigned_store != str(store_code):
                continue

            st_code = assigned_store or "UNASSIGNED"
            st_name = HARAVAN_STORE_NAMES.get(st_code, f"Chi nhánh {st_code}" if assigned_store else "Chưa phân tuyến (Haravan)")

            # Parse created_at timestamp
            created_raw = doc.get("created_at")
            order_dt = datetime.now(timezone.utc)
            if isinstance(created_raw, str) and created_raw.strip():
                try:
                    order_dt = datetime.fromisoformat(created_raw.strip().replace("Z", "+00:00"))
                    if order_dt.tzinfo is None:
                        order_dt = order_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            elif isinstance(created_raw, datetime):
                order_dt = created_raw if created_raw.tzinfo else created_raw.replace(tzinfo=timezone.utc)

            if from_dt and order_dt < from_dt:
                continue
            if to_dt and order_dt > to_dt:
                continue

            # Extract customer name
            billing = doc.get("billing_address") if isinstance(doc.get("billing_address"), dict) else {}
            shipping = doc.get("shipping_address") if isinstance(doc.get("shipping_address"), dict) else {}
            customer = doc.get("customer") if isinstance(doc.get("customer"), dict) else {}
            cust_name = (
                billing.get("name")
                or shipping.get("name")
                or f"{customer.get('last_name') or ''} {customer.get('first_name') or ''}".strip()
                or "Khách lẻ Haravan"
            )

            if search_kw:
                haystack = f"{order_number} {hrv_id} {cust_name}".lower()
                if search_kw not in haystack:
                    continue

            completion_info = internal.get("completion_info") if isinstance(internal.get("completion_info"), dict) else {}
            actual_total = completion_info.get("actual_total")
            total_val = float(actual_total if actual_total is not None else (doc.get("total_price") or 0.0))
            subtotal_val = float(doc.get("subtotal_price") or total_val)
            discount_val = float(doc.get("total_discounts") or 0.0)
            shipping_lines = doc.get("shipping_lines") if isinstance(doc.get("shipping_lines"), list) else []
            delivery_fee_val = float(
                completion_info.get("actual_shipping")
                or sum(float(s.get("price") or 0.0) for s in shipping_lines if isinstance(s, dict))
                or 0.0
            )
            cancel_reason = internal.get("cancel_reason") or doc.get("cancel_reason")

            if hrv_id:
                seen.add(hrv_id)
            if order_number:
                seen.add(order_number)

            results.append(
                SimpleNamespace(
                    id=f"hrv-{hrv_id or clean_number}",
                    order_code=order_number if order_number.startswith("#") else f"HRV-{clean_number}",
                    channel_order_id=hrv_id or order_number,
                    display_order_id=order_number,
                    channel_code=ch_code,
                    channel_name=ch_name,
                    store_code=st_code,
                    store_name=st_name,
                    status=mapped_status,
                    order_time=order_dt,
                    customer_name=cust_name,
                    subtotal_amount=subtotal_val,
                    discount_amount=discount_val,
                    delivery_fee=delivery_fee_val,
                    total_amount=total_val,
                    cancellation_reason=cancel_reason,
                )
            )

        return results

    @classmethod
    async def get_full_analytics(
        cls,
        db: AsyncSession,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        channel_code: Optional[str] = None,
        store_code: Optional[str] = None,
    ) -> FullAnalyticsReport:
        """Calculate comprehensive financial and operational analytics across channels and stores."""
        resolved_from, resolved_to = cls._resolve_date_window(from_date, to_date)
        criteria = cls._build_filter_criteria(resolved_from, resolved_to, channel_code, store_code)

        # 1. SQL Aggregation for PG Financial Summary (avoids loading full UnifiedOrder rows)
        summary_stmt = (
            select(
                func.count(UnifiedOrder.id),
                func.coalesce(func.sum(case((UnifiedOrder.status == UnifiedOrderStatus.DELIVERED, 1), else_=0)), 0),
                func.coalesce(func.sum(case((UnifiedOrder.status == UnifiedOrderStatus.CANCELLED, 1), else_=0)), 0),
                func.coalesce(func.sum(case((UnifiedOrder.status != UnifiedOrderStatus.CANCELLED, UnifiedOrder.total_amount), else_=0.0)), 0.0),
                func.coalesce(func.sum(case((UnifiedOrder.status == UnifiedOrderStatus.DELIVERED, UnifiedOrder.total_amount), else_=0.0)), 0.0),
                func.coalesce(func.sum(case((UnifiedOrder.status != UnifiedOrderStatus.CANCELLED, UnifiedOrder.subtotal_amount), else_=0.0)), 0.0),
                func.coalesce(func.sum(case((UnifiedOrder.status != UnifiedOrderStatus.CANCELLED, UnifiedOrder.discount_amount), else_=0.0)), 0.0),
                func.coalesce(func.sum(case((UnifiedOrder.status != UnifiedOrderStatus.CANCELLED, UnifiedOrder.delivery_fee), else_=0.0)), 0.0),
            )
            .join(Channel, UnifiedOrder.channel_id == Channel.id)
            .join(Store, UnifiedOrder.store_id == Store.id)
        )
        if criteria:
            summary_stmt = summary_stmt.where(*criteria)
        sum_row = (await db.execute(summary_stmt)).one()

        pg_total = int(sum_row[0] or 0)
        pg_completed = int(sum_row[1] or 0)
        pg_cancelled = int(sum_row[2] or 0)
        pg_gross_rev = float(sum_row[3] or 0.0)
        pg_completed_rev = float(sum_row[4] or 0.0)
        pg_subtotal = float(sum_row[5] or 0.0)
        pg_discounts = float(sum_row[6] or 0.0)
        pg_delivery_fees = float(sum_row[7] or 0.0)

        # Lightweight key projection for MongoDB deduplication
        keys_stmt = (
            select(
                UnifiedOrder.channel_order_id,
                UnifiedOrder.order_code,
                UnifiedOrder.display_order_id,
            )
            .join(Channel, UnifiedOrder.channel_id == Channel.id)
            .join(Store, UnifiedOrder.store_id == Store.id)
        )
        if criteria:
            keys_stmt = keys_stmt.where(*criteria)
        existing_keys: Set[str] = set()
        if isinstance(db, AsyncSession):
            keys_res = await db.execute(keys_stmt)
            for ch_oid, o_code, disp_id in keys_res.all():
                if ch_oid:
                    existing_keys.add(str(ch_oid).strip())
                if o_code:
                    existing_keys.add(str(o_code).strip())
                if disp_id:
                    existing_keys.add(str(disp_id).strip())

        hrv_orders = await asyncio.to_thread(
            cls._fetch_haravan_orders_for_report,
            db=db,
            from_date=resolved_from,
            to_date=resolved_to,
            channel_code=channel_code,
            store_code=store_code,
            existing_keys=existing_keys,
        )

        # Combine PG SQL aggregates + Haravan Mongo orders
        hrv_total = len(hrv_orders)
        hrv_completed = sum(1 for o in hrv_orders if o.status == UnifiedOrderStatus.DELIVERED)
        hrv_cancelled = sum(1 for o in hrv_orders if o.status == UnifiedOrderStatus.CANCELLED)
        hrv_non_cancelled = [o for o in hrv_orders if o.status != UnifiedOrderStatus.CANCELLED]

        total_orders = pg_total + hrv_total
        completed_orders = pg_completed + hrv_completed
        cancelled_orders = pg_cancelled + hrv_cancelled
        active_orders = total_orders - completed_orders - cancelled_orders

        total_gross_revenue = pg_gross_rev + sum(float(o.total_amount or 0.0) for o in hrv_non_cancelled)
        completed_revenue = pg_completed_rev + sum(
            float(o.total_amount or 0.0) for o in hrv_orders if o.status == UnifiedOrderStatus.DELIVERED
        )
        total_subtotal = pg_subtotal + sum(float(o.subtotal_amount or 0.0) for o in hrv_non_cancelled)
        total_discounts = pg_discounts + sum(float(o.discount_amount or 0.0) for o in hrv_non_cancelled)
        total_delivery_fees = pg_delivery_fees + sum(float(o.delivery_fee or 0.0) for o in hrv_non_cancelled)

        # Platform commission estimated ~20%
        estimated_commission = round(total_gross_revenue * 0.20, 2)
        estimated_net_payout = round(total_gross_revenue - estimated_commission, 2)

        completion_rate = round((completed_orders / total_orders * 100), 1) if total_orders > 0 else 0.0
        cancellation_rate = round((cancelled_orders / total_orders * 100), 1) if total_orders > 0 else 0.0
        non_cancelled_count = (pg_total - pg_cancelled) + len(hrv_non_cancelled)
        aov = round(total_gross_revenue / non_cancelled_count, 2) if non_cancelled_count else 0.0

        summary = FinancialSummary(
            total_gross_revenue=total_gross_revenue,
            completed_revenue=completed_revenue,
            total_subtotal=total_subtotal,
            total_discounts=total_discounts,
            total_delivery_fees=total_delivery_fees,
            estimated_commission=estimated_commission,
            estimated_net_payout=estimated_net_payout,
            total_orders=total_orders,
            completed_orders=completed_orders,
            cancelled_orders=cancelled_orders,
            active_orders=active_orders,
            completion_rate=completion_rate,
            cancellation_rate=cancellation_rate,
            average_order_value=aov,
        )

        # 2. Channel Analytics Breakdown (SQL GROUP BY)
        channels_res = await db.execute(select(Channel).order_by(Channel.code))
        all_channels = channels_res.scalars().all()

        channel_map: Dict[str, ChannelAnalyticsItem] = {
            c.code: ChannelAnalyticsItem(
                channel_code=c.code,
                channel_name=c.name,
                order_count=0,
                completed_count=0,
                cancelled_count=0,
                revenue=0.0,
                share_percentage=0.0,
            )
            for c in all_channels
        }

        ch_agg_stmt = (
            select(
                Channel.code,
                Channel.name,
                func.count(UnifiedOrder.id),
                func.coalesce(func.sum(case((UnifiedOrder.status == UnifiedOrderStatus.DELIVERED, 1), else_=0)), 0),
                func.coalesce(func.sum(case((UnifiedOrder.status == UnifiedOrderStatus.CANCELLED, 1), else_=0)), 0),
                func.coalesce(func.sum(case((UnifiedOrder.status != UnifiedOrderStatus.CANCELLED, UnifiedOrder.total_amount), else_=0.0)), 0.0),
            )
            .join(Channel, UnifiedOrder.channel_id == Channel.id)
            .join(Store, UnifiedOrder.store_id == Store.id)
            .group_by(Channel.code, Channel.name)
        )
        if criteria:
            ch_agg_stmt = ch_agg_stmt.where(*criteria)
        for ch_code, ch_name, cnt, comp_cnt, canc_cnt, rev in (await db.execute(ch_agg_stmt)).all():
            if not ch_code:
                continue
            if ch_code not in channel_map:
                channel_map[ch_code] = ChannelAnalyticsItem(
                    channel_code=ch_code,
                    channel_name=ch_name or ch_code,
                    order_count=0,
                    completed_count=0,
                    cancelled_count=0,
                    revenue=0.0,
                    share_percentage=0.0,
                )
            item = channel_map[ch_code]
            item.order_count += int(cnt or 0)
            item.completed_count += int(comp_cnt or 0)
            item.cancelled_count += int(canc_cnt or 0)
            item.revenue += float(rev or 0.0)

        for o in hrv_orders:
            ch_code = o.channel_code
            if not ch_code:
                continue
            if ch_code not in channel_map:
                channel_map[ch_code] = ChannelAnalyticsItem(
                    channel_code=ch_code,
                    channel_name=getattr(o, "channel_name", None) or ch_code,
                    order_count=0,
                    completed_count=0,
                    cancelled_count=0,
                    revenue=0.0,
                    share_percentage=0.0,
                )
            item = channel_map[ch_code]
            item.order_count += 1
            if o.status == UnifiedOrderStatus.DELIVERED:
                item.completed_count += 1
            elif o.status == UnifiedOrderStatus.CANCELLED:
                item.cancelled_count += 1
            if o.status != UnifiedOrderStatus.CANCELLED:
                item.revenue += float(o.total_amount or 0.0)

        channel_items = list(channel_map.values())
        if total_gross_revenue > 0:
            for item in channel_items:
                item.share_percentage = round((item.revenue / total_gross_revenue * 100), 1)

        # 3. Store Analytics Breakdown (SQL GROUP BY)
        stores_res = await db.execute(select(Store).order_by(Store.code))
        all_stores = stores_res.scalars().all()

        store_map: Dict[str, StoreAnalyticsItem] = {
            s.code: StoreAnalyticsItem(
                store_code=s.code,
                store_name=s.name,
                order_count=0,
                completed_count=0,
                cancelled_count=0,
                revenue=0.0,
                share_percentage=0.0,
            )
            for s in all_stores
        }

        st_agg_stmt = (
            select(
                Store.code,
                Store.name,
                func.count(UnifiedOrder.id),
                func.coalesce(func.sum(case((UnifiedOrder.status == UnifiedOrderStatus.DELIVERED, 1), else_=0)), 0),
                func.coalesce(func.sum(case((UnifiedOrder.status == UnifiedOrderStatus.CANCELLED, 1), else_=0)), 0),
                func.coalesce(func.sum(case((UnifiedOrder.status != UnifiedOrderStatus.CANCELLED, UnifiedOrder.total_amount), else_=0.0)), 0.0),
            )
            .join(Channel, UnifiedOrder.channel_id == Channel.id)
            .join(Store, UnifiedOrder.store_id == Store.id)
            .group_by(Store.code, Store.name)
        )
        if criteria:
            st_agg_stmt = st_agg_stmt.where(*criteria)
        for st_code, st_name, cnt, comp_cnt, canc_cnt, rev in (await db.execute(st_agg_stmt)).all():
            if not st_code:
                continue
            if st_code not in store_map:
                store_map[st_code] = StoreAnalyticsItem(
                    store_code=st_code,
                    store_name=st_name or f"Chi nhánh {st_code}",
                    order_count=0,
                    completed_count=0,
                    cancelled_count=0,
                    revenue=0.0,
                    share_percentage=0.0,
                )
            s_item = store_map[st_code]
            s_item.order_count += int(cnt or 0)
            s_item.completed_count += int(comp_cnt or 0)
            s_item.cancelled_count += int(canc_cnt or 0)
            s_item.revenue += float(rev or 0.0)

        for o in hrv_orders:
            st_code = o.store_code
            if not st_code:
                continue
            if st_code not in store_map:
                store_map[st_code] = StoreAnalyticsItem(
                    store_code=st_code,
                    store_name=getattr(o, "store_name", None) or f"Chi nhánh {st_code}",
                    order_count=0,
                    completed_count=0,
                    cancelled_count=0,
                    revenue=0.0,
                    share_percentage=0.0,
                )
            s_item = store_map[st_code]
            s_item.order_count += 1
            if o.status == UnifiedOrderStatus.DELIVERED:
                s_item.completed_count += 1
            elif o.status == UnifiedOrderStatus.CANCELLED:
                s_item.cancelled_count += 1
            if o.status != UnifiedOrderStatus.CANCELLED:
                s_item.revenue += float(o.total_amount or 0.0)

        store_items = list(store_map.values())
        if total_gross_revenue > 0:
            for s_item in store_items:
                s_item.share_percentage = round((s_item.revenue / total_gross_revenue * 100), 1)

        # 4. Daily Trends (lightweight 3-column projection instead of full ORM objects)
        daily_dict: Dict[str, DailyTrendItem] = {}

        today_vn = datetime.now(VN_TZ).date()
        start_date = from_date or (today_vn - timedelta(days=6))
        end_date = to_date or today_vn

        curr_d = start_date
        while curr_d <= end_date:
            ds = curr_d.strftime("%Y-%m-%d")
            daily_dict[ds] = DailyTrendItem(date=ds, revenue=0.0, order_count=0, completed_count=0)
            curr_d += timedelta(days=1)

        trend_stmt = (
            select(UnifiedOrder.order_time, UnifiedOrder.status, UnifiedOrder.total_amount)
            .join(Channel, UnifiedOrder.channel_id == Channel.id)
            .join(Store, UnifiedOrder.store_id == Store.id)
        )
        if criteria:
            trend_stmt = trend_stmt.where(*criteria)
        for o_time, o_status, o_total in (await db.execute(trend_stmt)).all():
            if o_time:
                dt_vn = (
                    o_time.astimezone(VN_TZ)
                    if o_time.tzinfo
                    else o_time.replace(tzinfo=timezone.utc).astimezone(VN_TZ)
                )
                ds = dt_vn.strftime("%Y-%m-%d")
                if ds not in daily_dict:
                    daily_dict[ds] = DailyTrendItem(date=ds, revenue=0.0, order_count=0, completed_count=0)
                d_item = daily_dict[ds]
                d_item.order_count += 1
                if o_status == UnifiedOrderStatus.DELIVERED:
                    d_item.completed_count += 1
                if o_status != UnifiedOrderStatus.CANCELLED:
                    d_item.revenue += float(o_total or 0.0)

        for o in hrv_orders:
            if o.order_time:
                dt_vn = (
                    o.order_time.astimezone(VN_TZ)
                    if o.order_time.tzinfo
                    else o.order_time.replace(tzinfo=timezone.utc).astimezone(VN_TZ)
                )
                ds = dt_vn.strftime("%Y-%m-%d")
                if ds not in daily_dict:
                    daily_dict[ds] = DailyTrendItem(date=ds, revenue=0.0, order_count=0, completed_count=0)
                d_item = daily_dict[ds]
                d_item.order_count += 1
                if o.status == UnifiedOrderStatus.DELIVERED:
                    d_item.completed_count += 1
                if o.status != UnifiedOrderStatus.CANCELLED:
                    d_item.revenue += float(o.total_amount or 0.0)

        daily_trends = sorted(daily_dict.values(), key=lambda x: x.date)

        return FullAnalyticsReport(
            filter_from=resolved_from.isoformat(),
            filter_to=resolved_to.isoformat(),
            summary=summary,
            channels=channel_items,
            stores=store_items,
            daily_trends=daily_trends,
        )

    @classmethod
    async def get_reconciliation_orders(
        cls,
        db: AsyncSession,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        channel_code: Optional[str] = None,
        store_code: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[ReconciliationOrderItem], int]:
        """Fetch paginated financial reconciliation orders."""
        resolved_from, resolved_to = cls._resolve_date_window(from_date, to_date)
        criteria = cls._build_filter_criteria(resolved_from, resolved_to, channel_code, store_code)

        if search:
            search_clean = f"%{search.strip()}%"
            criteria.append(
                (UnifiedOrder.order_code.ilike(search_clean))
                | (UnifiedOrder.channel_order_id.ilike(search_clean))
                | (UnifiedOrder.display_order_id.ilike(search_clean))
                | (UnifiedOrder.customer_name.ilike(search_clean))
            )

        # Count total
        count_stmt = (
            select(func.count(UnifiedOrder.id))
            .join(Channel, UnifiedOrder.channel_id == Channel.id)
            .join(Store, UnifiedOrder.store_id == Store.id)
        )
        if criteria:
            count_stmt = count_stmt.where(*criteria)
        total_count = (await db.execute(count_stmt)).scalar_one()

        # Paginated items
        stmt = (
            select(UnifiedOrder)
            .join(Channel, UnifiedOrder.channel_id == Channel.id)
            .join(Store, UnifiedOrder.store_id == Store.id)
            .options(selectinload(UnifiedOrder.channel), selectinload(UnifiedOrder.store))
            .order_by(desc(UnifiedOrder.order_time))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        if criteria:
            stmt = stmt.where(*criteria)

        res = await db.execute(stmt)
        orders: List[Any] = list(res.scalars().all())

        # Merge Haravan MongoDB orders when running against a live AsyncSession
        existing_keys: Set[str] = set()
        if isinstance(db, AsyncSession):
            keys_stmt = (
                select(
                    UnifiedOrder.channel_order_id,
                    UnifiedOrder.order_code,
                    UnifiedOrder.display_order_id,
                )
                .join(Channel, UnifiedOrder.channel_id == Channel.id)
                .join(Store, UnifiedOrder.store_id == Store.id)
            )
            if criteria:
                keys_stmt = keys_stmt.where(*criteria)
            for ch_oid, o_code, disp_id in (await db.execute(keys_stmt)).all():
                if ch_oid:
                    existing_keys.add(str(ch_oid).strip())
                if o_code:
                    existing_keys.add(str(o_code).strip())
                if disp_id:
                    existing_keys.add(str(disp_id).strip())

        hrv_orders = await asyncio.to_thread(
            cls._fetch_haravan_orders_for_report,
            db=db,
            from_date=resolved_from,
            to_date=resolved_to,
            channel_code=channel_code,
            store_code=store_code,
            search=search,
            existing_keys=existing_keys,
        )
        if hrv_orders:
            # Fetch at most page * page_size top PG rows to merge-sort uniformly without loading all rows
            top_n = page * page_size
            top_stmt = (
                select(UnifiedOrder)
                .join(Channel, UnifiedOrder.channel_id == Channel.id)
                .join(Store, UnifiedOrder.store_id == Store.id)
                .options(selectinload(UnifiedOrder.channel), selectinload(UnifiedOrder.store))
                .order_by(desc(UnifiedOrder.order_time))
                .limit(top_n)
            )
            if criteria:
                top_stmt = top_stmt.where(*criteria)
            top_pg = list((await db.execute(top_stmt)).scalars().all())
            combined = top_pg + hrv_orders
            combined.sort(
                key=lambda x: x.order_time or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            total_count = int(total_count) + len(hrv_orders)
            start_idx = (page - 1) * page_size
            orders = combined[start_idx : start_idx + page_size]

        items = []
        for o in orders:
            tot = float(o.total_amount or 0.0)
            if o.status == UnifiedOrderStatus.CANCELLED:
                comm = 0.0
                net = 0.0
            else:
                comm = round(tot * 0.20, 2)
                net = round(tot - comm, 2)

            item = ReconciliationOrderItem(
                id=o.id,
                order_code=o.order_code,
                channel_order_id=o.channel_order_id,
                display_order_id=o.display_order_id,
                channel_code=o.channel_code or "UNKNOWN",
                channel_name=o.channel_name or "Chưa phân loại",
                store_code=o.store_code or "N/A",
                store_name=o.store_name or "Chi nhánh Nam An",
                status=o.status.value,
                order_time=o.order_time,
                customer_name=o.customer_name,
                subtotal_amount=float(o.subtotal_amount or 0.0),
                discount_amount=float(o.discount_amount or 0.0),
                delivery_fee=float(o.delivery_fee or 0.0),
                total_amount=tot,
                estimated_commission=comm,
                estimated_net_amount=net,
                cancellation_reason=o.cancellation_reason,
            )
            items.append(item)

        return items, total_count

    @classmethod
    def _write_csv_row(cls, writer: Any, o: Any) -> None:
        tot = float(o.total_amount or 0.0)
        if o.status == UnifiedOrderStatus.CANCELLED:
            comm = 0.0
            net = 0.0
        else:
            comm = round(tot * 0.20, 2)
            net = round(tot - comm, 2)

        if o.order_time:
            dt_vn = (
                o.order_time.astimezone(VN_TZ)
                if o.order_time.tzinfo
                else o.order_time.replace(tzinfo=timezone.utc).astimezone(VN_TZ)
            )
            order_time_str = dt_vn.strftime("%d/%m/%Y %H:%M:%S")
        else:
            order_time_str = ""

        writer.writerow([
            cls._sanitize_csv_cell(o.order_code),
            cls._sanitize_csv_cell(o.channel_order_id),
            cls._sanitize_csv_cell(o.display_order_id or ""),
            cls._sanitize_csv_cell(o.channel_code or ""),
            cls._sanitize_csv_cell(o.store_code or ""),
            cls._sanitize_csv_cell(o.store_name or ""),
            order_time_str,
            cls._sanitize_csv_cell(o.customer_name or ""),
            f"{float(o.subtotal_amount or 0.0):.0f}",
            f"{float(o.discount_amount or 0.0):.0f}",
            f"{float(o.delivery_fee or 0.0):.0f}",
            f"{tot:.0f}",
            f"{comm:.0f}",
            f"{net:.0f}",
            o.status.value,
            cls._sanitize_csv_cell(o.cancellation_reason or "")
        ])

    @classmethod
    async def export_reconciliation_csv(
        cls,
        db: AsyncSession,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        channel_code: Optional[str] = None,
        store_code: Optional[str] = None,
    ) -> str:
        """Generate CSV string with UTF-8 BOM encoding for direct opening in Microsoft Excel."""
        resolved_from, resolved_to = cls._resolve_date_window(from_date, to_date)
        criteria = cls._build_filter_criteria(resolved_from, resolved_to, channel_code, store_code)

        output = io.StringIO()
        # UTF-8 BOM so Excel opens Vietnamese characters correctly
        output.write("\ufeff")

        writer = csv.writer(output, delimiter=",", quoting=csv.QUOTE_MINIMAL)

        # Header row
        writer.writerow([
            "Mã Đơn Nội Bộ",
            "Mã Đơn Sàn",
            "Mã Hiển Thị",
            "Kênh Bán",
            "Mã Chi Nhánh",
            "Tên Chi Nhánh",
            "Thời Gian Đặt",
            "Khách Hàng",
            "Tiền Hàng (VNĐ)",
            "Giảm Giá Khuyến Mãi (VNĐ)",
            "Phí Vận Chuyển (VNĐ)",
            "Tổng Thực Thu (VNĐ)",
            "Phí Hoa Hồng Ước Tính (VNĐ)",
            "Thực Nhận Ước Tính (VNĐ)",
            "Trạng Thái",
            "Lý Do Hủy (nếu có)"
        ])

        # Stream PG orders in bounded batches to avoid loading all rows into memory
        batch_size = 1000
        offset = 0
        existing_keys: Set[str] = set()
        while True:
            stmt = (
                select(UnifiedOrder)
                .join(Channel, UnifiedOrder.channel_id == Channel.id)
                .join(Store, UnifiedOrder.store_id == Store.id)
                .options(selectinload(UnifiedOrder.channel), selectinload(UnifiedOrder.store))
                .order_by(desc(UnifiedOrder.order_time))
                .offset(offset)
                .limit(batch_size)
            )
            if criteria:
                stmt = stmt.where(*criteria)

            res = await db.execute(stmt)
            batch = list(res.scalars().all())
            if not batch:
                break

            for o in batch:
                if o.channel_order_id:
                    existing_keys.add(str(o.channel_order_id).strip())
                if o.order_code:
                    existing_keys.add(str(o.order_code).strip())
                if o.display_order_id:
                    existing_keys.add(str(o.display_order_id).strip())
                cls._write_csv_row(writer, o)

            if len(batch) < batch_size:
                break
            offset += len(batch)

        hrv_orders = await asyncio.to_thread(
            cls._fetch_haravan_orders_for_report,
            db=db,
            from_date=resolved_from,
            to_date=resolved_to,
            channel_code=channel_code,
            store_code=store_code,
            existing_keys=existing_keys,
        )
        if hrv_orders:
            hrv_orders.sort(
                key=lambda x: x.order_time or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            for o in hrv_orders:
                cls._write_csv_row(writer, o)

        return output.getvalue()


report_service = ReportService()
