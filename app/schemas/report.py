from datetime import datetime, date
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class FinancialSummary(BaseModel):
    """Financial and operational KPI summary."""
    total_gross_revenue: float = Field(0.0, description="Tổng doanh thu gộp của các đơn không hủy (VNĐ)")
    completed_revenue: float = Field(0.0, description="Doanh thu các đơn đã hoàn tất giao hàng (VNĐ)")
    total_subtotal: float = Field(0.0, description="Tổng tiền hàng niêm yết (VNĐ)")
    total_discounts: float = Field(0.0, description="Tổng chiết khấu / mã khuyến mãi sàn (VNĐ)")
    total_delivery_fees: float = Field(0.0, description="Tổng phí vận chuyển (VNĐ)")
    estimated_commission: float = Field(0.0, description="Ước tính phí hoa hồng sàn (~20%) (VNĐ)")
    estimated_net_payout: float = Field(0.0, description="Doanh thu thực nhận ước tính sau phí sàn (VNĐ)")
    
    total_orders: int = Field(0, description="Tổng số lượng đơn phát sinh")
    completed_orders: int = Field(0, description="Số đơn đã giao thành công")
    cancelled_orders: int = Field(0, description="Số đơn bị hủy")
    active_orders: int = Field(0, description="Số đơn đang xử lý (chờ duyệt, làm món, giao hàng)")
    
    completion_rate: float = Field(0.0, description="Tỷ lệ hoàn thành đơn (%)")
    cancellation_rate: float = Field(0.0, description="Tỷ lệ hủy đơn (%)")
    average_order_value: float = Field(0.0, description="Giá trị trung bình mỗi đơn (AOV) (VNĐ)")


class ChannelAnalyticsItem(BaseModel):
    """Revenue and order share per sales channel."""
    channel_code: str
    channel_name: str
    order_count: int = 0
    completed_count: int = 0
    cancelled_count: int = 0
    revenue: float = 0.0
    share_percentage: float = 0.0


class StoreAnalyticsItem(BaseModel):
    """Revenue and order share per Nam An branch."""
    store_code: str
    store_name: str
    order_count: int = 0
    completed_count: int = 0
    cancelled_count: int = 0
    revenue: float = 0.0
    share_percentage: float = 0.0


class DailyTrendItem(BaseModel):
    """Time-series trend item for charts."""
    date: str
    revenue: float = 0.0
    order_count: int = 0
    completed_count: int = 0


class FullAnalyticsReport(BaseModel):
    """Full comprehensive analytics and performance report."""
    filter_from: Optional[str] = None
    filter_to: Optional[str] = None
    summary: FinancialSummary
    channels: List[ChannelAnalyticsItem]
    stores: List[StoreAnalyticsItem]
    daily_trends: List[DailyTrendItem]


class ReconciliationOrderItem(BaseModel):
    """Detailed financial row for accounting reconciliation."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    order_code: str
    channel_order_id: str
    display_order_id: Optional[str] = None
    channel_code: str
    channel_name: str
    store_code: str
    store_name: str
    status: str
    order_time: Optional[datetime] = None
    customer_name: Optional[str] = None
    subtotal_amount: float = 0.0
    discount_amount: float = 0.0
    delivery_fee: float = 0.0
    total_amount: float = 0.0
    estimated_commission: float = 0.0
    estimated_net_amount: float = 0.0
    payment_method: Optional[str] = "ONLINE"
    cancellation_reason: Optional[str] = None
