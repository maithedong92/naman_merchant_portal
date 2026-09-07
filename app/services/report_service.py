import csv
import io
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, func, select
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


class ReportService:
    """Service handling multi-channel financial analytics and reconciliation for Nam An Market."""

    @staticmethod
    def _build_filter_criteria(
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        channel_code: Optional[str] = None,
        store_code: Optional[str] = None,
    ):
        """Construct SQLAlchemy filter clauses from date ranges and channel/store codes."""
        criteria = []

        if from_date:
            from_dt = datetime.combine(from_date, time.min).replace(tzinfo=timezone.utc)
            criteria.append(UnifiedOrder.order_time >= from_dt)

        if to_date:
            to_dt = datetime.combine(to_date, time.max).replace(tzinfo=timezone.utc)
            criteria.append(UnifiedOrder.order_time <= to_dt)

        if channel_code:
            criteria.append(Channel.code == channel_code.upper())

        if store_code:
            criteria.append(Store.code == str(store_code))

        return criteria

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
        criteria = cls._build_filter_criteria(from_date, to_date, channel_code, store_code)

        # Base query joining channel and store
        stmt = (
            select(UnifiedOrder)
            .join(Channel, UnifiedOrder.channel_id == Channel.id)
            .join(Store, UnifiedOrder.store_id == Store.id)
            .options(selectinload(UnifiedOrder.channel), selectinload(UnifiedOrder.store))
            .order_by(desc(UnifiedOrder.order_time))
        )
        if criteria:
            stmt = stmt.where(*criteria)

        res = await db.execute(stmt)
        orders: List[UnifiedOrder] = list(res.scalars().all())

        # 1. Financial Summary
        total_orders = len(orders)
        completed_orders = sum(1 for o in orders if o.status == UnifiedOrderStatus.DELIVERED)
        cancelled_orders = sum(
            1 for o in orders if o.status in (UnifiedOrderStatus.CANCELLED, UnifiedOrderStatus.FAILED)
        )
        active_orders = total_orders - completed_orders - cancelled_orders

        non_cancelled = [
            o for o in orders if o.status not in (UnifiedOrderStatus.CANCELLED, UnifiedOrderStatus.FAILED)
        ]

        total_gross_revenue = sum(float(o.total_amount or 0.0) for o in non_cancelled)
        completed_revenue = sum(
            float(o.total_amount or 0.0) for o in orders if o.status == UnifiedOrderStatus.DELIVERED
        )
        total_subtotal = sum(float(o.subtotal_amount or 0.0) for o in non_cancelled)
        total_discounts = sum(float(o.discount_amount or 0.0) for o in non_cancelled)
        total_delivery_fees = sum(float(o.delivery_fee or 0.0) for o in non_cancelled)

        # Platform commission estimated ~20%
        estimated_commission = round(total_gross_revenue * 0.20, 2)
        estimated_net_payout = round(total_gross_revenue - estimated_commission, 2)

        completion_rate = round((completed_orders / total_orders * 100), 1) if total_orders > 0 else 0.0
        cancellation_rate = round((cancelled_orders / total_orders * 100), 1) if total_orders > 0 else 0.0
        aov = round(total_gross_revenue / len(non_cancelled), 2) if non_cancelled else 0.0

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

        # 2. Channel Analytics Breakdown
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

        for o in orders:
            ch_code = o.channel_code
            if ch_code in channel_map:
                item = channel_map[ch_code]
                item.order_count += 1
                if o.status == UnifiedOrderStatus.DELIVERED:
                    item.completed_count += 1
                elif o.status in (UnifiedOrderStatus.CANCELLED, UnifiedOrderStatus.FAILED):
                    item.cancelled_count += 1
                if o.status not in (UnifiedOrderStatus.CANCELLED, UnifiedOrderStatus.FAILED):
                    item.revenue += float(o.total_amount or 0.0)

        channel_items = list(channel_map.values())
        if total_gross_revenue > 0:
            for item in channel_items:
                item.share_percentage = round((item.revenue / total_gross_revenue * 100), 1)

        # 3. Store Analytics Breakdown
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

        for o in orders:
            st_code = o.store_code
            if st_code in store_map:
                s_item = store_map[st_code]
                s_item.order_count += 1
                if o.status == UnifiedOrderStatus.DELIVERED:
                    s_item.completed_count += 1
                elif o.status in (UnifiedOrderStatus.CANCELLED, UnifiedOrderStatus.FAILED):
                    s_item.cancelled_count += 1
                if o.status not in (UnifiedOrderStatus.CANCELLED, UnifiedOrderStatus.FAILED):
                    s_item.revenue += float(o.total_amount or 0.0)

        store_items = list(store_map.values())
        if total_gross_revenue > 0:
            for s_item in store_items:
                s_item.share_percentage = round((s_item.revenue / total_gross_revenue * 100), 1)

        # 4. Daily Trends
        daily_dict: Dict[str, DailyTrendItem] = {}

        # Default to last 7 days timeline if no specific date range
        start_date = from_date or (date.today() - timedelta(days=6))
        end_date = to_date or date.today()

        curr_d = start_date
        while curr_d <= end_date:
            ds = curr_d.strftime("%Y-%m-%d")
            daily_dict[ds] = DailyTrendItem(date=ds, revenue=0.0, order_count=0, completed_count=0)
            curr_d += timedelta(days=1)

        for o in orders:
            if o.order_time:
                ds = o.order_time.strftime("%Y-%m-%d")
                if ds not in daily_dict:
                    daily_dict[ds] = DailyTrendItem(date=ds, revenue=0.0, order_count=0, completed_count=0)
                d_item = daily_dict[ds]
                d_item.order_count += 1
                if o.status == UnifiedOrderStatus.DELIVERED:
                    d_item.completed_count += 1
                if o.status not in (UnifiedOrderStatus.CANCELLED, UnifiedOrderStatus.FAILED):
                    d_item.revenue += float(o.total_amount or 0.0)

        daily_trends = sorted(daily_dict.values(), key=lambda x: x.date)

        return FullAnalyticsReport(
            filter_from=from_date.isoformat() if from_date else None,
            filter_to=to_date.isoformat() if to_date else None,
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
        criteria = cls._build_filter_criteria(from_date, to_date, channel_code, store_code)

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
        orders = res.scalars().all()

        items = []
        for o in orders:
            tot = float(o.total_amount or 0.0)
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
    async def export_reconciliation_csv(
        cls,
        db: AsyncSession,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        channel_code: Optional[str] = None,
        store_code: Optional[str] = None,
    ) -> str:
        """Generate CSV string with UTF-8 BOM encoding for direct opening in Microsoft Excel."""
        criteria = cls._build_filter_criteria(from_date, to_date, channel_code, store_code)

        stmt = (
            select(UnifiedOrder)
            .join(Channel, UnifiedOrder.channel_id == Channel.id)
            .join(Store, UnifiedOrder.store_id == Store.id)
            .options(selectinload(UnifiedOrder.channel), selectinload(UnifiedOrder.store))
            .order_by(desc(UnifiedOrder.order_time))
        )
        if criteria:
            stmt = stmt.where(*criteria)

        res = await db.execute(stmt)
        orders = res.scalars().all()

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

        for o in orders:
            tot = float(o.total_amount or 0.0)
            comm = round(tot * 0.20, 2)
            net = round(tot - comm, 2)
            order_time_str = o.order_time.strftime("%d/%m/%Y %H:%M:%S") if o.order_time else ""

            writer.writerow([
                o.order_code,
                o.channel_order_id,
                o.display_order_id or "",
                o.channel_code or "",
                o.store_code or "",
                o.store_name or "",
                order_time_str,
                o.customer_name or "",
                f"{float(o.subtotal_amount or 0.0):.0f}",
                f"{float(o.discount_amount or 0.0):.0f}",
                f"{float(o.delivery_fee or 0.0):.0f}",
                f"{tot:.0f}",
                f"{comm:.0f}",
                f"{net:.0f}",
                o.status.value,
                o.cancellation_reason or ""
            ])

        return output.getvalue()


report_service = ReportService()
